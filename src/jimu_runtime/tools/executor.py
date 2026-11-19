"""Execute one pinned Tool origin and return immutable invocation candidates."""

import asyncio
import json
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from math import isfinite
from typing import NoReturn

from pydantic import TypeAdapter

from jimu_runtime.bundle.semantic_inputs import require_definition
from jimu_runtime.bundle.trust import TrustPin
from jimu_runtime.domain.definition_enums import (
    EvidenceType,
    FactScope,
    ModelCapability,
    ProducerCardinality,
    ProducerKind,
)
from jimu_runtime.domain.definitions.capabilities import (
    ProfileDefinition,
    ToolGroundingBinding,
)
from jimu_runtime.domain.definitions.tools import (
    ToolDefinition,
    ToolPlanDefinition,
    ToolStep,
    plan_ancestors,
)
from jimu_runtime.domain.enums import (
    ArtifactKind,
    AttemptStatus,
    ExecutionStatus,
    FactStatus,
    NodeRunStatus,
    RuntimeRecordKind,
)
from jimu_runtime.domain.fact_schemas.canonicalization import canonical_bytes
from jimu_runtime.domain.fact_selectors import ProducerCatalogueContentRef
from jimu_runtime.domain.nodes.envelopes import NodeInputEnvelope
from jimu_runtime.domain.nodes.grounding import GroundingInput
from jimu_runtime.domain.nodes.tool_execution import ToolNodeInput, ToolNodeOutput
from jimu_runtime.domain.policies import (
    EvidencePolicy,
    EvidencePolicyArtifact,
    ResourceRequirements,
    ResourceRequirementsArtifact,
    ToolExecutionPolicy,
    ToolExecutionPolicyArtifact,
)
from jimu_runtime.domain.primitives import Digest
from jimu_runtime.domain.producers import (
    FactProducerDeclaration,
    ProducerCatalogue,
    ToolStepProducer,
)
from jimu_runtime.domain.runtime.evidence import (
    DerivedImageEvidence,
    EvidenceAsset,
    ObjectEvidenceScope,
    OriginalImageEvidence,
    TaskEvidenceAccess,
)
from jimu_runtime.domain.runtime.executions import (
    Attempt,
    ConfiguredToolPin,
    ExecutionRecord,
    NodeRun,
)
from jimu_runtime.domain.runtime.facts import (
    ApplicabilityBasisRef,
    AttemptRef,
    EvidenceRef,
    FactDiagnostic,
    FactErrorReason,
    FactNotApplicableReason,
    FactReason,
    FactRecord,
    FactRef,
    KnownFact,
    NodeRunRef,
    ObjectRef,
    ProfileContentRef,
    ToolExecutionRef,
    ToolFactSource,
)
from jimu_runtime.domain.runtime.invocations import ModelInvocation, SucceededInvocation
from jimu_runtime.domain.runtime.media import (
    BBoxXYXY,
    CoordinateSystem,
    FinalizedAsset,
    MediaAsset,
)
from jimu_runtime.domain.runtime.perception import ObjectRecord
from jimu_runtime.domain.runtime.tool_executions import (
    FailedToolAttempt,
    FailedToolResult,
    GroundingBindingOrigin,
    KnownToolOutputState,
    NonKnownToolOutputState,
    ReturnedToolAttempt,
    ToolArtifactRecordRef,
    ToolArtifactRef,
    ToolArtifactRefs,
    ToolAttemptResult,
    ToolAuthorityObservation,
    ToolError,
    ToolExecution,
    ToolInvocationOrigin,
    ToolOutputState,
    ToolPlanStepOrigin,
    ToolResourceUsage,
    ToolResult,
    UndispatchedToolAttempt,
)
from jimu_runtime.domain.schema_types import (
    RawDtoSchemaDescriptor,
    ToolInputRecordDescriptor,
    ToolResultSchemaDescriptor,
    TypedValueSchemaDescriptor,
)
from jimu_runtime.domain.tool_mappings import StepOutputInput
from jimu_runtime.domain.tool_outputs import ToolFactOutputDeclaration
from jimu_runtime.domain.validation.authority_inputs import (
    AttemptDescriptor,
    EmittedOutput,
    MappedOutputResult,
    NodeRunDescriptor,
    ObjectDescriptor,
    OptionalAbsentOutput,
)
from jimu_runtime.domain.validation.definition_inputs import (
    ProfileRegistration,
    ToolPlanRegistration,
)
from jimu_runtime.domain.validation.invocations import validate_tool_execution
from jimu_runtime.domain.validation.issues import ValidationIssue
from jimu_runtime.domain.validation.schema_descriptors import (
    describe_field,
    resolve_descriptor,
    tool_output_omission_path,
)
from jimu_runtime.domain.validation.tool_plans import ToolRegistration
from jimu_runtime.domain.values import (
    ListTypeDefinition,
    NumberValue,
    RecordTypeDefinition,
    RecordValue,
    TypeDefinition,
    Value,
    validate_typed_value,
)
from jimu_runtime.tools.audit import (
    TOOL_INVALID_OUTPUT_CODE,
    TOOL_INVALID_OUTPUT_DIAGNOSTIC,
    TOOL_NON_KNOWN_DIAGNOSTIC,
    TOOL_REPORTED_DIAGNOSTIC_CODE,
    TOOL_REPORTED_FAILURE_CODE,
    TOOL_UNVERIFIED_VERSION_DIAGNOSTIC,
    TOOL_VALIDATION_DIAGNOSTIC,
)
from jimu_runtime.tools.registry import (
    BeforeDispatch,
    CPUWorkerExchange,
    NestedModelPort,
    NestedModelRequest,
    NestedModelResponse,
    ToolDispatch,
    ToolWorkerFailure,
    ToolWorkerRegistry,
    ToolWorkerResponse,
)
from jimu_runtime.tools.step_attempts import (
    AncestorToolEvidence as AncestorToolEvidence,
)
from jimu_runtime.workers.resources import ResourceLease
from jimu_runtime.workflow.pins import ResolvedExecutionPins

K = RuntimeRecordKind
_RESULT = TypeAdapter[ToolResult](ToolResult)
_FACT = TypeAdapter[FactRecord](FactRecord)
_RESOURCES = TypeAdapter[ResourceRequirements](ResourceRequirements)
_MAX_JSON_DEPTH = 32


@dataclass(frozen=True, slots=True)
class GroundingCall:
    inputs: GroundingInput
    profile_ref: ProfileContentRef


@dataclass(frozen=True, slots=True)
class PlanStepCall:
    inputs: ToolNodeInput
    step_id: str


type ToolCall = GroundingCall | PlanStepCall
type Clock = Callable[[], datetime]
type RecordResponse = Callable[[ToolDispatch, bytes], Awaitable[EvidenceAsset | None]]


async def _record_before_deadline(
    recorder: RecordResponse, dispatch: ToolDispatch, content: bytes, budget: float
) -> EvidenceAsset | None:
    async def record() -> EvidenceAsset | None:
        return await recorder(dispatch, content)

    def observe_completion(task: asyncio.Task[EvidenceAsset | None]) -> None:
        if not task.cancelled():
            task.exception()

    if budget <= 0:
        raise TimeoutError
    task = asyncio.create_task(record())
    task.add_done_callback(observe_completion)
    try:
        done, _ = await asyncio.wait((task,), timeout=budget)
        if not done:
            raise TimeoutError
        return task.result()
    finally:
        if not task.done():
            task.cancel()


@dataclass(frozen=True, slots=True)
class ToolCallResult:
    execution: ToolExecution
    facts: tuple[FactRecord, ...]
    evidence: tuple[EvidenceAsset, ...]
    nested_invocations: tuple[ModelInvocation, ...] = ()
    retryable: bool = False
    response_digest: Digest | None = None
    response_byte_count: int | None = None
    request_digest: Digest | None = None


@dataclass(frozen=True, slots=True)
class _Site:
    profile: ProfileRegistration
    tool: ToolRegistration
    origin: ToolInvocationOrigin
    coordinates: CoordinateSystem
    object_record: ObjectRecord | None = None
    plan: ToolPlanRegistration | None = None
    step: ToolStep | None = None
    producer_catalogue_ref: ProducerCatalogueContentRef | None = None
    producer_catalogue: ProducerCatalogue | None = None
    producers: tuple[FactProducerDeclaration, ...] = ()

    @property
    def profile_ref(self) -> ProfileContentRef:
        return ProfileContentRef.model_validate(self.profile.ref.model_dump())

    @property
    def object_ref(self) -> ObjectRef | None:
        if self.object_record is None:
            return None
        return ObjectRef(record_kind=K.OBJECT, record_id=self.object_record.object_id)


