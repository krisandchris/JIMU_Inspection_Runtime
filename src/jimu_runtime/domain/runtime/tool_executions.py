"""Immutable Tool invocation audit with separate grounding and step origins."""

from typing import Annotated, Literal, Self

from pydantic import (
    BeforeValidator,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)

from jimu_runtime.domain.base import ContractModel
from jimu_runtime.domain.definition_enums import FactScope
from jimu_runtime.domain.definition_support import (
    AbsentOptional,
    Description,
    NonNegativeBudget,
    OptionalContract,
    UTCDateTime,
    immutable_sequence,
    omit_default_from_schema,
)
from jimu_runtime.domain.definitions.capabilities import ToolGroundingBinding
from jimu_runtime.domain.enums import FactStatus, RuntimeRecordKind
from jimu_runtime.domain.fact_selectors import ToolContentRef, ToolPlanContentRef
from jimu_runtime.domain.primitives import ArtifactId, Digest, RecordId, Version
from jimu_runtime.domain.runtime.facts import (
    AttemptRef,
    EvidenceRefs,
    FactReason,
    FactRefs,
    NodeRunRef,
    ObjectRef,
    ProfileContentRef,
    TypedRuntimeRef,
    require_unique_runtime_refs,
)
from jimu_runtime.domain.validation.authority_inputs import (
    EmittedOutput,
    MappedOutputResult,
)
from jimu_runtime.domain.validation.issues import ValidationIssue
from jimu_runtime.domain.values import FieldName, Value

type ToolInvocationOriginKind = Literal["grounding_binding", "tool_plan_step"]
type ImplementationVersionResolution = Literal[
    "resolved", "unavailable", "not_dispatched"
]


class GroundingBindingOrigin(ContractModel):
    kind: Literal["grounding_binding"]
    node_id: Literal["N03"]
    grounding_binding: ToolGroundingBinding


class ToolPlanStepOrigin(ContractModel):
    kind: Literal["tool_plan_step"]
    node_id: Literal["N05"]
    tool_plan_ref: ToolPlanContentRef
    step_id: ArtifactId
    producer_declaration_ids: Annotated[
        tuple[ArtifactId, ...],
        Field(max_length=64, json_schema_extra={"uniqueItems": True}),
        BeforeValidator(immutable_sequence),
    ]

    @model_validator(mode="after")
    def unique_producers(self) -> Self:
        if len(set(self.producer_declaration_ids)) != len(
            self.producer_declaration_ids
        ):
            raise ValueError("producer declaration IDs must be unique")
        return self


type ToolInvocationOrigin = Annotated[
    GroundingBindingOrigin | ToolPlanStepOrigin, Field(discriminator="kind")
]


class ToolArtifactRecordRef(TypedRuntimeRef):
    record_kind: Literal[
        RuntimeRecordKind.CONTEXT_SNAPSHOT,
        RuntimeRecordKind.EVIDENCE,
        RuntimeRecordKind.FACT,
        RuntimeRecordKind.MODEL_INVOCATION,
        RuntimeRecordKind.OBJECT,
        RuntimeRecordKind.TOOL_EXECUTION,
    ]


class ToolArtifactRef(ContractModel):
    artifact_ref: ToolArtifactRecordRef
    digest: Digest


type ToolArtifactRefs = Annotated[
    tuple[ToolArtifactRef, ...],
    Field(max_length=1024, json_schema_extra={"uniqueItems": True}),
    BeforeValidator(immutable_sequence),
]


class ToolError(ContractModel):
    error_class: Literal[
        "transport",
        "timeout",
        "rate_limit",
        "resource_exhausted",
        "validation",
        "association",
        "unavailable",
        "cancelled",
        "internal",
    ]
    code: ArtifactId
    diagnostic_refs: EvidenceRefs

    @model_validator(mode="after")
    def unique_diagnostics(self) -> Self:
        require_unique_runtime_refs(self.diagnostic_refs)
        return self


class ToolResourceUsage(OptionalContract):
    cpu_time_ms: AbsentOptional[NonNegativeBudget] = Field(
        default=None,
        validate_default=False,
        exclude_if=lambda value: value is None,
        json_schema_extra=omit_default_from_schema,
    )
    peak_memory_bytes: AbsentOptional[NonNegativeBudget] = Field(
        default=None,
        validate_default=False,
        exclude_if=lambda value: value is None,
        json_schema_extra=omit_default_from_schema,
    )
    peak_gpu_memory_bytes: AbsentOptional[NonNegativeBudget] = Field(
        default=None,
        validate_default=False,
        exclude_if=lambda value: value is None,
        json_schema_extra=omit_default_from_schema,
    )

    @model_validator(mode="after")
    def measured_counter_required(self) -> Self:
        if all(
            value is None
            for value in (
                self.cpu_time_ms,
                self.peak_memory_bytes,
                self.peak_gpu_memory_bytes,
            )
        ):
            raise ValueError("resource usage requires at least one measured counter")
        return self


