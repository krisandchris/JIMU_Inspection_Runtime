"""Strict record shapes for physical dispatch auditing."""

from hashlib import sha256
from typing import Annotated, Literal, Self

from pydantic import BeforeValidator, ConfigDict, Field, TypeAdapter, model_validator

from jimu_runtime.bundle.trust import TrustPin
from jimu_runtime.domain.base import ContractModel
from jimu_runtime.domain.definition_enums import ModelCapability
from jimu_runtime.domain.definition_support import (
    NonNegativeBudget,
    ReviewReportRef,
    ShortText,
    immutable_sequence,
)
from jimu_runtime.domain.fact_schemas.canonicalization import canonical_bytes
from jimu_runtime.domain.fact_selectors import ToolContentRef, ToolPlanContentRef
from jimu_runtime.domain.primitives import ArtifactId, Digest, RecordId, Version
from jimu_runtime.domain.runtime.facts import ObjectRef, ProfileContentRef
from jimu_runtime.domain.runtime.invocations import (
    ActualProviderIdentity,
    ConfiguredProviderIdentity,
    InvocationError,
    InvocationNodeId,
    InvocationPurpose,
    ModelInvocation,
    PrimaryProviderRoute,
    ProviderKind,
    ProviderRouteTrace,
    SucceededInvocation,
)
from jimu_runtime.domain.runtime.tool_executions import (
    FailedToolResult,
    ReturnedToolAttempt,
    ToolArtifactRefs,
    ToolError,
    ToolExecution,
    ToolInvocationOrigin,
)
from jimu_runtime.domain.schema_types import ToolInputSchemaRef, ToolOutputSchemaRef
from jimu_runtime.tools.audit import tool_diagnostics_are_normalized


class _JournalRecord(ContractModel):
    model_config = ConfigDict(hide_input_in_errors=True)


class DispatchLease(_JournalRecord):
    lease_id: RecordId
    owner_id: RecordId
    scope_id: RecordId


class _ModelTarget(_JournalRecord):
    node_id: InvocationNodeId
    configured_provider: ConfiguredProviderIdentity
    purposes: Annotated[
        tuple[InvocationPurpose, ...],
        Field(min_length=1, max_length=2),
        BeforeValidator(immutable_sequence),
    ]
    profile_refs: Annotated[
        tuple[ProfileContentRef, ...],
        Field(max_length=1024),
        BeforeValidator(immutable_sequence),
    ] = ()
    object_ref: ObjectRef | None = None


class UnselectedModelDispatchTarget(_ModelTarget):
    kind: Literal["model_unselected"] = "model_unselected"


class _SelectedModelTarget(_ModelTarget):
    route_trace: ProviderRouteTrace
    selected_provider_id: ArtifactId
    selected_provider_kind: ProviderKind
    selected_deployment_id: ArtifactId
    selected_deployment_version: Version
    selected_model_id: ShortText
    selected_model_version: Version
    endpoint_descriptor_ref: TrustPin
    owner_ref: TrustPin
    version_evidence: ReviewReportRef | None = None

    @model_validator(mode="after")
    def verify_route(self) -> Self:
        route = self.route_trace
        configured = self.configured_provider
        if self.selected_deployment_id != route.selected_deployment_id:
            raise ValueError("dispatch target differs from selected route")
        if isinstance(route, PrimaryProviderRoute) and (
            self.selected_provider_id,
            self.selected_provider_kind,
            self.selected_deployment_id,
        ) != (
            configured.provider_id,
            configured.provider_kind,
            configured.primary_deployment_id,
        ):
            raise ValueError("primary dispatch differs from configured provider")
        return self


class ModelDispatchTarget(_SelectedModelTarget):
    kind: Literal["model"] = "model"


class _NestedModelTarget(_ModelTarget):
    parent_node_run_id: RecordId
    parent_attempt_id: RecordId
    tool_ref: ToolContentRef
    tool_plan_ref: ToolPlanContentRef
    step_id: ArtifactId
    bound_input_digest: Digest

    @model_validator(mode="after")
    def verify_step_scope(self) -> Self:
        if (
            self.node_id != "N05"
            or self.purposes != (ModelCapability.VISION_AUXILIARY,)
            or len(self.profile_refs) != 1
            or self.object_ref is None
        ):
            raise ValueError("nested model dispatch requires an exact N05 step scope")
        return self


class NestedModelDispatchTarget(_SelectedModelTarget, _NestedModelTarget):
    kind: Literal["nested_model"] = "nested_model"


class UnselectedNestedModelDispatchTarget(_NestedModelTarget):
    kind: Literal["nested_model_unselected"] = "nested_model_unselected"


