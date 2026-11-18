"""Immutable candidate Fact data; no producer admission or authority selection."""

from typing import Annotated, Literal, Self

from pydantic import (
    BeforeValidator,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)
from pydantic.json_schema import JsonSchemaValue

from jimu_runtime.domain.base import ContractModel
from jimu_runtime.domain.definition_enums import FactScope, ProducerKind
from jimu_runtime.domain.definition_support import (
    AbsentOptional,
    Description,
    OptionalContract,
    UTCDateTime,
    immutable_sequence,
    omit_default_from_schema,
)
from jimu_runtime.domain.enums import ArtifactKind, FactStatus, RuntimeRecordKind
from jimu_runtime.domain.fact_selectors import (
    FactSchemaContentRef,
    ProducerCatalogueContentRef,
    QuestionContentRef,
    SystemProducerRequirement,
    ToolContentRef,
    ToolPlanContentRef,
)
from jimu_runtime.domain.primitives import ArtifactId, RecordId, Version
from jimu_runtime.domain.references import ContentRef, RuntimeRecordRef
from jimu_runtime.domain.validation.issues import IssuePath
from jimu_runtime.domain.values import FiniteNumber, Value

type RuntimeUTCDateTime = UTCDateTime


class ProfileContentRef(ContentRef):
    artifact_kind: Literal[ArtifactKind.PROFILE]


class PolicyContentRef(ContentRef):
    artifact_kind: Literal[ArtifactKind.POLICY]


class TypedRuntimeRef(RuntimeRecordRef):
    @field_validator("record_kind", mode="before")
    @classmethod
    def decode_record_kind(cls, value: object) -> RuntimeRecordKind:
        return RuntimeRecordKind._decode(value)


class EvidenceRef(TypedRuntimeRef):
    record_kind: Literal[RuntimeRecordKind.EVIDENCE]


class FactRef(TypedRuntimeRef):
    record_kind: Literal[RuntimeRecordKind.FACT]


class ObjectRef(TypedRuntimeRef):
    record_kind: Literal[RuntimeRecordKind.OBJECT]


class NodeRunRef(TypedRuntimeRef):
    record_kind: Literal[RuntimeRecordKind.NODE_RUN]


class AttemptRef(TypedRuntimeRef):
    record_kind: Literal[RuntimeRecordKind.ATTEMPT]


class ObservationBatchRef(TypedRuntimeRef):
    record_kind: Literal[RuntimeRecordKind.OBSERVATION_BATCH]


class ModelInvocationRef(TypedRuntimeRef):
    record_kind: Literal[RuntimeRecordKind.MODEL_INVOCATION]


class ToolExecutionRef(TypedRuntimeRef):
    record_kind: Literal[RuntimeRecordKind.TOOL_EXECUTION]


class ExecutionEventRef(TypedRuntimeRef):
    record_kind: Literal[RuntimeRecordKind.EXECUTION_EVENT]


class QuestionSelectionRef(TypedRuntimeRef):
    record_kind: Literal[RuntimeRecordKind.QUESTION_SELECTION_RECORD]


class AuthoritySelectionRef(TypedRuntimeRef):
    record_kind: Literal[RuntimeRecordKind.AUTHORITATIVE_RESULT_SELECTION]


class ApplicabilityBasisRef(TypedRuntimeRef):
    record_kind: Literal[RuntimeRecordKind.EVIDENCE, RuntimeRecordKind.CONTEXT_SNAPSHOT]


def require_unique_runtime_refs(refs: tuple[RuntimeRecordRef, ...]) -> None:
    if len({(ref.record_kind, ref.record_id) for ref in refs}) != len(refs):
        raise ValueError("runtime references must be unique")


type EvidenceRefs = Annotated[
    tuple[EvidenceRef, ...],
    Field(max_length=1024, json_schema_extra={"uniqueItems": True}),
    BeforeValidator(immutable_sequence),
]
type FactRefs = Annotated[
    tuple[FactRef, ...],
    Field(max_length=1024, json_schema_extra={"uniqueItems": True}),
    BeforeValidator(immutable_sequence),
]


