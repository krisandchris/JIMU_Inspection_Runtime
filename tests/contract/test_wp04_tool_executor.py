"""Exercise both Tool origins using the admitted Bundle and a supervised worker."""

import asyncio
import json
import sys
from copy import deepcopy
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Event
from time import sleep

import pytest

from jimu_runtime.bundle import semantics
from jimu_runtime.bundle.compiler import ReferenceSlot
from jimu_runtime.bundle.integrity import sha256_digest
from jimu_runtime.bundle.semantic_inputs import require_definition
from jimu_runtime.bundle.store import LocalBundleStore
from jimu_runtime.bundle.trust import TrustPin
from jimu_runtime.domain.definition_enums import (
    CapabilityFamily,
    CapabilityFeature,
    EvidenceType,
)
from jimu_runtime.domain.definitions.capabilities import ProfileDefinition
from jimu_runtime.domain.definitions.tools import ToolDefinition
from jimu_runtime.domain.enums import (
    ArtifactKind,
    AttemptStatus,
    ExecutionStatus,
    FactStatus,
    NodeRunStatus,
    RuntimeRecordKind,
)
from jimu_runtime.domain.fact_schemas.canonicalization import canonical_bytes
from jimu_runtime.domain.nodes.discovery import ProfileHit
from jimu_runtime.domain.nodes.envelopes import NodeInputEnvelope, SucceededNodeOutput
from jimu_runtime.domain.nodes.grounding import GroundingInput, GroundingProfileInput
from jimu_runtime.domain.nodes.observation import ObjectImageView
from jimu_runtime.domain.nodes.tool_execution import (
    InvokedToolStep,
    ToolNodeInput,
    ToolNodeOutput,
    ToolPlanRunSummary,
)
from jimu_runtime.domain.policies import (
    EvidencePolicyArtifact,
    RemoteResourceRequirements,
)
from jimu_runtime.domain.runtime.applicability import ContextSnapshotRef
from jimu_runtime.domain.runtime.evidence import (
    EvidenceAsset,
    NonSpatialEvidence,
    ObjectEvidenceScope,
    RetentionPolicyRef,
    TaskEvidenceAccess,
)
from jimu_runtime.domain.runtime.executions import (
    Attempt,
    AttemptLink,
    ExecutionRecord,
    NodeId,
    NodeRun,
)
from jimu_runtime.domain.runtime.facts import (
    AttemptRef,
    EvidenceRef,
    FactRef,
    ModelInvocationRef,
    NodeRunRef,
    ObjectRef,
    PolicyContentRef,
    ProfileContentRef,
    QuestionSelectionRef,
    ToolExecutionRef,
)
from jimu_runtime.domain.runtime.invocations import ModelInvocation
from jimu_runtime.domain.runtime.media import (
    BBoxXYXY,
    CoordinateSystem,
    FinalizedAsset,
    ImageDimensions,
    MediaAsset,
    StorageReference,
)
from jimu_runtime.domain.runtime.perception import (
    GroundingRecordRef,
    ObjectRecord,
    ToolGroundingProvenance,
)
from jimu_runtime.domain.runtime.tool_executions import (
    ReturnedToolAttempt,
    ToolArtifactRecordRef,
    ToolArtifactRef,
    ToolError,
    ToolExecution,
)
from jimu_runtime.domain.validation.authority_inputs import NodeAttemptOwner
from jimu_runtime.domain.values import ListValue, RecordValue
from jimu_runtime.tools import (
    GroundingCall,
    IsolatedCPUWorker,
    NestedModelResponse,
    PlanStepCall,
    RemoteToolWorker,
    ToolCancelled,
    ToolExecutor,
    ToolImplementation,
    ToolWorkerFailure,
    ToolWorkerRegistry,
    ToolWorkerResponse,
    original_xyxy,
    validate_tool_round,
)
from jimu_runtime.tools.executor import _check_nested_response
from jimu_runtime.workers.resource_inventory import (
    HostInventory,
    RemoteCapacity,
    RemoteQuota,
    ResourceScope,
)
from jimu_runtime.workers.resources import ResourceAdmission
from tests.fixtures.wp02.compiler.scenario import (
    TOOL,
    TOOL_CAPABILITY,
    VERSION,
    approve_source_bytes,
    capability_snapshot,
    source_scenario,
)
from tests.fixtures.wp03.pins.support import accepted_scenario

K = RuntimeRecordKind
_WORKER_BODY = """
import json
import sys
import time

case = json.load(sys.stdin)["image"]
if case == "timeout":
    time.sleep(5)
if case == "output-limit":
    print("x" * 5000)
    raise SystemExit
if case == "invalid":
    print('{"status":"succeeded","status":"failed"}')
    raise SystemExit
payload = {}
states = []
if case == "ground":
    payload["boxes"] = {"kind": "list", "value": [
        {"kind": "integer", "value": n} for n in (0, 1, 5, 7)
    ]}
elif case != "missing":
    payload["outside"] = {"kind": "bool", "value": True}
    states.append({"output_name": "outside", "fact_status": "known", "evidence_refs": []})
if case != "ground":
    payload["other"] = {"kind": "bool", "value": False}
    states.append({"output_name": "other", "fact_status": "known", "evidence_refs": []})
if case.startswith(("nested-", "shared-", "narrow-")):
    outside = payload.pop("outside")
    if case.endswith("-bad"):
        outside = {"kind": "integer", "value": 8 if case.startswith("narrow-") else 1}
    fields = {"outside": outside}
    if case.startswith("shared-"):
        fields["other"] = payload.pop("other")
    payload["group"] = {"kind": "record", "value": fields}
    if "-unknown-" in case:
        if case.endswith("-empty"):
            fields.clear()
        else:
            payload.pop("group")
        states[0].update(
            fact_status="unknown",
            reason={"reason_code": "unavailable", "message": "synthetic absence", "diagnostic_refs": []},
        )
print(json.dumps({
    "status": "succeeded",
    "payload": {"kind": "record", "value": payload},
    "output_states": states,
    "evidence": [],
    "evidence_refs": [],
    "diagnostics": [],
}))
"""