def _decode_tool_output_state(raw: object) -> object:
    if isinstance(raw, dict) and "fact_status" in raw:
        return {**raw, "fact_status": FactStatus._decode(raw["fact_status"])}
    return raw


class _ToolOutputState(ContractModel):
    output_name: ArtifactId
    evidence_refs: EvidenceRefs

    @model_validator(mode="before")
    @classmethod
    def decode_status(cls, raw: object) -> object:
        return _decode_tool_output_state(raw)

    @model_validator(mode="after")
    def unique_evidence(self) -> Self:
        require_unique_runtime_refs(self.evidence_refs)
        return self


class KnownToolOutputState(_ToolOutputState):
    fact_status: Literal[FactStatus.KNOWN]


class NonKnownToolOutputState(_ToolOutputState):
    fact_status: Literal[
        FactStatus.UNKNOWN,
        FactStatus.ERROR,
        FactStatus.NOT_APPLICABLE,
        FactStatus.NOT_EVALUATED,
    ]
    reason: FactReason


type ToolOutputState = Annotated[
    KnownToolOutputState | NonKnownToolOutputState,
    Field(discriminator="fact_status"),
    BeforeValidator(_decode_tool_output_state),
]


class ToolResultEvidence(ContractModel):
    name: FieldName
    evidence_refs: Annotated[EvidenceRefs, Field(min_length=1)]

    @model_validator(mode="after")
    def unique_evidence(self) -> Self:
        require_unique_runtime_refs(self.evidence_refs)
        return self


class _ToolResult(ContractModel):
    diagnostics: Annotated[
        tuple[ValidationIssue, ...],
        Field(max_length=1024),
        BeforeValidator(immutable_sequence),
    ]
    evidence_refs: EvidenceRefs
    evidence: Annotated[
        tuple[ToolResultEvidence, ...],
        Field(max_length=64),
        BeforeValidator(immutable_sequence),
    ]
    output_states: Annotated[
        tuple[ToolOutputState, ...],
        Field(max_length=64),
        BeforeValidator(immutable_sequence),
    ]

    @model_validator(mode="after")
    def unique_results(self) -> Self:
        names = {item.output_name for item in self.output_states}
        if len(names) != len(self.output_states):
            raise ValueError("output states require unique names")
        if len({slot.name for slot in self.evidence}) != len(self.evidence):
            raise ValueError("evidence slots require unique names")
        require_unique_runtime_refs(self.evidence_refs)
        if any(
            not set(ref.stable_json() for ref in refs)
            <= {ref.stable_json() for ref in self.evidence_refs}
            for refs in (
                *(slot.evidence_refs for slot in self.evidence),
                *(state.evidence_refs for state in self.output_states),
            )
        ):
            raise ValueError(
                "named Tool result evidence must reference envelope evidence"
            )
        return self


class SucceededToolResult(_ToolResult, OptionalContract):
    status: Literal["succeeded"]
    payload: AbsentOptional[Value] = Field(
        default=None,
        validate_default=False,
        exclude_if=lambda value: value is None,
        json_schema_extra=omit_default_from_schema,
    )

    @model_validator(mode="after")
    def known_output_requires_payload(self) -> Self:
        if self.payload is None and any(
            isinstance(item, KnownToolOutputState) for item in self.output_states
        ):
            raise ValueError("known output requires an available payload")
        return self


class PartialToolResult(_ToolResult, OptionalContract):
    status: Literal["partial"]
    payload: AbsentOptional[Value] = Field(
        default=None,
        validate_default=False,
        exclude_if=lambda value: value is None,
        json_schema_extra=omit_default_from_schema,
    )

    @model_validator(mode="after")
    def partial_output_provenance(self) -> Self:
        if not self.diagnostics and not any(
            isinstance(item, NonKnownToolOutputState) for item in self.output_states
        ):
            raise ValueError(
                "partial Tool result requires a diagnostic or non-known output"
            )
        if self.payload is None and any(
            isinstance(item, KnownToolOutputState) for item in self.output_states
        ):
            raise ValueError("known output requires an available payload")
        return self