def _now() -> datetime:
    return datetime.now(UTC)


def _associate(
    resolved: ResolvedExecutionPins,
    execution: ExecutionRecord,
    node: NodeRun,
    attempt: Attempt,
    envelope: NodeInputEnvelope,
) -> None:
    if (
        resolved.pins != execution.pins
        or envelope.execution_id != execution.execution_id
        or (envelope.task_id, envelope.release_id, envelope.bundle_digest)
        != (execution.task_id, resolved.pins.release_id, resolved.pins.bundle_digest)
        or (envelope.node_run_id, envelope.node_id, envelope.attempt_id)
        != (node.node_run_id, node.node_id, attempt.attempt_id)
        or envelope.node_config_ref.stable_json() != node.config_ref.stable_json()
        or envelope.trace != node.trace
        or node.trace.trace_id != execution.trace.trace_id
        or envelope.upstream_artifacts != node.upstream_inputs
        or not attempt.belongs_to_node(node)
        or execution.status is not ExecutionStatus.RUNNING
        or node.status is not NodeRunStatus.RUNNING
        or attempt.status is not AttemptStatus.RUNNING
        or not node.attempt_history
        or (
            node.attempt_history[-1].attempt_ref.record_id,
            node.attempt_history[-1].attempt_number,
        )
        != (attempt.attempt_id, attempt.attempt_number)
    ):
        raise ValueError("Tool call requires the exact live pinned node Attempt")


def _site(resolved: ResolvedExecutionPins, call: ToolCall, media: MediaAsset) -> _Site:
    catalog = resolved.catalogue
    if isinstance(call, GroundingCall):
        profile_ref = call.profile_ref
        profile = require_definition(catalog, profile_ref, ProfileDefinition)
        binding = profile.grounding_binding
        if (
            not isinstance(binding, ToolGroundingBinding)
            or not any(
                item.hit.profile_ref == profile_ref
                and item.grounding_binding == binding
                for item in call.inputs.profiles
            )
            or call.inputs.original_media_ref != media.media_ref
        ):
            raise ValueError(
                "N03 Tool must use the exact Profile hit, binding and media"
            )
        coordinates = call.inputs.original_coordinate_system
        tool_ref = binding.tool_ref
        site = _Site(
            profile=ProfileRegistration.model_validate(
                {
                    "registration_kind": "profile",
                    "ref": profile_ref.model_dump(),
                    "definition": profile,
                }
            ),
            tool=ToolRegistration(
                ref=tool_ref,
                definition=require_definition(catalog, tool_ref, ToolDefinition),
            ),
            origin=GroundingBindingOrigin(
                kind="grounding_binding", node_id="N03", grounding_binding=binding
            ),
            coordinates=coordinates,
        )
    else:
        inputs, obj = call.inputs, call.inputs.object_record
        profile_ref = obj.profile_ref
        profile = require_definition(catalog, profile_ref, ProfileDefinition)
        plan = require_definition(catalog, inputs.tool_plan_ref, ToolPlanDefinition)
        inputs.check_plan(profile, plan)
        step = next((step for step in plan.steps if step.step_id == call.step_id), None)
        if step is None or obj.source_media_ref != media.media_ref:
            raise ValueError("N05 Tool must use the exact bound step and media")
        (producer_entry,) = catalog.of_kind(ArtifactKind.FACT_PRODUCER_CATALOG)
        producer_catalogue = require_definition(
            catalog, producer_entry.ref, ProducerCatalogue
        )
        producer_ref = ProducerCatalogueContentRef.model_validate(
            producer_entry.ref.model_dump()
        )
        producers = tuple(
            item
            for item in producer_catalogue.declarations
            if isinstance(item.producer, ToolStepProducer)
            and item.profile_ref is not None
            and item.profile_ref.stable_json() == profile_ref.stable_json()
            and item.producer.tool_plan_ref == inputs.tool_plan_ref
            and item.producer.step_id == step.step_id
            and item.producer.tool_ref == step.tool_ref
        )
        if {entry.output_name: entry.fact_key for entry in producers} != {
            mapping.output_name: mapping.target_fact_key
            for mapping in step.fact_output_mappings
        }:
            raise ValueError("N05 Tool must pin every mapped output producer")
        site = _Site(
            profile=ProfileRegistration.model_validate(
                {
                    "registration_kind": "profile",
                    "ref": profile_ref.model_dump(),
                    "definition": profile,
                }
            ),
            tool=ToolRegistration(
                ref=step.tool_ref,
                definition=require_definition(catalog, step.tool_ref, ToolDefinition),
            ),
            origin=ToolPlanStepOrigin(
                kind="tool_plan_step",
                node_id="N05",
                tool_plan_ref=inputs.tool_plan_ref,
                step_id=step.step_id,
                producer_declaration_ids=tuple(
                    item.declaration_id for item in producers
                ),
            ),
            coordinates=obj.coordinate_system,
            object_record=obj,
            plan=ToolPlanRegistration(
                registration_kind="tool_plan", ref=inputs.tool_plan_ref, definition=plan
            ),
            step=step,
            producer_catalogue_ref=producer_ref,
            producer_catalogue=producer_catalogue,
            producers=producers,
        )
    if (
        not isinstance(media.stage, FinalizedAsset)
        or site.coordinates.frame != "original_image"
        or site.coordinates.dimensions != media.dimensions
    ):
        raise ValueError(
            "Tool coordinate basis requires exact finalized original media"
        )
    return site


def _tool_error(error_class: str, code: str) -> ToolError:
    return ToolError.model_validate(
        {"error_class": error_class, "code": code, "diagnostic_refs": ()}
    )


def _tool_input_refs(envelope: NodeInputEnvelope) -> ToolArtifactRefs:
    kinds = {
        K.CONTEXT_SNAPSHOT,
        K.EVIDENCE,
        K.FACT,
        K.MODEL_INVOCATION,
        K.OBJECT,
        K.TOOL_EXECUTION,
    }
    return tuple(
        ToolArtifactRef(
            artifact_ref=ToolArtifactRecordRef.model_validate(item.ref.model_dump()),
            digest=item.digest,
        )
        for item in envelope.upstream_artifacts
        if item.ref.record_kind in kinds
    )


def _raw_value(definition: TypeDefinition, raw: object) -> object:
    if isinstance(definition, RecordTypeDefinition):
        if not isinstance(raw, dict):
            raise ValueError("Tool input requires a record")
        columns = {item.name: item.type_definition for item in definition.fields}
        if not raw.keys() <= columns.keys():
            raise ValueError("Tool input contains an undeclared field")
        raw = {name: _raw_value(columns[name], raw[name]) for name in raw}
    elif isinstance(definition, ListTypeDefinition):
        if (
            not isinstance(raw, (tuple, list))
            or not definition.min_items <= len(raw) <= definition.max_items
        ):
            raise ValueError("Tool input array does not satisfy the declared bound")
        raw = tuple(_raw_value(definition.item_type, item) for item in raw)
    return {"kind": definition.kind, "value": raw}


def _bounded_request(
    resolved: ResolvedExecutionPins,
    site: _Site,
    value: Mapping[str, object],
    limit: int,
) -> bytes:
    descriptor = resolve_descriptor(
        site.tool.definition.input_schema_ref, resolved.schema_descriptors
    )
    if len(value) > 64:
        raise ValueError("Tool input contains too many root fields")
    body: object = dict(value)
    if isinstance(descriptor, ToolInputRecordDescriptor):
        declared = {column.name: column for column in descriptor.fields}
        if not value.keys() <= declared.keys() or any(
            column.required and column.name not in value for column in descriptor.fields
        ):
            raise ValueError("Tool input fields must match the pinned shape")
        fields: dict[str, object] = {}
        for name, field_data in value.items():
            column = declared[name]
            checked = validate_typed_value(
                column.type_definition,
                _raw_value(column.type_definition, field_data)
                if column.representation == "raw_dto"
                else field_data,
            )
            fields[name] = (
                field_data
                if column.representation == "raw_dto"
                else checked.model_dump(mode="json")
            )
        body = fields
    elif isinstance(descriptor, RawDtoSchemaDescriptor):
        validate_typed_value(
            descriptor.type_definition,
            _raw_value(descriptor.type_definition, dict(value)),
        )
    elif isinstance(descriptor, TypedValueSchemaDescriptor):
        body = validate_typed_value(descriptor.type_definition, body).model_dump(
            mode="json"
        )
    else:
        raise ValueError("Tool input does not resolve a normalized input contract")
    output = bytearray()
    encoder = json.JSONEncoder(
        ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":")
    )
    for piece in encoder.iterencode(body):
        data = piece.encode("utf-8")
        if len(output) + len(data) > limit:
            raise ValueError("Tool input exceeds the configured byte limit")
        output.extend(data)
    return bytes(output)


