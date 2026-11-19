"""N04 projects one exact Object dispatch into independently validated Facts."""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, replace
from decimal import Decimal
from typing import Literal, cast

from jimu_runtime.bundle.integrity import sha256_digest
from jimu_runtime.bundle.schema_validation import (
    INSUFFICIENT_VISIBILITY,
    OfflineResponseSchema,
    compile_offline_response_schema,
)
from jimu_runtime.bundle.semantic_inputs import build_fact_schema_registry
from jimu_runtime.domain.definition_enums import (
    EvidenceType,
    FactScope,
    ModelCapability,
    ProducerKind,
)
from jimu_runtime.domain.definitions.capabilities import (
    ProfileDefinition,
    QuestionDefinition,
)
from jimu_runtime.domain.definitions.prompts import PromptTemplateDefinition
from jimu_runtime.domain.enums import ArtifactKind, AttemptStatus, FactStatus
from jimu_runtime.domain.fact_schemas.canonicalization import JSONValue
from jimu_runtime.domain.fact_schemas.models import FactSchemaDefinition
from jimu_runtime.domain.fact_schemas.registry import FactSchemaRegistry
from jimu_runtime.domain.fact_selectors import (
    ProducerCatalogueContentRef,
    QuestionContentRef,
)
from jimu_runtime.domain.nodes.envelopes import (
    FailedNodeOutput,
    NodeError,
    NodeInputEnvelope,
)
from jimu_runtime.domain.nodes.observation import (
    JointQuestionInput,
    ObjectImageView,
    ObservationInput,
    ObservationRetry,
)
from jimu_runtime.domain.policies import (
    EvidencePolicyArtifact,
    QuestionValidationPolicy,
    QuestionValidationPolicyArtifact,
    ResourceRequirementsArtifact,
    UnknownPolicy,
    UnknownPolicyArtifact,
)
from jimu_runtime.domain.producers import (
    FactProducerDeclaration,
    ProducerCatalogue,
    QuestionProducer,
)
from jimu_runtime.domain.references import SchemaRef
from jimu_runtime.domain.runtime.authority import ActorRef, ObservationBatchSelection
from jimu_runtime.domain.runtime.evidence import (
    EvidenceAsset,
    NonSpatialEvidence,
    ObjectEvidenceScope,
    ProjectEvidenceAccess,
    TaskEvidenceAccess,
)
from jimu_runtime.domain.runtime.facts import (
    EvidenceRef,
    FactRef,
    ObjectRef,
    ObservationBatchRef,
    PolicyContentRef,
    QuestionFactSource,
)
from jimu_runtime.domain.runtime.intake import ContextSnapshot
from jimu_runtime.domain.runtime.invocations import (
    FailedInvocation,
    InvocationContractSnapshot,
    InvocationError,
    ModelInvocation,
)
from jimu_runtime.domain.runtime.media import FinalizedAsset
from jimu_runtime.domain.runtime.observations import ObservationBatch, QuestionResult
from jimu_runtime.domain.runtime.perception import ObjectRecord
from jimu_runtime.domain.runtime.selection import QuestionSelectionRecord
from jimu_runtime.domain.values import (
    BooleanTypeDefinition,
    EnumTypeDefinition,
    ListTypeDefinition,
    RecordTypeDefinition,
    TypeDefinition,
)
from jimu_runtime.gateway.bridge import (
    CapabilityGateway,
    ModelCall,
    require_model_output,
)
from jimu_runtime.gateway.http_wire import ChatContract, VerifiedImage, strict_json
from jimu_runtime.gateway.invocation import DispatchOutcome
from jimu_runtime.inspection.perception.model import (
    ModelAccess,
    associated_invocation,
    build_chat_contract,
    gateway_binding,
    image_input,
    invocation_ref,
    issue,
    literal_prompt,
    model_failure,
    node_output,
    original_frame,
    prepare_binding,
    response_schema_bytes,
    runtime_id,
    schema_resource,
)
from jimu_runtime.workflow.mock_context import snapshot_artifact, snapshot_ref
from jimu_runtime.workflow.mock_support import (
    VERSION,
    K,
    VerifiedExecutionScope,
    artifact,
    record_digest,
)
from jimu_runtime.workflow.worker import AdapterRejected, ClaimedNode
from jimu_runtime.workflow.worker_nodes import NodeResult, ObservationRetryWitness
from jimu_runtime.workflow.worker_records import ObservationMessage

