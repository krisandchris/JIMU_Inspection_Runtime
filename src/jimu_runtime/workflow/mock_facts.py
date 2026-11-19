"""Offline joint observation and bound geometry-tool fixture for parking."""

from dataclasses import dataclass
from datetime import datetime

from pydantic import ValidationError

from jimu_runtime.bundle.artifacts import DRAFT_2020_12, SchemaResource
from jimu_runtime.bundle.integrity import sha256_digest
from jimu_runtime.domain.definition_enums import (
    EvidenceType,
    FactScope,
    ModelCapability,
    ProducerKind,
    ToolProviderKind,
)
from jimu_runtime.domain.definition_support import reference_identity
from jimu_runtime.domain.definitions.capabilities import (
    ProfileDefinition,
    QuestionDefinition,
)
from jimu_runtime.domain.definitions.models import ModelBinding
from jimu_runtime.domain.definitions.prompts import PromptTemplateDefinition
from jimu_runtime.domain.definitions.tools import ToolDefinition, ToolPlanDefinition
from jimu_runtime.domain.enums import ArtifactKind, FactStatus
from jimu_runtime.domain.fact_schemas.canonicalization import JSONValue, canonical_bytes
from jimu_runtime.domain.fact_schemas.models import FactSchemaDefinition
from jimu_runtime.domain.fact_selectors import ProducerCatalogueContentRef
from jimu_runtime.domain.nodes.envelopes import (
    NodeInputEnvelope,
    NodeOutputEnvelope,
    check_envelope_pair,
)
from jimu_runtime.domain.nodes.observation import (
    JointQuestionInput,
    ObjectImageView,
    ObservationInput,
)
from jimu_runtime.domain.nodes.tool_execution import (
    InvokedToolStep,
    ToolNodeInput,
    ToolNodeOutput,
    ToolPlanRunSummary,
)
from jimu_runtime.domain.policies import EvidencePolicyArtifact, ToolSkipPolicyArtifact
from jimu_runtime.domain.producers import (
    FactProducerDeclaration,
    ProducerCatalogue,
    QuestionProducer,
    ToolStepProducer,
)
from jimu_runtime.domain.runtime.authority import ObservationBatchSelection
from jimu_runtime.domain.runtime.evidence import (
    EvidenceAsset,
    NonSpatialEvidence,
    ObjectEvidenceScope,
    RetentionPolicyRef,
    TaskEvidenceAccess,
)
from jimu_runtime.domain.runtime.executions import NodeId
from jimu_runtime.domain.runtime.facts import (
    EvidenceRef,
    FactRecord,
    FactRef,
    KnownFact,
    ModelInvocationRef,
    ObjectRef,
    ObservationBatchRef,
    PolicyContentRef,
    QuestionFactSource,
    ToolExecutionRef,
    ToolFactSource,
)
from jimu_runtime.domain.runtime.intake import ContextSnapshot
from jimu_runtime.domain.runtime.invocations import (
    InvocationContractSnapshot,
    ModelInvocation,
)
from jimu_runtime.domain.runtime.media import FinalizedAsset, StorageReference
from jimu_runtime.domain.runtime.observations import ObservationBatch, QuestionResult
from jimu_runtime.domain.runtime.perception import ObjectRecord
from jimu_runtime.domain.runtime.selection import QuestionSelectionRecord
from jimu_runtime.domain.runtime.tool_executions import (
    FailedToolAttempt,
    KnownToolOutputState,
    ReturnedToolAttempt,
    SucceededToolResult,
    ToolArtifactRecordRef,
    ToolArtifactRef,
    ToolAuthorityObservation,
    ToolError,
    ToolExecution,
    ToolPlanStepOrigin,
)
from jimu_runtime.domain.schema_types import ToolResultSchemaDescriptor
from jimu_runtime.domain.tool_mappings import ImageInput
from jimu_runtime.domain.validation.authority_inputs import EmittedOutput
from jimu_runtime.domain.validation.schema_descriptors import resolve_descriptor
from jimu_runtime.domain.values import BooleanValue, Value, validate_typed_value
from jimu_runtime.workflow.mock_context import snapshot_artifact, snapshot_ref
from jimu_runtime.workflow.mock_perception import original_coordinate_system
from jimu_runtime.workflow.mock_support import (
    VERSION,
    K,
    SyntheticArtifact,
    SyntheticCall,
    SyntheticExecution,
    VerifiedExecutionScope,
    artifact,
    record_digest,
    synthetic_issue,
)