def _no_duplicate_fields(entries: list[tuple[str, object]]) -> dict[str, object]:
    result = dict(entries)
    if len(entries) != len(result):
        raise ValueError("Tool response contains duplicate fields")
    return result


def _reject_constant(_: str) -> NoReturn:
    raise ValueError("Tool response contains a nonfinite number")


def _finite_float(text: str) -> float:
    value = float(text)
    if not isfinite(value):
        _reject_constant(text)
    return value


def _validate_json_depth(text: str) -> None:
    inside_string = escaped = False
    depth = 0
    for character in text:
        if inside_string:
            if escaped:
                escaped = False
            elif character == "\\":
                escaped = True
            elif character == '"':
                inside_string = False
        elif character == '"':
            inside_string = True
        elif character in "[{":
            depth += 1
            if depth > _MAX_JSON_DEPTH:
                raise ValueError("Tool response exceeds the JSON nesting limit")
        elif character in "]}":
            depth -= 1


def _decode_result(content: bytes) -> ToolResult:
    text = content.decode("utf-8")
    _validate_json_depth(text)
    raw = json.loads(
        text,
        object_pairs_hook=_no_duplicate_fields,
        parse_constant=_reject_constant,
        parse_float=_finite_float,
    )
    return _RESULT.validate_python(raw)


def original_xyxy(coords: Sequence[float], coordinates: CoordinateSystem) -> BBoxXYXY:
    """Validate a registered grounding field against the original pixel frame."""
    if coordinates.frame != "original_image" or len(coords) != 4:
        raise ValueError("grounding requires original-image XYXY")
    result = BBoxXYXY.model_validate({"xyxy": tuple(coords)})
    result.validate_bounds(coordinates.dimensions)
    return result


def _read_value(payload: Value | None, path: tuple[str, ...]) -> Value | None:
    for name in path:
        if not isinstance(payload, RecordValue):
            return None
        payload = payload.value.get(name)
    return payload


def _remove_value(payload: Value | None, path: tuple[str, ...]) -> Value | None:
    if not path:
        return None
    if not isinstance(payload, RecordValue) or path[0] not in payload.value:
        return payload
    values = dict(payload.value)
    child = _remove_value(values[path[0]], path[1:])
    if child is None:
        del values[path[0]]
    else:
        values[path[0]] = child
    return RecordValue(kind="record", value=values)


def _evidence_ref(asset: EvidenceAsset) -> EvidenceRef:
    return EvidenceRef(record_kind=K.EVIDENCE, record_id=asset.evidence_id)


def _verify_evidence(
    execution: ExecutionRecord,
    site: _Site,
    media: MediaAsset,
    policy: EvidencePolicy,
    supplied: tuple[EvidenceAsset, ...],
    *,
    audit_response: EvidenceAsset | None,
    ancestors: Mapping[EvidenceRef, EvidenceAsset] | None = None,
) -> dict[EvidenceRef, EvidenceAsset]:
    by_ref = {_evidence_ref(asset): asset for asset in supplied}
    if len(by_ref) != len(supplied):
        raise ValueError("Tool evidence IDs must be unique")
    if audit_response is not None:
        ref = _evidence_ref(audit_response)
        if ref in by_ref and by_ref[ref] != audit_response:
            raise ValueError("Tool response evidence conflicts with the saved asset")
        by_ref[ref] = audit_response
    for asset in by_ref.values():
        original = isinstance(asset.spatial, OriginalImageEvidence)
        ancestor = None if ancestors is None else ancestors.get(_evidence_ref(asset))
        if ancestor is not None and ancestor != asset:
            raise ValueError("Tool must preserve immutable ancestor evidence")
        if (
            not isinstance(asset.stage, FinalizedAsset)
            or asset.scope.execution_id != execution.execution_id
            or (
                isinstance(asset.access_scope, TaskEvidenceAccess)
                and asset.access_scope.task_id != execution.task_id
            )
            or (
                isinstance(asset.scope, ObjectEvidenceScope)
                and (
                    asset.scope.object_ref != site.object_ref
                    or asset.scope.profile_ref != site.profile_ref
                )
            )
            or (
                site.object_ref is not None
                and not original
                and not isinstance(asset.scope, ObjectEvidenceScope)
            )
            or (
                asset is not audit_response
                and asset.evidence_type not in policy.allowed_evidence_types
            )
        ):
            raise ValueError("Tool evidence is unavailable in the exact required scope")
        if isinstance(asset.spatial, (OriginalImageEvidence, DerivedImageEvidence)):
            asset.validate_original_media(media)
            source = (
                asset.spatial.coordinate_system
                if isinstance(asset.spatial, OriginalImageEvidence)
                else asset.spatial.transforms[0].source
            )
            if source != site.coordinates:
                raise ValueError(
                    "Tool evidence must keep the exact original coordinate frame"
                )
        if not original and (
            asset.access_scope.scope != policy.access_scope
            or ancestor is None
            and (
                asset.retention.evidence_policy_ref.stable_json()
                != site.tool.definition.evidence_policy_ref.stable_json()
                or asset.retention.retention_class != policy.retention_class
            )
        ):
            raise ValueError(
                "Tool evidence must match the pinned retention and access policy"
            )
    return by_ref


def _ancestor_evidence(
    resolved: ResolvedExecutionPins,
    execution: ExecutionRecord,
    site: _Site,
    media: MediaAsset,
    request: bytes,
    policy: EvidencePolicy,
    proofs: tuple[AncestorToolEvidence, ...],
) -> tuple[dict[EvidenceRef, EvidenceAsset], dict[EvidenceRef, str]]:
    if site.plan is None or site.step is None or site.object_ref is None:
        if not proofs:
            return {}, {}
        raise ValueError("ancestor evidence is only supported on a bound ToolPlan")
    if site.tool.ref.artifact_id == "wheel_boundary_geometry" and not {
        item.target.field_path
        for item in site.step.input_bindings
        if isinstance(item.source, StepOutputInput) and item.required
    } <= {proof.input_path for proof in proofs}:
        raise ValueError("required ToolPlan ancestor evidence is missing")
    if not proofs:
        return {}, {}
    plan = site.plan.definition
    steps = {step.step_id: step for step in plan.steps}
    ancestors = plan_ancestors(plan.steps)[site.step.step_id]
    bindings = {
        binding.target.field_path: binding for binding in site.step.input_bindings
    }
    inputs = json.loads(request)
    assets: dict[EvidenceRef, EvidenceAsset] = {}
    slots: dict[EvidenceRef, str] = {}
    allowed_slots = {
        path[0]
        for output in site.tool.definition.fact_outputs
        for path in output.evidence_paths or ()
    }
    paths: set[tuple[str, ...]] = set()
    for proof in proofs:
        binding = bindings.get(proof.input_path)
        source = None if binding is None else binding.source
        if (
            not isinstance(source, StepOutputInput)
            or source.step_id not in ancestors
            or proof.input_path in paths
        ):
            raise ValueError("ancestor evidence requires an exact ToolPlan input edge")
        paths.add(proof.input_path)
        previous_step = steps[source.step_id]
        completion = type(proof.completion).model_validate(proof.completion)
        previous = completion.result.execution
        selected = completion.selection
        origin = previous.invocation_origin
        returned = previous.attempt_result
        if (
            selected is None
            or not isinstance(origin, ToolPlanStepOrigin)
            or not isinstance(returned, ReturnedToolAttempt)
            or isinstance(returned.result, FailedToolResult)
            or previous.status != "succeeded"
            or (previous.execution_id, previous.profile_ref, previous.object_ref)
            != (execution.execution_id, site.profile_ref, site.object_ref)
            or previous.trace_id != execution.trace.trace_id
            or (origin.tool_plan_ref, origin.step_id, previous.tool_ref)
            != (site.plan.ref, previous_step.step_id, previous_step.tool_ref)
            or not any(
                pin.tool_ref == previous.tool_ref
                and pin.implementation_version == previous.actual_implementation_version
                for pin in resolved.pins.tool_pins
            )
        ):
            raise ValueError(
                "ancestor evidence must come from the selected exact child"
            )
        definition = require_definition(
            resolved.catalogue, previous_step.tool_ref, ToolDefinition
        )
        previous_policy = require_definition(
            resolved.catalogue, definition.evidence_policy_ref, EvidencePolicyArtifact
        ).policy
        declaration = next(
            (
                item
                for item in definition.fact_outputs
                if item.value_path == source.selector.field_path
            ),
            None,
        )
        if (
            source.selector.schema_ref != definition.output_schema_ref
            or declaration is None
        ):
            raise ValueError(
                "ancestor evidence requires the mapped output's exact schema"
            )
        result = returned.result
        value = _read_value(result.payload, declaration.value_path)
        projected = inputs
        for key in proof.input_path:
            projected = projected.get(key) if isinstance(projected, dict) else None
        ref = _evidence_ref(proof.asset)
        fact = next(
            (
                item
                for item in completion.result.facts
                if isinstance(item.source, ToolFactSource)
                and item.source.output_name == declaration.output_name
            ),
            None,
        )
        evidence_names = {path[0] for path in declaration.evidence_paths or ()}
        source_slots = tuple(
            slot.name
            for slot in result.evidence
            if slot.name in evidence_names and slot.evidence_refs == (ref,)
        )
        result_slot = proof.result_evidence_slot or (
            source_slots[0] if len(source_slots) == 1 else None
        )
        if (
            value is None
            or value.model_dump(mode="json") != projected
            or not any(
                isinstance(state, KnownToolOutputState)
                and state.output_name == declaration.output_name
                for state in result.output_states
            )
            or len(source_slots) != 1
            or result_slot not in allowed_slots
            or result_slot in slots.values()
            or not isinstance(fact, KnownFact)
            or not isinstance(fact.source, ToolFactSource)
            or fact.source.tool_execution_ref != selected.selected_result_ref
            or (fact.source.tool_plan_ref, fact.source.step_id, fact.source.tool_ref)
            != (origin.tool_plan_ref, origin.step_id, previous.tool_ref)
            or (fact.execution_id, fact.profile_ref, fact.object_id, fact.attempt_id)
            != (
                execution.execution_id,
                site.profile_ref,
                site.object_ref.record_id,
                selected.selected_attempt_id,
            )
            or (fact.fact_key, fact.fact_schema_ref, fact.value)
            != (declaration.fact_key, declaration.fact_schema_ref, value)
            or fact.trace_id != previous.trace_id
            or not any(
                mapping.output_name == declaration.output_name
                and mapping.target_fact_key == fact.fact_key
                for mapping in previous_step.fact_output_mappings
            )
            or FactRef(record_kind=K.FACT, record_id=fact.fact_id)
            not in selected.selected_fact_refs
            or ref not in fact.evidence_refs
            or proof.asset not in completion.result.evidence
            or ref not in previous.evidence_refs
            or ref in assets
            or not any(
                item.artifact_ref.record_kind is K.EVIDENCE
                and item.artifact_ref.record_id == ref.record_id
                and item.digest == proof.asset.sha256
                for item in previous.output_refs
            )
            or (proof.asset.producer_id, proof.asset.producer_version)
            != (previous.tool_ref.artifact_id, previous.actual_implementation_version)
            or proof.asset.evidence_type not in previous_policy.allowed_evidence_types
            or proof.asset.retention.evidence_policy_ref.stable_json()
            != definition.evidence_policy_ref.stable_json()
            or proof.asset.retention.retention_class != previous_policy.retention_class
            or proof.asset.access_scope.scope != previous_policy.access_scope
        ):
            raise ValueError(
                "ancestor evidence must preserve the exact output slot and Fact"
            )
        assets[ref] = proof.asset
        assert result_slot is not None
        slots[ref] = result_slot
    _verify_evidence(
        execution,
        site,
        media,
        policy,
        tuple(assets.values()),
        audit_response=None,
        ancestors=assets,
    )
    return assets, slots