type RequestEvidenceKind = Literal["compiled_prompt", "response_schema"]
type StageRequestEvidence = Callable[
    [ClaimedNode, ObjectRecord, RequestEvidenceKind, bytes, PolicyContentRef],
    Awaitable[EvidenceAsset],
]
_ACTOR = ActorRef(
    actor_kind="service", actor_id="jimu.workflow", identity_registry_id="jimu.runtime"
)
_PURPOSE = ModelCapability.OBJECT_OBSERVATION


@dataclass(frozen=True, slots=True)
class QuestionPlan:
    ref: QuestionContentRef
    definition: QuestionDefinition
    declaration: FactProducerDeclaration
    fact_schema: FactSchemaDefinition
    validation: QuestionValidationPolicyArtifact
    unknown: UnknownPolicy
    fact_payload: OfflineResponseSchema
    response_payload: OfflineResponseSchema


@dataclass(frozen=True, slots=True)
class ObservationPlan:
    profile: ProfileDefinition
    questions: tuple[QuestionPlan, ...]
    catalogue_ref: ProducerCatalogueContentRef
    fact_registry: FactSchemaRegistry
    evidence_policy_ref: PolicyContentRef
    contract: ChatContract
    prompt: str
    schema: bytes
    output_limit: int


def _resolve_questions(
    scope: VerifiedExecutionScope, obj: ObjectRecord, fact_registry: FactSchemaRegistry
) -> tuple[ProducerCatalogueContentRef, tuple[QuestionPlan, ...]]:
    (member,) = scope.catalogue.of_kind(ArtifactKind.FACT_PRODUCER_CATALOG)
    catalogue_ref = ProducerCatalogueContentRef.model_validate(member.ref.model_dump())
    catalogue = scope.resolve(catalogue_ref, ProducerCatalogue)
    declarations = {
        row.producer.question_ref: row
        for row in catalogue.declarations
        if row.profile_ref is not None
        and row.profile_ref.stable_json() == obj.profile_ref.stable_json()
        and isinstance(row.producer, QuestionProducer)
    }
    plans = []
    for ref in obj.effective_question_refs:
        question = scope.resolve(ref, QuestionDefinition)
        declaration = declarations.get(ref)
        if declaration is None or (
            declaration.fact_key,
            declaration.fact_schema_ref,
            declaration.scope,
        ) != (
            question.primary_fact_key,
            question.primary_fact_schema_ref,
            FactScope.OBJECT,
        ):
            raise AdapterRejected("perception.question_producer_mismatch")
        definition = fact_registry.resolve(declaration.fact_schema_ref)
        fact_type = definition.type_definition
        if not isinstance(
            fact_type, (EnumTypeDefinition, BooleanTypeDefinition, RecordTypeDefinition)
        ):
            raise AdapterRejected("perception.unsupported_question_fact_type")
        if (
            isinstance(fact_type, EnumTypeDefinition)
            and INSUFFICIENT_VISIBILITY in fact_type.members
        ):
            raise AdapterRejected("perception.visibility_token_conflicts_with_fact")
        unknown = scope.resolve(
            question.unknown_policy_ref, UnknownPolicyArtifact
        ).policy
        if (
            unknown.insufficient_visibility != "unknown"
            or unknown.non_known_value != "forbidden"
            or not unknown.reason_required
        ):
            raise AdapterRejected("perception.unsupported_question_unknown_policy")
        validation = scope.resolve(
            question.validation_policy_ref, QuestionValidationPolicyArtifact
        )
        template = scope.resolve(question.prompt_template_ref, PromptTemplateDefinition)
        plans.append(
            QuestionPlan(
                ref,
                question,
                declaration,
                definition,
                validation,
                unknown,
                compile_offline_response_schema(
                    schema_resource(scope, definition.payload_schema_ref).document
                ),
                compile_offline_response_schema(
                    schema_resource(scope, template.response_schema_ref).document
                ),
            )
        )
    return catalogue_ref, tuple(plans)


