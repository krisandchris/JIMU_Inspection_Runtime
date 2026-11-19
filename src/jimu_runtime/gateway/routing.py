"""Select a deployment from immutable Execution pins and current host evidence."""

from dataclasses import dataclass
from datetime import datetime
from typing import Annotated, NoReturn, Self, cast

from pydantic import Field, TypeAdapter, model_validator

from jimu_runtime.bundle.capabilities import CapabilitySnapshot, DeploymentContract
from jimu_runtime.bundle.semantic_inputs import require_definition
from jimu_runtime.bundle.trust import TrustPin
from jimu_runtime.domain.base import ContractModel
from jimu_runtime.domain.definition_enums import ModelCapability
from jimu_runtime.domain.definition_support import (
    NonNegativeBudget,
    ReviewReportRef,
    ShortText,
    UTCDateTime,
    require_unique_references,
)
from jimu_runtime.domain.definitions.models import ModelBinding, ModelBindingContentRef
from jimu_runtime.domain.enums import AttemptStatus, NodeRunStatus, RuntimeRecordKind
from jimu_runtime.domain.policies import (
    EvaluatedFallbackPolicy,
    FallbackPolicy,
    FallbackPolicyArtifact,
    LocalPrimaryProviderPolicy,
    ProviderPolicy,
    ProviderPolicyArtifact,
    ResourceRequirements,
    ResourceRequirementsArtifact,
    TriggerError,
)
from jimu_runtime.domain.primitives import ArtifactId, Digest, RecordId
from jimu_runtime.domain.runtime.executions import ConfiguredModelPin
from jimu_runtime.domain.runtime.facts import (
    ModelInvocationRef,
    ObjectRef,
    ProfileContentRef,
)
from jimu_runtime.domain.runtime.invocations import (
    ConfiguredProviderIdentity,
    FailedInvocation,
    FallbackProviderRoute,
    InvocationContractSnapshot,
    InvocationPurpose,
    LocalPrimaryExceptionIdentity,
    ModelInvocation,
    PrimaryProviderRoute,
    ProviderRouteTrace,
    UnavailableProviderIdentity,
    UndispatchedInvocation,
)
from jimu_runtime.domain.tool_mappings import (
    ProfileContentRef as ReviewProfileContentRef,
)
from jimu_runtime.domain.tool_mappings import RegionContentRef
from jimu_runtime.domain.validation.authority_inputs import (
    AttemptDescriptor,
    NodeAttemptOwner,
    NodeRunDescriptor,
)
from jimu_runtime.domain.validation.invocations import (
    NestedModelStepOwner,
    validate_nested_model_step_owner,
)
from jimu_runtime.domain.validation.policies import (
    ModelBindingRegistration,
    PolicyRegistration,
    _deployment_matches,
    _scope_covers,
)
from jimu_runtime.domain.validation.review_inputs import (
    FallbackTransitionAssessment,
    LocalExceptionAssessment,
    ReviewedDeployment,
    ReviewMetadata,
    ReviewScope,
)
from jimu_runtime.workflow.pins import ResolvedExecutionPins, ResolvedModelDeployments

_UTC = TypeAdapter[UTCDateTime](UTCDateTime)
_FAILURE_STATES = {AttemptStatus.FAILED, AttemptStatus.TIMED_OUT}
_PURPOSE_NODE = {
    ModelCapability.PROFILE_DISCOVERY: "N02",
    "optional_question_proposal": "N02",
    ModelCapability.OBJECT_GROUNDING: "N03",
    ModelCapability.OBJECT_OBSERVATION: "N04",
    ModelCapability.CANDIDATE_PREDICTION: "N06",
}


class RouteRejected(ValueError):
    """One stable routing reason safe to retain in local diagnostics."""

    def __init__(self, code: ArtifactId, message: str) -> None:
        super().__init__(message)
        self.code = code


def _reject(code: ArtifactId, message: str) -> NoReturn:
    raise RouteRejected(code, message)


class DeploymentOwnership(ContractModel):
    """Exact, authenticated host routing data; carries no endpoint URL or secret."""

    deployment: ReviewedDeployment
    owner_ref: TrustPin
    endpoint_descriptor_ref: TrustPin
    model_id: ShortText
    version_evidence: ReviewReportRef | None


