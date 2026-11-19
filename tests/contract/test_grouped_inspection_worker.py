"""Exercise grouped grounding through the signed release and installed Worker."""

import asyncio
import base64
import json
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from io import BytesIO
from pathlib import Path

import pytest
from PIL import Image
from sqlalchemy import text

from examples.nonmotor_vehicle_illegal_parking.compilable.lifecycle import ACTOR
from examples.nonmotor_vehicle_illegal_parking.compilable.scene import (
    LIMITS,
    load_source,
    with_source_provenance,
)
from jimu_runtime.bundle.compiler import BundleCompileInput
from jimu_runtime.bundle.integrity import sha256_digest
from jimu_runtime.bundle.semantic_inputs import require_definition
from jimu_runtime.domain.definition_enums import EvidenceType
from jimu_runtime.domain.definitions.tools import ToolDefinition
from jimu_runtime.domain.enums import ArtifactKind, DecisionResult
from jimu_runtime.domain.runtime.evidence import (
    DerivedImageEvidence,
    ObjectEvidenceScope,
)
from jimu_runtime.domain.runtime.executions import NodeId
from jimu_runtime.gateway.bridge import ModelCall, ModelCallFailure
from jimu_runtime.gateway.bridge_tools import ToolExecutionAdapter
from jimu_runtime.gateway.http_wire import VerifiedImage
from jimu_runtime.gateway.invocation import DispatchOutcome
from jimu_runtime.inspection.geometry import synthetic_cpu_implementations
from jimu_runtime.inspection.geometry.node import GeometryNodeAdapter
from jimu_runtime.inspection.media import MediaError
from jimu_runtime.tools.executor import ToolExecutor
from jimu_runtime.tools.registry import ToolWorkerRegistry
from jimu_runtime.tools.step_attempts import StepAttemptStore
from jimu_runtime.workflow import repository
from jimu_runtime.workflow.engine import BranchKey
from jimu_runtime.workflow.worker import ClaimedNode, WorkerStep
from jimu_runtime.workflow.worker_inspection import (
    VerifiedMediaWorkerHost,
    inspection_worker,
)
from jimu_runtime.workflow.worker_records import DecisionMessage
from jimu_runtime.workflow.worker_state import load_records
from tests.fixtures.wp04 import http_support
from tests.fixtures.wp05 import perception

loopback_only = http_support.loopback_only

_BOXES = ((47, 25, 52, 33), (3, 4, 14, 18), (40, 23, 46, 32))
_PARKED = (40, 23, 52, 33)
_ISOLATED = (3, 4, 14, 18)
_GROUPS = {_ISOLATED: (_BOXES[1],), _PARKED: (_BOXES[2], _BOXES[0])}
_ANSWERS = {
    "vehicle_type": "electric_bicycle",
    "parking_state": "parked",
    "parking_position": "over_boundary",
}


def _grouped_source() -> BundleCompileInput:
    source = load_source()
    (profile,) = (row for row in source.artifacts if row.kind is ArtifactKind.PROFILE)
    document = json.loads(profile.content)
    document["grounding_grouping"] = {
        "mode": "connected_boxes",
        "max_gap_pixels": 1,
    }
    grouped = replace(profile, content=json.dumps(document).encode())
    return with_source_provenance(
        replace(
            source,
            artifacts=tuple(
                grouped if row is profile else row for row in source.artifacts
            ),
        )
    )


def _check_marked_png(data: bytes, box: tuple[int, ...]) -> None:
    left, top, right, bottom = box
    perimeter = {
        (x, y)
        for y in range(top, bottom)
        for x in range(left, right)
        if x in {left, right - 1} or y in {top, bottom - 1}
    }
    with (
        Image.open(BytesIO(perception.IMAGE)) as original,
        Image.open(BytesIO(data)) as marked,
    ):
        assert (marked.format, marked.mode, marked.size) == (
            "PNG",
            "RGB",
            original.size,
        )
        expected = original.load()
        actual = marked.load()
        assert expected is not None and actual is not None
        for y in range(marked.height):
            for x in range(marked.width):
                assert actual[x, y] == (
                    (255, 0, 0) if (x, y) in perimeter else expected[x, y]
                )