class ToolDispatchTarget(_JournalRecord):
    kind: Literal["tool"] = "tool"
    tool_ref: ToolContentRef
    invocation_origin: ToolInvocationOrigin
    profile_ref: ProfileContentRef
    object_ref: ObjectRef | None = None
    capability_id: ArtifactId
    contract_version: Version
    implementation_version: Version
    implementation_proof_ref: TrustPin
    input_schema_ref: ToolInputSchemaRef | None = None
    output_schema_ref: ToolOutputSchemaRef | None = None
    input_refs: ToolArtifactRefs | None = None
    parent_node_run_id: RecordId | None = None
    parent_attempt_id: RecordId | None = None

    @model_validator(mode="after")
    def verify_origin(self) -> Self:
        origin = self.invocation_origin
        if origin.kind == "grounding_binding":
            if (
                self.object_ref is not None
                or origin.grounding_binding.tool_ref != self.tool_ref
            ):
                raise ValueError(
                    "grounding dispatch requires the exact Tool and no Object"
                )
        elif self.object_ref is None:
            raise ValueError("Tool Plan dispatch requires an Object")
        if (self.parent_node_run_id is None) != (self.parent_attempt_id is None):
            raise ValueError("nested Tool dispatch requires both parent identities")
        if self.parent_attempt_id is not None and origin.kind != "tool_plan_step":
            raise ValueError("only Tool Plan steps may have a parent Attempt")
        return self


type DispatchTarget = Annotated[
    ModelDispatchTarget
    | UnselectedModelDispatchTarget
    | NestedModelDispatchTarget
    | UnselectedNestedModelDispatchTarget
    | ToolDispatchTarget,
    Field(discriminator="kind"),
]


class DispatchIntent(_JournalRecord):
    schema_version: Literal["1.0.0"] = "1.0.0"
    dispatch_id: RecordId
    operation_id: RecordId
    trace_id: RecordId
    request_digest: Digest
    input_digest: Digest
    response_contract_digest: Digest
    resource_lease: DispatchLease | None = None
    retain_complete_response: bool | None = Field(
        default=None, exclude_if=lambda value: value is None
    )
    target: DispatchTarget

    @model_validator(mode="after")
    def verify_tool_contract(self) -> Self:
        if self.retain_complete_response is not None and not isinstance(
            self.target, (ModelDispatchTarget, NestedModelDispatchTarget)
        ):
            raise ValueError("response retention requires a selected model target")
        if isinstance(self.target, ToolDispatchTarget):
            schema = self.target.output_schema_ref
            if schema is not None and schema.digest != self.response_contract_digest:
                raise ValueError(
                    "Tool response contract differs from its pinned output schema"
                )
        return self


class ProviderDispatchObservation(_JournalRecord):
    """Only allowlisted scalars after removing credential-bearing values."""

    model_id: ShortText | None = None
    deployment_version: Version | None = None
    model_version: Version | None = None


class ToolImplementationObservation(_JournalRecord):
    implementation_version: Version
    proof_ref: TrustPin


class _DispatchResult(_JournalRecord):
    schema_version: Literal["1.0.0"] = "1.0.0"
    boundary: Literal["not_dispatched", "sent_unknown", "acknowledged"]
    response_digest: Digest | None = None
    observed_response_bytes: NonNegativeBudget = 0
    response_status: Annotated[int, Field(ge=100, le=599)] | None = None
    acknowledgement: (
        Literal["http_headers", "tool_process", "provider_receipt"] | None
    ) = None

    @model_validator(mode="after")
    def verify_boundary(self) -> Self:
        if (self.acknowledgement is not None) != (self.boundary == "acknowledged"):
            raise ValueError("acknowledgement source must match dispatch boundary")
        if (self.response_status is not None) != (
            self.acknowledgement == "http_headers"
        ):
            raise ValueError("HTTP acknowledgement requires the observed wire status")
        if self.boundary != "acknowledged" and (
            self.response_digest is not None or self.observed_response_bytes
        ):
            raise ValueError("response bytes require observed acknowledgement")
        return self