def scope_constraints() -> list[JsonSchemaValue]:
    return [
        {
            "if": {"properties": {"scope": {"const": "object"}}, "required": ["scope"]},
            "then": {"required": ["object_id", "profile_ref"]},
        },
        {
            "if": {
                "properties": {"scope": {"const": "execution"}},
                "required": ["scope"],
            },
            "then": {"properties": {"object_id": False, "profile_ref": False}},
        },
    ]


def _scope_schema(schema: JsonSchemaValue) -> None:
    schema["allOf"] = scope_constraints()


class ScopedRecord(OptionalContract):
    """Flat scope shape shared by roots and finite descriptors; not a new record kind."""

    model_config = ConfigDict(json_schema_extra=_scope_schema)
    execution_id: RecordId
    scope: FactScope
    object_id: AbsentOptional[RecordId] = Field(
        default=None,
        validate_default=False,
        exclude_if=lambda value: value is None,
        json_schema_extra=omit_default_from_schema,
    )
    profile_ref: AbsentOptional[ProfileContentRef] = Field(
        default=None,
        validate_default=False,
        exclude_if=lambda value: value is None,
        json_schema_extra=omit_default_from_schema,
    )

    @model_validator(mode="before")
    @classmethod
    def decode_scope(cls, raw: object) -> object:
        if isinstance(raw, dict) and "scope" in raw:
            return {**raw, "scope": FactScope._decode(raw["scope"])}
        return raw

    @model_validator(mode="after")
    def validate_scope(self) -> Self:
        if self.scope is FactScope.OBJECT:
            if self.object_id is None or self.profile_ref is None:
                raise ValueError("object scope requires object_id and profile_ref")
        elif self.object_id is not None or self.profile_ref is not None:
            raise ValueError("execution scope forbids object_id and profile_ref")
        return self


class FactDiagnostic(ContractModel):
    code: ArtifactId
    path: IssuePath
    message: Description


class FactReason(ContractModel):
    reason_code: ArtifactId
    message: Description
    diagnostic_refs: EvidenceRefs

    @model_validator(mode="after")
    def unique_diagnostics(self) -> Self:
        require_unique_runtime_refs(self.diagnostic_refs)
        return self


class FactErrorReason(FactReason):
    diagnostics: Annotated[
        tuple[FactDiagnostic, ...],
        Field(min_length=1, max_length=64),
        BeforeValidator(immutable_sequence),
    ]


class FactNotApplicableReason(FactReason):
    basis_refs: Annotated[
        tuple[PolicyContentRef | ApplicabilityBasisRef, ...],
        Field(min_length=1, max_length=64, json_schema_extra={"uniqueItems": True}),
        BeforeValidator(immutable_sequence),
    ]

    @model_validator(mode="after")
    def unique_basis(self) -> Self:
        keys: list[tuple[object, ...]] = []
        for ref in self.basis_refs:
            key: tuple[object, ...]
            if isinstance(ref, ContentRef):
                key = (ref.artifact_kind, ref.artifact_id, ref.version)
            else:
                key = (ref.record_kind, ref.record_id)
            keys.append(key)
        if len(set(keys)) != len(keys):
            raise ValueError("basis references must be unique")
        return self


def _decode_source(raw: object) -> object:
    if isinstance(raw, dict) and "source_kind" in raw:
        return {**raw, "source_kind": ProducerKind._decode(raw["source_kind"])}
    return raw


class _FactSource(ContractModel):
    @model_validator(mode="before")
    @classmethod
    def decode_kind(cls, raw: object) -> object:
        return _decode_source(raw)


class QuestionFactSource(_FactSource):
    source_kind: Literal[ProducerKind.VLM_QUESTION]
    question_ref: QuestionContentRef
    observation_batch_ref: ObservationBatchRef
    model_invocation_ref: ModelInvocationRef


class ToolFactSource(_FactSource):
    source_kind: Literal[ProducerKind.TOOL]
    tool_execution_ref: ToolExecutionRef
    tool_plan_ref: ToolPlanContentRef
    step_id: ArtifactId
    tool_ref: ToolContentRef
    output_name: ArtifactId