def _plan(
    scope: VerifiedExecutionScope,
    obj: ObjectRecord,
    snapshot: ContextSnapshot,
    selection: QuestionSelectionRecord,
) -> ObservationPlan:
    profile = scope.resolve(obj.profile_ref, ProfileDefinition)
    scope.check_snapshot(snapshot, snapshot_ref(snapshot))
    obj.check_selection(scope.execution_id, selection)
    if (
        obj.source_media_ref != scope.media.media_ref
        or obj.coordinate_system != original_frame(scope)
        or obj.entity_ref != profile.entity_ref
        or selection.context_snapshot_digest != record_digest(snapshot)
        or selection.context_snapshot_ref != snapshot_ref(snapshot)
    ):
        raise AdapterRejected("perception.object_or_context_pin_mismatch")
    if not obj.effective_question_refs:
        raise AdapterRejected("perception.no_effective_questions")
    ref = profile.observation_binding_ref
    binding = prepare_binding(scope, ref, _PURPOSE)
    contract = build_chat_contract(scope, ref, obj.effective_question_refs)
    fact_registry = build_fact_schema_registry(scope.catalogue)
    catalogue_ref, questions = _resolve_questions(scope, obj, fact_registry)
    policy_refs = {
        PolicyContentRef.model_validate(
            plan.definition.evidence_policy_ref.model_dump()
        )
        for plan in questions
    }
    if len(policy_refs) != 1:
        raise AdapterRejected("perception.unsupported_joint_evidence_policies")
    (policy_ref,) = policy_refs
    policy = scope.resolve(policy_ref, EvidencePolicyArtifact).policy
    if EvidenceType.MODEL_REQUEST not in policy.allowed_evidence_types:
        raise AdapterRejected("perception.request_evidence_not_permitted")
    prompts: dict[str, JSONValue] = {}
    for plan in questions:
        question = plan.definition
        if question.context_allowlist:
            raise AdapterRejected("perception.unsupported_question_context")
        template = scope.resolve(question.prompt_template_ref, PromptTemplateDefinition)
        schema_ref = scope.catalogue.resolve(template.response_schema_ref).ref
        # Embedded instructions use the admitted per-Question prompt while
        # the stock transport validates the independent joint response slots.
        exact_schema = SchemaRef.model_validate(
            {
                **schema_ref.model_dump(),
                "schema_dialect": contract.output_schema_ref.schema_dialect,
            }
        )
        prompts[plan.ref.artifact_id] = literal_prompt(
            scope, question.prompt_template_ref, _PURPOSE, exact_schema
        )
    model_input = image_input(scope)
    if obj.marked_frame is not None:
        model_input["image"] = obj.marked_frame.media_ref.model_dump(mode="json")
    model_input["bbox"] = {"xyxy": list(obj.bbox.xyxy)}
    prompt = binding.prompt(
        model_input, {"object_id": obj.object_id, "questions": prompts}
    )
    return ObservationPlan(
        profile,
        questions,
        catalogue_ref,
        fact_registry,
        policy_ref,
        contract,
        prompt,
        response_schema_bytes(scope, binding.definition.output_schema_ref),
        scope.resolve(
            binding.definition.resource_requirements_ref, ResourceRequirementsArtifact
        ).policy.output_byte_limit,
    )


def _request_inputs(
    scope: VerifiedExecutionScope,
    envelope: NodeInputEnvelope,
    obj: ObjectRecord,
    snapshot: ContextSnapshot,
    selection: QuestionSelectionRecord,
    plan: ObservationPlan,
    evidence: tuple[EvidenceAsset, ...],
    retry: ObservationRetry | None,
) -> ObservationInput:
    if len(evidence) != 2 or evidence[0].evidence_id == evidence[1].evidence_id:
        raise AdapterRejected("perception.incomplete_request_evidence")
    _check_evidence(scope, obj, plan, evidence)
    (prompt, schema) = evidence
    inputs = ObservationInput(
        schema_version=VERSION,
        envelope=envelope,
        object_record=obj,
        context_snapshot_ref=snapshot_ref(snapshot),
        image_view=ObjectImageView.for_object(obj),
        observation_binding_ref=plan.profile.observation_binding_ref,
        compiled_prompt_evidence_ref=_evidence_ref(prompt),
        contract_snapshot=InvocationContractSnapshot(
            configured_output_schema_ref=plan.contract.output_schema_ref,
            effective_question_refs=obj.effective_question_refs,
            compiler_version=VERSION,
            response_schema_sha256=plan.contract.response_schema_digest,
            response_schema_evidence_ref=_evidence_ref(schema),
        ),
        joint_questions=tuple(
            JointQuestionInput(
                question_ref=question.ref,
                prompt_template_ref=question.definition.prompt_template_ref,
                primary_fact_key=question.definition.primary_fact_key,
                primary_fact_schema_ref=question.definition.primary_fact_schema_ref,
                context_view=(),
            )
            for question in plan.questions
        ),
        **({} if retry is None else {"retry": retry}),
    )
    inputs.check_configuration(
        obj.profile_ref,
        plan.profile,
        selection,
        tuple((question.ref, question.definition) for question in plan.questions),
    )
    return inputs


def _evidence_ref(evidence: EvidenceAsset) -> EvidenceRef:
    return EvidenceRef(record_kind=K.EVIDENCE, record_id=evidence.evidence_id)