class DeploymentOwnershipRegistration(ContractModel):
    ownership: DeploymentOwnership
    valid_from: UTCDateTime
    valid_until: UTCDateTime
    revoked_at: UTCDateTime | None

    @model_validator(mode="after")
    def ordered_window(self) -> Self:
        if self.valid_from >= self.valid_until:
            raise ValueError("deployment ownership needs an ordered validity window")
        return self

    def is_current(self, at: datetime) -> bool:
        return self.valid_from <= at < self.valid_until and (
            self.revoked_at is None or at < self.revoked_at
        )


class CurrentRoutingState(ContractModel):
    """A trusted current registry view of availability, reviews and ownership."""

    capabilities: CapabilitySnapshot
    ownership: Annotated[
        tuple[DeploymentOwnershipRegistration, ...], Field(max_length=1024)
    ]

    @model_validator(mode="after")
    def unique_owners(self) -> Self:
        keys = {
            (
                row.ownership.deployment.provider_id,
                row.ownership.deployment.deployment_id,
            )
            for row in self.ownership
        }
        if len(keys) != len(self.ownership):
            raise ValueError("current deployment ownership must be unique")
        return self


class RoutePolicy(ContractModel):
    """Historical route configuration resolved from the job's trusted Execution."""

    execution_input_digest: Digest
    binding: ModelBindingRegistration
    configured_pin: ConfiguredModelPin
    resolved: ResolvedModelDeployments
    provider_registration: PolicyRegistration
    fallback_registration: PolicyRegistration
    resource_registration: PolicyRegistration
    ownership: Annotated[tuple[DeploymentOwnership, ...], Field(max_length=1024)]

    @classmethod
    def from_execution(
        cls,
        execution: ResolvedExecutionPins,
        binding_ref: ModelBindingContentRef,
        ownership: tuple[DeploymentOwnership, ...],
    ) -> Self:
        """Read pinned artifacts and deployments from already authenticated inputs."""
        binding = require_definition(execution.catalogue, binding_ref, ModelBinding)
        configured = next(
            (
                pin
                for pin in execution.pins.model_pins
                if pin.binding_ref == binding_ref
            ),
            None,
        )
        resolved = next(
            (
                row
                for row in execution.model_deployments
                if row.binding_ref == binding_ref
            ),
            None,
        )
        if configured is None or resolved is None:
            _reject(
                "model.binding_not_pinned", "Execution has no exact model binding pin."
            )
        return cls(
            execution_input_digest=execution.pins.input_digest,
            binding=ModelBindingRegistration(ref=binding_ref, definition=binding),
            configured_pin=configured,
            resolved=resolved,
            provider_registration=PolicyRegistration(
                ref=binding.provider_policy_ref,
                artifact=require_definition(
                    execution.catalogue,
                    binding.provider_policy_ref,
                    ProviderPolicyArtifact,
                ),
            ),
            fallback_registration=PolicyRegistration(
                ref=binding.fallback_policy_ref,
                artifact=require_definition(
                    execution.catalogue,
                    binding.fallback_policy_ref,
                    FallbackPolicyArtifact,
                ),
            ),
            resource_registration=PolicyRegistration(
                ref=binding.resource_requirements_ref,
                artifact=require_definition(
                    execution.catalogue,
                    binding.resource_requirements_ref,
                    ResourceRequirementsArtifact,
                ),
            ),
            ownership=ownership,
        )

    @model_validator(mode="after")
    def exact_pins(self) -> Self:
        definition = self.binding.definition
        pin = self.configured_pin
        provider = self.provider_registration.artifact
        fallback = self.fallback_registration.artifact
        if (
            not isinstance(provider, ProviderPolicyArtifact)
            or not isinstance(fallback, FallbackPolicyArtifact)
            or not isinstance(
                self.resource_registration.artifact, ResourceRequirementsArtifact
            )
            or (pin.binding_ref, self.resolved.binding_ref)
            != (self.binding.ref, self.binding.ref)
            or (pin.provider_policy_ref, self.provider_registration.ref)
            != (definition.provider_policy_ref, definition.provider_policy_ref)
            or (pin.fallback_policy_ref, self.fallback_registration.ref)
            != (definition.fallback_policy_ref, definition.fallback_policy_ref)
            or self.resource_registration.ref != definition.resource_requirements_ref
            or (pin.primary_deployment_id, pin.deployments)
            != (provider.policy.primary_deployment_id, provider.policy.deployments)
        ):
            raise ValueError("route policy differs from the immutable Execution pins")
        if len(self.resolved.deployments) != len(pin.deployments):
            raise ValueError("each configured deployment needs an exact resolved pin")
        for requirement, deployment in zip(
            pin.deployments, self.resolved.deployments, strict=True
        ):
            if not _deployment_matches(deployment, requirement, definition):
                raise ValueError(
                    "resolved deployment differs from its configured requirement"
                )
        owned = {row.deployment for row in self.ownership}
        if not owned <= set(self.resolved.deployments) or len(owned) != len(
            self.ownership
        ):
            raise ValueError(
                "historical ownership must refer to unique resolved deployments"
            )
        known = {row.deployment_id for row in pin.deployments}
        if any(
            edge.from_deployment_id not in known or edge.to_deployment_id not in known
            for edge in fallback.policy.allowed_transitions
        ):
            raise ValueError("fallback endpoints must be configured deployments")
        return self

    @property
    def provider(self) -> ProviderPolicy:
        return cast(ProviderPolicyArtifact, self.provider_registration.artifact).policy

    @property
    def fallback(self) -> FallbackPolicy:
        return cast(FallbackPolicyArtifact, self.fallback_registration.artifact).policy

    @property
    def resources(self) -> ResourceRequirements:
        return cast(
            ResourceRequirementsArtifact, self.resource_registration.artifact
        ).policy