class FailedToolResult(_ToolResult):
    status: Literal["failed"]
    error: ToolError

    @model_validator(mode="after")
    def no_known_output(self) -> Self:
        if any(isinstance(item, KnownToolOutputState) for item in self.output_states):
            raise ValueError("failed Tool result cannot claim known named output")
        return self


type ToolResult = Annotated[
    SucceededToolResult | PartialToolResult | FailedToolResult,
    Field(discriminator="status"),
]


class ReturnedToolAttempt(ContractModel):
    disposition: Literal["returned"]
    result: ToolResult


class FailedToolAttempt(ContractModel):
    disposition: Literal["failed"]
    error: ToolError


class UndispatchedToolAttempt(ContractModel):
    disposition: Literal["not_dispatched"]
    error: ToolError


type ToolAttemptResult = Annotated[
    ReturnedToolAttempt | FailedToolAttempt | UndispatchedToolAttempt,
    Field(discriminator="disposition"),
]


class ToolAuthorityObservation(ContractModel):
    """Qualification and mapped outputs observed for a returned Tool Plan step."""

    arrival_qualification: Literal["on_time", "late", "unresolved"]
    envelope_validation: Literal["trusted", "untrusted", "unresolved"]
    association_validation: Literal["trusted", "untrusted", "unresolved"]
    mapped_outputs: Annotated[
        tuple[MappedOutputResult, ...],
        Field(max_length=64),
        BeforeValidator(immutable_sequence),
    ]

    @model_validator(mode="after")
    def unique_mapped_outputs(self) -> Self:
        if len({value.output_name for value in self.mapped_outputs}) != len(
            self.mapped_outputs
        ):
            raise ValueError("authority evidence requires unique output names")
        require_unique_runtime_refs(
            tuple(
                value.fact_ref
                for value in self.mapped_outputs
                if isinstance(value, EmittedOutput)
            )
        )
        return self