def _source(output_layout="flat"):
    source = source_scenario()
    patched = []
    features = ("geometry_output", "rgb_image", "original_xyxy")
    (plan_source,) = tuple(
        item for item in source.artifacts if item.kind is ArtifactKind.TOOL_PLAN
    )
    grounding_inputs = json.loads(plan_source.content)["steps"][0]["input_bindings"]
    for entry in source.artifacts:
        value = json.loads(entry.content)
        slots = entry.reference_slots
        if output_layout == "narrow":
            if (
                entry.kind is ArtifactKind.FACT_SCHEMA
                and entry.artifact_id == "outside"
            ):
                value["type_definition"] = {
                    "kind": "integer",
                    "minimum": 0,
                    "maximum": 10,
                }
            elif entry.kind is ArtifactKind.RULE:
                condition = value["condition_ast"]["arguments"][1]
                value["condition_ast"]["arguments"][1] = {
                    "kind": "comparison",
                    "operator": "eq",
                    "left": condition,
                    "right": {
                        "kind": "literal",
                        "constant": {
                            "type_definition": {"kind": "integer"},
                            "value": {"kind": "integer", "value": 1},
                        },
                    },
                }
                slots = tuple(
                    replace(
                        slot, location=(*slot.location[:3], "left", *slot.location[3:])
                    )
                    if slot.location[:3] == ("condition_ast", "arguments", 1)
                    else slot
                    for slot in slots
                )
        if entry.kind is ArtifactKind.PROFILE:
            old = value["grounding_binding"]
            assert old["kind"] == "model"
            value["grounding_binding"] = {
                "kind": "tool",
                "tool_ref": None,
                "input_bindings": grounding_inputs,
            }
            slots = tuple(
                slot for slot in slots if slot.location[:1] != ("grounding_binding",)
            ) + (
                ReferenceSlot(
                    location=("grounding_binding", "tool_ref"),
                    target=(ArtifactKind.TOOL, TOOL, VERSION),
                ),
                *(
                    replace(slot, location=("grounding_binding", *slot.location[2:]))
                    for slot in plan_source.reference_slots
                    if slot.location[:3] == ("steps", 0, "input_bindings")
                ),
            )
        elif entry.kind is ArtifactKind.TOOL:
            value["capability_tags"].append("object_grounding")
            value["implementation_requirement"]["required_features"] = list(features)
            value["fact_outputs"][0]["value_path"] = ["outside"]
            value["fact_outputs"].append(
                {
                    **value["fact_outputs"][0],
                    "output_name": "other",
                    "fact_key": "parking.other",
                    "value_path": ["other"],
                }
            )
            if output_layout != "flat":
                value["fact_outputs"][0]["value_path"] = ["group", "outside"]
                if output_layout == "shared":
                    value["fact_outputs"][1]["value_path"] = ["group", "other"]
            slots = (
                *slots,
                ReferenceSlot(
                    location=("fact_outputs", 1, "fact_schema_ref"),
                    target=(ArtifactKind.FACT_SCHEMA, "other", VERSION),
                ),
            )
        elif entry.kind is ArtifactKind.TOOL_PLAN:
            value["steps"][0]["fact_output_mappings"].append(
                {
                    "output_name": "other",
                    "target_fact_key": "parking.other",
                }
            )
            extra = deepcopy(value["steps"][0])
            extra["step_id"] = "evidence"
            extra["fact_output_mappings"] = []
            value["steps"].append(extra)
            slots = (
                *slots,
                *(
                    replace(slot, location=("steps", 1, *slot.location[2:]))
                    for slot in slots
                    if slot.location[:2] == ("steps", 0)
                    and slot.location[2] != "fact_output_mappings"
                ),
            )
        elif entry.kind is ArtifactKind.TOOL_OUTPUT_SCHEMA:
            for label in ("SucceededToolResult", "PartialToolResult"):
                props = value["$defs"][label]["properties"]
                props["payload"] = {
                    "type": "object",
                    "properties": {
                        "kind": {"const": "record"},
                        "value": {
                            "type": "object",
                            "properties": {
                                "outside": props["payload"],
                                "other": props["payload"],
                                "boxes": {
                                    "type": "object",
                                    "properties": {
                                        "kind": {"const": "list"},
                                        "value": {
                                            "type": "array",
                                            "minItems": 4,
                                            "maxItems": 4,
                                            "items": {
                                                "type": "object",
                                                "additionalProperties": False,
                                                "properties": {
                                                    "kind": {"const": "integer"},
                                                    "value": {
                                                        "type": "integer",
                                                        "minimum": 0,
                                                        "maximum": 8,
                                                    },
                                                },
                                                "required": ["kind", "value"],
                                            },
                                        },
                                    },
                                    "required": ["kind", "value"],
                                    "additionalProperties": False,
                                },
                            },
                            "additionalProperties": False,
                        },
                    },
                    "required": ["kind", "value"],
                    "additionalProperties": False,
                }
                if output_layout != "flat":
                    fields = props["payload"]["properties"]["value"]["properties"]
                    grouped = {"outside": fields.pop("outside")}
                    if output_layout == "narrow":
                        grouped["outside"] = {
                            "type": "object",
                            "additionalProperties": False,
                            "properties": {
                                "kind": {"const": "integer"},
                                "value": {
                                    "type": "integer",
                                    "minimum": 0,
                                    "maximum": 3,
                                },
                            },
                            "required": ["kind", "value"],
                        }
                    if output_layout == "shared":
                        grouped["other"] = fields.pop("other")
                    fields["group"] = {
                        "type": "object",
                        "additionalProperties": False,
                        "properties": {
                            "kind": {"const": "record"},
                            "value": {
                                "type": "object",
                                "properties": grouped,
                                "required": list(grouped),
                                "additionalProperties": False,
                            },
                        },
                        "required": ["kind", "value"],
                    }
        elif entry.kind is ArtifactKind.POLICY:
            if entry.artifact_id == "evidence":
                value["policy"]["original_media_required"] = False
                value["policy"]["allowed_evidence_types"].append("tool_output")
            if entry.artifact_id in ("resources", "execution"):
                value["policy"]["timeout_ms"] = 2000
                if entry.artifact_id == "resources":
                    value["policy"]["output_byte_limit"] = 4096
        patched.append(
            replace(entry, content=json.dumps(value).encode(), reference_slots=slots)
        )
        if entry.kind is ArtifactKind.FACT_SCHEMA and entry.artifact_id == "outside":
            value.update(
                schema_id="other",
                semantic_key="parking.other",
                type_definition={"kind": "bool"},
            )
            patched.append(
                replace(
                    entry,
                    artifact_id="other",
                    path="schemas/facts/definitions/other.json",
                    content=json.dumps(value).encode(),
                    origin=replace(entry.origin, artifact_id="fact_schema:other"),
                )
            )
        if entry.kind is ArtifactKind.PROFILE:
            second_id = "other_profile"
            value["profile_id"] = second_id
            patched.append(
                replace(
                    entry,
                    artifact_id=second_id,
                    content=json.dumps(value).encode(),
                    origin=replace(entry.origin, artifact_id=f"profile:{second_id}"),
                    reference_slots=slots,
                )
            )
    metadata = replace(
        source.metadata,
        required_capabilities=tuple(
            entry.model_copy(
                update={
                    "required_features": tuple(
                        CapabilityFeature(feature) for feature in features
                    )
                }
            )
            if entry.family is CapabilityFamily.TOOL_IMPLEMENTATION
            else entry
            for entry in source.metadata.required_capabilities
        ),
    )
    host = capability_snapshot()
    capabilities = tuple(
        entry.model_copy(
            update={
                "supported_features": tuple(
                    CapabilityFeature(feature) for feature in features
                )
            }
        )
        if entry.capability_id == TOOL_CAPABILITY
        else entry
        for entry in host.capabilities
    )
    protocols = tuple(host.runtime_protocols)
    (tool_protocol,) = tuple(
        item for item in protocols if item.role == "tool_execution"
    )
    from jimu_runtime.bundle.integrity import canonicalize_json

    output_bytes = next(
        entry.content
        for entry in patched
        if entry.kind is ArtifactKind.TOOL_OUTPUT_SCHEMA
    )
    digest = sha256_digest(
        canonicalize_json(output_bytes, max_bytes=256_000, max_depth=48)
    )
    tool_protocol = tool_protocol.model_copy(
        update={
            "output_schema_ref": tool_protocol.output_schema_ref.model_copy(
                update={"digest": digest}
            ),
        }
    )
    host = host.model_copy(
        update={
            "capabilities": capabilities,
            "runtime_protocols": (
                *(
                    protocol
                    for protocol in protocols
                    if protocol.role != "tool_execution"
                ),
                tool_protocol,
                tool_protocol.model_copy(update={"role": "tool_grounding"}),
            ),
        }
    )
    return approve_source_bytes(
        replace(source, metadata=metadata, artifacts=tuple(patched))
    ), host