class RouteOperation(ContractModel):
    """Identifies one logical call across Attempts and wire encodings."""

    operation_id: RecordId
    execution_input_digest: Digest
    node_run: NodeRunDescriptor
    binding_ref: ModelBindingContentRef
    input_digest: Digest
    response_contract_digest: Digest
    trace_id: RecordId
    purposes: Annotated[
        tuple[InvocationPurpose, ...], Field(min_length=1, max_length=2)
    ]
    profile_refs: Annotated[tuple[ProfileContentRef, ...], Field(max_length=1024)]
    region_ref: RegionContentRef | None = None
    object_ref: ObjectRef | None = None
    source_discovery_invocation_ref: ModelInvocationRef | None = None
    contract_snapshot: InvocationContractSnapshot | None = None
    nested_step_input_digest: Digest | None = None

    @model_validator(mode="after")
    def checked_shape(self) -> Self:
        require_unique_references(self.profile_refs)
        purposes = set(self.purposes)
        if len(purposes) != len(self.purposes) or (
            len(purposes) > 1
            and purposes
            != {ModelCapability.PROFILE_DISCOVERY, "optional_question_proposal"}
        ):
            raise ValueError("invalid operation purposes")
        if (
            any(
                purpose in _PURPOSE_NODE
                and _PURPOSE_NODE[purpose] != self.node_run.node_id
                for purpose in purposes
            )
            or self.node_run.node_id == "N01"
        ):
            raise ValueError("operation purpose does not belong to its node")
        if (self.source_discovery_invocation_ref is not None) != (
            purposes == {"optional_question_proposal"}
        ):
            raise ValueError(
                "only separate Question proposals pin a discovery invocation"
            )
        if ModelCapability.OBJECT_GROUNDING in purposes and len(self.profile_refs) != 1:
            raise ValueError("grounding must pin one Profile")
        if self.node_run.node_id in {"N02", "N03"} and self.object_ref is not None:
            raise ValueError("discovery and grounding do not take an Object")
        if ModelCapability.OBJECT_OBSERVATION in purposes:
            if (
                len(self.profile_refs) != 1
                or self.object_ref is None
                or self.contract_snapshot is None
                or self.contract_snapshot.response_schema_sha256
                != self.response_contract_digest
            ):
                raise ValueError(
                    "observation needs an Object, Profile and exact joint response contract"
                )
        elif self.contract_snapshot is not None:
            raise ValueError("only observation pins a joint response contract")
        return self


class DeploymentBudget(ContractModel):
    deployment_id: ArtifactId
    remaining: NonNegativeBudget


class RouteBudget(ContractModel):
    """Remaining physical-call budgets obtained from the trusted scheduler."""

    node_remaining: NonNegativeBudget
    operation_remaining: NonNegativeBudget
    deployments: Annotated[tuple[DeploymentBudget, ...], Field(max_length=1024)]

    @model_validator(mode="after")
    def unique_deployments(self) -> Self:
        if len({row.deployment_id for row in self.deployments}) != len(
            self.deployments
        ):
            raise ValueError("duplicate deployment budget")
        return self

    def permits(self, deployment_id: ArtifactId) -> bool:
        return (
            self.node_remaining > 0
            and self.operation_remaining > 0
            and any(
                row.deployment_id == deployment_id and row.remaining > 0
                for row in self.deployments
            )
        )