@dataclass(frozen=True, slots=True)
class SyntheticEvidence:
    record: EvidenceAsset
    content: bytes

    @property
    def ref(self) -> EvidenceRef:
        return EvidenceRef(record_kind=K.EVIDENCE, record_id=self.record.evidence_id)

    @property
    def artifact(self) -> SyntheticArtifact:
        return artifact(K.EVIDENCE, self.ref.record_id, self.record)


@dataclass(frozen=True, slots=True)
class SyntheticObservationResult:
    envelope: NodeOutputEnvelope
    batch: ObservationBatch
    invocation: ModelInvocation
    evidence: tuple[SyntheticEvidence, ...]
    artifacts: tuple[SyntheticArtifact, ...]


@dataclass(frozen=True, slots=True)
class SyntheticToolResult:
    output: ToolNodeOutput
    executions: tuple[ToolExecution, ...]
    facts: tuple[FactRecord, ...]
    artifacts: tuple[SyntheticArtifact, ...]


def object_artifact(obj: ObjectRecord) -> SyntheticArtifact:
    return artifact(K.OBJECT, obj.object_id, obj)


def check_object(scope: VerifiedExecutionScope, obj: ObjectRecord) -> ProfileDefinition:
    profile = scope.resolve(obj.profile_ref, ProfileDefinition)
    if (
        obj.execution_id != scope.execution_id
        or obj.source_media_ref != scope.media.media_ref
        or obj.coordinate_system != original_coordinate_system(scope)
        or obj.entity_ref != profile.entity_ref
    ):
        raise ValueError(
            "synthetic Object must preserve execution, Profile, image and source frame"
        )
    return profile


def _evidence(
    scope: VerifiedExecutionScope,
    call: SyntheticCall,
    obj: ObjectRecord,
    part: str,
    content: bytes,
    mime: str,
    policy_ref: PolicyContentRef,
) -> SyntheticEvidence:
    evidence_id = call.id(part, obj.object_id)
    digest = sha256_digest(content)
    policy = scope.resolve(policy_ref, EvidencePolicyArtifact).policy
    return SyntheticEvidence(
        EvidenceAsset(
            schema_version=VERSION,
            evidence_id=evidence_id,
            evidence_type=EvidenceType.MODEL_REQUEST,
            scope=ObjectEvidenceScope(
                scope="object",
                execution_id=scope.execution_id,
                object_ref=ObjectRef(record_kind=K.OBJECT, record_id=obj.object_id),
                profile_ref=obj.profile_ref,
            ),
            storage=StorageReference(
                store_id="fixture.memory", object_key=f"mock/{evidence_id}"
            ),
            sha256=digest,
            mime_type=mime,
            byte_size=len(content),
            stage=FinalizedAsset(
                stage="finalized",
                staged_at=call.at,
                verified_at=call.at,
                verified_sha256=digest,
                object_version=VERSION,
            ),
            spatial=NonSpatialEvidence(kind="non_spatial"),
            producer_id="fixture.parking",
            producer_version=VERSION,
            access_scope=TaskEvidenceAccess(
                scope="task",
                project_id=scope.task.idempotency_scope.project_id,
                task_id=scope.task.task_id,
            ),
            retention=RetentionPolicyRef(
                evidence_policy_ref=policy_ref, retention_class=policy.retention_class
            ),
            created_at=call.at,
        ),
        content,
    )