class ToolExecution(OptionalContract):
    model_config = ConfigDict(
        json_schema_extra={
            "allOf": [
                {
                    "if": {
                        "properties": {
                            "invocation_origin": {
                                "properties": {"kind": {"const": "grounding_binding"}}
                            }
                        },
                        "required": ["invocation_origin"],
                    },
                    "then": {
                        "properties": {
                            "scope": {"const": "execution"},
                            "object_ref": False,
                            "generated_fact_refs": {"maxItems": 0},
                        }
                    },
                    "else": {
                        "properties": {"scope": {"const": "object"}},
                        "required": ["object_ref"],
                    },
                },
                {
                    "if": {
                        "properties": {
                            "implementation_version_resolution": {"const": "resolved"}
                        },
                        "required": ["implementation_version_resolution"],
                    },
                    "then": {"required": ["actual_implementation_version"]},
                    "else": {"properties": {"actual_implementation_version": False}},
                },
                {
                    "if": {
                        "properties": {
                            "implementation_version_resolution": {
                                "const": "unavailable"
                            }
                        },
                        "required": ["implementation_version_resolution"],
                    },
                    "then": {"required": ["implementation_version_unavailable_reason"]},
                    "else": {
                        "properties": {
                            "implementation_version_unavailable_reason": False
                        }
                    },
                },
            ],
        }
    )
    schema_version: Literal["1.0.0"]
    tool_execution_id: RecordId
    execution_id: RecordId
    profile_ref: ProfileContentRef
    tool_ref: ToolContentRef
    node_run_ref: NodeRunRef
    attempt_ref: AttemptRef
    invocation_origin: ToolInvocationOrigin
    scope: FactScope
    object_ref: AbsentOptional[ObjectRef] = Field(
        default=None,
        validate_default=False,
        exclude_if=lambda value: value is None,
        json_schema_extra=omit_default_from_schema,
    )
    input_refs: ToolArtifactRefs
    output_refs: ToolArtifactRefs
    implementation_version_resolution: ImplementationVersionResolution
    actual_implementation_version: AbsentOptional[Version] = Field(
        default=None,
        validate_default=False,
        exclude_if=lambda value: value is None,
        json_schema_extra=omit_default_from_schema,
    )
    implementation_version_unavailable_reason: AbsentOptional[Description] = Field(
        default=None,
        validate_default=False,
        exclude_if=lambda value: value is None,
        json_schema_extra=omit_default_from_schema,
    )
    status: Literal["succeeded", "partial", "failed", "not_dispatched"]
    attempt_result: ToolAttemptResult
    started_at: UTCDateTime
    completed_at: UTCDateTime
    resource_usage: AbsentOptional[ToolResourceUsage] = Field(
        default=None,
        validate_default=False,
        exclude_if=lambda value: value is None,
        json_schema_extra=omit_default_from_schema,
    )
    authority_observation: AbsentOptional[ToolAuthorityObservation] = Field(
        default=None,
        validate_default=False,
        exclude_if=lambda value: value is None,
        json_schema_extra=omit_default_from_schema,
    )
    generated_fact_refs: FactRefs
    evidence_refs: EvidenceRefs
    trace_id: RecordId

    @field_validator("scope", mode="before")
    @classmethod
    def decode_scope(cls, raw: object) -> FactScope:
        return FactScope._decode(raw)

    @model_validator(mode="after")
    def origin_scope(self) -> Self:
        origin = self.invocation_origin
        if isinstance(origin, GroundingBindingOrigin):
            if (
                self.scope is not FactScope.EXECUTION
                or self.object_ref is not None
                or self.generated_fact_refs
            ):
                raise ValueError(
                    "N03 grounding is execution scoped and cannot emit Tool Facts"
                )
            if origin.grounding_binding.tool_ref != self.tool_ref:
                raise ValueError(
                    "N03 binding and invocation must use the same exact Tool reference"
                )
        elif self.scope is not FactScope.OBJECT or self.object_ref is None:
            raise ValueError(
                "N05 Tool Plan step requires an Object scope and reference"
            )
        elif self.generated_fact_refs and not origin.producer_declaration_ids:
            raise ValueError("generated Tool Facts require producer declarations")
        return self

    @model_validator(mode="after")
    def implementation_resolution(self) -> Self:
        resolution = self.implementation_version_resolution
        if (self.actual_implementation_version is not None) != (
            resolution == "resolved"
        ):
            raise ValueError(
                "resolved implementation version requires the actual version"
            )
        if (self.implementation_version_unavailable_reason is not None) != (
            resolution == "unavailable"
        ):
            raise ValueError(
                "only unavailable implementations require an unavailability reason"
            )
        if (resolution == "not_dispatched") != isinstance(
            self.attempt_result, UndispatchedToolAttempt
        ):
            raise ValueError(
                "implementation resolution must agree with dispatch evidence"
            )
        if self.generated_fact_refs and resolution != "resolved":
            raise ValueError(
                "Tool Fact generation requires the actual implementation version"
            )
        return self

    @model_validator(mode="after")
    def matching_attempt_status(self) -> Self:
        result = self.attempt_result
        status = (
            result.result.status
            if isinstance(result, ReturnedToolAttempt)
            else result.disposition
        )
        if self.status != status:
            raise ValueError("Tool execution status and Attempt result must agree")
        if self.completed_at < self.started_at:
            raise ValueError("Tool completion cannot predate its start")
        if isinstance(result, UndispatchedToolAttempt) and (
            self.resource_usage is not None or self.output_refs
        ):
            raise ValueError("undispatched Tool cannot have run usage or outputs")
        if isinstance(result, FailedToolAttempt) and self.output_refs:
            raise ValueError("failed transport cannot claim returned Tool outputs")
        if self.generated_fact_refs and not isinstance(result, ReturnedToolAttempt):
            raise ValueError("reported Tool Facts require a normalized Tool result")
        for refs in (
            self.generated_fact_refs,
            self.evidence_refs,
            tuple(item.artifact_ref for item in self.input_refs),
            tuple(item.artifact_ref for item in self.output_refs),
        ):
            require_unique_runtime_refs(refs)
        if isinstance(result, ReturnedToolAttempt) and not {
            ref.stable_json() for ref in result.result.evidence_refs
        } <= {ref.stable_json() for ref in self.evidence_refs}:
            raise ValueError(
                "Tool result evidence must be retained in its execution audit"
            )
        return self

    @model_validator(mode="after")
    def authority_matches_attempt(self) -> Self:
        observation = self.authority_observation
        if observation is None:
            return self
        attempt = self.attempt_result
        if not isinstance(self.invocation_origin, ToolPlanStepOrigin) or (
            not isinstance(attempt, ReturnedToolAttempt)
            or self.implementation_version_resolution != "resolved"
        ):
            raise ValueError(
                "authority observation requires a returned N05 step with resolved implementation"
            )
        states = {
            item.output_name: item.fact_status for item in attempt.result.output_states
        }
        emitted = tuple(
            item
            for item in observation.mapped_outputs
            if isinstance(item, EmittedOutput)
        )
        if {item.fact_ref.record_id for item in emitted} != {
            ref.record_id for ref in self.generated_fact_refs
        } or any(
            states.get(item.output_name) is not item.fact_status for item in emitted
        ):
            raise ValueError(
                "authority observation must retain exact emitted Fact references and output states"
            )
        return self