@pytest.fixture
def tool_fixture(tmp_path):
    source, host = _source()
    return accepted_scenario(tmp_path, source=source, host=host)


def _run_objects(scenario, node_id):
    resolved = scenario.resolve()
    task = scenario.task
    at = datetime.now(UTC)
    exe = ExecutionRecord(
        schema_version=VERSION,
        execution_id="wp04-test-execution",
        task_id=task.task_id,
        pins=resolved.pins,
        trace=task.trace,
        status=ExecutionStatus.RUNNING,
        created_at=at,
        started_at=at,
        model_invocation_refs=(),
    )
    node_ref = NodeRunRef(record_kind=K.NODE_RUN, record_id=f"test-{node_id.value}")
    attempt_ref = AttemptRef(
        record_kind=K.ATTEMPT, record_id=f"test-{node_id.value}-attempt"
    )
    attempt = Attempt(
        schema_version=VERSION,
        attempt_id=attempt_ref.record_id,
        execution_id=exe.execution_id,
        owner=NodeAttemptOwner(owner_kind="node", node_run_ref=node_ref),
        attempt_number=1,
        status=AttemptStatus.RUNNING,
        created_at=at,
        started_at=at,
    )
    node = NodeRun(
        schema_version=VERSION,
        node_run_id=node_ref.record_id,
        execution_id=exe.execution_id,
        node_id=node_id,
        trace=exe.trace,
        config_ref=resolved.policy_ref,
        upstream_inputs=(),
        input_digest=exe.pins.input_digest,
        status=NodeRunStatus.RUNNING,
        created_at=at,
        started_at=at,
        attempt_history=(AttemptLink(attempt_ref=attempt_ref, attempt_number=1),),
        output_refs=(),
    )
    envelope = NodeInputEnvelope(
        schema_version=VERSION,
        node_id=node_id,
        task_id=task.task_id,
        execution_id=exe.execution_id,
        release_id=task.release_id,
        bundle_digest=task.bundle_digest,
        node_run_id=node.node_run_id,
        attempt_id=attempt.attempt_id,
        trace=exec_trace(exe),
        upstream_artifacts=(),
        node_config_ref=resolved.policy_ref,
    )
    dimensions = ImageDimensions(width=8, height=8)
    coordinates = CoordinateSystem(
        coordinate_system_id="image-frame",
        frame="original_image",
        dimensions=dimensions,
    )
    media = MediaAsset(
        schema_version=VERSION,
        media_ref=task.media_ref,
        storage=StorageReference(store_id="fixture", object_key="test/image.png"),
        stage=FinalizedAsset(
            stage="finalized",
            staged_at=at,
            verified_at=at,
            verified_sha256=task.media_ref.sha256,
            object_version=VERSION,
        ),
        color_space="RGB",
        mime_type="image/png",
        byte_size=10,
        dimensions=dimensions,
    )
    profiles = tuple(
        (
            ProfileContentRef.model_validate(entry.ref.model_dump()),
            require_definition(resolved.catalogue, entry.ref, ProfileDefinition),
        )
        for entry in resolved.catalogue.of_kind(ArtifactKind.PROFILE)
    )
    profile_ref, profile = profiles[0]
    selected = dict(
        profile_ref=profile_ref,
        question_selection_ref=QuestionSelectionRef(
            record_kind=K.QUESTION_SELECTION_RECORD, record_id="question"
        ),
        effective_question_refs=profile.question_refs,
    )
    if node_id is NodeId.N03:
        site = GroundingCall(
            GroundingInput(
                schema_version=VERSION,
                envelope=envelope,
                original_media_ref=media.media_ref,
                original_coordinate_system=coordinates,
                profiles=tuple(
                    GroundingProfileInput(
                        hit=ProfileHit.model_validate(
                            {
                                **selected,
                                "profile_ref": ref,
                                "effective_question_refs": row.question_refs,
                                "model_invocation_ref": ModelInvocationRef(
                                    record_kind=K.MODEL_INVOCATION,
                                    record_id="discovery",
                                ),
                            }
                        ),
                        grounding_binding=row.grounding_binding,
                    )
                    for ref, row in profiles
                ),
            ),
            profile_ref,
        )
    else:
        obj = ObjectRecord(
            **selected,
            schema_version=VERSION,
            object_id="object",
            execution_id=exe.execution_id,
            entity_ref=profile.entity_ref,
            grounding_record_ref=GroundingRecordRef(
                record_kind=K.GROUNDING_RECORD, record_id="grounding"
            ),
            source_media_ref=media.media_ref,
            coordinate_system=coordinates,
            bbox=BBoxXYXY(xyxy=(0.0, 1.0, 5.0, 7.0)),
            grounding_producer=ToolGroundingProvenance(
                kind="tool",
                tool_ref=resolved.pins.tool_pins[0].tool_ref,
                tool_execution_ref=ToolExecutionRef(
                    record_kind=K.TOOL_EXECUTION, record_id="grounding-tool"
                ),
                actual_implementation_version=VERSION,
                nested_model_invocation_refs=(),
            ),
            visibility="visible",
            localization_status="localized",
            evidence_refs=(),
        )
        site = PlanStepCall(
            ToolNodeInput(
                schema_version=VERSION,
                envelope=envelope,
                object_record=obj,
                image_view=ObjectImageView(roi=obj.bbox, transforms=()),
                context_snapshot_ref=ContextSnapshotRef(
                    record_kind=K.CONTEXT_SNAPSHOT, record_id="context"
                ),
                tool_plan_ref=profile.tool_plan_ref,
                validated_observation_fact_refs=(),
            ),
            TOOL,
        )
    return resolved, exe, node, attempt, site, media