def _require_result_evidence(
    result: ToolResult,
    evidence: Mapping[EvidenceRef, EvidenceAsset],
    policy: EvidencePolicy,
) -> None:
    refs = set(result.evidence_refs)
    diagnostic_refs = {
        ref
        for state in result.output_states
        if isinstance(state, NonKnownToolOutputState)
        for ref in state.reason.diagnostic_refs
    }
    if isinstance(result, FailedToolResult):
        diagnostic_refs.update(result.error.diagnostic_refs)
    if not refs | diagnostic_refs <= evidence.keys():
        raise ValueError("Tool result referenced unavailable evidence")
    if not isinstance(result, FailedToolResult):
        present = {evidence[ref].evidence_type for ref in refs}
        required = set(policy.required_evidence_types)
        if policy.original_media_required:
            required.add(EvidenceType.ORIGINAL_IMAGE)
        if not required <= present:
            raise ValueError("Tool result lacks required validated evidence")


def _normalize_diagnostics(result: ToolResult) -> ToolResult:
    raw = result.model_dump()
    raw["diagnostics"] = tuple(
        ValidationIssue(
            code=TOOL_REPORTED_DIAGNOSTIC_CODE,
            message=TOOL_VALIDATION_DIAGNOSTIC,
            path=(),
            related_refs=(),
        )
        for _ in result.diagnostics
    )
    raw["output_states"] = tuple(
        {
            **state.model_dump(),
            "reason": FactReason(
                reason_code=f"tool_reported_{state.fact_status.value}",
                message=TOOL_NON_KNOWN_DIAGNOSTIC,
                diagnostic_refs=state.reason.diagnostic_refs,
            ),
        }
        if isinstance(state, NonKnownToolOutputState)
        else state
        for state in result.output_states
    )
    if isinstance(result, FailedToolResult):
        raw["error"] = ToolError(
            error_class=result.error.error_class,
            code=TOOL_REPORTED_FAILURE_CODE,
            diagnostic_refs=result.error.diagnostic_refs,
        )
    return _RESULT.validate_python(raw)


class _UnrepresentablePartialPayload(ValueError):
    """The pinned payload cannot retain a known sibling independently."""


def _check_named_outputs(
    result: ToolResult,
    site: _Site,
    schema: ToolResultSchemaDescriptor,
    resolved: ResolvedExecutionPins,
) -> ToolResult:
    if isinstance(result, FailedToolResult):
        return result
    contract = {item.output_name: item for item in site.tool.definition.fact_outputs}
    states = {item.output_name: item for item in result.output_states}
    if states.keys() - contract.keys():
        raise ValueError("Tool result contains an undeclared output state")
    payload = result.payload
    if site.step is None:
        if payload is not None:
            validate_typed_value(schema.payload_type, payload)
        return result

    def failed(state: ToolOutputState | None, name: str) -> NonKnownToolOutputState:
        return NonKnownToolOutputState(
            output_name=name,
            fact_status=FactStatus.ERROR,
            evidence_refs=() if state is None else state.evidence_refs,
            reason=FactReason(
                reason_code=TOOL_INVALID_OUTPUT_CODE,
                message=TOOL_INVALID_OUTPUT_DIAGNOSTIC,
                diagnostic_refs=(),
            ),
        )

    mappings = {item.output_name for item in site.step.fact_output_mappings}
    requires_partial = False
    invalid_paths: list[tuple[str, ...]] = []
    protected_paths: list[tuple[str, ...]] = []
    nonknown_paths: list[tuple[str, ...]] = []
    for name in sorted(mappings | states.keys()):
        state = states.get(name)
        declaration = contract[name]
        value = _read_value(payload, declaration.value_path)
        acceptable = state is not None and (
            isinstance(state, KnownToolOutputState) == (value is not None)
        )
        if (
            isinstance(state, NonKnownToolOutputState)
            and state.fact_status is FactStatus.NOT_APPLICABLE
        ):
            acceptable &= bool(state.reason.diagnostic_refs)
        if value is not None and acceptable:
            try:
                pinned = describe_field(schema, declaration.value_path)
                validate_typed_value(pinned.type_definition, value)
                resolved.fact_schemas.validate_payload(
                    declaration.fact_schema_ref, value
                )
            except ValueError:
                acceptable = False
        if not acceptable:
            states[name] = failed(state, name)
            invalid_paths.append(declaration.value_path)
            requires_partial = True
        elif state is not None:
            if state.fact_status is FactStatus.KNOWN:
                protected_paths.append(declaration.value_path)
            else:
                nonknown_paths.append(declaration.value_path)
            if state.fact_status is FactStatus.ERROR:
                requires_partial |= declaration.required
    for path in invalid_paths:
        if payload is None:
            break
        omission = tool_output_omission_path(schema, path, tuple(protected_paths))
        if omission is None:
            if _read_value(payload, path) is None:
                try:
                    validate_typed_value(schema.payload_type, payload)
                except ValueError as exc:
                    raise _UnrepresentablePartialPayload from exc
                continue
            raise _UnrepresentablePartialPayload
        payload = _remove_value(payload, omission)
    for path in nonknown_paths:
        if payload is None:
            break
        omission = tool_output_omission_path(schema, path, tuple(protected_paths))
        if omission is None:
            try:
                validate_typed_value(schema.payload_type, payload)
            except ValueError as exc:
                raise _UnrepresentablePartialPayload from exc
            continue
        subtree = _read_value(payload, omission)
        if subtree is None:
            continue
        try:
            validate_typed_value(
                describe_field(schema, omission).type_definition, subtree
            )
        except ValueError:
            payload = _remove_value(payload, omission)
            requires_partial = True
    if payload is not None:
        validate_typed_value(schema.payload_type, payload)
    if not requires_partial:
        return result
    return _RESULT.validate_python(
        {
            "status": "partial",
            "output_states": tuple(states.values()),
            "evidence_refs": result.evidence_refs,
            "evidence": result.evidence,
            "diagnostics": result.diagnostics,
            **({"payload": payload} if payload is not None else {}),
        }
    )