class SystemFactSource(_FactSource):
    source_kind: Literal[ProducerKind.SYSTEM]
    requirement: SystemProducerRequirement
    system_output_ref: ExecutionEventRef
    output_name: ArtifactId


type FactSource = Annotated[
    QuestionFactSource | ToolFactSource | SystemFactSource,
    Field(discriminator="source_kind"),
    BeforeValidator(_decode_source),
]


def _decode_fact(raw: object) -> object:
    if isinstance(raw, dict) and "status" in raw:
        return {**raw, "status": FactStatus._decode(raw["status"])}
    return raw


def _fact_schema(schema: JsonSchemaValue) -> None:
    constraints = scope_constraints()
    constraints.append(
        {
            "if": {
                "properties": {"scope": {"const": "execution"}},
                "required": ["scope"],
            },
            "then": {"properties": {"producer_kind": {"const": "system"}}},
        }
    )
    for kind in ("vlm_question", "tool", "system"):
        constraints.append(
            {
                "if": {
                    "properties": {"producer_kind": {"const": kind}},
                    "required": ["producer_kind"],
                },
                "then": {
                    "properties": {
                        "source": {"properties": {"source_kind": {"const": kind}}}
                    }
                },
            }
        )
    schema["allOf"] = constraints


class _FactBase(ScopedRecord):
    model_config = ConfigDict(json_schema_extra=_fact_schema)

    schema_version: Literal["1.0.0"]
    fact_id: RecordId
    fact_key: ArtifactId
    fact_schema_ref: FactSchemaContentRef
    producer_catalogue_ref: ProducerCatalogueContentRef
    producer_declaration_id: ArtifactId
    producer_kind: ProducerKind
    producer_id: ArtifactId
    producer_version: Version
    attempt_id: RecordId
    source: FactSource
    evidence_refs: EvidenceRefs
    created_at: RuntimeUTCDateTime
    trace_id: RecordId
    confidence: AbsentOptional[FiniteNumber] = Field(
        default=None,
        validate_default=False,
        exclude_if=lambda value: value is None,
        json_schema_extra=omit_default_from_schema,
    )

    @model_validator(mode="before")
    @classmethod
    def decode_status(cls, raw: object) -> object:
        return _decode_fact(raw)

    @model_validator(mode="after")
    def local_producer_identity(self) -> Self:
        require_unique_runtime_refs(self.evidence_refs)
        if self.source.source_kind is not self.producer_kind:
            raise ValueError("source_kind must equal producer_kind")
        if (
            self.scope is FactScope.EXECUTION
            and self.producer_kind is not ProducerKind.SYSTEM
        ):
            raise ValueError("execution Facts require a system producer")
        if isinstance(self.source, QuestionFactSource):
            identity = (
                self.source.question_ref.artifact_id,
                self.source.question_ref.version,
            )
        elif isinstance(self.source, ToolFactSource):
            identity = (self.source.tool_ref.artifact_id, self.source.tool_ref.version)
        else:
            identity = (
                self.source.requirement.capability_id,
                self.source.requirement.contract_version,
            )
        if (self.producer_id, self.producer_version) != identity:
            raise ValueError(
                "producer ID/version must equal source definition identity"
            )
        return self


class KnownFact(_FactBase):
    status: Literal[FactStatus.KNOWN]
    value: Value


class UnknownFact(_FactBase):
    status: Literal[FactStatus.UNKNOWN]
    reason: FactReason


class ErrorFact(_FactBase):
    status: Literal[FactStatus.ERROR]
    reason: FactErrorReason


class NotApplicableFact(_FactBase):
    status: Literal[FactStatus.NOT_APPLICABLE]
    reason: FactNotApplicableReason


class NotEvaluatedFact(_FactBase):
    status: Literal[FactStatus.NOT_EVALUATED]
    reason: FactReason


type FactRecord = Annotated[
    KnownFact | UnknownFact | ErrorFact | NotApplicableFact | NotEvaluatedFact,
    Field(discriminator="status"),
    BeforeValidator(_decode_fact),
]
