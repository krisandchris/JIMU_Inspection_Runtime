"""Run signed scene bundles with real providers through production Runtime ports."""

import argparse
import asyncio
import importlib
import json
from dataclasses import replace
from io import BytesIO
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

from PIL import Image
from sqlalchemy import text

from examples.nonmotor_vehicle_illegal_parking.compilable import scene
from examples.nonmotor_vehicle_illegal_parking.compilable import lifecycle
from jimu_runtime.domain.enums import ArtifactKind
from jimu_runtime.domain.runtime.media import ImageDimensions
from jimu_runtime.gateway.bridge_tools import ToolExecutionAdapter
from jimu_runtime.inspection.geometry.node import GeometryNodeAdapter
from jimu_runtime.tools.executor import ToolExecutor
from jimu_runtime.tools.registry import ToolWorkerRegistry
from jimu_runtime.tools.step_attempts import StepAttemptStore
from jimu_runtime.workflow import repository
from jimu_runtime.workflow.worker import WorkerStep
from jimu_runtime.workflow.worker_inspection import inspection_worker
from jimu_runtime.workflow.worker_state import load_records
from tests.fixtures.wp05 import perception

HARNESSED_MODEL = "jimu/live-adapter"


def provider_budgets(source):
    artifacts = []
    for artifact in source.artifacts:
        if artifact.kind is ArtifactKind.POLICY and artifact.artifact_id in {
            "remote-resources", "resources", "tool-execution"
        }:
            body = json.loads(artifact.content)
            policy = body["policy"]
            policy.update(timeout_ms=85000, input_byte_limit=12*1024*1024)
            if "output_byte_limit" in policy:
                policy["output_byte_limit"] = 2*1024*1024
            artifact = replace(artifact, content=json.dumps(body).encode())
        artifacts.append(artifact)
    return scene.with_source_provenance(replace(source, artifacts=tuple(artifacts)))


def config_override(**kwargs):
    limits = kwargs["limits"].model_copy(update={
        "timeout_ms": 85000,
        "io_timeout_ms": 84000,
        "request_bytes": 12*1024*1024,
        "response_bytes": 2*1024*1024,
        "image_bytes": 8*1024*1024,
    })
    endpoint = ChatEndpoint(**{**kwargs, "model_id": HARNESSED_MODEL, "limits": limits})
    return endpoint


ChatEndpoint = perception.ChatEndpoint
SourceMedia = perception.original_media


def fn_at(path):
    module, name = path.split(":", 1)
    return getattr(importlib.import_module(module), name)


def json_file(path, body):
    path.write_text(json.dumps(body, ensure_ascii=False, indent=2, default=str)+"\n")