def _joint_schema(
    scope: VerifiedExecutionScope, questions: tuple[QuestionDefinition, ...]
) -> bytes:
    properties: dict[str, JSONValue] = {}
    for question in questions:
        definition = scope.resolve(
            question.primary_fact_schema_ref, FactSchemaDefinition
        )
        child = scope.catalogue.resolve(definition.payload_schema_ref).value
        if not isinstance(child, SchemaResource) or question.question_id in properties:
            raise ValueError(
                "joint fixture schema needs unique Questions and pinned value schemas"
            )
        properties[question.question_id] = {
            key: value
            for key, value in child.document.items()
            if key not in {"$schema", "$id"}
        }
    return canonical_bytes(
        {
            "$schema": DRAFT_2020_12,
            "type": "object",
            "properties": properties,
            "required": list(properties),
            "additionalProperties": False,
        }
    )


def prepare_observation(
    scope: VerifiedExecutionScope,
    envelope: NodeInputEnvelope,
    obj: ObjectRecord,
    snapshot: ContextSnapshot,
    at: datetime,
) -> tuple[ObservationInput, tuple[SyntheticEvidence, ...]]:
    call = scope.check_call(envelope, NodeId.N04, at)
    scope.check_snapshot(snapshot, snapshot_ref(snapshot))
    profile = check_object(scope, obj)
    call.check_upstream(object_artifact(obj), snapshot_artifact(snapshot))
    binding = scope.resolve(profile.observation_binding_ref, ModelBinding)
    schema = scope.catalogue.resolve(binding.output_schema_ref)
    if not isinstance(schema.value, SchemaResource):
        raise ValueError("observation model output requires its pinned schema")
    questions = tuple(
        scope.resolve(ref, QuestionDefinition) for ref in obj.effective_question_refs
    )
    joint_questions = tuple(
        JointQuestionInput(
            question_ref=ref,
            prompt_template_ref=question.prompt_template_ref,
            primary_fact_key=question.primary_fact_key,
            primary_fact_schema_ref=question.primary_fact_schema_ref,
            context_view=(),
        )
        for ref, question in zip(obj.effective_question_refs, questions, strict=True)
    )
    prompts = tuple(
        scope.resolve(question.prompt_template_ref, PromptTemplateDefinition)
        for question in questions
    )
    if any(prompt.variables for prompt in prompts) or any(
        question.context_allowlist for question in questions
    ):
        raise ValueError(
            "parking fixture supports literal Question prompts without external context substitution"
        )
    policy_ref = PolicyContentRef.model_validate(
        questions[0].evidence_policy_ref.model_dump()
    )
    prompt = _evidence(
        scope,
        call,
        obj,
        "joint-prompt",
        "\n".join(item.template_text for item in prompts).encode(),
        "text/plain",
        policy_ref,
    )
    response_schema = _evidence(
        scope,
        call,
        obj,
        "joint-schema",
        _joint_schema(scope, questions),
        "application/schema+json",
        policy_ref,
    )
    return ObservationInput(
        schema_version=VERSION,
        envelope=envelope,
        object_record=obj,
        context_snapshot_ref=snapshot_ref(snapshot),
        image_view=ObjectImageView.for_object(obj),
        observation_binding_ref=profile.observation_binding_ref,
        compiled_prompt_evidence_ref=prompt.ref,
        contract_snapshot=InvocationContractSnapshot(
            configured_output_schema_ref=binding.output_schema_ref,
            effective_question_refs=obj.effective_question_refs,
            compiler_version=VERSION,
            response_schema_sha256=response_schema.record.sha256,
            response_schema_evidence_ref=response_schema.ref,
        ),
        joint_questions=joint_questions,
    ), (prompt, response_schema)


def producer_catalogue(
    scope: SyntheticExecution,
) -> tuple[ProducerCatalogueContentRef, ProducerCatalogue]:
    (member,) = scope.catalogue.of_kind(ArtifactKind.FACT_PRODUCER_CATALOG)
    ref = ProducerCatalogueContentRef.model_validate(member.ref.model_dump())
    return ref, scope.resolve(ref, ProducerCatalogue)