@dataclass(frozen=True, slots=True)
class RecordedInvocation:
    """Independently recovered operation association and terminal Attempt witness."""

    operation: RouteOperation
    attempt: AttemptDescriptor
    invocation: ModelInvocation
    nested_step_owner: NestedModelStepOwner | None = None


@dataclass(frozen=True, slots=True)
class PlannedRoute:
    policy: RoutePolicy
    operation: RouteOperation
    attempt: AttemptDescriptor
    configured: ConfiguredProviderIdentity
    selected: ReviewedDeployment
    trace: ProviderRouteTrace
    ownership: DeploymentOwnership
    reviews: tuple[ReviewMetadata, ...]
    history: tuple[RecordedInvocation, ...]
    approved_at: datetime
    authorization_valid_until: datetime
    nested_step_owner: NestedModelStepOwner | None = None

    def authorization_active(self, at: datetime) -> bool:
        return (
            at.utcoffset() is not None
            and self.approved_at <= at < self.authorization_valid_until
        )

    @property
    def binding(self) -> ModelBinding:
        return self.policy.binding.definition

    @property
    def resource_requirements(self) -> ResourceRequirements:
        return self.policy.resources


def _history_matches(
    entry: RecordedInvocation,
    operation: RouteOperation,
    configured: ConfiguredProviderIdentity,
    deployment: ReviewedDeployment | None,
    *,
    nested: bool,
) -> bool:
    invocation = entry.invocation
    historical = entry.operation
    logical = (
        historical.model_copy(update={"node_run": operation.node_run})
        if nested
        else historical
    )
    return (
        logical == operation
        and _attempt_matches(historical, entry.attempt)
        and entry.attempt.status in _FAILURE_STATES
        and invocation.attempt_ref == entry.attempt.attempt_ref
        and invocation.execution_id == operation.node_run.execution_id
        and invocation.node_run_ref == historical.node_run.node_run_ref
        and invocation.node_id == operation.node_run.node_id
        and invocation.configured_provider == configured
        and invocation.trace_id == operation.trace_id
        and set(invocation.purposes) == set(operation.purposes)
        and invocation.profile_refs == operation.profile_refs
        and invocation.object_ref == operation.object_ref
        and invocation.source_discovery_invocation_ref
        == operation.source_discovery_invocation_ref
        and invocation.contract_snapshot == operation.contract_snapshot
        and isinstance(invocation.outcome, (FailedInvocation, UndispatchedInvocation))
        and deployment is not None
    )


def _attempt_matches(operation: RouteOperation, attempt: AttemptDescriptor) -> bool:
    return (
        isinstance(attempt.owner, NodeAttemptOwner)
        and attempt.owner.node_run_ref == operation.node_run.node_run_ref
        and attempt.execution_id == operation.node_run.execution_id
    )


def _require_step_owner(
    owner: NestedModelStepOwner,
    operation: RouteOperation,
    attempt: AttemptDescriptor,
    *,
    live: bool,
) -> None:
    try:
        validate_nested_model_step_owner(owner, live=live)
    except ValueError:
        _reject(
            "model.nested_step_owner_invalid",
            "Nested model operation requires a verified N05 child owner.",
        )
    lineage = owner.lineage
    if (
        owner.child_node != operation.node_run
        or owner.child_attempt != attempt
        or operation.binding_ref != lineage.binding_ref
        or operation.profile_refs != (lineage.profile_ref,)
        or operation.object_ref != lineage.object_ref
        or operation.purposes != (ModelCapability.VISION_AUXILIARY,)
        or operation.nested_step_input_digest != lineage.bound_input_digest
    ):
        _reject(
            "model.nested_step_owner_invalid",
            "Operation differs from its exact N05 child step pins.",
        )