def _check_evidence(
    scope: VerifiedExecutionScope,
    obj: ObjectRecord,
    plan: ObservationPlan,
    evidence: tuple[EvidenceAsset, ...],
) -> None:
    policy = scope.resolve(plan.evidence_policy_ref, EvidencePolicyArtifact).policy
    project_id = scope.task.idempotency_scope.project_id
    access = (
        TaskEvidenceAccess(
            scope="task", project_id=project_id, task_id=scope.task.task_id
        )
        if policy.access_scope == "task"
        else ProjectEvidenceAccess(scope="project", project_id=project_id)
    )
    expected_scope = ObjectEvidenceScope(
        scope="object",
        execution_id=obj.execution_id,
        profile_ref=obj.profile_ref,
        object_ref=ObjectRef(record_kind=K.OBJECT, record_id=obj.object_id),
    )
    for record, mime, data in zip(
        evidence,
        ("text/plain", "application/schema+json"),
        (plan.prompt.encode("utf-8"), plan.schema),
        strict=True,
    ):
        if (
            record.evidence_type is not EvidenceType.MODEL_REQUEST
            or record.scope != expected_scope
            or record.access_scope != access
            or record.retention.evidence_policy_ref != plan.evidence_policy_ref
            or record.retention.retention_class != policy.retention_class
            or not isinstance(record.stage, FinalizedAsset)
            or not isinstance(record.spatial, NonSpatialEvidence)
            or record.sha256 != sha256_digest(data)
            or record.byte_size != len(data)
            or record.mime_type != mime
        ):
            raise AdapterRejected("perception.request_evidence_mismatch")


def verify_observation_inputs(
    scope: VerifiedExecutionScope,
    inputs: ObservationInput,
    snapshot: ContextSnapshot,
    selection: QuestionSelectionRecord,
    evidence: tuple[EvidenceAsset, ...],
    *,
    retry_witness: ObservationRetryWitness | None = None,
) -> None:
    """Reconstruct the exact admitted prompt/schema and verify their metadata."""
    obj = inputs.object_record
    retry, _ = _trusted_retry(inputs.envelope, obj, retry_witness)
    plan = _plan(scope, obj, snapshot, selection)
    expected = _request_inputs(
        scope, inputs.envelope, obj, snapshot, selection, plan, evidence, retry
    )
    if expected != inputs:
        raise AdapterRejected("perception.observation_input_pin_mismatch")


def _typed_question_payload(
    definition: TypeDefinition,
    raw: object,
    policy: QuestionValidationPolicy,
) -> dict[str, object]:
    """Wrap each raw JSON field using the admitted kind, preserving exact numbers."""
    if isinstance(raw, str) and (
        policy.max_string_chars is not None and len(raw) > policy.max_string_chars
    ):
        raise ValueError("question string exceeds its configured limit")
    if isinstance(definition, RecordTypeDefinition):
        field_types = {field.name: field.type_definition for field in definition.fields}
        if not isinstance(raw, dict) or not raw.keys() <= field_types.keys():
            raise ValueError("question record requires declared fields")
        raw = {
            name: _typed_question_payload(field_types[name], value, policy)
            for name, value in raw.items()
        }
    elif isinstance(definition, ListTypeDefinition):
        if not isinstance(raw, list) or (
            policy.max_items is not None and len(raw) > policy.max_items
        ):
            raise ValueError("question list exceeds its configured shape or limit")
        raw = [
            _typed_question_payload(definition.item_type, item, policy) for item in raw
        ]
    return {"kind": definition.kind, "value": raw}


def _registry_payload(payload: object) -> object:
    """Convert schema-checked JSON numbers to the registry's lossless float format."""
    if isinstance(payload, dict):
        if payload.get("kind") == "number" and type(payload.get("value")) in (
            int,
            Decimal,
        ):
            value = payload["value"]
            try:
                converted = float(value)
            except OverflowError:
                raise ValueError(
                    "question number exceeds the supported range"
                ) from None
            if Decimal(str(converted)) != value:
                raise ValueError("question number exceeds the supported precision")
            return {"kind": "number", "value": converted}
        return {name: _registry_payload(value) for name, value in payload.items()}
    if isinstance(payload, list):
        return [_registry_payload(value) for value in payload]
    return payload