def _fact_fields(
    scope: SyntheticExecution,
    call: SyntheticCall,
    obj: ObjectRecord,
    declaration: FactProducerDeclaration,
    catalogue_ref: ProducerCatalogueContentRef,
    source: QuestionFactSource | ToolFactSource,
) -> dict[str, object]:
    return {
        "schema_version": VERSION,
        "fact_id": call.id("fact", obj.object_id, declaration.declaration_id),
        "scope": FactScope.OBJECT,
        "execution_id": scope.execution_id,
        "profile_ref": obj.profile_ref,
        "object_id": obj.object_id,
        "fact_key": declaration.fact_key,
        "fact_schema_ref": declaration.fact_schema_ref,
        "producer_catalogue_ref": catalogue_ref,
        "producer_declaration_id": declaration.declaration_id,
        "producer_kind": declaration.producer_kind,
        "producer_id": declaration.producer_id,
        "producer_version": declaration.producer_version,
        "attempt_id": call.envelope.attempt_id,
        "source": source,
        "evidence_refs": (),
        "created_at": call.at,
        "trace_id": call.envelope.trace.trace_id,
    }


def observe(
    scope: SyntheticExecution,
    inputs: ObservationInput,
    snapshot: ContextSnapshot,
    selection: QuestionSelectionRecord,
    at: datetime,
    *,
    readings: tuple[Value, ...] | None = None,
    previous: ObservationBatch | None = None,
) -> SyntheticObservationResult:
    inputs = ObservationInput.model_validate(inputs)
    call = scope.check_call(inputs.envelope, NodeId.N04, at)
    scope.check_snapshot(snapshot, inputs.context_snapshot_ref)
    expected, evidence = prepare_observation(
        scope, inputs.envelope, inputs.object_record, snapshot, at
    )
    if expected.model_dump(exclude={"retry"}) != inputs.model_dump(exclude={"retry"}):
        raise ValueError(
            "joint observation must consume the exact fixture prompt, Schema and Object image view"
        )
    obj = inputs.object_record
    profile = scope.resolve(obj.profile_ref, ProfileDefinition)
    questions = tuple(
        (ref, scope.resolve(ref, QuestionDefinition))
        for ref in obj.effective_question_refs
    )
    inputs.check_configuration(obj.profile_ref, profile, selection, questions)
    call.check_upstream(
        artifact(K.QUESTION_SELECTION_RECORD, selection.selection_id, selection)
    )
    if selection.context_snapshot_digest != record_digest(snapshot):
        raise ValueError(
            "joint observation must inherit the exact selection Snapshot digest"
        )
    if previous is not None:
        call.check_upstream(
            artifact(K.OBSERVATION_BATCH, previous.observation_batch_id, previous)
        )
    raw = (
        readings
        if readings is not None
        else tuple(BooleanValue(kind="bool", value=True) for _ in questions)
    )
    if len(raw) != len(questions):
        raise ValueError(
            "fixture observation responses must match the exact joint Question order"
        )
    obj_ref = ObjectRef(record_kind=K.OBJECT, record_id=obj.object_id)
    invocation = scope.model_invocation(
        call,
        "N04",
        ModelCapability.OBJECT_OBSERVATION,
        inputs.observation_binding_ref,
        (obj.profile_ref,),
        record_digest(inputs),
        sha256_digest(
            canonical_bytes(
                {
                    ref.artifact_id: value.model_dump(mode="json")
                    for (ref, _), value in zip(questions, raw, strict=True)
                }
            )
        ),
        object_ref=obj_ref,
        contract=inputs.contract_snapshot,
        evidence_refs=tuple(item.ref for item in evidence),
    )
    batch_ref = ObservationBatchRef(
        record_kind=K.OBSERVATION_BATCH, record_id=call.id("batch", obj.object_id)
    )
    inv_ref = ModelInvocationRef(
        record_kind=K.MODEL_INVOCATION, record_id=invocation.model_invocation_id
    )
    catalogue_ref, catalogue = producer_catalogue(scope)
    results: list[QuestionResult] = []
    for (question_ref, question), value in zip(questions, raw, strict=True):
        (declaration,) = tuple(
            item
            for item in catalogue.declarations
            if item.profile_ref is not None
            and reference_identity(item.profile_ref)
            == reference_identity(obj.profile_ref)
            and isinstance(item.producer, QuestionProducer)
            and item.producer.question_ref == question_ref
        )
        if (declaration.fact_schema_ref, declaration.fact_key) != (
            question.primary_fact_schema_ref,
            question.primary_fact_key,
        ):
            raise ValueError(
                "synthetic Question must retain its exact primary Fact declaration"
            )
        fields = _fact_fields(
            scope,
            call,
            obj,
            declaration,
            catalogue_ref,
            QuestionFactSource(
                source_kind=ProducerKind.VLM_QUESTION,
                question_ref=question_ref,
                observation_batch_ref=batch_ref,
                model_invocation_ref=inv_ref,
            ),
        )
        definition = scope.resolve(declaration.fact_schema_ref, FactSchemaDefinition)
        try:
            validated = validate_typed_value(definition.type_definition, value)
        except ValidationError:
            issue = synthetic_issue(
                "synthetic-question-invalid",
                "Synthetic answer failed its independent Fact schema",
                (question_ref.artifact_id,),
            )
            fact = {
                **fields,
                "status": FactStatus.ERROR,
                "reason": {
                    "reason_code": issue.code,
                    "message": issue.message,
                    "diagnostic_refs": (),
                    "diagnostics": (
                        {
                            "code": issue.code,
                            "message": issue.message,
                            "path": issue.path,
                        },
                    ),
                },
            }
            validation = {"status": "invalid", "issues": (issue,)}
        else:
            fact = {**fields, "status": FactStatus.KNOWN, "value": validated}
            validation = {"status": "validated"}
        results.append(
            QuestionResult.model_validate(
                {
                    "question_ref": question_ref,
                    "primary_fact_key": question.primary_fact_key,
                    "primary_fact_schema_ref": question.primary_fact_schema_ref,
                    "primary_fact": fact,
                    "validation": validation,
                }
            )
        )
    batch = ObservationBatch.model_validate(
        {
            "schema_version": VERSION,
            "profile_ref": obj.profile_ref,
            "question_selection_ref": obj.question_selection_ref,
            "effective_question_refs": obj.effective_question_refs,
            "observation_batch_id": batch_ref.record_id,
            "execution_id": scope.execution_id,
            "object_ref": obj_ref,
            "node_run_ref": call.node_run_ref,
            "attempt_ref": call.attempt_ref,
            "model_invocation_ref": inv_ref,
            "outcome": "partial"
            if any(item.validation.status == "invalid" for item in results)
            else "complete",
            "envelope_validation": "trusted",
            "association_validation": "trusted",
            "arrival_qualification": "on_time",
            "question_results": tuple(results),
            "validation_issues": (),
            **({} if previous is None else {"retry_of_batch_ref": previous.batch_ref}),
        }
    )
    artifacts = (
        *(item.artifact for item in evidence),
        artifact(K.MODEL_INVOCATION, invocation.model_invocation_id, invocation),
        artifact(K.OBSERVATION_BATCH, batch.observation_batch_id, batch),
        *(
            artifact(K.FACT, item.primary_fact.fact_id, item.primary_fact)
            for item in results
        ),
    )
    envelope = call.output(artifacts)
    check_envelope_pair(inputs.envelope, envelope)
    inputs.check_batch(batch, (invocation,), previous)
    return SyntheticObservationResult(envelope, batch, invocation, evidence, artifacts)