def _fact_reason(state: NonKnownToolOutputState) -> FactReason:
    reason = state.reason
    if state.fact_status is FactStatus.ERROR:
        return FactErrorReason(
            **reason.model_dump(),
            diagnostics=(
                FactDiagnostic(
                    code=reason.reason_code, message=reason.message, path=()
                ),
            ),
        )
    if state.fact_status is FactStatus.NOT_APPLICABLE:
        return FactNotApplicableReason(
            **reason.model_dump(),
            basis_refs=tuple(
                ApplicabilityBasisRef(record_kind=K.EVIDENCE, record_id=ref.record_id)
                for ref in reason.diagnostic_refs
            ),
        )
    return reason


def _fact_candidates(
    result: ToolResult,
    site: _Site,
    execution: ExecutionRecord,
    attempt: Attempt,
    execution_id: str,
    at: datetime,
) -> tuple[tuple[FactRecord, ...], tuple[MappedOutputResult, ...]]:
    if (
        site.object_ref is None
        or site.producer_catalogue_ref is None
        or site.step is None
    ):
        return (), ()
    declarations = {
        item.output_name: item for item in site.tool.definition.fact_outputs
    }
    by_name = {state.output_name: state for state in result.output_states}
    evidence = {entry.name: entry.evidence_refs for entry in result.evidence}
    facts: list[FactRecord] = []
    outputs: list[MappedOutputResult] = []
    for producer in site.producers:
        name = producer.output_name
        state = by_name.get(name)
        if state is None:
            continue
        declaration = declarations[name]
        if (
            producer.cardinality is ProducerCardinality.ZERO_OR_ONE
            and isinstance(state, NonKnownToolOutputState)
            and state.fact_status is FactStatus.NOT_EVALUATED
        ):
            outputs.append(
                OptionalAbsentOutput(
                    disposition="optional_absence",
                    output_name=name,
                    reason=state.reason,
                )
            )
            continue
        generated = _project_fact(
            execution,
            attempt,
            site,
            execution_id,
            at,
            result,
            producer,
            declaration,
            state,
            evidence,
        )
        facts.append(generated)
        outputs.append(
            EmittedOutput(
                disposition="emitted",
                output_name=name,
                fact_status=state.fact_status,
                fact_ref=FactRef(record_kind=K.FACT, record_id=generated.fact_id),
            )
        )
    return tuple(facts), tuple(outputs)


def _project_fact(
    execution: ExecutionRecord,
    attempt: Attempt,
    site: _Site,
    tool_execution_id: str,
    at: datetime,
    result: ToolResult,
    producer: FactProducerDeclaration,
    declaration: ToolFactOutputDeclaration,
    state: ToolOutputState,
    evidence: Mapping[str, tuple[EvidenceRef, ...]],
) -> FactRecord:
    assert (
        site.object_ref is not None and site.plan is not None and site.step is not None
    )
    ref = site.tool.ref
    payload = None if isinstance(result, FailedToolResult) else result.payload
    fields: dict[str, object] = {}
    if isinstance(state, NonKnownToolOutputState):
        fields["reason"] = _fact_reason(state)
    else:
        fields["value"] = _read_value(payload, declaration.value_path)
        if declaration.confidence_path is not None:
            confidence = _read_value(payload, declaration.confidence_path)
            if confidence is not None:
                assert isinstance(confidence, NumberValue)
                fields["confidence"] = confidence.value
    refs = tuple(
        dict.fromkeys(
            (
                *state.evidence_refs,
                *(
                    ref
                    for path in (declaration.evidence_paths or ())
                    for ref in evidence.get(path[0], ())
                ),
            )
        )
    )
    return _FACT.validate_python(
        {
            **fields,
            "schema_version": "1.0.0",
            "fact_id": _stable_id(
                "tool-fact", tool_execution_id, producer.declaration_id
            ),
            "fact_key": producer.fact_key,
            "fact_schema_ref": producer.fact_schema_ref,
            "producer_catalogue_ref": site.producer_catalogue_ref,
            "producer_declaration_id": producer.declaration_id,
            "producer_kind": ProducerKind.TOOL,
            "producer_id": ref.artifact_id,
            "producer_version": ref.version,
            "attempt_id": attempt.attempt_id,
            "execution_id": execution.execution_id,
            "scope": FactScope.OBJECT,
            "object_id": site.object_ref.record_id,
            "profile_ref": site.profile_ref,
            "status": state.fact_status,
            "source": {
                "source_kind": ProducerKind.TOOL,
                "tool_execution_ref": ToolExecutionRef(
                    record_kind=K.TOOL_EXECUTION, record_id=tool_execution_id
                ),
                "tool_plan_ref": site.plan.ref,
                "step_id": site.step.step_id,
                "tool_ref": ref,
                "output_name": producer.output_name,
            },
            "evidence_refs": refs,
            "created_at": at,
            "trace_id": execution.trace.trace_id,
        }
    )


def _stable_id(prefix: str, *parts: str) -> str:
    return f"{prefix}:{sha256(json.dumps(parts, separators=(',', ':')).encode()).hexdigest()}"


def _check_record(
    resolved: ResolvedExecutionPins,
    site: _Site,
    node: NodeRun,
    attempt: Attempt,
    result: ToolCallResult,
    peers: tuple[ToolExecution, ...],
) -> None:
    obj = site.object_record
    object_view = None
    if obj is not None and site.object_ref is not None:
        object_view = ObjectDescriptor(
            object_ref=site.object_ref,
            execution_id=obj.execution_id,
            profile_ref=obj.profile_ref,
        )
    schema = resolve_descriptor(
        site.tool.definition.output_schema_ref, resolved.schema_descriptors
    )
    if not isinstance(schema, ToolResultSchemaDescriptor):
        raise ValueError("the pinned Tool output must be a ToolResult")
    nested_ids = {
        ref.artifact_ref.record_id
        for ref in result.execution.output_refs
        if ref.artifact_ref.record_kind is K.MODEL_INVOCATION
    }
    validate_tool_execution(
        result.execution,
        node_run=NodeRunDescriptor.model_validate(
            {
                "node_run_ref": NodeRunRef(
                    record_kind=K.NODE_RUN, record_id=node.node_run_id
                ),
                "execution_id": node.execution_id,
                "node_id": node.node_id.value,
                "status": node.status,
            }
        ),
        attempt=AttemptDescriptor(
            attempt_ref=AttemptRef(record_kind=K.ATTEMPT, record_id=attempt.attempt_id),
            execution_id=attempt.execution_id,
            attempt_number=attempt.attempt_number,
            owner=attempt.owner,
            status=attempt.status,
        ),
        profile=site.profile,
        tool=site.tool,
        object_record=object_view,
        plan=site.plan,
        output_schema=schema,
        producer_catalogue_ref=site.producer_catalogue_ref,
        producer_catalogue=site.producer_catalogue,
        generated_facts=result.facts,
        fact_schemas=resolved.fact_schemas,
        peer_executions=peers,
        nested_invocations=tuple(
            item
            for item in result.nested_invocations
            if item.model_invocation_id in nested_ids
        ),
    )


def _validate_nested_invocation(
    invocation: ModelInvocation,
    dispatch: ToolDispatch,
    definition: ToolDefinition,
    started_at: datetime,
    received_at: datetime,
) -> None:
    if (
        definition.model_binding_ref is None
        or invocation.configured_provider.binding_ref.stable_json()
        != definition.model_binding_ref.stable_json()
        or invocation.purposes != (ModelCapability.VISION_AUXILIARY,)
        or (
            invocation.execution_id,
            invocation.node_id,
            invocation.node_run_ref.record_id,
            invocation.attempt_ref.record_id,
            invocation.trace_id,
            invocation.profile_refs,
            invocation.object_ref,
        )
        != (
            dispatch.execution_id,
            dispatch.origin.node_id,
            dispatch.node_run_id,
            dispatch.attempt_id,
            dispatch.trace_id,
            (dispatch.profile_ref,),
            dispatch.object_ref,
        )
        or not started_at
        <= invocation.started_at
        <= invocation.completed_at
        <= received_at
    ):
        raise ValueError("nested Tool invocation lacks an exact causal record")


