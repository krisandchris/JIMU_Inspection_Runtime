"""Trusted host wiring into the installed Runtime's configured Worker entry."""

import asyncio
import json
import os
import sys
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

from cases import BASE_FRAME_DIGEST, read_frame
from harness import ACTOR, Harness
from sqlalchemy.engine import make_url

import jimu_runtime
from examples.nonmotor_vehicle_illegal_parking.compilable import scene
from jimu_runtime.domain.definition_enums import ModelCapability
from jimu_runtime.domain.definitions.capabilities import (
    ModelBindingContentRef,
    ProfileDefinition,
)
from jimu_runtime.domain.definitions.models import ModelBinding
from jimu_runtime.domain.definitions.tools import ToolDefinition
from jimu_runtime.domain.enums import ArtifactKind
from jimu_runtime.domain.runtime.intake import AcceptedTask
from jimu_runtime.gateway.bridge import CapabilityGateway, ModelCallLimits
from jimu_runtime.gateway.bridge_tools import ToolExecutionAdapter
from jimu_runtime.gateway.http import HttpModelTransport
from jimu_runtime.gateway.journal import DispatchJournal
from jimu_runtime.inspection.geometry import synthetic_cpu_implementations
from jimu_runtime.inspection.geometry.node import GeometryNodeAdapter
from jimu_runtime.inspection.perception.model import build_chat_contract
from jimu_runtime.tools.executor import ToolExecutor
from jimu_runtime.tools.registry import ToolWorkerRegistry
from jimu_runtime.tools.step_attempts import StepAttemptStore
from jimu_runtime.workers.resource_inventory import (
    HostInventory,
    RemoteCapacity,
    RemoteQuota,
    ResourceScope,
)
from jimu_runtime.workers.resources import ResourceAdmission
from jimu_runtime.workflow.mock_support import VerifiedExecutionScope
from jimu_runtime.workflow.pins import resolve_execution_pins
from jimu_runtime.workflow.postgres import create_postgres_engine
from jimu_runtime.workflow.repository import get_execution
from jimu_runtime.workflow.worker import WorkerStep, WorkflowWorker
from jimu_runtime.workflow.worker_inspection import (
    VerifiedMediaWorkerHost,
    inspection_worker,
)

QUOTA = "synthetic.loopback.quota"