def _question_result(
    env: NodeInputEnvelope,
    obj: ObjectRecord,
    plan: ObservationPlan,
    question: QuestionPlan,
    batch_ref: ObservationBatchRef,
    invocation: ModelInvocation,
    value: object,
    failed_slot: str | None,
) -> QuestionResult:
    ref, declaration = question.ref, question.declaration
    fields = {
        "schema_version": VERSION,
        "fact_id": runtime_id(env, "fact", obj.object_id, declaration.declaration_id),
        "scope": FactScope.OBJECT,
        "execution_id": obj.execution_id,
        "profile_ref": obj.profile_ref,
        "object_id": obj.object_id,
        "fact_key": declaration.fact_key,
        "fact_schema_ref": declaration.fact_schema_ref,
        "producer_catalogue_ref": plan.catalogue_ref,
        "producer_declaration_id": declaration.declaration_id,
        "producer_kind": declaration.producer_kind,
        "producer_id": declaration.producer_id,
        "producer_version": declaration.producer_version,
        "attempt_id": env.attempt_id,
        "source": QuestionFactSource(
            source_kind=ProducerKind.VLM_QUESTION,
            question_ref=ref,
            observation_batch_ref=batch_ref,
            model_invocation_ref=invocation_ref(invocation),
        ),
        "evidence_refs": (),
        "created_at": invocation.completed_at,
        "trace_id": env.trace.trace_id,
    }
    validation: dict[str, object] = {"status": "validated"}
    if failed_slot is None:
        if value == INSUFFICIENT_VISIBILITY:
            fields.update(
                status=FactStatus(question.unknown.insufficient_visibility),
                reason={
                    "reason_code": INSUFFICIENT_VISIBILITY,
                    "message": "The answer cannot be determined from visible image content.",
                    "diagnostic_refs": (),
                },
            )
        else:
            try:
                typed = _typed_question_payload(
                    question.fact_schema.type_definition,
                    value,
                    question.validation.policy,
                )
                question.fact_payload.validate(typed)
                question.response_payload.validate(typed)
                fields.update(
                    status=FactStatus.KNOWN,
                    value=plan.fact_registry.validate_payload(
                        declaration.fact_schema_ref, _registry_payload(typed)
                    ),
                )
            except ValueError:
                failed_slot = "perception.question_invalid"
    if failed_slot is not None:
        diagnostic = issue(
            failed_slot,
            "Question slot is missing or does not satisfy its declared contract.",
            (ref.artifact_id,),
        )
        validation = {"status": "invalid", "issues": (diagnostic,)}
        fields.update(
            status=FactStatus.ERROR,
            reason={
                "reason_code": failed_slot,
                "message": diagnostic.message,
                "diagnostic_refs": (),
                "diagnostics": (
                    {
                        "code": diagnostic.code,
                        "message": diagnostic.message,
                        "path": diagnostic.path,
                    },
                ),
            },
        )
    return QuestionResult.model_validate(
        {
            "question_ref": ref,
            "primary_fact_key": declaration.fact_key,
            "primary_fact_schema_ref": declaration.fact_schema_ref,
            "primary_fact": fields,
            "validation": validation,
        }
    )


def _slots(
    plan: ObservationPlan, output: bytes
) -> tuple[dict[str, object], tuple[str, ...], tuple[str, ...]]:
    if type(output) is not bytes or not 1 <= len(output) <= plan.output_limit:
        raise AdapterRejected("perception.invalid_joint_response_size")
    try:
        raw = strict_json(output, exact_numbers=True)
        invalid, missing = plan.contract.validate_response_content(
            output.decode("utf-8"), raw
        )
        if not isinstance(raw, dict):
            raise ValueError
    except ValueError:
        raise AdapterRejected("perception.untrusted_joint_response") from None
    return cast(dict[str, object], raw), invalid, missing


def _project_results(
    plan: ObservationPlan,
    inputs: ObservationInput,
    batch_ref: ObservationBatchRef,
    invocation: ModelInvocation,
    raw: dict[str, object],
    invalid: tuple[str, ...],
    missing: tuple[str, ...],
) -> tuple[QuestionResult, ...]:
    return tuple(
        _question_result(
            inputs.envelope,
            inputs.object_record,
            plan,
            question,
            batch_ref,
            invocation,
            raw.get(question.ref.artifact_id),
            "perception.question_missing"
            if question.ref.artifact_id in missing
            else "perception.question_invalid"
            if question.ref.artifact_id in invalid
            else None,
        )
        for question in plan.questions
    )