def exec_trace(execution):
    return execution.trace


def _worker(tmp_path, resolved, *, expected_capacity=True, worker_factory=None):
    worker_script = tmp_path / "isolated-worker.py"
    worker_script.write_text(_WORKER_BODY)
    resource = ResourceAdmission.provision(
        tmp_path / "local-resources.sqlite3",
        HostInventory(
            "wp04-tools",
            lease_ttl_ms=5000,
            cpu_slots=int(expected_capacity),
            scopes=(ResourceScope("wp04-testing", "cpu", 1),),
        ),
    )
    (pin,) = resolved.pins.tool_pins
    definition = require_definition(resolved.catalogue, pin.tool_ref, ToolDefinition)
    proof = TrustPin(
        identity="test-tool",
        version=VERSION,
        sha256=sha256_digest(worker_script.read_bytes()),
    )

    def check_spatial(dispatch, result, coordinate_system):
        payload = result.payload
        assert isinstance(payload, RecordValue)
        boxes = payload.value.get("boxes")
        if dispatch.origin.kind == "grounding_binding":
            if not isinstance(boxes, ListValue):
                raise ValueError("missing bounds")
            original_xyxy(
                tuple(float(item.value) for item in boxes.value), coordinate_system
            )

    registration = ToolImplementation(
        capability_id=definition.implementation_requirement.capability_id,
        contract_version=definition.implementation_requirement.contract_version,
        version=pin.implementation_version,
        proof=proof,
        provider_kind=definition.provider_kind,
        input_schema_ref=definition.input_schema_ref,
        output_schema_ref=definition.output_schema_ref,
        resource_class="cpu",
        worker=worker_factory(resource, proof)
        if worker_factory
        else IsolatedCPUWorker(
            resource,
            (sys.executable, "-I", str(worker_script)),
            "wp04-testing",
            "test-worker",
            VERSION,
            proof,
            worker_script,
        ),
        spatial_result_check=check_spatial,
    )
    return ToolExecutor(ToolWorkerRegistry((registration,))), proof


async def _allow(dispatch, lease):
    assert dispatch.request and lease.lease_id
    return True


def _invoke(
    executor,
    proof,
    args,
    case,
    *,
    peers=(),
    time_limit=3.0,
    before_dispatch=_allow,
    record_response=None,
    invoke_nested_model=None,
):
    resolved, execution, node, attempt, call, media = args
    return asyncio.run(
        executor.execute(
            resolved=resolved,
            execution=execution,
            node=node,
            attempt=attempt,
            call=call,
            media=media,
            input_fields={"image": case},
            implementation_proof=proof,
            deadline=datetime.now(UTC) + timedelta(seconds=time_limit),
            before_dispatch=before_dispatch,
            peers=peers,
            record_response=record_response,
            invoke_nested_model=invoke_nested_model,
        )
    )


def test_isolated_cpu_same_tool_and_same_round_siblings(tool_fixture, tmp_path):
    n05 = _run_objects(tool_fixture, NodeId.N05)
    resolved, _, node, attempt, call, media = n05
    executor, proof = _worker(tmp_path, resolved)
    one = _invoke(executor, proof, n05, "step")
    assert one.request_digest == sha256_digest(
        executor.prepare_step_input(resolved, call, media, {"image": "step"})
    )
    two = _invoke(
        executor,
        proof,
        (*n05[:4], replace(call, step_id="evidence"), media),
        "step",
        peers=(one.execution,),
    )
    n03 = _run_objects(tool_fixture, NodeId.N03)
    grounded = _invoke(
        executor, proof, n03, "ground", peers=(one.execution, two.execution)
    )
    assert grounded.execution.status == "succeeded", (
        grounded.execution.model_dump_json()
    )
    assert grounded.execution.tool_ref == one.execution.tool_ref
    assert not grounded.facts and grounded.execution.object_ref is None
    second_profile = n03[4].inputs.profiles[1].hit.profile_ref
    grounded_sibling = _invoke(
        executor,
        proof,
        (*n03[:4], replace(n03[4], profile_ref=second_profile), n03[5]),
        "ground",
        peers=(one.execution, two.execution, grounded.execution),
    )
    assert grounded_sibling.execution.status == "succeeded"
    assert grounded.execution.attempt_ref == grounded_sibling.execution.attempt_ref
    assert grounded.execution.profile_ref != grounded_sibling.execution.profile_ref
    assert one.execution.status == two.execution.status == "succeeded"
    assert one.execution.attempt_ref == two.execution.attempt_ref
    assert one.execution.tool_execution_id != two.execution.tool_execution_id
    assert one.facts[0].status is FactStatus.KNOWN and not two.facts
    for item in (one, two, grounded):
        assert item.execution == ToolExecution.model_validate_json(
            item.execution.model_dump_json()
        )
    outputs = (one.execution, two.execution)
    now = datetime.now(UTC)
    summary = ToolNodeOutput(
        schema_version=VERSION,
        envelope=SucceededNodeOutput(
            schema_version=VERSION,
            status=NodeRunStatus.SUCCEEDED,
            node_id=NodeId.N05,
            node_run_id=node.node_run_id,
            attempt_id=attempt.attempt_id,
            trace=node.trace,
            output_artifacts=(),
            started_at=now,
            finished_at=now,
            duration_ms=0,
        ),
        summary=ToolPlanRunSummary(
            tool_plan_ref=call.inputs.tool_plan_ref,
            object_ref=ObjectRef(
                record_kind=K.OBJECT, record_id=call.inputs.object_record.object_id
            ),
            status="succeeded",
            steps=tuple(
                InvokedToolStep(
                    disposition="invoked",
                    step_id=item.invocation_origin.step_id,
                    tool_execution_ref=ToolExecutionRef(
                        record_kind=K.TOOL_EXECUTION, record_id=item.tool_execution_id
                    ),
                )
                for item in outputs
            ),
            validation_issues=(),
        ),
        tool_fact_refs=tuple(
            FactRef(record_kind=K.FACT, record_id=fact.fact_id) for fact in one.facts
        ),
        evidence_refs=(),
    )
    validate_tool_round(
        resolved=resolved,
        node=node,
        attempt=attempt,
        inputs=call.inputs,
        output=summary,
        media=media,
        calls=(one, two),
    )
    with pytest.raises(ValueError, match="duplicated or mismatched"):
        _invoke(
            executor,
            proof,
            n05,
            "step",
            peers=(
                one.execution.model_copy(update={"tool_execution_id": "different-id"}),
            ),
        )