class BeforeSecondToolAdapter(ToolExecutionAdapter):
    """Pause before opening step two's child, after step one is committed."""

    def __init__(self, marker: Path, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.marker = marker

    async def execute_step(self, claimed, inputs, step_id, input_fields, **kwargs):
        if step_id == "tactile_paving_segmentation":
            self.marker.write_text("next step reached before child admission\n")
            await asyncio.Event().wait()
        return await super().execute_step(
            claimed, inputs, step_id, input_fields, **kwargs
        )


class PauseAdmittedTool:
    """Hold a durable child just before its real worker's physical exchange."""

    def __init__(self, genuine, marker: Path):
        self.genuine = genuine
        self.marker = marker

    async def run(self, dispatch, before_dispatch):
        self.marker.write_text("second child admitted without physical dispatch\n")
        await asyncio.Event().wait()
        return await self.genuine.run(dispatch, before_dispatch)


async def open_worker(
    host: Harness,
    task: AcceptedTask,
    execution_id: str,
    pause: Path | None = None,
    pause_after_admission: bool = False,
) -> WorkflowWorker:
    resolved = resolve_execution_pins(
        host.manager(),
        host.ledger,
        task,
        scope=host.scope,
        host=host.snapshot,
        validation_at=datetime.now(UTC),
        limits=scene.LIMITS,
    )
    original = await host.media.verified_task_original(host.scope, task)
    scope = VerifiedExecutionScope(
        task,
        execution_id,
        resolved.pins,
        original.asset,
        resolved.files,
        scene.LIMITS.max_file_bytes,
        scene.LIMITS.max_json_depth,
    )
    (row,) = scope.catalogue.of_kind(ArtifactKind.PROFILE)
    profile = scope.resolve(row.ref, ProfileDefinition)
    contracts = []
    for pin in scope.pins.model_pins:
        ref = ModelBindingContentRef.model_validate(pin.binding_ref.model_dump())
        binding = scope.resolve(ref, ModelBinding)
        contracts.append(
            build_chat_contract(
                scope,
                ref,
                profile.question_refs
                if binding.capability is ModelCapability.OBJECT_OBSERVATION
                else (),
            )
        )
    resources_path = host.root / "capacity.db"
    inventory = HostInventory(
        "wp05-independently-bound-host",
        3500,
        1,
        (
            ResourceScope("cpu", "cpu", 1),
            ResourceScope("model", "remote", 1, pool_or_quota_id=QUOTA),
        ),
        (RemoteQuota(QUOTA, 1, RemoteCapacity.LOCAL_TRANSPORT),),
    )
    resources = (
        ResourceAdmission(resources_path, inventory)
        if resources_path.is_file()
        else ResourceAdmission.provision(resources_path, inventory)
    )
    journal = DispatchJournal(host.db)
    transport = HttpModelTransport(
        (host.endpoint,), tuple(contracts), max_concurrency=1
    )
    gateway = CapabilityGateway(
        host.db,
        journal,
        resources,
        transport,
        host.current_models,
        ModelCallLimits(
            node=4,
            operation=2,
            deployment=4,
            retryable=("timeout", "transport", "unavailable"),
        ),
        response_writer=host.media,
    )
    tool_defs = tuple(
        scope.resolve(pin.tool_ref, ToolDefinition) for pin in scope.pins.tool_pins
    )
    implementations = synthetic_cpu_implementations(
        resources,
        "cpu",
        "independent-geometry",
        tool_defs,
        host.tool_proofs(),
        synthetic_image_digests=frozenset(
            {
                BASE_FRAME_DIGEST,
                read_frame(scene.DIRECTORY, "two").digest,
                read_frame(scene.DIRECTORY, "wheel-on-tactile").digest,
            }
        ),
    )
    if pause_after_admission and pause is not None:
        implementations = tuple(
            replace(entry, worker=PauseAdmittedTool(entry.worker, pause))
            if entry.capability_id == "synthetic.cpu.tactile_paving_segmentation"
            else entry
            for entry in implementations
        )
    tool_services = (
        ToolExecutor(ToolWorkerRegistry(implementations)),
        StepAttemptStore(host.db, actor=ACTOR),
        journal,
        host.capabilities,
    )
    tool_runner = (
        ToolExecutionAdapter(*tool_services)
        if pause is None or pause_after_admission
        else BeforeSecondToolAdapter(pause, *tool_services)
    )
    adapter = GeometryNodeAdapter(host.media, tool_runner)

    async def capture(scope, task):
        current = await host.worker_source(scope, task)
        return current.device, current.camera

    return inspection_worker(
        host.db,
        host.manager(),
        host.ledger,
        host=VerifiedMediaWorkerHost(host.media, host.capabilities, capture),
        bundle_limits=scene.LIMITS,
        media=host.media,
        gateway=gateway,
        execute_tools=adapter.execute,
        model_resource_scope_id="model",
    )


async def main() -> None:
    # The caller sends the ephemeral isolated PostgreSQL URL over stdin.
    inputs = json.loads(await asyncio.to_thread(sys.stdin.readline))
    if inputs["diagnose"]:

        def trace_frame(frame, event, _argument):
            if event == "call" and frame.f_code.co_name == "_failed_node":
                error = frame.f_locals.get("error")
                print(
                    json.dumps(
                        {
                            "adapter_error": type(error).__name__,
                            "diagnostic": str(error)[:700],
                        }
                    ),
                    file=sys.stderr,
                    flush=True,
                )
            if event == "exception" and frame.f_code.co_name == "_check_result":
                _kind, exc, _trace = _argument
                if (
                    str(exc)
                    == "tool result differs from its immutable scope, configuration or child owner"
                ):
                    from types import SimpleNamespace

                    fields = SimpleNamespace(**frame.f_locals)
                    record = fields.tool
                    expected, observed = fields.expected_inputs, record.input_refs
                    print(
                        json.dumps(
                            {
                                "diagnostic": "step_input_refs",
                                "step": fields.bound.step.step_id,
                                "expected": [
                                    (
                                        row.artifact_ref.record_kind.value,
                                        row.artifact_ref.record_id,
                                    )
                                    for row in expected
                                ],
                                "observed": [
                                    (
                                        row.artifact_ref.record_kind.value,
                                        row.artifact_ref.record_id,
                                    )
                                    for row in observed
                                ],
                                "shared_exactly_match": all(
                                    item in observed for item in expected
                                ),
                            },
                            sort_keys=True,
                        ),
                        file=sys.stderr,
                        flush=True,
                    )
            return trace_frame

        sys.settrace(trace_frame)
    db = create_postgres_engine(make_url(inputs["database"]))
    try:
        if inputs["installed"]:
            assert (
                Path(jimu_runtime.__file__)
                .resolve()
                .is_relative_to(Path(sys.prefix).resolve())
            )
        host = Harness(db, Path(inputs["root"]), inputs["endpoint"])
        host.revision = inputs["revision"]
        if inputs["revoke_model"]:
            host.model_revoked_at = datetime.now(UTC)
        execution_id = inputs["execution_id"]
        async with db.connect() as connection:
            locked = await get_execution(connection, host.scope, execution_id)
        assert locked is not None
        worker = await open_worker(
            host,
            locked.task,
            execution_id,
            Path(inputs["pause"]) if inputs["pause"] else None,
            inputs["pause_after_admission"],
        )
        print(
            json.dumps(
                {"ready": os.getpid(), "runtime_install": str(jimu_runtime.__file__)}
            ),
            flush=True,
        )
        while line := await asyncio.to_thread(sys.stdin.readline):
            requested = json.loads(line)
            if requested == "read":
                final = await worker.read_finalization(host.scope, execution_id)
                foreign = host.scope.model_copy(
                    update={"client_id": "wp05.wrong-client"}
                )
                assert await worker.read_finalization(foreign, execution_id) is None
                print(
                    json.dumps(
                        {
                            "final": None
                            if final is None
                            else final.model_dump(mode="json")
                        }
                    ),
                    flush=True,
                )
            elif requested == "step":
                value = await worker.run_once(
                    host.scope, execution_id, owner=inputs["owner"], lease_ms=5000
                )
                print(json.dumps({"step": value.value}), flush=True)
                if value is WorkerStep.RETRY_SCHEDULED:
                    await asyncio.sleep(0.05)
            else:
                raise ValueError("unknown independent worker control request")
    finally:
        await db.dispose()


if __name__ == "__main__":
    asyncio.run(main())