def _trusted_retry(
    envelope: NodeInputEnvelope,
    obj: ObjectRecord,
    witness: ObservationRetryWitness | None,
) -> tuple[ObservationRetry | None, ObservationBatch | None]:
    if witness is None:
        return None, None
    message, attempt, node = witness.message, witness.attempt, witness.node
    prior, request = message.batch, message.inputs
    before = request.envelope
    if (
        node.node_id != envelope.node_id
        or node.node_run_id != envelope.node_run_id
        or node.execution_id != envelope.execution_id
        or node.trace != envelope.trace
        or not node.attempt_history
        or node.attempt_history[-1].attempt_ref.record_id != envelope.attempt_id
        or not any(
            link.attempt_ref.record_id == attempt.attempt_id
            and link.attempt_number == attempt.attempt_number
            for link in node.attempt_history[:-1]
        )
        or not attempt.belongs_to_node(node)
        or attempt.attempt_number >= node.attempt_history[-1].attempt_number
        or attempt.status is not AttemptStatus.FAILED
        or message.authority is not None
        or not isinstance(message.output, FailedNodeOutput)
        or not message.output.error.retryable
        or message.output.started_at != attempt.started_at
        or message.output.attempt_id != attempt.attempt_id
        or message.output.node_run_id != node.node_run_id
        or message.invocation.model_invocation_id
        != prior.model_invocation_ref.record_id
        or message.invocation.attempt_ref != prior.attempt_ref
        or message.invocation.object_ref != prior.object_ref
        or prior.outcome in {"complete", "partial"}
        or prior.arrival_qualification != "on_time"
        or (
            before.execution_id,
            before.node_run_id,
            before.attempt_id,
            before.trace,
            before.release_id,
            before.bundle_digest,
        )
        != (
            envelope.execution_id,
            envelope.node_run_id,
            attempt.attempt_id,
            envelope.trace,
            envelope.release_id,
            envelope.bundle_digest,
        )
        or before.upstream_artifacts != envelope.upstream_artifacts
        or request.object_record != obj
        or (
            prior.execution_id,
            prior.node_run_ref.record_id,
            prior.attempt_ref.record_id,
            prior.object_ref.record_id,
            prior.profile_ref,
            prior.question_selection_ref,
            prior.effective_question_refs,
        )
        != (
            obj.execution_id,
            envelope.node_run_id,
            attempt.attempt_id,
            obj.object_id,
            obj.profile_ref,
            obj.question_selection_ref,
            obj.effective_question_refs,
        )
    ):
        raise AdapterRejected("perception.foreign_or_nonretryable_observation")
    return ObservationRetry(
        previous_batch_ref=prior.batch_ref, reason=message.output.error.code
    ), prior


type BatchOutcome = Literal[
    "transport_error", "parse_error", "association_error", "complete", "partial"
]
type SlotReceipt = tuple[tuple[str, ...], tuple[str, ...]]


def _receipt_slots(
    plan: ObservationPlan,
    normalized: bytes,
    slot_receipt: SlotReceipt | None,
) -> tuple[dict[str, object], tuple[str, ...], tuple[str, ...]]:
    raw, invalid, missing = _slots(plan, normalized)
    if slot_receipt is not None and (invalid, missing) != slot_receipt:
        raise AdapterRejected("perception.joint_slot_receipt_mismatch")
    return raw, invalid, missing


def _failed_batch(
    invocation: ModelInvocation,
    failure: InvocationError | None,
    diagnostic: str | None = None,
) -> tuple[BatchOutcome, NodeError]:
    actual = invocation.outcome
    if actual.status != "succeeded":
        if failure is not None and failure != actual.error or diagnostic is not None:
            raise AdapterRejected("perception.observation_failure_pin_mismatch")
        failure = actual.error
    elif failure is None and diagnostic is None:
        raise AdapterRejected("perception.observation_failure_unverified")
    if diagnostic is not None:
        kind: BatchOutcome = (
            "association_error"
            if diagnostic == "perception.joint_slot_receipt_mismatch"
            else "parse_error"
        )
    elif failure is not None and (
        isinstance(actual, FailedInvocation) or actual.status == "succeeded"
    ):
        kind = (
            "association_error"
            if failure.error_class == "association"
            else "parse_error"
            if failure.error_class == "validation"
            else "transport_error"
        )
    else:
        kind = "transport_error"
    error = model_failure(failure, diagnostic or "perception.observation_unavailable")
    if kind in {"parse_error", "association_error"}:
        error = error.model_copy(update={"retryable": True})
    return kind, error