async def run(
    directory, url, image_path, build_source=scene.load_source, *, region=None,
    make_tool_entries=None, caps=None,
):
    image = image_path.read_bytes()
    with Image.open(BytesIO(image)) as opened:
        if opened.format != "PNG" or opened.mode != "RGB":
            raise ValueError("One privacy-reviewed RGB PNG is required")
        dims = ImageDimensions(width=opened.width, height=opened.height)

    source = provider_budgets(build_source())
    context_id = uuid4().hex
    local_submission = lifecycle.local_submission
    scope = lifecycle.SCOPE
    device = lifecycle.DEVICE
    camera = lifecycle.CAMERA

    def submit_bytes(_):
        original = local_submission(image)
        context = original.context.model_copy(update={"region_id": region}) if region else original.context
        return original.model_copy(update={
            "idempotency_key": f"trial.{context_id}",
            "request_id": f"trial.{context_id}",
            "context": context,
        })

    def original_media(scene_run):
        return SourceMedia(scene_run).model_copy(update={
            "dimensions": dims, "byte_size": len(image),
        })

    schedule = perception._LIMITS.model_copy(update={
        "default": perception._LIMITS.default.model_copy(update={"timeout_ms": 95000}),
        "overrides": tuple(
            item.model_copy(update={"budget": item.budget.model_copy(update={"timeout_ms": 95000})})
            for item in perception._LIMITS.overrides
        ),
    })

    with (
        patch.object(lifecycle, "local_submission", submit_bytes),
        patch.object(lifecycle, "REGION", region or lifecycle.REGION),
        patch.object(lifecycle, "SCOPE", scope.model_copy(update={"region_id": region}) if region else scope),
        patch.object(perception, "local_submission", submit_bytes),
        patch.object(perception, "SCOPE", scope.model_copy(update={"region_id": region}) if region else scope),
        patch.object(perception, "original_media", original_media),
        patch.object(perception, "IMAGE", image),
        patch.object(perception, "ChatEndpoint", config_override),
        patch.object(perception, "load_source", lambda: source),
        patch.object(perception, "_LIMITS", schedule),
        patch.object(lifecycle, "capability_snapshot", caps or scene.capability_snapshot),
        patch.object(perception, "capability_snapshot", caps or scene.capability_snapshot),
    ):
        async with perception.perception_fixture(directory, url, with_media=True) as fixture:
            accepted = fixture.accepted
            task = accepted.task
            eid = accepted.execution.execution_id
            scope = task.idempotency_scope
            json_file(directory/"intake.json", accepted.model_dump(mode="json") if hasattr(accepted,"model_dump") else {
                "task": task.model_dump(mode="json"),
                "execution": accepted.execution.model_dump(mode="json"),
            })
            print(f"task={task.task_id} execution={eid} bundle={task.bundle_digest}",flush=True)
            tool_registry = ToolWorkerRegistry(
                () if make_tool_entries is None else make_tool_entries(fixture)
            )
            geometry = GeometryNodeAdapter(
                fixture.media_store,
                ToolExecutionAdapter(
                    ToolExecutor(tool_registry),
                    StepAttemptStore(fixture.application, actor=lifecycle.ACTOR),
                    fixture.journal,
                    fixture.host.capabilities,
                )
            )
            worker_options = dict(
                host=fixture.host, bundle_limits=scene.LIMITS,
                media=fixture.media_store, gateway=fixture.gateway,
                execute_tools=geometry.execute, model_resource_scope_id="model"
            )
            worker = inspection_worker(
                fixture.application, fixture.releases, fixture.store, **worker_options
            )
            dispositions = []
            for index in range(80):
                step = await worker.run_once(scope, eid, owner=f"live-{index}", lease_ms=99000)
                dispositions.append(step.value)
                print(index, step.value, flush=True)
                if step is WorkerStep.FINALIZED:
                    break
                if step in {WorkerStep.IDLE, WorkerStep.RETRY_SCHEDULED}:
                    await asyncio.sleep(0.15)

            final = await worker.read_finalization(scope,eid)
            async with fixture.application.connect() as connection:
                locked = await repository.get_execution(connection, scope, eid)
                if locked is None:
                    raise RuntimeError("Persisted Execution unavailable")
                data = await load_records(connection, scope, locked)
                db = (await connection.execute(text("SELECT current_setting('server_version') AS postgres_version"))).mappings().one()
            json_file(directory/"result.json",{
                "task_id":task.task_id,
                "execution_id":eid,
                "bundle_digest":task.bundle_digest,
                "release_id":task.release_id,
                "media":task.media_ref.model_dump(mode="json"),
                "postgres_version":db["postgres_version"],
                "dispositions":dispositions,
                "execution":locked.execution.model_dump(mode="json"),
                "nodes":[node.model_dump(mode="json") for node in data.runs.values()],
                "tool_step_nodes":[node.model_dump(mode="json") for node in data.tool_step_nodes],
                "node_outputs":[out.model_dump(mode="json") for out in data.messages.values()],
                "final":None if final is None else final.model_dump(mode="json"),
            })
            if final is None:
                raise RuntimeError("No finalization record; inspect result.json")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", type=Path, required=True)
    parser.add_argument("--image", type=Path, required=True)
    parser.add_argument("--url", required=True)
    parser.add_argument("--build-source")
    parser.add_argument("--caps")
    parser.add_argument("--tool-entries")
    parser.add_argument("--region")
    opts = parser.parse_args()
    opts.directory.mkdir(parents=True,exist_ok=False)
    asyncio.run(run(
        opts.directory, opts.url, opts.image,
        fn_at(opts.build_source) if opts.build_source else scene.load_source,
        region=opts.region,
        make_tool_entries=fn_at(opts.tool_entries) if opts.tool_entries else None,
        caps=fn_at(opts.caps) if opts.caps else None,
    ))
