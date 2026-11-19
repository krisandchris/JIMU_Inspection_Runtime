"""Immutable Model Gateway audit data for a single invocation."""

from typing import Annotated, Literal, Self

from pydantic import BeforeValidator, ConfigDict, Field, model_validator

from jimu_runtime.domain.base import ContractModel
from jimu_runtime.domain.definition_enums import ModelCapability
from jimu_runtime.domain.definition_support import (
    AbsentOptional,
    Description,
    NonNegativeBudget,
    OptionalContract,
    ReviewReportRef,
    ShortText,
    UTCDateTime,
    immutable_sequence,
    omit_default_from_schema,
    require_unique_references,
)
from jimu_runtime.domain.definitions.models import (
    ModelBindingContentRef,
    PolicyContentRef,
)
from jimu_runtime.domain.fact_selectors import QuestionContentRef
from jimu_runtime.domain.policies import TriggerError
from jimu_runtime.domain.primitives import ArtifactId, Digest, RecordId, Version
from jimu_runtime.domain.references import SchemaRef
from jimu_runtime.domain.runtime.facts import (
    AttemptRef,
    EvidenceRef,
    EvidenceRefs,
    ModelInvocationRef,
    NodeRunRef,
    ObjectRef,
    ProfileContentRef,
    require_unique_runtime_refs,
)

type ProviderKind = Literal["remote", "local"]
type ProviderRoutingMode = Literal["remote_primary", "local_primary_exception"]
type RouteKind = Literal["primary", "fallback"]
type ProviderVersionResolution = Literal["reported", "registry_verified", "unavailable"]
type InvocationPurpose = ModelCapability | Literal["optional_question_proposal"]
type InvocationNodeId = Literal["N02", "N03", "N04", "N05", "N06"]

_PURPOSE_NODE = {
    ModelCapability.PROFILE_DISCOVERY: "N02",
    "optional_question_proposal": "N02",
    ModelCapability.OBJECT_GROUNDING: "N03",
    ModelCapability.OBJECT_OBSERVATION: "N04",
    ModelCapability.CANDIDATE_PREDICTION: "N06",
}


class InvocationIdentity(ContractModel):
    model_invocation_id: RecordId
    execution_id: RecordId
    node_id: InvocationNodeId
    node_run_ref: NodeRunRef
    attempt_ref: AttemptRef


class ConfiguredProviderIdentity(ContractModel):
    binding_ref: ModelBindingContentRef
    provider_policy_ref: PolicyContentRef
    primary_deployment_id: ArtifactId
    provider_id: ArtifactId
    provider_kind: ProviderKind
    logical_model_id: ArtifactId


class _ActualProviderIdentity(ContractModel):
    provider_id: ArtifactId
    deployment_id: ArtifactId
    provider_kind: ProviderKind
    model_id: ShortText


class _ResolvedProviderIdentity(_ActualProviderIdentity):
    deployment_version: Version
    model_version: Version


class ReportedProviderIdentity(_ResolvedProviderIdentity):
    version_resolution: Literal["reported"]
    version_evidence: RecordId


class RegistryVerifiedProviderIdentity(_ResolvedProviderIdentity):
    version_resolution: Literal["registry_verified"]
    version_evidence: ReviewReportRef


class UnavailableProviderIdentity(_ActualProviderIdentity):
    version_resolution: Literal["unavailable"]
    version_unavailable_reason: Description


type ActualProviderIdentity = Annotated[
    ReportedProviderIdentity
    | RegistryVerifiedProviderIdentity
    | UnavailableProviderIdentity,
    Field(discriminator="version_resolution"),
]


class LocalPrimaryExceptionIdentity(ContractModel):
    exception_id: ArtifactId
    version: Version
    approval_ref: ReviewReportRef


class _ProviderRoute(OptionalContract):
    model_config = ConfigDict(
        json_schema_extra={
            "if": {
                "properties": {"routing_mode": {"const": "local_primary_exception"}},
                "required": ["routing_mode"],
            },
            "then": {"required": ["local_primary_exception"]},
            "else": {"properties": {"local_primary_exception": False}},
        }
    )
    routing_mode: ProviderRoutingMode
    selected_deployment_id: ArtifactId
    fallback_policy_ref: PolicyContentRef
    local_primary_exception: AbsentOptional[LocalPrimaryExceptionIdentity] = Field(
        default=None,
        validate_default=False,
        exclude_if=lambda value: value is None,
        json_schema_extra=omit_default_from_schema,
    )

    @model_validator(mode="after")
    def exception_matches_mode(self) -> Self:
        if (self.local_primary_exception is not None) != (
            self.routing_mode == "local_primary_exception"
        ):
            raise ValueError(
                "local primary exception evidence required only in local mode"
            )
        return self


class PrimaryProviderRoute(_ProviderRoute):
    route_kind: Literal["primary"]