def test_signed_worker_keeps_grouped_object_identity_and_image_scope(
    tmp_path: Path, loopback_only: set[int], monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: Counter[str] = Counter()
    observations: dict[tuple[int, ...], tuple[bytes, dict[str, str]]] = {}

    async def peer(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        request = await http_support.read_request(reader)
        body = json.loads(request.body)
        schema = body["response_format"]["json_schema"]["schema"]
        name = schema["$id"].split(":")[-2]
        calls[name] += 1
        reply = {
            "profile_discovery-result": {"profile_hits": ["nonmotor_vehicle"]},
            "object_grounding-result": {"boxes": [{"xyxy": box} for box in _BOXES]},
            "object_observation-result": _ANSWERS,
            "candidate_prediction-result": {"categories": []},
        }[name]
        if name in {
            "profile_discovery-result",
            "object_grounding-result",
            "object_observation-result",
        }:
            prompt, image = body["messages"][0]["content"]
            media_type, encoded = image["image_url"]["url"].split(",", 1)
            assert media_type == "data:image/png;base64"
            data = base64.b64decode(encoded, validate=True)
            if name == "object_observation-result":
                _, model_input, metadata = prompt["text"].splitlines()
                request_input = json.loads(model_input)
                box = tuple(request_input["bbox"]["xyxy"])
                assert box in _GROUPS and box not in observations
                assert set(json.loads(metadata)["questions"]) == set(_ANSWERS)
                _check_marked_png(data, box)
                observations[box] = (data, request_input["image"])
            else:
                assert data == perception.IMAGE
        writer.write(
            http_support.response(
                http_support.chat_body(model=body["model"], content=json.dumps(reply)),
                headers=(
                    (b"X-Request-ID", f"grouped-{calls.total()}".encode()),
                    (b"X-Jimu-Deployment-Version", b"1.0.0"),
                    (b"X-Jimu-Model-Version", b"1.0.0"),
                ),
            )
        )
        await writer.drain()

    async def run() -> None:
        asyncio.get_running_loop().set_default_executor(
            ThreadPoolExecutor(max_workers=1)
        )
        monkeypatch.setattr(perception, "load_source", _grouped_source)
        async with (
            http_support.http_peer(loopback_only, peer) as url,
            perception.perception_fixture(tmp_path, url, with_media=True) as fixture,
        ):
            media = fixture.media_store
            assert media is not None
            accepted = fixture.accepted
            scope = accepted.task.idempotency_scope
            eid = accepted.execution.execution_id
            invoke_model = fixture.gateway.invoke_model
            rejected_objects: set[str] = set()
            original = VerifiedImage(
                perception.IMAGE, "image/png", accepted.task.media_ref.sha256
            )

            async def reject_substituted_observation_frames(
                claimed: ClaimedNode, call: ModelCall
            ) -> DispatchOutcome:
                if claimed.node.node_id is NodeId.N04:
                    object_record = claimed.previous.object_record(claimed.branch)
                    grounding = claimed.previous.grounding
                    assert grounding is not None
                    (other_object,) = (
                        item for item in grounding.objects if item != object_record
                    )
                    other_frame = await media.read_marked_frame(scope, other_object)
                    assert call.image is not None
                    assert (
                        len({original.sha256, other_frame.sha256, call.image.sha256})
                        == 3
                    )
                    before = calls.copy()
                    for wrong_frame in (original, other_frame):
                        with pytest.raises(
                            ModelCallFailure, match=r"^model\.marked_frame_mismatch$"
                        ):
                            await invoke_model(
                                claimed, replace(call, image=wrong_frame)
                            )
                    assert calls == before
                    async with fixture.application.connect() as connection:
                        dispatches = await connection.scalar(
                            text("""
                                SELECT count(*) FROM jimu_workflow.dispatch_intents
                                WHERE project_id = :project AND client_id = :client
                                    AND execution_id = :execution AND node_run_id = :node
                            """),
                            {
                                "project": scope.project_id,
                                "client": scope.client_id,
                                "execution": eid,
                                "node": claimed.node.node_run_id,
                            },
                        )
                    assert dispatches == 0
                    rejected_objects.add(object_record.object_id)
                return await invoke_model(claimed, call)

            monkeypatch.setattr(
                fixture.gateway, "invoke_model", reject_substituted_observation_frames
            )
            catalogue = fixture.scene.execution.catalogue
            definitions = tuple(
                require_definition(catalogue, item.ref, ToolDefinition)
                for item in catalogue.of_kind(ArtifactKind.TOOL)
            )
            tool_registry = ToolWorkerRegistry(
                synthetic_cpu_implementations(
                    fixture.resources,
                    "cpu",
                    "grouped-inspection-tools",
                    definitions,
                    fixture.tool_proofs,
                    synthetic_image_digests=frozenset({accepted.task.media_ref.sha256}),
                )
            )
            tools = GeometryNodeAdapter(
                media,
                ToolExecutionAdapter(
                    ToolExecutor(tool_registry),
                    StepAttemptStore(fixture.application, actor=ACTOR),
                    fixture.journal,
                    fixture.host.capabilities,
                ),
            )

            async def capture_registry(request_scope, task):
                source = await fixture.host.worker_source(request_scope, task)
                return source.device, source.camera

            host = VerifiedMediaWorkerHost(
                media, fixture.host.capabilities, capture_registry
            )

            def open_worker():
                return inspection_worker(
                    fixture.application,
                    fixture.releases,
                    fixture.store,
                    host=host,
                    bundle_limits=LIMITS,
                    media=media,
                    gateway=fixture.gateway,
                    execute_tools=tools.execute,
                    model_resource_scope_id="model",
                )

            dispositions = []
            for index in range(32):
                step = await open_worker().run_once(
                    scope, eid, owner=f"grouped-inspection-{index}", lease_ms=10000
                )
                dispositions.append(step)
                if step is WorkerStep.FINALIZED:
                    break
                if step in {WorkerStep.IDLE, WorkerStep.RETRY_SCHEDULED}:
                    await asyncio.sleep(0.025)
            final = await open_worker().read_finalization(scope, eid)
            assert final is not None, dispositions
            assert calls == {
                "profile_discovery-result": 1,
                "object_grounding-result": 1,
                "object_observation-result": 2,
                "candidate_prediction-result": 2,
            }

            async with fixture.application.connect() as connection:
                locked = await repository.get_execution(connection, scope, eid)
                assert locked is not None
                state = await load_records(connection, scope, locked)
            grounding = state.inputs().grounding
            assert grounding is not None and len(grounding.objects) == 2
            objects = {obj.bbox.xyxy: obj for obj in grounding.objects}
            assert set(objects) == set(observations) == set(_GROUPS)
            decisions = {item.object_ref.record_id: item for item in final.decisions}
            assert len(final.decisions) == len(decisions) == 2
            assert (
                set(decisions)
                == rejected_objects
                == {obj.object_id for obj in objects.values()}
            )
            frame_digests = set()
            for box, obj in objects.items():
                assert (
                    tuple(row.xyxy for row in obj.grounding_member_boxes)
                    == _GROUPS[box]
                )
                frame = obj.marked_frame
                assert frame is not None
                assert obj.source_media_ref == accepted.task.media_ref
                assert obj.coordinate_system.frame == "original_image"
                assert obj.coordinate_system.dimensions == fixture.host.media.dimensions
                data, ref = observations[box]
                assert frame.media_ref.model_dump(mode="json") == ref
                assert frame.byte_size == len(data)
                assert frame.mime_type == "image/png"
                assert frame.media_ref.sha256 == sha256_digest(data)
                frame_digests.add(frame.media_ref.sha256)
                assert (await media.read_marked_frame(scope, obj)).content == data

                inputs = state.inputs(obj.object_id)
                observation, tool = inputs.observation, inputs.tool
                assert (
                    observation is not None and observation.batch.outcome == "complete"
                )
                assert observation.inputs.object_record == obj
                assert observation.inputs.image_view.marked_media_ref == frame.media_ref
                assert tool is not None
                expected_status = "succeeded" if box == _PARKED else "partial"
                assert tool.output.summary.status == expected_status, (
                    tool.output.summary
                )
                assert tool.output.summary.object_ref.record_id == obj.object_id
                assert tool.inputs.object_record == obj
                assert tool.inputs.image_view.roi == obj.bbox
                assert not tool.inputs.image_view.transforms
                assert tool.inputs.image_view.marked_media_ref == frame.media_ref
                (wheel_mask,) = [
                    evidence
                    for evidence in tool.evidence
                    if evidence.evidence_type is EvidenceType.MASK
                    and evidence.producer_id == "wheel_segmentation"
                ]
                assert isinstance(wheel_mask.scope, ObjectEvidenceScope)
                assert wheel_mask.scope.object_ref.record_id == obj.object_id
                assert isinstance(wheel_mask.spatial, DerivedImageEvidence)
                assert wheel_mask.spatial.original_media_ref == accepted.task.media_ref
                decision = state.messages[BranchKey(NodeId.N06, obj.object_id)]
                assert isinstance(decision, DecisionMessage)
                assert decision.draft.inputs.object_record == obj
                assert decision.draft.decision == decisions[obj.object_id]
            parked_decision = decisions[objects[_PARKED].object_id]
            assert parked_decision.result is DecisionResult.VIOLATION, (
                parked_decision.model_dump_json()
            )
            isolated_decision = decisions[objects[_ISOLATED].object_id]
            assert isolated_decision.result is DecisionResult.UNCERTAIN, (
                isolated_decision.model_dump_json()
            )
            (match,) = final.coverage.matches
            assert match.object_ref == parked_decision.object_ref
            assert len(frame_digests) == 2
            assert accepted.task.media_ref.sha256 not in frame_digests

            with pytest.raises(MediaError, match="marked_frame_unavailable"):
                await media.read_marked_frame(
                    scope.model_copy(update={"client_id": "other.client"}),
                    objects[_PARKED],
                )
            with pytest.raises(MediaError, match="marked_frame_identity_conflict"):
                await media.read_marked_frame(
                    scope,
                    objects[_PARKED].model_copy(
                        update={"marked_frame": objects[_ISOLATED].marked_frame}
                    ),
                )

    asyncio.run(run())