@pytest.mark.parametrize(
    ("case", "status", "error_code"),
    [
        ("invalid", "failed", "tool_result_invalid"),
        ("missing", "partial", None),
        ("too-long" * 300, "not_dispatched", "tool_input_invalid"),
    ],
)
def test_bounded_output_failures_preserve_audit(
    tool_fixture, tmp_path, case, status, error_code
):
    args = _run_objects(tool_fixture, NodeId.N05)
    executor, proof = _worker(tmp_path, args[0])
    result = _invoke(executor, proof, args, case)
    assert result.execution.status == status
    if error_code:
        assert result.execution.attempt_result.error.code == error_code
        assert not result.facts
    else:
        assert isinstance(result.execution.attempt_result, ReturnedToolAttempt)
        assert {fact.fact_key: fact.status for fact in result.facts} == {
            "parking.outside": FactStatus.ERROR,
            "parking.other": FactStatus.KNOWN,
        }
    assert (result.response_digest is not None) == (status != "not_dispatched")


def test_unavailable_capacity_never_calls_start_hook(tool_fixture, tmp_path):
    args = _run_objects(tool_fixture, NodeId.N05)
    executor, proof = _worker(tmp_path, args[0], expected_capacity=False)

    async def unexpected(*_):
        pytest.fail("resource rejection must precede dispatch")

    result = _invoke(executor, proof, args, "step", before_dispatch=unexpected)
    assert result.execution.status == "not_dispatched"
    assert result.execution.attempt_result.error.error_class == "resource_exhausted"


def test_implementation_pin_mismatch_rejects_dispatch(tool_fixture, tmp_path):
    args = _run_objects(tool_fixture, NodeId.N05)
    executor, proof = _worker(tmp_path, args[0])
    result = _invoke(
        executor, proof.model_copy(update={"sha256": "0" * 64}), args, "step"
    )
    assert result.execution.status == "not_dispatched"
    assert (
        result.execution.attempt_result.error.code == "tool_implementation_unavailable"
    )


def _tool_evidence(args, body_path, dispatch, content):
    resolved, execution, _, _, call, _ = args
    (pin,) = resolved.pins.tool_pins
    tool = require_definition(resolved.catalogue, pin.tool_ref, ToolDefinition)
    policy = require_definition(
        resolved.catalogue, tool.evidence_policy_ref, EvidencePolicyArtifact
    ).policy
    body_path.write_bytes(content)
    digest = sha256_digest(content)
    now = datetime.now(UTC)
    return EvidenceAsset(
        schema_version=VERSION,
        evidence_id="raw-response",
        evidence_type=EvidenceType.TOOL_OUTPUT,
        scope=ObjectEvidenceScope(
            scope="object",
            execution_id=execution.execution_id,
            object_ref=dispatch.object_ref,
            profile_ref=call.inputs.object_record.profile_ref,
        ),
        storage=StorageReference(store_id="fixture", object_key=body_path.name),
        sha256=digest,
        mime_type="application/json",
        byte_size=len(content),
        stage=FinalizedAsset(
            stage="finalized",
            staged_at=now,
            verified_at=now,
            verified_sha256=digest,
            object_version=VERSION,
        ),
        spatial=NonSpatialEvidence(kind="non_spatial"),
        producer_id="tool-audit",
        producer_version=VERSION,
        access_scope=TaskEvidenceAccess(
            scope="task", project_id="fixture-project", task_id=execution.task_id
        ),
        retention=RetentionPolicyRef(
            evidence_policy_ref=PolicyContentRef.model_validate(
                tool.evidence_policy_ref.model_dump()
            ),
            retention_class=policy.retention_class,
        ),
        created_at=now,
    )


def test_raw_failed_response_keeps_exact_evidence(tool_fixture, tmp_path):
    args = _run_objects(tool_fixture, NodeId.N05)
    executor, proof = _worker(tmp_path, args[0])
    body_path = tmp_path / "raw-evidence.json"

    async def save_raw(dispatch, content):
        return _tool_evidence(args, body_path, dispatch, content)

    result = _invoke(executor, proof, args, "invalid", record_response=save_raw)
    assert result.execution.status == "failed"
    assert result.execution.attempt_result.error.code == "tool_result_invalid"
    assert result.response_digest == sha256_digest(body_path.read_bytes())
    assert result.response_byte_count == body_path.stat().st_size
    assert result.execution.evidence_refs[0].record_id == result.evidence[0].evidence_id
    assert not result.execution.output_refs and not result.facts


@pytest.mark.parametrize(
    ("case", "time_limit", "error_class"),
    [
        ("output-limit", 3.0, "validation"),
        ("timeout", 0.7, "timeout"),
    ],
)
def test_isolated_process_releases_after_timeout_or_oversize(
    tool_fixture, tmp_path, case, time_limit, error_class
):
    args = _run_objects(tool_fixture, NodeId.N05)
    executor, proof = _worker(tmp_path, args[0])
    result = _invoke(executor, proof, args, case, time_limit=time_limit)
    assert result.execution.status == "failed"
    assert result.execution.attempt_result.error.error_class == error_class
    assert result.response_digest is None and result.response_byte_count is None
    assert result.request_digest == sha256_digest(
        json.dumps({"image": case}, separators=(",", ":")).encode()
    )
    fresh = _run_objects(tool_fixture, NodeId.N05)
    assert _invoke(executor, proof, fresh, "step").execution.status == "succeeded"