class _Routing:
    def __init__(
        self,
        policy: RoutePolicy,
        operation: RouteOperation,
        attempt: AttemptDescriptor,
        state: CurrentRoutingState,
        budget: RouteBudget,
        at: datetime,
        nested_step_owner: NestedModelStepOwner | None,
    ) -> None:
        at = _UTC.validate_python(at)
        self.policy = policy
        self.operation = operation
        self.attempt = attempt
        self.state = state
        self.budget = budget
        self.at = at
        self.nested_step_owner = nested_step_owner
        if nested_step_owner is not None:
            _require_step_owner(nested_step_owner, operation, attempt, live=True)
        elif operation.nested_step_input_digest is not None:
            _reject(
                "model.nested_step_owner_invalid",
                "A nested model operation requires its verified child owner.",
            )
        self.deployment_pins = {
            row.deployment_id: row for row in policy.resolved.deployments
        }
        self.requirements = {
            row.deployment_id: row for row in policy.configured_pin.deployments
        }
        primary = self.requirements[policy.configured_pin.primary_deployment_id]
        self.configured = ConfiguredProviderIdentity(
            binding_ref=policy.binding.ref,
            provider_policy_ref=policy.provider_registration.ref,
            primary_deployment_id=primary.deployment_id,
            provider_id=primary.provider_id,
            provider_kind=primary.provider_kind,
            logical_model_id=primary.logical_model_id,
        )
        binding = policy.binding.definition
        allowed = (
            {ModelCapability.PROFILE_DISCOVERY, "optional_question_proposal"}
            if binding.capability is ModelCapability.PROFILE_DISCOVERY
            else {binding.capability}
        )
        snapshot = operation.contract_snapshot
        expected_response = (
            snapshot.response_schema_sha256
            if snapshot is not None
            else binding.output_schema_ref.digest
        )
        if (
            operation.binding_ref != policy.binding.ref
            or operation.execution_input_digest != policy.execution_input_digest
            or not set(operation.purposes) <= allowed
            or operation.response_contract_digest != expected_response
            or (
                snapshot is not None
                and snapshot.configured_output_schema_ref != binding.output_schema_ref
            )
            or not _attempt_matches(operation, attempt)
            or attempt.status is not AttemptStatus.RUNNING
            or operation.node_run.status is not NodeRunStatus.RUNNING
        ):
            _reject(
                "model.operation_pin_mismatch",
                "Operation must match its immutable binding and live Attempt.",
            )
        if not state.capabilities.is_current(at):
            _reject(
                "model.capability_snapshot_expired",
                "No current trusted capability snapshot is available.",
            )
        if budget.node_remaining == 0 or budget.operation_remaining == 0:
            _reject(
                "model.call_budget_exhausted",
                "Node or operation has exhausted its physical call budget.",
            )
        self.reviews = {
            review.report_ref: review
            for review in state.capabilities.reviews_at(at)
            if review.verdict == "approved"
            and review.valid_from <= at < review.valid_until
        }
        self.local_exception = self._local_exception()

    def _scope(self, scope: ReviewScope) -> bool:
        return all(
            _scope_covers(
                scope,
                self.policy.binding.ref,
                ReviewProfileContentRef.model_validate(profile.model_dump())
                if profile is not None
                else None,
                self.operation.region_ref,
            )
            is True
            for profile in self.operation.profile_refs or (None,)
        )

    def _local_exception(self) -> LocalPrimaryExceptionIdentity | None:
        provider = self.policy.provider
        if not isinstance(provider, LocalPrimaryProviderPolicy):
            return None
        exception = provider.local_primary_exception
        review = self.reviews.get(exception.approval_ref)
        capability = self.policy.binding.definition.capability
        if (
            not exception.valid_from <= self.at < exception.valid_until
            or capability not in exception.applicable_capabilities
            or review is None
            or not any(
                isinstance(check, LocalExceptionAssessment)
                and (check.exception_id, check.exception_version, check.reason)
                == (exception.exception_id, exception.version, exception.reason)
                and check.primary.model_dump()
                == self.deployment_pins[provider.primary_deployment_id].model_dump()
                and capability in check.applicable_capabilities
                and self._scope(check.scope)
                for check in review.assessments
            )
        ):
            _reject(
                "model.local_primary_unapproved",
                "Local primary has no current exact approval for this scope.",
            )
        return LocalPrimaryExceptionIdentity(
            exception_id=exception.exception_id,
            version=exception.version,
            approval_ref=exception.approval_ref,
        )

    def _ownership(
        self, selected: ReviewedDeployment
    ) -> tuple[DeploymentOwnership, datetime]:
        ownership = next(
            (item for item in self.policy.ownership if item.deployment == selected),
            None,
        )
        owned = next(
            (
                row
                for row in self.state.ownership
                if row.ownership == ownership and row.is_current(self.at)
            ),
            None,
        )
        if ownership is None or owned is None:
            _reject(
                "model.ownership_unavailable",
                "Exact pinned deployment ownership is unavailable or revoked.",
            )
        definition = self.policy.binding.definition
        contract = DeploymentContract(
            capability=definition.capability,
            input_schema_ref=definition.input_schema_ref,
            output_schema_ref=definition.output_schema_ref,
        )
        features = set(self.requirements[selected.deployment_id].required_features)
        registered = next(
            (
                row
                for row in self.state.capabilities.model_deployments
                if row.identity == selected
                and row.is_current(self.at)
                and features.issubset(row.supported_features)
                and contract in row.contracts
            ),
            None,
        )
        if registered is None:
            _reject(
                "model.deployment_unavailable",
                "Current host does not offer the pinned deployment, features and exact contracts.",
            )
        expiry = min(
            owned.valid_until,
            owned.revoked_at or owned.valid_until,
            registered.valid_until,
            registered.revoked_at or registered.valid_until,
        )
        return ownership, expiry

    def _authorization_deadline(self, trace: ProviderRouteTrace) -> datetime:
        provider = self.policy.provider
        limit = self.state.capabilities.valid_until
        refs = (
            set(trace.evaluation_refs)
            if isinstance(trace, FallbackProviderRoute)
            else set()
        )
        if isinstance(provider, LocalPrimaryProviderPolicy):
            exception = provider.local_primary_exception
            limit = min(limit, exception.valid_until)
            refs.add(exception.approval_ref)
        for row in self.state.capabilities.model_reviews:
            if row.metadata.report_ref in refs:
                limit = min(
                    limit,
                    row.metadata.valid_until,
                    row.revoked_at or row.metadata.valid_until,
                )
        return limit

    def _plan(
        self,
        trace: ProviderRouteTrace,
        history: tuple[RecordedInvocation, ...],
    ) -> PlannedRoute:
        selected = self.deployment_pins[trace.selected_deployment_id]
        if not self.budget.permits(selected.deployment_id):
            _reject(
                "model.call_budget_exhausted",
                "Selected deployment has no remaining physical call budget.",
            )
        ownership, expiry = self._ownership(selected)
        return PlannedRoute(
            self.policy,
            self.operation,
            self.attempt,
            self.configured,
            selected,
            trace,
            ownership,
            tuple(self.reviews.values()),
            history,
            self.at,
            min(expiry, self._authorization_deadline(trace)),
            self.nested_step_owner,
        )

    def primary(self) -> PlannedRoute:
        trace = PrimaryProviderRoute(
            route_kind="primary",
            routing_mode=self.policy.provider.routing_mode,
            selected_deployment_id=self.configured.primary_deployment_id,
            fallback_policy_ref=self.policy.fallback_registration.ref,
            **self._exception_field(),
        )
        return self._plan(trace, ())

    def _exception_field(self) -> dict[str, LocalPrimaryExceptionIdentity]:
        if self.local_exception is None:
            return {}
        return {"local_primary_exception": self.local_exception}

    def _validate_history(self, history: tuple[RecordedInvocation, ...]) -> None:
        if not history:
            _reject(
                "model.fallback_history_invalid",
                "A subsequent call needs its prior terminal invocation history.",
            )
        prior_number = 0
        invocation_ids: set[RecordId] = set()
        attempt_ids = {self.attempt.attempt_ref}
        visited: set[ArtifactId] = set()
        previous: ModelInvocation | None = None
        step = self.nested_step_owner
        child_nodes = {self.operation.node_run.node_run_ref}
        parent_attempts = (
            {step.parent_attempt.attempt_ref} if step is not None else set()
        )
        current_number = (
            step.parent_attempt.attempt_number
            if step is not None
            else self.attempt.attempt_number
        )
        for entry in history:
            inv = entry.invocation
            route = inv.route_trace
            selected = route.selected_deployment_id
            deployment = self.deployment_pins.get(selected)
            older = entry.nested_step_owner
            number = entry.attempt.attempt_number
            if step is not None and older is not None:
                _require_step_owner(older, entry.operation, entry.attempt, live=False)
                if (
                    older.lineage != step.lineage
                    or older.child_node.node_run_ref in child_nodes
                    or older.parent_attempt.attempt_ref in parent_attempts
                ):
                    _reject(
                        "model.fallback_history_invalid",
                        "Nested fallback requires distinct, ordered children of the same exact step.",
                    )
                child_nodes.add(older.child_node.node_run_ref)
                parent_attempts.add(older.parent_attempt.attempt_ref)
                number = older.parent_attempt.attempt_number
            elif step is not None or older is not None:
                _reject(
                    "model.fallback_history_invalid",
                    "Every nested fallback record needs a durable child owner.",
                )
            if (
                not _history_matches(
                    entry,
                    self.operation,
                    self.configured,
                    deployment,
                    nested=step is not None,
                )
                or not prior_number < number < current_number
                or entry.attempt.attempt_ref in attempt_ids
                or inv.model_invocation_id in invocation_ids
                or inv.completed_at > self.at
                or route.routing_mode != self.policy.provider.routing_mode
                or route.fallback_policy_ref != self.policy.fallback_registration.ref
                or route.local_primary_exception != self.local_exception
                or (previous is not None and previous.completed_at > inv.started_at)
                or (
                    deployment is not None
                    and not self._compatible_actual(inv, deployment)
                )
            ):
                _reject(
                    "model.fallback_history_invalid",
                    "Prior invocation does not belong to this exact operation and ordered Attempt history.",
                )
            if previous is None:
                if not isinstance(route, PrimaryProviderRoute):
                    _reject(
                        "model.fallback_history_invalid",
                        "Invocation history must begin at the pinned primary.",
                    )
            elif route != previous.route_trace:
                if (
                    not isinstance(route, FallbackProviderRoute)
                    or selected in visited
                    or route.from_deployment_id
                    != previous.route_trace.selected_deployment_id
                    or route.trigger_invocation_ref.record_id
                    != previous.model_invocation_id
                    or not isinstance(
                        previous.outcome, (FailedInvocation, UndispatchedInvocation)
                    )
                    or route.trigger_error != previous.outcome.error.error_class
                    or not isinstance(self.policy.fallback, EvaluatedFallbackPolicy)
                    or route.trigger_error not in self.policy.fallback.trigger_errors
                    or not set(route.evaluation_refs).issubset(
                        self.policy.fallback.evaluation_refs
                    )
                    or not any(
                        (edge.from_deployment_id, edge.to_deployment_id)
                        == (route.from_deployment_id, selected)
                        for edge in self.policy.fallback.allowed_transitions
                    )
                ):
                    _reject(
                        "model.fallback_history_invalid",
                        "Invocation history changes deployment without a new declared hop.",
                    )
            visited.add(selected)
            invocation_ids.add(inv.model_invocation_id)
            attempt_ids.add(entry.attempt.attempt_ref)
            previous = inv
            prior_number = number

    def _compatible_actual(
        self, prior: ModelInvocation, deployment: ReviewedDeployment
    ) -> bool:
        actual = prior.actual_provider
        if actual is None:
            return True
        ownership = next(
            (item for item in self.policy.ownership if item.deployment == deployment),
            None,
        )
        return (
            ownership is not None
            and (
                actual.provider_id,
                actual.deployment_id,
                actual.provider_kind,
                actual.model_id,
            )
            == (
                deployment.provider_id,
                deployment.deployment_id,
                deployment.provider_kind,
                ownership.model_id,
            )
            and (
                isinstance(actual, UnavailableProviderIdentity)
                or (actual.deployment_version, actual.model_version)
                == (deployment.deployment_version, deployment.model_version)
            )
        )

    def _evaluation_refs(
        self,
        source: ReviewedDeployment,
        target: ReviewedDeployment,
        trigger: TriggerError,
    ) -> tuple[ReviewReportRef, ...]:
        fallback = self.policy.fallback
        if not isinstance(fallback, EvaluatedFallbackPolicy):
            return ()
        binding = self.policy.binding.definition
        covered = []
        for reference in fallback.evaluation_refs:
            review = self.reviews.get(reference)
            if review is not None and any(
                isinstance(check, FallbackTransitionAssessment)
                and check.source == source
                and check.target == target
                and trigger in check.trigger_errors
                and check.capability == binding.capability
                and check.input_schema_ref == binding.input_schema_ref
                and check.output_schema_ref == binding.output_schema_ref
                and self._scope(check.scope)
                for check in review.assessments
            ):
                covered.append(reference)
        return tuple(covered)

    def retry(
        self,
        history: tuple[RecordedInvocation, ...],
        retryable_errors: tuple[TriggerError, ...],
    ) -> PlannedRoute:
        self._validate_history(history)
        prior = history[-1].invocation
        if (
            not isinstance(prior.outcome, (FailedInvocation, UndispatchedInvocation))
            or prior.outcome.error.error_class not in retryable_errors
        ):
            _reject(
                "model.retry_not_permitted",
                "The actual failure was not configured for retry.",
            )
        route = prior.route_trace
        if isinstance(route, FallbackProviderRoute):
            covered = self._evaluation_refs(
                self.deployment_pins[route.from_deployment_id],
                self.deployment_pins[route.to_deployment_id],
                route.trigger_error,
            )
            if not set(route.evaluation_refs) <= set(covered):
                _reject(
                    "model.fallback_review_unavailable",
                    "The prior fallback's exact authorization is unavailable.",
                )
        return self._plan(route, history)

    def fallback(self, history: tuple[RecordedInvocation, ...]) -> PlannedRoute:
        self._validate_history(history)
        prior = history[-1].invocation
        fallback = self.policy.fallback
        if (
            not isinstance(fallback, EvaluatedFallbackPolicy)
            or not isinstance(prior.outcome, (FailedInvocation, UndispatchedInvocation))
            or prior.outcome.error.error_class not in fallback.trigger_errors
        ):
            _reject(
                "model.fallback_not_permitted",
                "The actual failure does not allow a fallback.",
            )
        trigger = cast(TriggerError, prior.outcome.error.error_class)
        source = self.deployment_pins[prior.route_trace.selected_deployment_id]
        visited = {row.invocation.route_trace.selected_deployment_id for row in history}
        plans = []
        for edge in fallback.allowed_transitions:
            if (
                edge.from_deployment_id != source.deployment_id
                or edge.to_deployment_id in visited
            ):
                continue
            target = self.deployment_pins[edge.to_deployment_id]
            refs = self._evaluation_refs(source, target, trigger)
            if not refs:
                continue
            trace = FallbackProviderRoute(
                route_kind="fallback",
                routing_mode=self.policy.provider.routing_mode,
                selected_deployment_id=target.deployment_id,
                fallback_policy_ref=self.policy.fallback_registration.ref,
                from_deployment_id=source.deployment_id,
                to_deployment_id=target.deployment_id,
                trigger_error=trigger,
                trigger_invocation_ref=ModelInvocationRef(
                    record_kind=RuntimeRecordKind.MODEL_INVOCATION,
                    record_id=prior.model_invocation_id,
                ),
                evaluation_refs=refs,
                **self._exception_field(),
            )
            try:
                plans.append(self._plan(trace, history))
            except RouteRejected:
                continue
        if len(plans) > 1:
            _reject(
                "model.fallback_ambiguous",
                "More than one legal fallback edge is available.",
            )
        if not plans:
            _reject(
                "model.fallback_unavailable",
                "No legal, reviewed and currently available fallback edge remains.",
            )
        return plans[0]