def _usable_nested_response(result: NestedModelResponse | None) -> bool:
    return (
        result is not None
        and result.invocation is not None
        and isinstance(result.invocation.outcome, SucceededInvocation)
        and result.error is None
        and isinstance(result.content, bytes)
        and bool(result.content)
    )


def _check_nested_response(
    response: ToolWorkerResponse,
    receipt: NestedModelResponse | None,
) -> None:
    invocations = response.nested_invocations
    causal = None if receipt is None else receipt.invocation
    expected = {
        invocation.model_invocation_id: sha256(
            canonical_bytes(invocation.model_dump(mode="json"))
        ).hexdigest()
        for invocation in invocations
    }
    actual = {
        ref.artifact_ref.record_id: ref.digest
        for ref in response.invocation_refs
        if ref.artifact_ref.record_kind is K.MODEL_INVOCATION
    }
    if (
        len(actual) != len(response.invocation_refs)
        or len(expected) != len(invocations)
        or expected != actual
        or (invocations and (causal is None or invocations != (causal,)))
    ):
        raise ValueError("nested Tool invocation differs from the gateway receipt")


@dataclass(frozen=True, slots=True)
class _Audit:
    execution: ExecutionRecord
    node: NodeRun
    attempt: Attempt
    site: _Site
    invocation_id: str
    input_refs: ToolArtifactRefs
    policy: ToolExecutionPolicy
    started_at: datetime
    request_digest: Digest | None = None

    def outcome(
        self,
        result: ToolAttemptResult,
        finished_at: datetime,
        *,
        version: str | None = None,
        evidence: tuple[EvidenceAsset, ...] = (),
        output_refs: ToolArtifactRefs = (),
        usage: ToolResourceUsage | None = None,
        facts: tuple[FactRecord, ...] = (),
        observation: ToolAuthorityObservation | None = None,
        nested_invocations: tuple[ModelInvocation, ...] = (),
        response_identity: tuple[Digest, int] | None = None,
        retryable: bool = False,
    ) -> ToolCallResult:
        site = self.site
        status = (
            result.result.status
            if isinstance(result, ReturnedToolAttempt)
            else result.disposition
        )
        resolution = (
            "not_dispatched"
            if isinstance(result, UndispatchedToolAttempt)
            else "resolved"
            if version is not None
            else "unavailable"
        )
        record = ToolExecution.model_validate(
            {
                "schema_version": "1.0.0",
                "tool_execution_id": self.invocation_id,
                "execution_id": self.execution.execution_id,
                "profile_ref": site.profile_ref,
                "tool_ref": site.tool.ref,
                "node_run_ref": NodeRunRef(
                    record_kind=K.NODE_RUN, record_id=self.node.node_run_id
                ),
                "attempt_ref": AttemptRef(
                    record_kind=K.ATTEMPT, record_id=self.attempt.attempt_id
                ),
                "invocation_origin": site.origin,
                "scope": FactScope.EXECUTION
                if site.object_ref is None
                else FactScope.OBJECT,
                **(
                    {"object_ref": site.object_ref}
                    if site.object_ref is not None
                    else {}
                ),
                "input_refs": self.input_refs,
                "output_refs": output_refs,
                "implementation_version_resolution": resolution,
                **(
                    {"actual_implementation_version": version}
                    if version is not None
                    else {}
                ),
                **(
                    {
                        "implementation_version_unavailable_reason": TOOL_UNVERIFIED_VERSION_DIAGNOSTIC
                    }
                    if resolution == "unavailable"
                    else {}
                ),
                "status": status,
                "attempt_result": result,
                "started_at": self.started_at,
                "completed_at": max(self.started_at, finished_at),
                **({"resource_usage": usage} if usage is not None else {}),
                **(
                    {"authority_observation": observation}
                    if observation is not None
                    else {}
                ),
                "generated_fact_refs": tuple(
                    FactRef(record_kind=K.FACT, record_id=fact.fact_id)
                    for fact in facts
                ),
                "evidence_refs": tuple(_evidence_ref(asset) for asset in evidence),
                "trace_id": self.execution.trace.trace_id,
            }
        )
        return ToolCallResult(
            execution=record,
            facts=facts,
            evidence=evidence,
            nested_invocations=nested_invocations,
            retryable=retryable,
            response_digest=None if response_identity is None else response_identity[0],
            response_byte_count=None
            if response_identity is None
            else response_identity[1],
            request_digest=self.request_digest,
        )

    def failure(
        self,
        error: ToolError,
        at: datetime,
        *,
        dispatched: bool,
        version: str | None = None,
        evidence: tuple[EvidenceAsset, ...] = (),
        response_identity: tuple[Digest, int] | None = None,
        usage: ToolResourceUsage | None = None,
        nested_invocations: tuple[ModelInvocation, ...] = (),
    ) -> ToolCallResult:
        result: ToolAttemptResult
        if dispatched:
            result = FailedToolAttempt(disposition="failed", error=error)
        else:
            result = UndispatchedToolAttempt(disposition="not_dispatched", error=error)
        return self.outcome(
            result,
            at,
            version=version,
            evidence=evidence,
            response_identity=response_identity,
            usage=usage,
            nested_invocations=nested_invocations,
            retryable=(
                self.attempt.attempt_number < self.policy.max_attempts
                and error.error_class in self.policy.retryable_error_classes
            ),
        )


class ToolCancelled(asyncio.CancelledError):
    """Cancellation propagates while carrying the truthful partial audit."""

    def __init__(self, result: ToolCallResult) -> None:
        super().__init__("Tool call cancelled")
        self.result = result


