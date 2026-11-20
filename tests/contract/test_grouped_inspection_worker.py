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
from jimu_runtime.tools.executor import ToolExecutor
from jimu_runtime.tools.registry import (
    ToolDispatch,
    ToolImplementation,
    ToolWorkerRegistry,
)
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


def _substitute_image(original: VerifiedImage, x: float, y: float) -> VerifiedImage:
    with Image.open(BytesIO(original.content)) as frame, BytesIO() as output:
        frame.putpixel((int(x), int(y)), (255, 0, 0))
        frame.save(output, format="PNG")
        content = output.getvalue()
    return VerifiedImage(content, "image/png", sha256_digest(content))


def _grouped_source(*, include_member_boxes: bool) -> BundleCompileInput:
    source = load_source()
    (profile,) = (row for row in source.artifacts if row.kind is ArtifactKind.PROFILE)
    document = json.loads(profile.content)
    document["grounding_grouping"] = {
        "mode": "connected_boxes",
        "max_gap_pixels": 1,
    }
    changes = {
        (profile.kind, profile.artifact_id): replace(
            profile, content=json.dumps(document).encode()
        )
    }
    if include_member_boxes:
        schema = next(
            row
            for row in source.artifacts
            if row.artifact_id == "object_observation-input"
        )
        payload = json.loads(schema.content)
        payload["properties"]["member_boxes"] = {
            "type": "array",
            "items": payload["properties"]["bbox"],
            "minItems": 1,
            "maxItems": 256,
        }
        payload["required"].append("member_boxes")
        # The effective input contract may define its record behind a local reference.
        properties = {
            key: payload.pop(key)
            for key in ("type", "properties", "required", "additionalProperties")
        }
        payload["$defs"] = {"Input": properties}
        payload["$ref"] = "#/$defs/Input"
        changes[schema.kind, schema.artifact_id] = replace(
            schema, content=json.dumps(payload).encode()
        )
    return with_source_provenance(
        replace(
            source,
            artifacts=tuple(
                changes.get((row.kind, row.artifact_id), row)
                for row in source.artifacts
            ),
        )
    )