def plan_primary(
    policy: RoutePolicy,
    operation: RouteOperation,
    attempt: AttemptDescriptor,
    current: CurrentRoutingState,
    budget: RouteBudget,
    at: datetime,
    *,
    nested_step_owner: NestedModelStepOwner | None = None,
) -> PlannedRoute:
    return _Routing(
        policy, operation, attempt, current, budget, at, nested_step_owner
    ).primary()


def plan_retry(
    policy: RoutePolicy,
    operation: RouteOperation,
    attempt: AttemptDescriptor,
    current: CurrentRoutingState,
    budget: RouteBudget,
    at: datetime,
    *,
    history: tuple[RecordedInvocation, ...],
    retryable_errors: tuple[TriggerError, ...],
    nested_step_owner: NestedModelStepOwner | None = None,
) -> PlannedRoute:
    return _Routing(
        policy, operation, attempt, current, budget, at, nested_step_owner
    ).retry(history, retryable_errors)


def plan_fallback(
    policy: RoutePolicy,
    operation: RouteOperation,
    attempt: AttemptDescriptor,
    current: CurrentRoutingState,
    budget: RouteBudget,
    at: datetime,
    *,
    history: tuple[RecordedInvocation, ...],
    nested_step_owner: NestedModelStepOwner | None = None,
) -> PlannedRoute:
    return _Routing(
        policy, operation, attempt, current, budget, at, nested_step_owner
    ).fallback(history)