def test_cancelled_process_does_not_keep_slot(tool_fixture, tmp_path):
    args = _run_objects(tool_fixture, NodeId.N05)
    resolved, execution, node, attempt, call, media = args
    executor, proof = _worker(tmp_path, resolved)

    async def invoke_cancel():
        accepted = asyncio.Event()

        async def allow_and_signal(dispatch, lease):
            accepted.set()
            return await _allow(dispatch, lease)

        task = asyncio.create_task(
            executor.execute(
                resolved=resolved,
                execution=execution,
                node=node,
                attempt=attempt,
                call=call,
                media=media,
                input_fields={"image": "timeout"},
                implementation_proof=proof,
                deadline=datetime.now(UTC) + timedelta(seconds=3),
                before_dispatch=allow_and_signal,
            )
        )
        await asyncio.wait_for(accepted.wait(), 2)
        await asyncio.sleep(0.15)
        task.cancel()
        with pytest.raises(ToolCancelled) as caught:
            await task
        result = caught.value.result
        assert result.execution.status == "failed" and not result.facts
        assert result.execution.attempt_result.error.error_class == "cancelled"

    asyncio.run(invoke_cancel())
    assert (
        _invoke(
            executor, proof, _run_objects(tool_fixture, NodeId.N05), "step"
        ).execution.status
        == "succeeded"
    )


def test_artifact_content_changed_after_host_pin(tool_fixture, tmp_path):
    args = _run_objects(tool_fixture, NodeId.N05)
    executor, proof = _worker(tmp_path, args[0])
    script = tmp_path / "isolated-worker.py"
    script.write_text(_WORKER_BODY + "\n# different bytes\n")
    result = _invoke(executor, proof, args, "step")
    assert result.execution.status == "not_dispatched"
    assert result.execution.attempt_result.error.code == "tool_cpu_artifact_changed"


def test_remote_worker_retains_capacity_until_provider_terminal_proof(
    tool_fixture, tmp_path
):
    args = _run_objects(tool_fixture, NodeId.N05)
    executor, proof = _worker(tmp_path, args[0])
    sent = []

    async def capture(dispatch, lease):
        sent.append(dispatch)
        return await _allow(dispatch, lease)

    _invoke(executor, proof, args, "step", before_dispatch=capture)
    remote_dispatch = replace(
        sent[0],
        resources=RemoteResourceRequirements.model_validate(
            {
                **sent[0].resources.model_dump(),
                "resource_class": "remote",
                "remote_quota_id": "test-quota",
            }
        ),
    )
    inventory = HostInventory(
        "wp04-remote-tests",
        lease_ttl_ms=5000,
        cpu_slots=0,
        scopes=(
            ResourceScope("test-remotes", "remote", 1, pool_or_quota_id="test-quota"),
        ),
        remote_quotas=(RemoteQuota("test-quota", 1, RemoteCapacity.PROVIDER_INFLIGHT),),
    )
    admission = ResourceAdmission.provision(
        tmp_path / "remote-resources.sqlite3",
        inventory,
        provider_terminal_verifier=lambda scope, dispatch_id, evidence_id: (
            scope,
            dispatch_id,
            evidence_id,
        )
        == ("test-remotes", remote_dispatch.tool_execution_id, "terminal"),
    )
    transmissions = []
    reservations = []

    async def allow_remote(dispatch, lease):
        assert dispatch is remote_dispatch
        reservations.append(lease)
        return True

    async def bounded_send(dispatch):
        transmissions.append(dispatch.request)
        return ToolWorkerResponse(b"{}", VERSION, proof)

    worker = RemoteToolWorker(admission, "test-remotes", "test-owner", bounded_send)
    assert asyncio.run(worker.run(remote_dispatch, allow_remote)).content == b"{}"
    assert len(transmissions) == len(reservations) == 1
    with pytest.raises(ToolWorkerFailure) as caught:
        asyncio.run(worker.run(remote_dispatch, allow_remote))
    assert not caught.value.dispatched
    assert caught.value.error.error_class == "resource_exhausted"
    assert not admission.settle_provider(
        reservations[0].lease_id, remote_dispatch.tool_execution_id, "wrong"
    )
    assert admission.settle_provider(
        reservations[0].lease_id, remote_dispatch.tool_execution_id, "terminal"
    )

    terminal_failure = replace(remote_dispatch, tool_execution_id="failed-remote")
    terminal_success = replace(remote_dispatch, tool_execution_id="next-remote")

    async def terminal_send(dispatch):
        observed = ToolWorkerResponse(
            b"{}", VERSION, proof, terminal_evidence_id="terminal"
        )
        if dispatch is terminal_failure:
            raise ToolWorkerFailure(
                ToolError(
                    error_class="transport",
                    code="observed_remote_failure",
                    diagnostic_refs=(),
                ),
                dispatched=True,
                response=observed,
            )
        return observed

    terminal_admission = ResourceAdmission.provision(
        tmp_path / "terminal-resources.sqlite3",
        inventory,
        provider_terminal_verifier=lambda scope, dispatch_id, proof_id: (
            scope == "test-remotes"
            and dispatch_id in {"failed-remote", "next-remote"}
            and proof_id == "terminal"
        ),
    )
    terminal_worker = RemoteToolWorker(
        terminal_admission, "test-remotes", "test-owner", terminal_send
    )
    with pytest.raises(ToolWorkerFailure, match="observed_remote_failure"):
        asyncio.run(terminal_worker.run(terminal_failure, _allow))
    assert asyncio.run(terminal_worker.run(terminal_success, _allow)).content == b"{}"