@pytest.mark.parametrize(
    "include_member_boxes", [False, True], ids=["legacy-input", "member-input"]
)
def test_signed_worker_keeps_grouped_object_identity_and_image_scope(
    tmp_path: Path,
    loopback_only: set[int],
    monkeypatch: pytest.MonkeyPatch,
    include_member_boxes: bool,
) -> None:
    calls: Counter[str] = Counter()
    model_image_refs: list[dict[str, str]] = []
    observations: dict[tuple[int, ...], str] = {}
    candidates: dict[tuple[int, ...], str] = {}

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
        prompt, image = body["messages"][0]["content"]
        media_type, encoded = image["image_url"]["url"].split(",", 1)
        assert media_type == "data:image/png;base64"
        assert base64.b64decode(encoded, validate=True) == perception.IMAGE
        _, model_input, metadata = prompt["text"].splitlines()
        request_input = json.loads(model_input)
        instructions = json.loads(metadata)
        model_image_refs.append(request_input["image"])
        if name == "object_observation-result":
            box = tuple(request_input["bbox"]["xyxy"])
            assert box in _GROUPS and box not in observations
            expected_members = [{"xyxy": list(member)} for member in _GROUPS[box]]
            if include_member_boxes:
                assert request_input["member_boxes"] == expected_members
                assert "member_boxes" not in instructions
            else:
                assert "member_boxes" not in request_input
                assert instructions["member_boxes"] == expected_members
            assert "original image" in instructions["image_view"]
            assert set(instructions["questions"]) == set(_ANSWERS)
            observations[box] = instructions["object_id"]
        elif name == "candidate_prediction-result":
            box = tuple(instructions["bbox"]["xyxy"])
            assert box in _GROUPS and box not in candidates
            candidates[box] = instructions["object_id"]
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
        async with (
            http_support.http_peer(loopback_only, peer) as url,
            perception.perception_fixture(
                tmp_path,
                url,
                with_media=True,
                source=_grouped_source(include_member_boxes=include_member_boxes),
            ) as fixture,
        ):
            media = fixture.media_store
            assert media is not None
            accepted = fixture.accepted
            scope = accepted.task.idempotency_scope
            eid = accepted.execution.execution_id
            invoke_model = fixture.gateway.invoke_model
            rejected_objects: set[str] = set()
            tool_dispatches: dict[tuple[str, str], ToolDispatch] = {}
            original = VerifiedImage(
                perception.IMAGE, "image/png", accepted.task.media_ref.sha256
            )

            async def unexpected_marked_frame(*args, **kwargs):
                pytest.fail("the Worker accessed marked-frame storage")

            monkeypatch.setattr(media, "stage_marked_frame", unexpected_marked_frame)
            monkeypatch.setattr(media, "read_marked_frame", unexpected_marked_frame)

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
                    assert call.image == original
                    altered = _substitute_image(original, *object_record.bbox.xyxy[:2])
                    other_frame = _substitute_image(
                        original, *other_object.bbox.xyxy[:2]
                    )
                    assert (
                        len({original.sha256, altered.sha256, other_frame.sha256}) == 3
                    )
                    before = calls.copy()
                    tampered = replace(
                        original,
                        content=original.content[:-1]
                        + bytes([original.content[-1] ^ 1]),
                    )
                    for wrong_frame in (
                        altered,
                        other_frame,
                        tampered,
                    ):
                        with pytest.raises(
                            ModelCallFailure, match=r"^model\.original_media_mismatch$"
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

            def inspect_tool_input(
                implementation: ToolImplementation,
            ) -> ToolImplementation:
                check = implementation.physical_input_check
                assert check is not None

                def inspect(dispatch: ToolDispatch, content: bytes) -> None:
                    check(dispatch, content)
                    assert dispatch.object_ref is not None
                    assert dispatch.original_media_ref == accepted.task.media_ref
                    assert dispatch.object_bbox is not None
                    box = dispatch.object_bbox.xyxy
                    assert (
                        tuple(row.xyxy for row in dispatch.object_member_boxes)
                        == _GROUPS[box]
                    )
                    tool_id = dispatch.tool_ref.artifact_id
                    if tool_id in {"wheel_segmentation", "tactile_paving_segmentation"}:
                        image = json.loads(content)["image"]
                        assert (
                            base64.b64decode(image, validate=True) == perception.IMAGE
                        )
                    tool_dispatches[dispatch.object_ref.record_id, tool_id] = dispatch

                return replace(implementation, physical_input_check=inspect)

            implementations = synthetic_cpu_implementations(
                fixture.resources,
                "cpu",
                "grouped-inspection-tools",
                definitions,
                fixture.tool_proofs,
                synthetic_image_digests=frozenset({accepted.task.media_ref.sha256}),
            )
            tool_registry = ToolWorkerRegistry(
                tuple(
                    inspect_tool_input(implementation)
                    for implementation in implementations
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

            async with fixture.application.connect() as connection:
                locked = await repository.get_execution(connection, scope, eid)
                assert locked is not None
                state = await load_records(connection, scope, locked)
                marked_frames = await connection.scalar(
                    text("""
                        SELECT count(*) FROM jimu_workflow.marked_frame_media
                        WHERE project_id = :project AND client_id = :client
                            AND execution_id = :execution
                    """),
                    {
                        "project": scope.project_id,
                        "client": scope.client_id,
                        "execution": eid,
                    },
                )
            assert calls == {
                "profile_discovery-result": 1,
                "object_grounding-result": 1,
                "object_observation-result": 2,
                "candidate_prediction-result": 2,
            }, {
                branch: (node.status, node.reason)
                for branch, node in state.runs.items()
            }
            assert (
                model_image_refs
                == [accepted.task.media_ref.model_dump(mode="json")] * calls.total()
            )
            assert marked_frames == 0
            common = state.inputs()
            assert common.snapshot().media_ref == accepted.task.media_ref
            discovery = common.discovery
            grounding = common.grounding
            assert discovery is not None
            assert discovery.inputs.original_media_ref == accepted.task.media_ref
            assert grounding is not None and len(grounding.objects) == 2
            assert grounding.inputs.original_media_ref == accepted.task.media_ref
            assert {record.source_media_ref for record in grounding.records} == {
                accepted.task.media_ref
            }
            objects = {obj.bbox.xyxy: obj for obj in grounding.objects}
            assert set(objects) == set(_GROUPS)
            assert (
                observations
                == candidates
                == {box: obj.object_id for box, obj in objects.items()}
            )
            decisions = {item.object_ref.record_id: item for item in final.decisions}
            assert len(final.decisions) == len(decisions) == 2
            assert (
                set(decisions)
                == rejected_objects
                == {obj.object_id for obj in objects.values()}
            )
            assert {
                object_id
                for object_id, tool_id in tool_dispatches
                if tool_id == "wheel_segmentation"
            } == set(decisions)
            for box, obj in objects.items():
                assert obj.grounding_member_boxes is not None
                assert (
                    tuple(row.xyxy for row in obj.grounding_member_boxes)
                    == _GROUPS[box]
                )
                assert obj.marked_frame is None
                assert "marked_frame" not in obj.model_dump(mode="json")
                assert obj.source_media_ref == accepted.task.media_ref
                assert obj.coordinate_system.frame == "original_image"
                assert obj.coordinate_system.dimensions == fixture.host.media.dimensions
                wheel = tool_dispatches[obj.object_id, "wheel_segmentation"]
                assert wheel.object_bbox == obj.bbox
                assert wheel.original_coordinates == obj.coordinate_system

                inputs = state.inputs(obj.object_id)
                observation, tool = inputs.observation, inputs.tool
                assert (
                    observation is not None and observation.batch.outcome == "complete"
                )
                assert observation.inputs.object_record == obj
                assert observation.inputs.image_view.roi == obj.bbox
                assert not observation.inputs.image_view.transforms
                assert observation.inputs.image_view.marked_media_ref is None
                assert tool is not None
                expected_status = "succeeded" if box == _PARKED else "partial"
                assert tool.output.summary.status == expected_status, (
                    tool.output.summary
                )
                assert tool.output.summary.object_ref.record_id == obj.object_id
                assert tool.inputs.object_record == obj
                assert tool.inputs.image_view.roi == obj.bbox
                assert not tool.inputs.image_view.transforms
                assert tool.inputs.image_view.marked_media_ref is None
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

    asyncio.run(run())