class ModelDispatchResult(_DispatchResult):
    kind: Literal["model"] = "model"
    error: InvocationError | None = None
    actual_provider: ActualProviderIdentity | None = None
    provider_request_id: RecordId | None = None
    provider_observation: ProviderDispatchObservation | None = None
    invocation: ModelInvocation | None = None
    normalized_response_digest: Digest | None = Field(
        default=None, exclude_if=lambda value: value is None
    )
    normalized_response_bytes: Annotated[int, Field(gt=0, le=2**63 - 1)] | None = Field(
        default=None, exclude_if=lambda value: value is None
    )
    response_evidence_error: ArtifactId | None = Field(
        default=None, exclude_if=lambda value: value is None
    )
    invalid_question_slots: Annotated[
        tuple[ArtifactId, ...],
        Field(max_length=1024),
        BeforeValidator(immutable_sequence),
    ] = Field(default=(), exclude_if=lambda value: not value)
    missing_question_slots: Annotated[
        tuple[ArtifactId, ...],
        Field(max_length=1024),
        BeforeValidator(immutable_sequence),
    ] = Field(default=(), exclude_if=lambda value: not value)
    response_bytes_omission_reason: (
        Literal["incomplete", "policy_disabled", "credential_echo", "unvalidated"]
        | None
    ) = None

    @model_validator(mode="after")
    def verify_model_result(self) -> Self:
        if self.response_evidence_error is not None and (
            self.acknowledgement != "http_headers"
            or self.response_digest is None
            or self.observed_response_bytes <= 0
        ):
            raise ValueError(
                "response evidence failure requires an attributable complete HTTP receipt"
            )
        slots = (*self.invalid_question_slots, *self.missing_question_slots)
        if slots:
            contract = (
                None if self.invocation is None else self.invocation.contract_snapshot
            )
            if (
                contract is None
                or self.normalized_response_digest is None
                or len(slots) != len(set(slots))
                or not set(slots)
                <= {ref.artifact_id for ref in contract.effective_question_refs}
            ):
                raise ValueError(
                    "question slot audit requires distinct exact normalized contract fields"
                )
        if (self.normalized_response_digest is None) != (
            self.normalized_response_bytes is None
        ):
            raise ValueError("normalized response proof requires digest and byte count")
        if self.normalized_response_digest is not None and (
            self.acknowledgement != "http_headers"
            or self.response_status is None
            or not 200 <= self.response_status <= 299
            or self.response_digest is None
            or self.observed_response_bytes <= 0
            or self.error is not None
            or self.response_bytes_omission_reason not in {None, "policy_disabled"}
        ):
            raise ValueError(
                "normalized response proof requires a validated whole HTTP response"
            )
        if (
            self.response_bytes_omission_reason == "incomplete"
            and self.response_digest is not None
        ):
            raise ValueError("incomplete provider response cannot claim a whole digest")
        if (
            self.boundary == "not_dispatched"
            and self.response_bytes_omission_reason is not None
        ):
            raise ValueError("known non-dispatch has no provider response")
        if self.boundary != "acknowledged" and self.error is None:
            raise ValueError("an unacknowledged call requires a normalized error")
        if self.boundary != "acknowledged" and (
            self.provider_request_id is not None
            or self.provider_observation is not None
        ):
            raise ValueError("provider response metadata requires acknowledgement")
        actual = self.actual_provider
        if actual is not None and (
            self.boundary == "not_dispatched"
            or (
                actual.version_resolution == "reported"
                and actual.version_evidence != self.provider_request_id
            )
        ):
            raise ValueError("invalid provider identity evidence for dispatch boundary")
        invocation = self.invocation
        if invocation is None:
            return self
        if (
            invocation.actual_provider != actual
            or invocation.provider_request_id != self.provider_request_id
        ):
            raise ValueError("invocation provider evidence differs from dispatch")
        outcome = invocation.outcome
        if isinstance(outcome, SucceededInvocation):
            if (
                outcome.response_digest != self.response_digest
                or self.error is not None
            ):
                raise ValueError(
                    "successful invocation requires the whole response and no error"
                )
        elif outcome.error != self.error or (
            (outcome.status == "not_dispatched") != (self.boundary == "not_dispatched")
        ):
            raise ValueError(
                "invocation failure must match the observed dispatch boundary"
            )
        return self


class ToolDispatchResult(_DispatchResult):
    kind: Literal["tool"] = "tool"
    error: ToolError | None = None
    implementation: ToolImplementationObservation | None = None
    tool_execution_digest: Digest | None = None
    execution: ToolExecution | None = None

    @model_validator(mode="after")
    def verify_tool_result(self) -> Self:
        if self.boundary != "acknowledged" and self.error is None:
            raise ValueError("an unacknowledged call requires a normalized error")
        if self.boundary == "not_dispatched" and self.implementation is not None:
            raise ValueError("undispatched Tool cannot have observed implementation")
        execution = self.execution
        if execution is None:
            return self
        if not tool_diagnostics_are_normalized(execution):
            raise ValueError("Tool Execution contains unnormalized diagnostics")
        if self.tool_execution_digest != tool_receipt_digest(execution):
            raise ValueError("canonical Tool Execution digest does not match")
        if (self.boundary == "not_dispatched") != (
            execution.status == "not_dispatched"
        ):
            raise ValueError(
                "Tool Execution differs from the observed dispatch boundary"
            )
        observed = self.implementation
        if (
            None if observed is None else observed.implementation_version
        ) != execution.actual_implementation_version:
            raise ValueError(
                "Tool Execution and observed implementation versions differ"
            )
        attempt = execution.attempt_result
        if isinstance(attempt, ReturnedToolAttempt):
            if self.boundary != "acknowledged":
                raise ValueError("returned Tool output requires acknowledgement")
            result = attempt.result
            error = result.error if isinstance(result, FailedToolResult) else None
        else:
            error = attempt.error
        if error != self.error:
            raise ValueError("Tool Execution and dispatch error differ")
        return self


type DispatchResult = Annotated[
    ModelDispatchResult | ToolDispatchResult, Field(discriminator="kind")
]
RESULT_ADAPTER = TypeAdapter[DispatchResult](
    DispatchResult, config=ConfigDict(hide_input_in_errors=True)
)


def tool_receipt_digest(execution: ToolExecution) -> str:
    """Digest normalized Tool Execution metadata independently of wire response bytes."""
    return sha256(canonical_bytes(execution.model_dump(mode="json"))).hexdigest()