def run_tool_plan(
    scope: SyntheticExecution,
    inputs: ToolNodeInput,
    snapshot: ContextSnapshot,
    at: datetime,
    *,
    outside: bool | None = True,
    observation_facts: tuple[FactRecord, ...] = (),
    observation_authority: ObservationBatchSelection | None = None,
) -> SyntheticToolResult:
    inputs = ToolNodeInput.model_validate(inputs)
    call = scope.check_call(inputs.envelope, NodeId.N05, at)
    scope.check_snapshot(snapshot, inputs.context_snapshot_ref)
    obj = inputs.object_record
    profile = check_object(scope, obj)
    obj_artifact = object_artifact(obj)
    call.check_upstream(obj_artifact, snapshot_artifact(snapshot))
    plan = scope.resolve(inputs.tool_plan_ref, ToolPlanDefinition)
    inputs.check_plan(profile, plan)
    inputs.check_observation_facts(plan, observation_facts, observation_authority)
    if observation_authority is not None:
        call.check_upstream(
            artifact(
                K.AUTHORITATIVE_RESULT_SELECTION,
                observation_authority.selection_id,
                observation_authority,
            )
        )
    call.check_upstream(
        *(artifact(K.FACT, fact.fact_id, fact) for fact in observation_facts)
    )
    (step,) = plan.steps
    tool = scope.resolve(step.tool_ref, ToolDefinition)
    (mapping,) = step.fact_output_mappings
    (declared_output,) = tuple(
        item for item in tool.fact_outputs if item.output_name == mapping.output_name
    )
    (version,) = tuple(
        item.implementation_version
        for item in scope.pins.tool_pins
        if item.tool_ref == step.tool_ref
    )
    skip = scope.resolve(step.skip_policy_ref, ToolSkipPolicyArtifact).policy
    if (
        tool.provider_kind is not ToolProviderKind.BUILTIN
        or not tool.implementation_requirement.capability_id.startswith("fixture.")
        or tool.model_binding_ref is not None
        or step.depends_on
        or step.requirement != "required"
        or len(step.input_bindings) != 1
        or not isinstance(step.input_bindings[0].source, ImageInput)
        or skip.condition_ast is not True
        or declared_output.value_path
        or version
        not in tool.implementation_requirement.allowed_implementation_versions
    ):
        raise ValueError(
            "fixture geometry requires an allowlisted single Tool step over the original image"
        )
    catalogue_ref, catalogue = producer_catalogue(scope)
    (declaration,) = tuple(
        item
        for item in catalogue.declarations
        if item.profile_ref is not None
        and reference_identity(item.profile_ref) == reference_identity(obj.profile_ref)
        and isinstance(item.producer, ToolStepProducer)
        and (
            item.producer.tool_plan_ref,
            item.producer.tool_ref,
            item.producer.step_id,
            item.output_name,
        )
        == (inputs.tool_plan_ref, step.tool_ref, step.step_id, mapping.output_name)
    )
    if (declaration.fact_schema_ref, declaration.fact_key) != (
        declared_output.fact_schema_ref,
        mapping.target_fact_key,
    ):
        raise ValueError(
            "fixture Tool output must preserve the declared Fact schema and mapping"
        )
    tool_ref = ToolExecutionRef(
        record_kind=K.TOOL_EXECUTION,
        record_id=call.id("tool", obj.object_id, step.step_id),
    )
    object_ref = ObjectRef(record_kind=K.OBJECT, record_id=obj.object_id)
    failure = None
    facts: tuple[FactRecord, ...] = ()
    fact_refs: tuple[FactRef, ...] = ()
    authority: ToolAuthorityObservation | None = None
    attempt: FailedToolAttempt | ReturnedToolAttempt
    if outside is None:
        failure = synthetic_issue(
            "synthetic-tool-failed", "Synthetic geometry could not return a result"
        )
        attempt = FailedToolAttempt(
            disposition="failed",
            error=ToolError(
                error_class="validation", code=failure.code, diagnostic_refs=()
            ),
        )
    else:
        tool_result_schema = resolve_descriptor(
            tool.output_schema_ref, scope.schema_descriptors
        )
        if (
            not isinstance(tool_result_schema, ToolResultSchemaDescriptor)
            or len(tool_result_schema.output_declarations) != 1
            or tool_result_schema.output_declarations[0].output_name
            != mapping.output_name
            or tool_result_schema.output_declarations[0].value_path
            != declared_output.value_path
            or not tool_result_schema.output_declarations[0].required
            or any(item.required for item in tool_result_schema.evidence_fields)
        ):
            raise ValueError(
                "fixture geometry requires exactly one pinned payload output without required named evidence"
            )
        candidate = BooleanValue(kind="bool", value=outside)
        validate_typed_value(tool_result_schema.payload_type, candidate)
        schema = scope.resolve(declaration.fact_schema_ref, FactSchemaDefinition)
        value = validate_typed_value(schema.type_definition, candidate)
        fact = KnownFact.model_validate(
            {
                **_fact_fields(
                    scope,
                    call,
                    obj,
                    declaration,
                    catalogue_ref,
                    ToolFactSource(
                        source_kind=ProducerKind.TOOL,
                        tool_execution_ref=tool_ref,
                        tool_plan_ref=inputs.tool_plan_ref,
                        step_id=step.step_id,
                        tool_ref=step.tool_ref,
                        output_name=mapping.output_name,
                    ),
                ),
                "status": FactStatus.KNOWN,
                "value": value,
            }
        )
        facts = (fact,)
        fact_refs = (FactRef(record_kind=K.FACT, record_id=fact.fact_id),)
        attempt = ReturnedToolAttempt(
            disposition="returned",
            result=SucceededToolResult(
                status="succeeded",
                diagnostics=(),
                evidence_refs=(),
                evidence=(),
                payload=value,
                output_states=(
                    KnownToolOutputState(
                        output_name=mapping.output_name,
                        fact_status=FactStatus.KNOWN,
                        evidence_refs=(),
                    ),
                ),
            ),
        )
        authority = ToolAuthorityObservation(
            envelope_validation="trusted",
            association_validation="trusted",
            arrival_qualification="on_time",
            mapped_outputs=(
                EmittedOutput(
                    disposition="emitted",
                    output_name=mapping.output_name,
                    fact_status=FactStatus.KNOWN,
                    fact_ref=fact_refs[0],
                ),
            ),
        )
    execution = ToolExecution.model_validate(
        {
            "schema_version": VERSION,
            "tool_execution_id": tool_ref.record_id,
            "execution_id": scope.execution_id,
            "profile_ref": obj.profile_ref,
            "tool_ref": step.tool_ref,
            "node_run_ref": call.node_run_ref,
            "attempt_ref": call.attempt_ref,
            "invocation_origin": ToolPlanStepOrigin(
                kind="tool_plan_step",
                node_id="N05",
                tool_plan_ref=inputs.tool_plan_ref,
                step_id=step.step_id,
                producer_declaration_ids=(declaration.declaration_id,),
            ),
            "scope": FactScope.OBJECT,
            "object_ref": object_ref,
            "input_refs": (
                ToolArtifactRef(
                    artifact_ref=ToolArtifactRecordRef(
                        record_kind=K.OBJECT, record_id=obj.object_id
                    ),
                    digest=obj_artifact.pin.digest,
                ),
            ),
            "output_refs": tuple(
                ToolArtifactRef(
                    artifact_ref=ToolArtifactRecordRef(
                        record_kind=K.FACT, record_id=fact.fact_id
                    ),
                    digest=record_digest(fact),
                )
                for fact in facts
            ),
            "implementation_version_resolution": "resolved",
            "actual_implementation_version": version,
            "status": "failed" if failure else "succeeded",
            "attempt_result": attempt,
            "started_at": at,
            "completed_at": at,
            **({} if authority is None else {"authority_observation": authority}),
            "generated_fact_refs": fact_refs,
            "evidence_refs": (),
            "trace_id": inputs.envelope.trace.trace_id,
        }
    )
    artifacts = (
        artifact(K.TOOL_EXECUTION, execution.tool_execution_id, execution),
        *(artifact(K.FACT, fact.fact_id, fact) for fact in facts),
    )
    output = ToolNodeOutput(
        schema_version=VERSION,
        envelope=call.output(artifacts, failure),
        summary=ToolPlanRunSummary(
            tool_plan_ref=inputs.tool_plan_ref,
            object_ref=object_ref,
            status="failed" if failure else "succeeded",
            steps=(
                InvokedToolStep(
                    disposition="invoked",
                    step_id=step.step_id,
                    tool_execution_ref=tool_ref,
                ),
            ),
            validation_issues=() if failure is None else (failure,),
        ),
        tool_fact_refs=fact_refs,
        evidence_refs=(),
    )
    output.check_steps(inputs, profile, plan, (execution,), facts)
    return SyntheticToolResult(output, (execution,), facts, artifacts)