class FallbackProviderRoute(_ProviderRoute):
    route_kind: Literal["fallback"]
    from_deployment_id: ArtifactId
    to_deployment_id: ArtifactId
    trigger_error: TriggerError
    trigger_invocation_ref: ModelInvocationRef
    evaluation_refs: Annotated[
        tuple[ReviewReportRef, ...],
        Field(min_length=1, max_length=1024, json_schema_extra={"uniqueItems": True}),
        BeforeValidator(immutable_sequence),
    ]

    @model_validator(mode="after")
    def exact_transition(self) -> Self:
        if (
            self.from_deployment_id == self.to_deployment_id
            or self.to_deployment_id != self.selected_deployment_id
        ):
            raise ValueError("fallback must select a distinct, evaluated target")
        keys = {
            (ref.review_registry_id, ref.report_id, ref.version)
            for ref in self.evaluation_refs
        }
        if len(keys) != len(self.evaluation_refs):
            raise ValueError("duplicate evaluation report identity")
        return self


type ProviderRouteTrace = Annotated[
    PrimaryProviderRoute | FallbackProviderRoute, Field(discriminator="route_kind")
]


class UsageRecord(OptionalContract):
    """Reported counters; an absent counter preserves lack of measurement."""

    input_tokens: AbsentOptional[NonNegativeBudget] = Field(
        default=None,
        validate_default=False,
        exclude_if=lambda value: value is None,
        json_schema_extra=omit_default_from_schema,
    )
    output_tokens: AbsentOptional[NonNegativeBudget] = Field(
        default=None,
        validate_default=False,
        exclude_if=lambda value: value is None,
        json_schema_extra=omit_default_from_schema,
    )
    request_bytes: AbsentOptional[NonNegativeBudget] = Field(
        default=None,
        validate_default=False,
        exclude_if=lambda value: value is None,
        json_schema_extra=omit_default_from_schema,
    )
    response_bytes: AbsentOptional[NonNegativeBudget] = Field(
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
                self.input_tokens,
                self.output_tokens,
                self.request_bytes,
                self.response_bytes,
            )
        ):
            raise ValueError("usage must include at least one reported counter")
        return self


class InvocationError(ContractModel):
    error_class: (
        TriggerError
        | Literal[
            "validation", "association", "resource_exhausted", "cancelled", "internal"
        ]
    )
    code: ArtifactId
    diagnostic_refs: EvidenceRefs

    @model_validator(mode="after")
    def unique_diagnostics(self) -> Self:
        require_unique_runtime_refs(self.diagnostic_refs)
        return self


class SucceededInvocation(ContractModel):
    status: Literal["succeeded"]
    response_digest: Digest


class FailedInvocation(ContractModel):
    status: Literal["failed"]
    error: InvocationError


class UndispatchedInvocation(ContractModel):
    status: Literal["not_dispatched"]
    error: InvocationError


type InvocationOutcome = Annotated[
    SucceededInvocation | FailedInvocation | UndispatchedInvocation,
    Field(discriminator="status"),
]


class InvocationContractSnapshot(ContractModel):
    configured_output_schema_ref: SchemaRef
    effective_question_refs: Annotated[
        tuple[QuestionContentRef, ...],
        Field(min_length=1, max_length=1024, json_schema_extra={"uniqueItems": True}),
        BeforeValidator(immutable_sequence),
    ]
    compiler_version: Version
    response_schema_sha256: Digest
    response_schema_evidence_ref: EvidenceRef

    @model_validator(mode="after")
    def unique_effective_questions(self) -> Self:
        require_unique_references(self.effective_question_refs)
        return self