def verify_observation_result(
    scope: VerifiedExecutionScope,
    message: ObservationMessage,
    snapshot: ContextSnapshot,
    selection: QuestionSelectionRecord,
    normalized_content: bytes | None,
    *,
    retry_witness: ObservationRetryWitness | None = None,
    failure: InvocationError | None = None,
    slot_receipt: SlotReceipt | None = None,
) -> None:
    """Reproject a batch from the independently authenticated receipt and prior Attempt."""
    inputs, batch = message.inputs, message.batch
    obj = inputs.object_record
    retry, previous_batch = _trusted_retry(inputs.envelope, obj, retry_witness)
    plan = _plan(scope, obj, snapshot, selection)
    if inputs != _request_inputs(
        scope,
        inputs.envelope,
        obj,
        snapshot,
        selection,
        plan,
        message.evidence,
        retry,
    ):
        raise AdapterRejected("perception.observation_input_pin_mismatch")
    inputs.check_batch(batch, (message.invocation,), previous_batch)
    if batch.observation_batch_id != runtime_id(
        inputs.envelope, "batch", obj.object_id
    ):
        raise AdapterRejected("perception.observation_batch_identity_mismatch")
    if batch.outcome in {"complete", "partial"}:
        if failure is not None or normalized_content is None:
            raise AdapterRejected("perception.observation_wire_missing")
        raw, invalid, missing = _receipt_slots(plan, normalized_content, slot_receipt)
        expected = _project_results(
            plan, inputs, batch.batch_ref, message.invocation, raw, invalid, missing
        )
        if batch.question_results != expected:
            raise AdapterRejected("perception.observation_fact_pin_mismatch")
    else:
        diagnostic = None
        if normalized_content is not None:
            if failure is not None or message.invocation.outcome.status != "succeeded":
                raise AdapterRejected("perception.observation_failure_pin_mismatch")
            try:
                _receipt_slots(plan, normalized_content, slot_receipt)
            except AdapterRejected as exc:
                diagnostic = exc.code
            else:
                raise AdapterRejected("perception.observation_failure_unverified")
        outcome, error = _failed_batch(message.invocation, failure, diagnostic)
        if (
            batch.outcome != outcome
            or batch.question_results
            or batch.validation_issues != error.validation_issues
            or not isinstance(message.output, FailedNodeOutput)
            or message.output.error != error
        ):
            raise AdapterRejected("perception.observation_failure_pin_mismatch")


def observation_model_call(
    inputs: ObservationInput,
    prompt: str,
    image: VerifiedImage,
    resource_scope_id: str,
) -> ModelCall:
    """Rebuild the same request identity for dispatch and durable validation."""
    obj = inputs.object_record
    return ModelCall(
        binding_ref=gateway_binding(inputs.observation_binding_ref),
        purposes=(_PURPOSE,),
        profile_refs=(obj.profile_ref,),
        prompt=prompt,
        resource_scope_id=resource_scope_id,
        image=image,
        object_ref=ObjectRef(record_kind=K.OBJECT, record_id=obj.object_id),
        contract_snapshot=inputs.contract_snapshot,
        evidence_refs=(
            inputs.compiled_prompt_evidence_ref,
            inputs.contract_snapshot.response_schema_evidence_ref,
        ),
    )


async def observe(
    claimed: ClaimedNode,
    envelope: NodeInputEnvelope,
    gateway: CapabilityGateway,
    access: ModelAccess,
    stage: StageRequestEvidence,
) -> NodeResult:
    scope, records = claimed.opened.scope, claimed.previous
    obj = records.object_record(claimed.branch)
    discovery = records.discovery
    started = claimed.attempt.started_at
    if discovery is None or started is None:
        raise AdapterRejected("perception.observation_inputs_unavailable")
    (selection,) = tuple(
        item
        for item in discovery.selections
        if item.selection_id == obj.question_selection_ref.record_id
    )
    snapshot = records.snapshot()
    scope.check_call(envelope, envelope.node_id, started).check_upstream(
        artifact(K.OBJECT, obj.object_id, obj),
        artifact(K.QUESTION_SELECTION_RECORD, selection.selection_id, selection),
        snapshot_artifact(snapshot),
    )
    retry, previous_batch = _trusted_retry(envelope, obj, records.observation_retry)
    plan = _plan(scope, obj, snapshot, selection)
    image = await access.observation_image(scope, obj)
    request_evidence = (
        await stage(
            claimed,
            obj,
            "compiled_prompt",
            plan.prompt.encode("utf-8"),
            plan.evidence_policy_ref,
        ),
        await stage(
            claimed, obj, "response_schema", plan.schema, plan.evidence_policy_ref
        ),
    )
    inputs = _request_inputs(
        scope, envelope, obj, snapshot, selection, plan, request_evidence, retry
    )
    call = observation_model_call(inputs, plan.prompt, image, access.resource_scope_id)
    dispatched = await gateway.invoke_model(claimed, call)
    # Only a receipt already marked late by the journal can supply archival
    # bytes after the gateway removes the authority-bearing output.
    if dispatched.late and dispatched.output is None and dispatched.error is None:
        archived = dispatched.transport_result.parsed_content
        if archived is not None:
            dispatched = replace(dispatched, output=archived)
    invocation = associated_invocation(claimed, call, dispatched)
    if invocation is None:
        require_model_output(dispatched)
        raise AdapterRejected("perception.observation_invocation_missing")
    return _observation_result(
        claimed, inputs, plan, request_evidence, dispatched, invocation, previous_batch
    )