def test_cancelled_remote_reservation_cleans_after_loop_shutdown(
    tool_fixture, tmp_path, monkeypatch
):
    args = _run_objects(tool_fixture, NodeId.N05)
    executor, proof = _worker(tmp_path, args[0])
    captured = []

    async def capture(dispatch, lease):
        captured.append(dispatch)
        return await _allow(dispatch, lease)

    _invoke(executor, proof, args, "step", before_dispatch=capture)
    remote = replace(
        captured[0],
        resources=RemoteResourceRequirements.model_validate(
            {
                **captured[0].resources.model_dump(),
                "resource_class": "remote",
                "remote_quota_id": "test-quota",
            }
        ),
    )
    admission = ResourceAdmission.provision(
        tmp_path / "cancelled-remote.sqlite3",
        HostInventory(
            "wp04-cancelled-remote",
            lease_ttl_ms=5000,
            cpu_slots=0,
            scopes=(
                ResourceScope(
                    "test-remotes", "remote", 1, pool_or_quota_id="test-quota"
                ),
            ),
            remote_quotas=(
                RemoteQuota("test-quota", 1, RemoteCapacity.PROVIDER_INFLIGHT),
            ),
        ),
    )
    original = admission.reserve
    started = Event()
    allow_commit = Event()

    def slow_reserve(*args, **kwargs):
        started.set()
        assert allow_commit.wait(2)
        return original(*args, **kwargs)

    async def unexpected(*_):
        pytest.fail("the canceled reservation or expired activation must not send")

    worker = RemoteToolWorker(admission, "test-remotes", "test-owner", unexpected)
    with monkeypatch.context() as patch:
        patch.setattr(admission, "reserve", slow_reserve)

        async def cancel_reserve():
            task = asyncio.create_task(worker.run(remote, unexpected))
            assert await asyncio.to_thread(started.wait, 1)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            allow_commit.set()

        asyncio.run(cancel_reserve())
    lease = admission.reserve(remote.resources, "test-remotes", owner_id="next-worker")
    assert admission.release_reserved(lease)

    original_activate = admission.activate_remote

    def slow_activate(*args, **kwargs):
        sleep(0.15)
        return original_activate(*args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(admission, "activate_remote", slow_activate)
        expired = replace(
            remote, deadline=datetime.now(UTC) + timedelta(milliseconds=100)
        )
        with pytest.raises(ToolWorkerFailure) as caught:
            asyncio.run(worker.run(expired, _allow))
        assert not caught.value.dispatched
        assert caught.value.error.error_class == "timeout"
    lease = admission.reserve(remote.resources, "test-remotes", owner_id="after-expiry")
    assert admission.release_reserved(lease)


def test_unbound_tool_cannot_be_given_a_model_gateway(tool_fixture, tmp_path):
    args = _run_objects(tool_fixture, NodeId.N05)
    executor, proof = _worker(tmp_path, args[0])

    async def unexpected(*_):
        pytest.fail("unbound Tool must not begin a dispatch or request a model")

    result = _invoke(
        executor,
        proof,
        args,
        "step",
        before_dispatch=unexpected,
        invoke_nested_model=unexpected,
    )
    assert result.execution.status == "not_dispatched"
    assert (
        result.execution.attempt_result.error.code == "tool_nested_binding_unavailable"
    )


def test_nested_references_must_echo_the_exact_causal_gateway_receipt():
    fixture = Path(__file__).parents[1] / "fixtures/wp01c/invocations/contracts.json"
    source = json.loads(fixture.read_text(encoding="utf-8"))
    invocation = ModelInvocation.model_validate_json(
        json.dumps(source["models"]["observation"])
    )

    def report(record):
        return ToolWorkerResponse(
            content=b"{}",
            actual_implementation_version=None,
            actual_implementation_proof=None,
            nested_invocations=(record,),
            invocation_refs=(
                ToolArtifactRef(
                    artifact_ref=ToolArtifactRecordRef(
                        record_kind=K.MODEL_INVOCATION,
                        record_id=record.model_invocation_id,
                    ),
                    digest=sha256_digest(
                        canonical_bytes(record.model_dump(mode="json"))
                    ),
                ),
            ),
        )

    confirmed = NestedModelResponse(invocation, b"{}", None)
    actual = report(invocation)
    _check_nested_response(actual, confirmed)
    with pytest.raises(ValueError, match="differs from the gateway receipt"):
        _check_nested_response(actual, None)
    with pytest.raises(ValueError, match="differs from the gateway receipt"):
        _check_nested_response(actual, NestedModelResponse(None, None, None))
    spliced = invocation.model_copy(update={"request_digest": "1" * 64})
    with pytest.raises(ValueError, match="differs from the gateway receipt"):
        _check_nested_response(report(spliced), confirmed)


def test_isolated_launch_uses_artifact_checked_before_dispatch(tool_fixture, tmp_path):
    args = _run_objects(tool_fixture, NodeId.N05)
    executor, proof = _worker(tmp_path, args[0])
    artifact = tmp_path / "isolated-worker.py"

    async def switch_after_verification(dispatch, lease):
        replacement = tmp_path / "replacement.py"
        replacement.write_text("raise RuntimeError('unverified artifact')\n")
        replacement.replace(artifact)
        return await _allow(dispatch, lease)

    result = _invoke(
        executor, proof, args, "step", before_dispatch=switch_after_verification
    )
    assert result.execution.status == "succeeded"
    assert result.execution.actual_implementation_version == VERSION
    assert len(result.facts) == 2


@pytest.mark.parametrize("fail", ["timeout", "io_error"])
def test_evidence_storage_failure_retains_physical_response(
    tool_fixture, tmp_path, fail
):
    args = _run_objects(tool_fixture, NodeId.N05)
    executor, proof = _worker(tmp_path, args[0])
    bodies = []

    async def cannot_store(_dispatch, content):
        bodies.append(content)
        if fail == "io_error":
            raise OSError("synthetic backing store failure")
        await asyncio.sleep(10)

    result = _invoke(
        executor, proof, args, "step", record_response=cannot_store, time_limit=1.5
    )
    assert result.execution.status == "failed"
    assert result.execution.attempt_result.error.code == (
        "tool_response_evidence_timeout"
        if fail == "timeout"
        else "tool_response_evidence_unavailable"
    )
    assert not result.retryable
    assert result.response_digest == sha256_digest(bodies[0])
    assert result.response_byte_count == len(bodies[0])
    assert result.execution.actual_implementation_version == VERSION
    assert not result.execution.evidence_refs


@pytest.mark.parametrize("layout", ["nested", "narrow"])
def test_independent_optional_subtree_retains_sibling_when_required_leaf_invalid(
    tmp_path, layout
):
    source, host = _source(layout)
    fixture = accepted_scenario(tmp_path, source=source, host=host)
    args = _run_objects(fixture, NodeId.N05)
    executor, proof = _worker(tmp_path, args[0])
    result = _invoke(executor, proof, args, f"{layout}-bad")
    assert result.execution.status == "partial"
    assert {fact.fact_key: fact.status for fact in result.facts} == {
        "parking.outside": FactStatus.ERROR,
        "parking.other": FactStatus.KNOWN,
    }


@pytest.mark.parametrize("group", ["empty", "absent"])
def test_explicit_unknown_prunes_only_an_invalid_present_group(tmp_path, group):
    source, host = _source("nested")
    fixture = accepted_scenario(tmp_path, source=source, host=host)
    args = _run_objects(fixture, NodeId.N05)
    executor, proof = _worker(tmp_path, args[0])
    result = _invoke(executor, proof, args, f"nested-unknown-{group}")
    assert result.execution.status == ("partial" if group == "empty" else "succeeded")
    facts = {fact.fact_key: fact for fact in result.facts}
    assert facts["parking.other"].status is FactStatus.KNOWN
    assert facts["parking.outside"].status is FactStatus.UNKNOWN
    assert facts["parking.outside"].reason.reason_code == "tool_reported_unknown"


def test_historical_shared_required_leaves_reopen_with_original_wire(
    tmp_path, monkeypatch
):
    source, host = _source("shared")
    original = semantics.build_schema_descriptors

    def historical_rules(catalogue, *, schema_index, require_independent_tool_outputs):
        assert require_independent_tool_outputs
        return original(catalogue, schema_index=schema_index)

    legacy = tmp_path / "legacy"
    legacy.mkdir()
    with monkeypatch.context() as old:
        old.setattr(semantics, "build_schema_descriptors", historical_rules)
        activated = accepted_scenario(legacy, source=source, host=host)
    fresh = tmp_path / "fresh"
    fresh.mkdir()
    with pytest.raises(AssertionError, match="tool_output_omission_shared"):
        accepted_scenario(fresh, source=source, host=host)
    reopened = LocalBundleStore(legacy / "ledger")
    args = _run_objects(
        replace(
            activated, store=reopened, manager=activated.authority.manager(reopened)
        ),
        NodeId.N05,
    )
    executor, proof = _worker(legacy, args[0])
    good = _invoke(executor, proof, args, "shared-good")
    assert good.execution.status == "succeeded"
    assert len(good.facts) == 2 and all(
        item.status is FactStatus.KNOWN for item in good.facts
    )
    bad = _invoke(executor, proof, args, "shared-bad")
    assert (
        bad.execution.attempt_result.error.code
        == "tool_partial_payload_unrepresentable"
    )
    assert bad.execution.status == "failed"
    assert bad.response_digest and bad.response_byte_count
    assert not bad.facts


@pytest.mark.parametrize("mode", ["success", "missing_reported_proof", "conflict"])
def test_worker_and_sink_merge_evidence_and_preserve_proven_failure(
    mode, tool_fixture, tmp_path
):
    args = _run_objects(tool_fixture, NodeId.N05)
    assets = []
    content = json.dumps(
        {
            "status": "succeeded",
            "payload": {
                "kind": "record",
                "value": {
                    "outside": {"kind": "bool", "value": True},
                    "other": {"kind": "bool", "value": False},
                },
            },
            "output_states": [
                {"output_name": name, "fact_status": "known", "evidence_refs": []}
                for name in ("outside", "other")
            ],
            "evidence": [],
            "evidence_refs": [],
            "diagnostics": [],
        }
    ).encode()

    class Port:
        def __init__(self, admission, proof):
            self.admission = admission
            self.proof = proof

        async def run(self, dispatch, before_dispatch):
            lease = self.admission.reserve(
                dispatch.resources, "wp04-testing", owner_id="response-worker"
            )
            try:
                assert await before_dispatch(dispatch, lease)
                asset = _tool_evidence(
                    args, tmp_path / "reported.json", dispatch, content
                )
                assets.append(asset)
                response = ToolWorkerResponse(
                    content,
                    VERSION
                    if mode == "success"
                    else "2.0.0"
                    if mode == "conflict"
                    else None,
                    self.proof if mode == "success" else None,
                    evidence=(asset,),
                )
                if mode == "success":
                    return response
                raise ToolWorkerFailure(
                    ToolError(
                        error_class="transport",
                        code="fixture_failure",
                        diagnostic_refs=(
                            EvidenceRef(
                                record_kind=K.EVIDENCE, record_id=asset.evidence_id
                            ),
                            EvidenceRef(record_kind=K.EVIDENCE, record_id="unknown"),
                        ),
                    ),
                    dispatched=True,
                    response=response,
                    observed_implementation=(VERSION, self.proof),
                )
            finally:
                self.admission.release_reserved(lease)

    async def record_response(_dispatch, _content):
        return assets[0]

    executor, proof = _worker(tmp_path, args[0], worker_factory=Port)
    observed = _invoke(executor, proof, args, "step", record_response=record_response)
    assert observed.evidence == (assets[0],)
    assert len(observed.execution.evidence_refs) == 1
    assert observed.response_digest == sha256_digest(content)
    if mode == "success":
        assert observed.execution.status == "succeeded"
        assert len(observed.facts) == 2
    else:
        assert observed.execution.status == "failed"
        assert observed.execution.attempt_result.error.code == "fixture_failure"
        assert (
            observed.execution.attempt_result.error.diagnostic_refs
            == observed.execution.evidence_refs
        )
        assert observed.execution.actual_implementation_version == (
            None if mode == "conflict" else VERSION
        )


def test_response_audit_returns_even_if_storage_cancellation_is_delayed(
    tool_fixture, tmp_path
):
    args = _run_objects(tool_fixture, NodeId.N05)
    executor, proof = _worker(tmp_path, args[0])
    resolved, execution, node, attempt, call, media = args

    async def exercise():
        storage_cancelled = asyncio.Event()
        storage_released = asyncio.Event()
        storage_finished = asyncio.Event()

        async def recover_slowly(dispatch, data):
            try:
                await asyncio.sleep(10)
            except asyncio.CancelledError:
                storage_cancelled.set()
                await storage_released.wait()
            result = _tool_evidence(args, tmp_path / "late.json", dispatch, data)
            storage_finished.set()
            return result

        try:
            outcome = await asyncio.wait_for(
                executor.execute(
                    resolved=resolved,
                    execution=execution,
                    node=node,
                    attempt=attempt,
                    call=call,
                    media=media,
                    input_fields={"image": "step"},
                    implementation_proof=proof,
                    deadline=datetime.now(UTC) + timedelta(milliseconds=900),
                    before_dispatch=_allow,
                    record_response=recover_slowly,
                ),
                timeout=1.8,
            )
            await asyncio.wait_for(storage_cancelled.wait(), timeout=0.5)
            assert not storage_finished.is_set()
            assert (
                outcome.execution.attempt_result.error.code
                == "tool_response_evidence_timeout"
            )
            assert outcome.response_digest is not None and not outcome.evidence
        finally:
            storage_released.set()
            await asyncio.wait_for(storage_finished.wait(), timeout=0.5)

    asyncio.run(exercise())