@dataclass(frozen=True, slots=True)
class ToolExecutor:
    registry: ToolWorkerRegistry
    clock: Clock = _now

    def prepare_step_input(
        self,
        resolved: ResolvedExecutionPins,
        call: PlanStepCall,
        media: MediaAsset,
        input_fields: Mapping[str, object],
    ) -> bytes:
        """Encode the exact step request before claiming its durable child Attempt."""
        site = _site(resolved, call, media)
        tool = site.tool.definition
        policy = require_definition(
            resolved.catalogue, tool.execution_policy_ref, ToolExecutionPolicyArtifact
        ).policy
        resources = require_definition(
            resolved.catalogue,
            tool.resource_requirements_ref,
            ResourceRequirementsArtifact,
        ).policy
        return _bounded_request(
            resolved,
            site,
            input_fields,
            min(policy.input_byte_limit, resources.input_byte_limit),
        )

    async def execute(
        self,
        *,
        resolved: ResolvedExecutionPins,
        execution: ExecutionRecord,
        node: NodeRun,
        attempt: Attempt,
        call: ToolCall,
        media: MediaAsset,
        input_fields: Mapping[str, object],
        implementation_proof: TrustPin,
        deadline: datetime,
        before_dispatch: BeforeDispatch,
        peers: tuple[ToolExecution, ...] = (),
        record_response: RecordResponse | None = None,
        invoke_nested_model: NestedModelPort | None = None,
        ancestor_evidence: tuple[AncestorToolEvidence, ...] = (),
        cpu_exchange: CPUWorkerExchange | None = None,
    ) -> ToolCallResult:
        """Execute a single Tool call with an admitted shape and truthful audit.

        Input field projection belongs to the application producing the pinned
        node inputs. This boundary validates the projected wire fields against
        the admitted Tool schema. The worker reserves resources and calls
        before_dispatch before starting a process or sending a remote request.
        """
        envelope = call.inputs.envelope
        _associate(resolved, execution, node, attempt, envelope)
        site = _site(resolved, call, media)
        definition = site.tool.definition
        catalog = resolved.catalogue
        policy = require_definition(
            catalog, definition.execution_policy_ref, ToolExecutionPolicyArtifact
        ).policy
        resources = require_definition(
            catalog, definition.resource_requirements_ref, ResourceRequirementsArtifact
        ).policy
        evidence_policy = require_definition(
            catalog, definition.evidence_policy_ref, EvidencePolicyArtifact
        ).policy
        output_schema = resolve_descriptor(
            definition.output_schema_ref, resolved.schema_descriptors
        )
        if not isinstance(output_schema, ToolResultSchemaDescriptor):
            raise ValueError("the pinned Tool output is not a ToolResult contract")
        now = self.clock()
        timeout_ms = min(policy.timeout_ms, resources.timeout_ms)
        end = (
            now + timedelta(milliseconds=timeout_ms)
            if timeout_ms < (deadline - now).total_seconds() * 1000
            else deadline
        )
        limits = _RESOURCES.validate_python(
            {
                **resources.model_dump(),
                "max_concurrency": min(
                    resources.max_concurrency, policy.max_concurrency
                ),
                "timeout_ms": timeout_ms,
                "input_byte_limit": min(
                    resources.input_byte_limit, policy.input_byte_limit
                ),
            }
        )
        invocation_id = _stable_id(
            "tool",
            execution.execution_id,
            node.node_run_id,
            attempt.attempt_id,
            site.profile_ref.stable_json(),
            site.origin.stable_json(),
            "" if site.object_ref is None else site.object_ref.record_id,
        )
        audit = _Audit(
            execution,
            node,
            attempt,
            site,
            invocation_id,
            _tool_input_refs(envelope),
            policy,
            now,
        )
        preliminary = audit.failure(
            _tool_error("internal", "tool_validation"), now, dispatched=False
        )
        _check_record(resolved, site, node, attempt, preliminary, peers)

        def stopped(error_class: str, code: str) -> ToolCallResult:
            return audit.failure(
                _tool_error(error_class, code), self.clock(), dispatched=False
            )

        pin = next(
            (pin for pin in resolved.pins.tool_pins if pin.tool_ref == site.tool.ref),
            None,
        )
        if pin is None:
            return stopped("unavailable", "tool_pin_unavailable")
        try:
            implementation = self.registry.resolve(
                site.tool.ref,
                definition,
                pin,
                resources.resource_class,
                implementation_proof,
            )
        except LookupError:
            return stopped("unavailable", "tool_implementation_unavailable")
        if (
            isinstance(call, GroundingCall)
            and implementation.spatial_result_check is None
        ):
            return stopped("validation", "tool_grounding_coordinate_contract")
        if (definition.model_binding_ref is None) != (invoke_nested_model is None):
            return stopped("association", "tool_nested_binding_unavailable")
        try:
            request = _bounded_request(
                resolved, site, input_fields, limits.input_byte_limit
            )
        except ValueError:
            return stopped("validation", "tool_input_invalid")
        audit = replace(audit, request_digest=sha256(request).hexdigest())
        try:
            ancestors, ancestor_slots = _ancestor_evidence(
                resolved,
                execution,
                site,
                media,
                request,
                evidence_policy,
                ancestor_evidence,
            )
        except ValueError:
            return stopped("association", "tool_ancestor_evidence_invalid")
        if ancestors:
            linked = {item.artifact_ref: item for item in audit.input_refs}
            for asset in ancestors.values():
                input_ref = ToolArtifactRecordRef(
                    record_kind=K.EVIDENCE, record_id=asset.evidence_id
                )
                known = linked.get(input_ref)
                if known is not None and known.digest != asset.sha256:
                    return stopped("association", "tool_ancestor_evidence_invalid")
                linked[input_ref] = ToolArtifactRef(
                    artifact_ref=input_ref, digest=asset.sha256
                )
            audit = replace(audit, input_refs=tuple(linked.values()))
        if cpu_exchange is not None and (
            implementation.resource_class != "cpu"
            or implementation.physical_input_check is None
            or implementation.physical_result_check is None
        ):
            return stopped("association", "tool_cpu_exchange_unavailable")
        if end <= self.clock():
            return stopped("timeout", "tool_deadline_elapsed")
        clearance_requested = cleared = nested_requested = worker_active = False
        nested_receipt: NestedModelResponse | None = None
        owning_task = asyncio.current_task()

        def nested_audit() -> tuple[ModelInvocation, ...]:
            if nested_receipt is None or nested_receipt.invocation is None:
                return ()
            return (nested_receipt.invocation,)

        async def invoke_once(request: NestedModelRequest) -> NestedModelResponse:
            nonlocal nested_requested, nested_receipt
            if (
                invoke_nested_model is None
                or not cleared
                or not worker_active
                or nested_requested
                or asyncio.current_task() is not owning_task
                or self.clock() >= end
                or not isinstance(request, NestedModelRequest)
                or (
                    request.media is not None
                    and request.media.sha256 != media.media_ref.sha256
                )
            ):
                raise ValueError(
                    "nested model operation is outside the live Tool dispatch"
                )
            nested_requested = True
            receipt = await invoke_nested_model(request)
            if not isinstance(receipt, NestedModelResponse):
                raise ValueError("invalid nested Gateway result")
            if receipt.invocation is not None:
                _validate_nested_invocation(
                    receipt.invocation,
                    dispatch,
                    definition,
                    audit.started_at,
                    self.clock(),
                )
            if receipt.content is not None and not _usable_nested_response(receipt):
                raise ValueError("model output requires a successful gateway receipt")
            nested_receipt = receipt
            return receipt

        dispatch = ToolDispatch(
            invocation_id,
            execution.execution_id,
            node.node_run_id,
            attempt.attempt_id,
            execution.trace.trace_id,
            site.profile_ref,
            site.object_ref,
            site.tool.ref,
            site.origin,
            implementation.capability_id,
            implementation.contract_version,
            implementation.version,
            implementation.proof,
            definition.input_schema_ref,
            definition.output_schema_ref,
            audit.input_refs,
            limits,
            request,
            end,
            invoke_once if invoke_nested_model is not None else None,
            original_coordinates=site.coordinates,
            object_bbox=None if site.object_record is None else site.object_record.bbox,
            original_media_ref=media.media_ref,
            task_id=execution.task_id,
        )
        if cpu_exchange is not None:
            assert implementation.physical_input_check is not None
            try:
                physical = cpu_exchange.prepare(dispatch)
                if (
                    type(physical) is not bytes
                    or not 0 < len(physical) <= limits.input_byte_limit
                ):
                    raise ValueError("CPU Tool physical request exceeds its bound")
                implementation.physical_input_check(dispatch, physical)
            except ValueError:
                return stopped("validation", "tool_cpu_input_invalid")
            except OSError:
                return stopped("unavailable", "tool_cpu_input_unreadable")
            dispatch = replace(dispatch, physical_request=physical)
            audit = replace(audit, request_digest=sha256(physical).hexdigest())

        async def guarded_begin(candidate: ToolDispatch, lease: ResourceLease) -> bool:
            nonlocal clearance_requested, cleared
            if (
                clearance_requested
                or candidate is not dispatch
                or (
                    lease.resource_class != limits.resource_class
                    or lease.input_byte_limit
                    < len(dispatch.physical_request or request)
                )
            ):
                raise ValueError("Tool worker presented an invalid dispatch clearance")
            clearance_requested = True
            if self.clock() >= end:
                return False
            cleared = await before_dispatch(dispatch, lease)
            return cleared and self.clock() < end

        failure: ToolError | None = None
        observed_implementation: tuple[str, TrustPin] | None = None
        response: ToolWorkerResponse | None
        try:
            async with asyncio.timeout(max(0.0, (end - self.clock()).total_seconds())):
                worker_active = True
                try:
                    response = await implementation.worker.run(dispatch, guarded_begin)
                finally:
                    worker_active = False
        except ToolWorkerFailure as exc:
            if not exc.dispatched and not nested_requested:
                return stopped(exc.error.error_class, exc.error.code)
            failure = exc.error
            observed_implementation = exc.observed_implementation
            response = exc.response
        except TimeoutError:
            return audit.failure(
                _tool_error("timeout", "tool_deadline_elapsed"),
                self.clock(),
                dispatched=cleared,
                nested_invocations=nested_audit(),
            )
        except asyncio.CancelledError as exc:
            result = audit.failure(
                _tool_error("cancelled", "tool_cancelled"),
                self.clock(),
                dispatched=cleared,
                nested_invocations=nested_audit(),
            )
            raise ToolCancelled(result) from exc
        except Exception:
            return audit.failure(
                _tool_error("internal", "tool_worker_failure"),
                self.clock(),
                dispatched=cleared,
                nested_invocations=nested_audit(),
            )

        if not cleared:
            return audit.failure(
                _tool_error("association", "tool_dispatch_clearance_missing"),
                self.clock(),
                dispatched=True,
            )
        if response is None:
            assert failure is not None
            return audit.failure(
                _tool_error(failure.error_class, failure.code),
                self.clock(),
                dispatched=True,
                version=(
                    pin.implementation_version
                    if observed_implementation
                    == (pin.implementation_version, implementation_proof)
                    else None
                ),
                nested_invocations=nested_audit(),
            )
        if (
            not isinstance(response.content, bytes)
            or len(response.content) > limits.output_byte_limit
        ):
            return audit.failure(
                _tool_error("validation", "tool_output_limit"),
                self.clock(),
                dispatched=True,
                nested_invocations=nested_audit(),
            )
        received_at = self.clock()
        digest = sha256(response.content).hexdigest()
        response_identity = digest, len(response.content)
        version = _actual_version(
            response, pin, implementation_proof, observed_implementation
        )
        raw_asset: EvidenceAsset | None = None
        saved: tuple[EvidenceAsset, ...] = ()
        nested_invocations = nested_audit()

        def response_failure(error: ToolError) -> ToolCallResult:
            saved_refs = {_evidence_ref(asset) for asset in saved}
            return audit.failure(
                error.model_copy(
                    update={
                        "diagnostic_refs": tuple(
                            ref for ref in error.diagnostic_refs if ref in saved_refs
                        )
                    }
                ),
                self.clock(),
                dispatched=True,
                version=version,
                evidence=saved,
                response_identity=response_identity,
                usage=response.resource_usage,
                nested_invocations=nested_invocations,
            )

        try:
            assets = _verify_evidence(
                execution,
                site,
                media,
                evidence_policy,
                response.evidence,
                audit_response=None,
                ancestors=ancestors,
            )
            saved = tuple(assets.values())
            if (
                record_response is not None
                and response.content
                and EvidenceType.TOOL_OUTPUT in evidence_policy.allowed_evidence_types
            ):
                budget = max(0.0, (end - self.clock()).total_seconds())
                try:
                    raw_asset = await _record_before_deadline(
                        record_response, dispatch, response.content, budget
                    )
                except TimeoutError:
                    return response_failure(
                        failure
                        or _tool_error("internal", "tool_response_evidence_timeout")
                    )
                except Exception:
                    return response_failure(
                        failure
                        or _tool_error("internal", "tool_response_evidence_unavailable")
                    )
                if raw_asset is not None and (
                    raw_asset.evidence_type is not EvidenceType.TOOL_OUTPUT
                    or raw_asset.sha256 != digest
                    or raw_asset.byte_size != len(response.content)
                ):
                    raise ValueError("Tool response evidence must preserve exact bytes")
                assets = _verify_evidence(
                    execution,
                    site,
                    media,
                    evidence_policy,
                    response.evidence,
                    audit_response=raw_asset,
                    ancestors=ancestors,
                )
                saved = tuple(assets.values())
            _check_nested_response(response, nested_receipt)
            if failure is not None:
                return response_failure(failure)
            if version is None:
                return audit.failure(
                    _tool_error("unavailable", "tool_actual_implementation_unverified"),
                    self.clock(),
                    dispatched=True,
                    evidence=saved,
                    response_identity=response_identity,
                    usage=response.resource_usage,
                    nested_invocations=nested_invocations,
                )
            content = response.content
            if cpu_exchange is not None:
                budget = max(0.0, (end - self.clock()).total_seconds())
                try:
                    async with asyncio.timeout(budget):
                        content, mediated_evidence = await cpu_exchange.materialize(
                            dispatch, content
                        )
                except TimeoutError:
                    return response_failure(
                        _tool_error("timeout", "tool_sidecar_evidence_timeout")
                    )
                except ValueError:
                    return response_failure(
                        _tool_error("validation", "tool_sidecar_invalid")
                    )
                except Exception:
                    return response_failure(
                        _tool_error("internal", "tool_sidecar_evidence_unavailable")
                    )
                if (
                    type(content) is not bytes
                    or not 0 < len(content) <= limits.output_byte_limit
                ):
                    raise ValueError("CPU Tool materialized result exceeds its bound")
                assets = _verify_evidence(
                    execution,
                    site,
                    media,
                    evidence_policy,
                    (*response.evidence, *mediated_evidence),
                    audit_response=raw_asset,
                    ancestors=ancestors,
                )
                saved = tuple(assets.values())
            normalized = _decode_result(content)
            if cpu_exchange is not None:
                assert implementation.physical_result_check is not None
                implementation.physical_result_check(
                    dispatch, response.content, normalized, saved
                )
            if not isinstance(normalized, FailedToolResult):
                for ref, expected_slot in ancestor_slots.items():
                    if not any(
                        slot.name == expected_slot and slot.evidence_refs == (ref,)
                        for slot in normalized.evidence
                    ):
                        raise ValueError(
                            "reused Tool evidence must keep its exact input slot"
                        )
            if (
                definition.model_binding_ref is not None
                and not isinstance(normalized, FailedToolResult)
                and (
                    not _usable_nested_response(nested_receipt)
                    or response.nested_invocations != nested_invocations
                )
            ):
                raise ValueError(
                    "usable Tool output requires the exact nested Gateway result"
                )
            _require_result_evidence(normalized, assets, evidence_policy)
            normalized = _normalize_diagnostics(normalized)
            normalized = _check_named_outputs(normalized, site, output_schema, resolved)
            if implementation.spatial_result_check is not None and not isinstance(
                normalized, FailedToolResult
            ):
                implementation.spatial_result_check(
                    dispatch, normalized, site.coordinates
                )
            at = self.clock()
            facts, outputs = _fact_candidates(
                normalized, site, execution, attempt, invocation_id, at
            )
            observation = None
            if site.step is not None and not isinstance(normalized, FailedToolResult):
                observation = ToolAuthorityObservation(
                    arrival_qualification="late" if received_at > end else "unresolved",
                    envelope_validation="trusted",
                    association_validation="trusted",
                    mapped_outputs=outputs,
                )
            output_refs: ToolArtifactRefs = (
                *response.invocation_refs,
                *(
                    ToolArtifactRef(
                        artifact_ref=ToolArtifactRecordRef(
                            record_kind=K.EVIDENCE, record_id=asset.evidence_id
                        ),
                        digest=asset.sha256,
                    )
                    for asset in assets.values()
                    if _evidence_ref(asset) not in ancestors
                    and (
                        cpu_exchange is None
                        or asset.evidence_type is not EvidenceType.ORIGINAL_IMAGE
                    )
                ),
            )
            result = audit.outcome(
                ReturnedToolAttempt(disposition="returned", result=normalized),
                at,
                version=version,
                evidence=saved,
                output_refs=output_refs,
                usage=response.resource_usage,
                facts=facts,
                observation=observation,
                nested_invocations=nested_invocations,
                response_identity=response_identity,
                retryable=(
                    isinstance(normalized, FailedToolResult)
                    and attempt.attempt_number < policy.max_attempts
                    and normalized.error.error_class in policy.retryable_error_classes
                ),
            )
            _check_record(resolved, site, node, attempt, result, peers)
        except _UnrepresentablePartialPayload:
            result = response_failure(
                _tool_error("validation", "tool_partial_payload_unrepresentable")
            )
        except ValueError:
            result = audit.failure(
                _tool_error("validation", "tool_result_invalid"),
                self.clock(),
                dispatched=True,
                version=version,
                evidence=saved,
                response_identity=response_identity,
                usage=response.resource_usage,
                nested_invocations=nested_invocations,
            )
        except asyncio.CancelledError as exc:
            result = audit.failure(
                _tool_error("cancelled", "tool_cancelled"),
                self.clock(),
                dispatched=True,
                version=version,
                evidence=saved,
                response_identity=response_identity,
                usage=response.resource_usage,
                nested_invocations=nested_invocations,
            )
            raise ToolCancelled(result) from exc
        return result