def _observation_result(
    claimed: ClaimedNode,
    inputs: ObservationInput,
    plan: ObservationPlan,
    request_evidence: tuple[EvidenceAsset, ...],
    dispatched: DispatchOutcome,
    invocation: ModelInvocation,
    previous_batch: ObservationBatch | None,
) -> NodeResult:
    obj, env = inputs.object_record, inputs.envelope
    batch_ref = ObservationBatchRef(
        record_kind=K.OBSERVATION_BATCH,
        record_id=runtime_id(env, "batch", obj.object_id),
    )
    inv_ref = invocation_ref(invocation)
    error = None
    results: tuple[QuestionResult, ...] = ()
    if dispatched.output is None:
        outcome, error = _failed_batch(invocation, dispatched.error)
    else:
        try:
            transport = dispatched.transport_result
            raw, invalid, missing = _receipt_slots(
                plan,
                dispatched.output,
                (
                    transport.invalid_question_slots,
                    transport.missing_question_slots,
                ),
            )
        except AdapterRejected as exc:
            outcome, error = _failed_batch(invocation, None, exc.code)
        else:
            results = _project_results(
                plan, inputs, batch_ref, invocation, raw, invalid, missing
            )
            outcome = (
                "partial"
                if any(item.validation.status == "invalid" for item in results)
                else "complete"
            )
    batch = ObservationBatch(
        schema_version=VERSION,
        observation_batch_id=batch_ref.record_id,
        execution_id=obj.execution_id,
        object_ref=ObjectRef(record_kind=K.OBJECT, record_id=obj.object_id),
        node_run_ref=invocation.node_run_ref,
        attempt_ref=invocation.attempt_ref,
        model_invocation_ref=inv_ref,
        outcome=outcome,
        arrival_qualification="on_time"
        if not dispatched.late and invocation.completed_at <= claimed.deadline
        else "late",
        envelope_validation="unresolved"
        if outcome == "transport_error"
        else "untrusted"
        if outcome == "parse_error"
        else "trusted",
        association_validation="untrusted"
        if outcome == "association_error"
        else "unresolved"
        if outcome in {"parse_error", "transport_error"}
        else "trusted",
        question_results=results,
        validation_issues=() if error is None else error.validation_issues,
        profile_ref=obj.profile_ref,
        question_selection_ref=obj.question_selection_ref,
        effective_question_refs=obj.effective_question_refs,
        **(
            {}
            if previous_batch is None
            else {"retry_of_batch_ref": previous_batch.batch_ref}
        ),
    )
    inputs.check_batch(batch, (invocation,), previous_batch)
    artifacts = (
        *(artifact(K.EVIDENCE, row.evidence_id, row) for row in request_evidence),
        artifact(K.MODEL_INVOCATION, invocation.model_invocation_id, invocation),
        artifact(K.OBSERVATION_BATCH, batch.observation_batch_id, batch),
        *(
            artifact(K.FACT, row.primary_fact.fact_id, row.primary_fact)
            for row in results
        ),
    )
    authority = (
        None
        if error is not None or batch.arrival_qualification != "on_time"
        else ObservationBatchSelection(
            schema_version=VERSION,
            execution_id=obj.execution_id,
            profile_ref=obj.profile_ref,
            scope=FactScope.OBJECT,
            object_id=obj.object_id,
            selection_group="observation_batch",
            selection_id=runtime_id(env, "observation-authority", obj.object_id),
            selected_attempt_id=env.attempt_id,
            selected_result_ref=batch.batch_ref,
            selected_fact_refs=tuple(
                FactRef(record_kind=K.FACT, record_id=row.primary_fact.fact_id)
                for row in results
            ),
            reason="validated_success"
            if previous_batch is None
            else "explicit_retry_resolution",
            authority_revision=1,
            actor_ref=_ACTOR,
            created_at=invocation.completed_at,
        )
    )
    output = node_output(claimed, artifacts, (invocation,), error)
    message = ObservationMessage(
        schema_version=VERSION,
        inputs=inputs,
        output=output,
        batch=batch,
        invocation=invocation,
        request_evidence=(),
        durable_request_evidence=request_evidence,
        authority=authority,
    )
    return NodeResult(
        message,
        artifacts
        if authority is None
        else (
            *artifacts,
            artifact(
                K.AUTHORITATIVE_RESULT_SELECTION, authority.selection_id, authority
            ),
        ),
        (inv_ref,),
    )