class ModelInvocation(InvocationIdentity, OptionalContract):
    model_config = ConfigDict(
        json_schema_extra={
            "if": {
                "properties": {
                    "outcome": {"properties": {"status": {"const": "not_dispatched"}}}
                },
                "required": ["outcome"],
            },
            "then": {
                "properties": {
                    "actual_provider": False,
                    "provider_request_id": False,
                    "usage": False,
                }
            },
            "else": {"required": ["actual_provider"]},
        }
    )
    schema_version: Literal["1.0.0"]
    configured_provider: ConfiguredProviderIdentity
    actual_provider: AbsentOptional[ActualProviderIdentity] = Field(
        default=None,
        validate_default=False,
        exclude_if=lambda value: value is None,
        json_schema_extra=omit_default_from_schema,
    )
    route_trace: ProviderRouteTrace
    purposes: Annotated[
        tuple[InvocationPurpose, ...],
        Field(min_length=1, max_length=2, json_schema_extra={"uniqueItems": True}),
        BeforeValidator(immutable_sequence),
    ]
    profile_refs: Annotated[
        tuple[ProfileContentRef, ...],
        Field(max_length=1024, json_schema_extra={"uniqueItems": True}),
        BeforeValidator(immutable_sequence),
    ]
    object_ref: AbsentOptional[ObjectRef] = Field(
        default=None,
        validate_default=False,
        exclude_if=lambda value: value is None,
        json_schema_extra=omit_default_from_schema,
    )
    source_discovery_invocation_ref: AbsentOptional[ModelInvocationRef] = Field(
        default=None,
        validate_default=False,
        exclude_if=lambda value: value is None,
        json_schema_extra=omit_default_from_schema,
    )
    contract_snapshot: AbsentOptional[InvocationContractSnapshot] = Field(
        default=None,
        validate_default=False,
        exclude_if=lambda value: value is None,
        json_schema_extra=omit_default_from_schema,
    )
    outcome: InvocationOutcome
    provider_request_id: AbsentOptional[RecordId] = Field(
        default=None,
        validate_default=False,
        exclude_if=lambda value: value is None,
        json_schema_extra=omit_default_from_schema,
    )
    request_digest: Digest
    started_at: UTCDateTime
    completed_at: UTCDateTime
    usage: AbsentOptional[UsageRecord] = Field(
        default=None,
        validate_default=False,
        exclude_if=lambda value: value is None,
        json_schema_extra=omit_default_from_schema,
    )
    evidence_refs: EvidenceRefs
    trace_id: RecordId

    @model_validator(mode="after")
    def validate_dispatch(self) -> Self:
        if self.completed_at < self.started_at:
            raise ValueError("invocation completion cannot predate its start")
        if isinstance(self.outcome, UndispatchedInvocation):
            if any(
                item is not None
                for item in (self.actual_provider, self.provider_request_id, self.usage)
            ):
                raise ValueError(
                    "undispatched invocation forbids actual provider, request ID and usage"
                )
        elif self.actual_provider is None:
            raise ValueError("dispatched invocation requires actual provider identity")
        if isinstance(self.actual_provider, ReportedProviderIdentity) and (
            self.actual_provider.version_evidence != self.provider_request_id
        ):
            raise ValueError(
                "reported version evidence must match the actual provider request ID"
            )
        require_unique_runtime_refs(self.evidence_refs)
        return self

    @model_validator(mode="after")
    def validate_route(self) -> Self:
        route = self.route_trace
        configured = self.configured_provider
        actual = self.actual_provider
        expected_kind = "remote" if route.routing_mode == "remote_primary" else "local"
        if configured.provider_kind != expected_kind:
            raise ValueError("configured provider kind does not match routing mode")
        if actual is not None and actual.deployment_id != route.selected_deployment_id:
            raise ValueError("actual deployment must match selected deployment")
        if isinstance(route, PrimaryProviderRoute):
            if route.selected_deployment_id != configured.primary_deployment_id:
                raise ValueError(
                    "primary audit must select configured primary deployment"
                )
            if actual is not None and (actual.provider_id, actual.provider_kind) != (
                configured.provider_id,
                configured.provider_kind,
            ):
                raise ValueError("actual primary must match configured provider")
        elif (
            route.to_deployment_id == configured.primary_deployment_id
            or route.trigger_invocation_ref.record_id == self.model_invocation_id
        ):
            raise ValueError(
                "fallback must select a non-primary deployment and reference a prior invocation"
            )
        return self

    @model_validator(mode="after")
    def validate_purposes(self) -> Self:
        purposes = set(self.purposes)
        if len(purposes) != len(self.purposes):
            raise ValueError("invocation purposes must be unique")
        shared = {ModelCapability.PROFILE_DISCOVERY, "optional_question_proposal"}
        if len(purposes) > 1 and purposes != shared:
            raise ValueError(
                "only discovery and optional Question proposals can share an invocation"
            )
        if any(
            purpose in _PURPOSE_NODE and _PURPOSE_NODE[purpose] != self.node_id
            for purpose in purposes
        ):
            raise ValueError("invocation purpose does not belong to the owning node")
        source = self.source_discovery_invocation_ref
        if (source is not None) != (purposes == {"optional_question_proposal"}):
            raise ValueError(
                "only additional proposals require a discovery invocation reference"
            )
        if source is not None and source.record_id == self.model_invocation_id:
            raise ValueError("additional proposal cannot reference itself as discovery")
        require_unique_references(self.profile_refs)
        if (
            purposes == {ModelCapability.OBJECT_GROUNDING}
            and len(self.profile_refs) != 1
        ):
            raise ValueError("grounding requires one Profile association")
        observation = purposes == {ModelCapability.OBJECT_OBSERVATION}
        if observation != (self.contract_snapshot is not None):
            raise ValueError(
                "only object observation requires a joint response contract snapshot"
            )
        if (
            self.contract_snapshot is not None
            and self.contract_snapshot.response_schema_evidence_ref
            not in self.evidence_refs
        ):
            raise ValueError(
                "invocation must retain the joint response schema evidence"
            )
        if observation and (len(self.profile_refs) != 1 or self.object_ref is None):
            raise ValueError(
                "observation requires one Profile and one Object association"
            )
        if self.node_id in {"N02", "N03"} and self.object_ref is not None:
            raise ValueError("discovery and grounding cannot reference an Object")
        return self