def _actual_version(
    response: ToolWorkerResponse,
    pin: ConfiguredToolPin,
    expected_proof: TrustPin,
    observed: tuple[str, TrustPin] | None,
) -> str | None:
    expected = (pin.implementation_version, expected_proof)
    reported = (
        response.actual_implementation_version,
        response.actual_implementation_proof,
    )
    if any(
        item is not None and item != wanted
        for item, wanted in zip(reported, expected, strict=True)
    ) or (observed is not None and observed != expected):
        return None
    if reported == expected or observed == expected:
        return pin.implementation_version
    return None


def validate_tool_round(
    *,
    resolved: ResolvedExecutionPins,
    node: NodeRun,
    attempt: Attempt,
    inputs: ToolNodeInput,
    output: ToolNodeOutput,
    media: MediaAsset,
    calls: tuple[ToolCallResult, ...],
) -> None:
    """Validate a single parent round with complete peers and N05 step summary."""
    records = tuple(call.execution for call in calls)
    facts = tuple(fact for call in calls for fact in call.facts)
    profile = require_definition(
        resolved.catalogue, inputs.object_record.profile_ref, ProfileDefinition
    )
    plan = require_definition(
        resolved.catalogue, inputs.tool_plan_ref, ToolPlanDefinition
    )
    output.check_steps(inputs, profile, plan, records, facts)
    for index, result in enumerate(calls):
        origin = result.execution.invocation_origin
        if not isinstance(origin, ToolPlanStepOrigin):
            raise ValueError("N05 round contains a non-step Tool invocation")
        site = _site(resolved, PlanStepCall(inputs, origin.step_id), media)
        _check_record(
            resolved,
            site,
            node,
            attempt,
            result,
            (*records[:index], *records[index + 1 :]),
        )
