"""Finite relations over supplied invocation records and their pinned definitions."""

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import NoReturn

from jimu_runtime.domain.definition_enums import (
    FactScope,
    ModelCapability,
    ProducerKind,
)
from jimu_runtime.domain.definition_support import reference_identity
from jimu_runtime.domain.definitions.models import ModelBindingContentRef
from jimu_runtime.domain.enums import AttemptStatus, NodeRunStatus, RuntimeRecordKind
from jimu_runtime.domain.fact_schemas.registry import FactSchemaRegistry
from jimu_runtime.domain.fact_selectors import (
    ProducerCatalogueContentRef,
    ToolContentRef,
    ToolPlanContentRef,
)
from jimu_runtime.domain.policies import (
    AdditionalSelectionCallPolicy,
    DeploymentRequirement,
    EvaluatedFallbackPolicy,
    FallbackPolicyArtifact,
    LocalPrimaryProviderPolicy,
    ProviderPolicyArtifact,
    QuestionSelectionPolicyArtifact,
    ResourceRequirementsArtifact,
    SelectionCallPolicy,
)
from jimu_runtime.domain.primitives import ArtifactId, Digest, RecordId
from jimu_runtime.domain.producers import ProducerCatalogue, ToolStepProducer
from jimu_runtime.domain.references import ContentRef
from jimu_runtime.domain.runtime.applicability import ContextSnapshotRef
from jimu_runtime.domain.runtime.facts import (
    FactRecord,
    KnownFact,
    NodeRunRef,
    ObjectRef,
    ToolFactSource,
)
from jimu_runtime.domain.runtime.facts import (
    ProfileContentRef as RuntimeProfileContentRef,
)
from jimu_runtime.domain.runtime.invocations import (
    ActualProviderIdentity,
    FailedInvocation,
    FallbackProviderRoute,
    ModelInvocation,
    SucceededInvocation,
    UnavailableProviderIdentity,
    UndispatchedInvocation,
)
from jimu_runtime.domain.runtime.selection import (
    AllDeclaredSelectionRecord,
    ContextRuleSelectionRecord,
    DispatchedSelectionFailure,
    FallbackSelectionRecord,
    ModelAdvisorySelectionRecord,
    QuestionSelectionRecord,
)
from jimu_runtime.domain.runtime.tool_executions import (
    FailedToolResult,
    GroundingBindingOrigin,
    ReturnedToolAttempt,
    ToolExecution,
    ToolPlanStepOrigin,
)
from jimu_runtime.domain.schema_types import ToolResultSchemaDescriptor
from jimu_runtime.domain.tool_mappings import ProfileContentRef, RegionContentRef
from jimu_runtime.domain.validation.authority_inputs import (
    AttemptDescriptor,
    NodeAttemptOwner,
    NodeRunDescriptor,
    ObjectDescriptor,
)
from jimu_runtime.domain.validation.definition_inputs import (
    ProfileRegistration,
    ToolPlanRegistration,
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
)
from jimu_runtime.domain.validation.tool_plans import ToolRegistration
from jimu_runtime.domain.values import RecordValue, Value, validate_typed_value


def _fail(message: str) -> NoReturn:
    raise ValueError(message)


def _same_ref(left: ContentRef, right: ContentRef) -> bool:
    return reference_identity(left) == reference_identity(right)


def validate_attempt_owner(
    *,
    execution_id: str,
    node_run_id: str,
    node_id: str,
    attempt_id: str,
    node_run: NodeRunDescriptor,
    attempt: AttemptDescriptor,
) -> None:
    if (
        node_run.execution_id != execution_id
        or node_run.node_run_ref.record_id != node_run_id
        or node_run.node_id != node_id
        or attempt.execution_id != execution_id
        or attempt.attempt_ref.record_id != attempt_id
        or not isinstance(attempt.owner, NodeAttemptOwner)
        or attempt.owner.node_run_ref != node_run.node_run_ref
    ):
        _fail("invocation must resolve the exact execution, node and actual Attempt")


def _one[T](items: Iterable[T], message: str) -> T:
    matches = iter(items)
    try:
        result = next(matches)
    except StopIteration:
        _fail(message)
    if next(matches, None) is not None:
        _fail(message)
    return result


def _model_index(
    invocations: tuple[ModelInvocation, ...],
    execution_id: str,
) -> dict[str, ModelInvocation]:
    result: dict[str, ModelInvocation] = {}
    for item in invocations:
        if item.execution_id != execution_id:
            continue
        if item.model_invocation_id in result:
            _fail("duplicate model invocation ID in supplied execution")
        result[item.model_invocation_id] = item
    return result


def _model_by_id(
    invocations: Mapping[str, ModelInvocation],
    invocation_id: str,
) -> ModelInvocation:
    item = invocations.get(invocation_id)
    if item is None:
        _fail("missing model invocation in supplied execution")
    return item


@dataclass(frozen=True, slots=True)
class NestedModelStepLineage:
    """Exact scope of a Tool's nested model operation, derived from durable records."""

    execution_id: RecordId
    parent_node_run_ref: NodeRunRef
    tool_plan_ref: ToolPlanContentRef
    step_id: ArtifactId
    tool_ref: ToolContentRef
    profile_ref: RuntimeProfileContentRef
    object_ref: ObjectRef
    binding_ref: ModelBindingContentRef
    bound_input_digest: Digest


@dataclass(frozen=True, slots=True)
class NestedModelStepOwner:
    """One actual child and its parent Attempt. These inputs are not a wire record."""

    lineage: NestedModelStepLineage
    parent_attempt: AttemptDescriptor
    child_node: NodeRunDescriptor
    child_attempt: AttemptDescriptor


@dataclass(frozen=True, slots=True)
class NestedModelStepFallbackWitness:
    prior: NestedModelStepOwner
    current: NestedModelStepOwner


def validate_nested_model_step_owner(
    owner: NestedModelStepOwner,
    *,
    live: bool,
) -> None:
    """Check lifecycle and ownership of a worker-supplied child scope."""
    parent, node, child = owner.parent_attempt, owner.child_node, owner.child_attempt
    lineage = owner.lineage
    valid_child = (
        {AttemptStatus.RUNNING}
        if live
        else {AttemptStatus.FAILED, AttemptStatus.TIMED_OUT}
    )
    if (
        node.node_id != "N05"
        or node.status is not (NodeRunStatus.RUNNING if live else NodeRunStatus.FAILED)
        or node.node_run_ref == lineage.parent_node_run_ref
        or parent.execution_id != lineage.execution_id
        or not isinstance(parent.owner, NodeAttemptOwner)
        or parent.owner.node_run_ref != lineage.parent_node_run_ref
        or parent.attempt_ref == child.attempt_ref
        or parent.status not in valid_child
        or child.status not in valid_child
        or child.attempt_number != 1
    ):
        _fail("nested model step requires exact N05 parent and child Attempt ownership")
    validate_attempt_owner(
        execution_id=lineage.execution_id,
        node_run_id=node.node_run_ref.record_id,
        node_id="N05",
        attempt_id=child.attempt_ref.record_id,
        node_run=node,
        attempt=child,
    )


def _validate_nested_model_handoff(
    witness: NestedModelStepFallbackWitness,
    prior: ModelInvocation,
    invocation: ModelInvocation,
    node_run: NodeRunDescriptor,
    attempt: AttemptDescriptor,
) -> None:
    earlier, current = witness.prior, witness.current
    validate_nested_model_step_owner(earlier, live=False)
    validate_nested_model_step_owner(current, live=True)
    lineage = current.lineage
    if (
        earlier.lineage != lineage
        or current.child_node != node_run
        or current.child_attempt != attempt
        or earlier.child_node.node_run_ref == current.child_node.node_run_ref
        or prior.trace_id != invocation.trace_id
        or earlier.parent_attempt.attempt_ref == current.parent_attempt.attempt_ref
        or earlier.parent_attempt.attempt_number
        >= current.parent_attempt.attempt_number
        or any(
            item.execution_id != lineage.execution_id
            or item.node_id != "N05"
            or item.node_run_ref != owner.child_node.node_run_ref
            or item.attempt_ref != owner.child_attempt.attempt_ref
            or item.configured_provider.binding_ref != lineage.binding_ref
            or item.purposes != (ModelCapability.VISION_AUXILIARY,)
            or item.profile_refs != (lineage.profile_ref,)
            or item.object_ref != lineage.object_ref
            for item, owner in ((prior, earlier), (invocation, current))
        )
    ):
        _fail(
            "nested model fallback must follow an earlier failed child of the same N05 step"
        )


def validate_model_invocation(
    invocation: ModelInvocation,
    *,
    node_run: NodeRunDescriptor,
    attempt: AttemptDescriptor,
    binding: ModelBindingRegistration,
    provider_policy: PolicyRegistration,
    fallback_policy: PolicyRegistration,
    prior_invocations: tuple[ModelInvocation, ...] = (),
    reviews: tuple[ReviewMetadata, ...] = (),
    region_ref: RegionContentRef | None = None,
    resolved_deployments: tuple[ReviewedDeployment, ...] = (),
    nested_step_fallback: NestedModelStepFallbackWitness | None = None,
) -> None:
    """Check route evidence. Resolved Execution pins only witness review endpoints."""
    validate_attempt_owner(
        execution_id=invocation.execution_id,
        node_run_id=invocation.node_run_ref.record_id,
        node_id=invocation.node_id,
        attempt_id=invocation.attempt_ref.record_id,
        node_run=node_run,
        attempt=attempt,
    )
    configured = invocation.configured_provider
    route = invocation.route_trace
    definition = binding.definition
    if nested_step_fallback is not None and (
        not isinstance(route, FallbackProviderRoute)
        or definition.capability is not ModelCapability.VISION_AUXILIARY
    ):
        _fail("nested model step witness only applies to an N05 auxiliary fallback")
    if (
        not _same_ref(configured.binding_ref, binding.ref)
        or not _same_ref(configured.provider_policy_ref, definition.provider_policy_ref)
        or not _same_ref(configured.provider_policy_ref, provider_policy.ref)
        or not _same_ref(route.fallback_policy_ref, definition.fallback_policy_ref)
        or not _same_ref(route.fallback_policy_ref, fallback_policy.ref)
        or configured.logical_model_id != definition.logical_model_id
        or not isinstance(provider_policy.artifact, ProviderPolicyArtifact)
        or not isinstance(fallback_policy.artifact, FallbackPolicyArtifact)
    ):
        _fail(
            "configured provider audit must match the pinned binding and typed policies"
        )
    allowed = {definition.capability}
    if definition.capability is ModelCapability.PROFILE_DISCOVERY:
        if set(invocation.purposes) - {
            ModelCapability.PROFILE_DISCOVERY,
            "optional_question_proposal",
        }:
            _fail("invocation purposes must match the configured binding capability")
    elif set(invocation.purposes) != allowed:
        _fail("invocation purposes must match the configured binding capability")
    if invocation.contract_snapshot is not None and (
        invocation.contract_snapshot.configured_output_schema_ref
        != definition.output_schema_ref
    ):
        _fail("joint response snapshot must pin the configured model output schema")
    provider = provider_policy.artifact.policy
    if (
        route.routing_mode != provider.routing_mode
        or configured.primary_deployment_id != provider.primary_deployment_id
    ):
        _fail(
            "routing mode and primary deployment must match the pinned provider policy"
        )
    deployments = {item.deployment_id: item for item in provider.deployments}
    primary = deployments[provider.primary_deployment_id]
    pinned = {identity.deployment_id: identity for identity in resolved_deployments}
    if resolved_deployments and (
        len(pinned) != len(resolved_deployments)
        or pinned.keys() != deployments.keys()
        or any(
            not _deployment_matches(identity, deployments[deployment_id], definition)
            for deployment_id, identity in pinned.items()
        )
    ):
        _fail("resolved deployment pins must match the complete configured binding")
    if (primary.provider_id, primary.provider_kind, primary.logical_model_id) != (
        configured.provider_id,
        configured.provider_kind,
        configured.logical_model_id,
    ):
        _fail("configured provider identity does not match the pinned primary")
    if isinstance(provider, LocalPrimaryProviderPolicy):
        exception = provider.local_primary_exception
        identity = route.local_primary_exception
        if (
            identity is None
            or (identity.exception_id, identity.version, identity.approval_ref)
            != (exception.exception_id, exception.version, exception.approval_ref)
            or definition.capability not in exception.applicable_capabilities
            or not exception.valid_from <= invocation.started_at < exception.valid_until
        ):
            _fail(
                "local primary exception identity, capability and validity must agree"
            )
        review = _one(
            (
                review
                for review in reviews
                if review.report_ref == exception.approval_ref
            ),
            "local primary requires its exact approval review",
        )
        if not _active_review(review, invocation) or not any(
            isinstance(assessment, LocalExceptionAssessment)
            and (
                assessment.exception_id,
                assessment.exception_version,
                assessment.reason,
            )
            == (exception.exception_id, exception.version, exception.reason)
            and definition.capability in assessment.applicable_capabilities
            and _scope_matches(assessment, invocation, region_ref)
            and _review_deployment_matches(
                assessment.primary,
                primary,
                invocation.actual_provider
                if not isinstance(route, FallbackProviderRoute)
                else None,
                pinned.get(primary.deployment_id),
            )
            for assessment in review.assessments
        ):
            _fail(
                "local primary review does not cover exact deployment, capability and scope"
            )
    selected = deployments.get(route.selected_deployment_id)
    actual = invocation.actual_provider
    if selected is None:
        _fail("selected model deployment is unregistered")
    if actual is not None:
        _validate_actual_deployment(
            actual, selected.provider_id, selected.provider_kind
        )
        _validate_observed_pin(actual, pinned.get(selected.deployment_id))
        if not isinstance(actual, UnavailableProviderIdentity) and (
            actual.model_version
            not in selected.actual_version_constraints.allowed_model_versions
            or actual.deployment_version
            not in selected.actual_version_constraints.allowed_deployment_versions
        ):
            _fail(
                "actual model and deployment versions must obey the pinned constraints"
            )
    if isinstance(route, FallbackProviderRoute):
        fallback = fallback_policy.artifact.policy
        if not isinstance(fallback, EvaluatedFallbackPolicy) or (
            route.trigger_error not in fallback.trigger_errors
            or not any(
                (edge.from_deployment_id, edge.to_deployment_id)
                == (route.from_deployment_id, route.to_deployment_id)
                for edge in fallback.allowed_transitions
            )
            or not set(ref.stable_json() for ref in route.evaluation_refs)
            <= {ref.stable_json() for ref in fallback.evaluation_refs}
        ):
            _fail("fallback trace must match an exact evaluated transition and trigger")
        prior = _model_by_id(
            _model_index(prior_invocations, invocation.execution_id),
            route.trigger_invocation_ref.record_id,
        )
        if (
            not isinstance(prior.outcome, (FailedInvocation, UndispatchedInvocation))
            or prior.outcome.error.error_class != route.trigger_error
            or prior.route_trace.selected_deployment_id != route.from_deployment_id
            or (
                nested_step_fallback is None
                and prior.node_run_ref != invocation.node_run_ref
            )
            or prior.attempt_ref == invocation.attempt_ref
            or prior.configured_provider != configured
            or set(prior.purposes) != set(invocation.purposes)
            or prior.profile_refs != invocation.profile_refs
            or prior.object_ref != invocation.object_ref
            or prior.source_discovery_invocation_ref
            != invocation.source_discovery_invocation_ref
            or prior.completed_at > invocation.started_at
        ):
            _fail(
                "fallback trigger must resolve a distinct earlier failed Attempt of the same operation"
            )
        if nested_step_fallback is not None:
            _validate_nested_model_handoff(
                nested_step_fallback, prior, invocation, node_run, attempt
            )
        source = deployments.get(route.from_deployment_id)
        if source is None:
            _fail("fallback source is not registered")
        source_pin = pinned.get(source.deployment_id)
        _validate_observed_pin(prior.actual_provider, source_pin)
        for reference in route.evaluation_refs:
            review = _one(
                (review for review in reviews if review.report_ref == reference),
                "fallback requires its exact independent evaluation",
            )
            if not _active_review(review, invocation) or not any(
                isinstance(assessment, FallbackTransitionAssessment)
                and assessment.capability is definition.capability
                and route.trigger_error in assessment.trigger_errors
                and assessment.input_schema_ref == definition.input_schema_ref
                and assessment.output_schema_ref == definition.output_schema_ref
                and _scope_matches(assessment, invocation, region_ref)
                and _review_deployment_matches(
                    assessment.source, source, prior.actual_provider, source_pin
                )
                and _review_deployment_matches(
                    assessment.target,
                    selected,
                    actual,
                    pinned.get(selected.deployment_id),
                )
                for assessment in review.assessments
            ):
                _fail(
                    "fallback review does not cover exact transition, contracts and scope"
                )


def _active_review(review: ReviewMetadata, invocation: ModelInvocation) -> bool:
    return (
        review.verdict == "approved"
        and review.valid_from <= invocation.started_at < review.valid_until
    )


def _scope_matches(
    assessment: FallbackTransitionAssessment | LocalExceptionAssessment,
    invocation: ModelInvocation,
    region_ref: RegionContentRef | None,
) -> bool:
    return all(
        _scope_covers(
            assessment.scope,
            invocation.configured_provider.binding_ref,
            ProfileContentRef.model_validate(profile.model_dump())
            if profile is not None
            else None,
            region_ref,
        )
        is True
        for profile in invocation.profile_refs or (None,)
    )


def _review_deployment_matches(
    reviewed: ReviewedDeployment,
    requirement: DeploymentRequirement,
    actual: ActualProviderIdentity | None,
    pinned: ReviewedDeployment | None,
) -> bool:
    witness = (
        pinned
        if isinstance(actual, UnavailableProviderIdentity) or actual is None
        else actual
    )
    return (
        (
            reviewed.provider_id,
            reviewed.deployment_id,
            reviewed.provider_kind,
            reviewed.logical_model_id,
        )
        == (
            requirement.provider_id,
            requirement.deployment_id,
            requirement.provider_kind,
            requirement.logical_model_id,
        )
        and _review_version_matches(
            reviewed.deployment_version,
            requirement.actual_version_constraints.allowed_deployment_versions,
            witness.deployment_version if witness is not None else None,
        )
        and _review_version_matches(
            reviewed.model_version,
            requirement.actual_version_constraints.allowed_model_versions,
            witness.model_version if witness is not None else None,
        )
    )


def _review_version_matches(
    reviewed: str, allowed: tuple[str, ...], witness: str | None
) -> bool:
    if witness is None:
        return allowed == (reviewed,)
    return reviewed == witness and reviewed in allowed


def _validate_observed_pin(
    actual: ActualProviderIdentity | None,
    pinned: ReviewedDeployment | None,
) -> None:
    if actual is None or pinned is None:
        return
    if (actual.provider_id, actual.provider_kind, actual.deployment_id) != (
        pinned.provider_id,
        pinned.provider_kind,
        pinned.deployment_id,
    ) or (
        not isinstance(actual, UnavailableProviderIdentity)
        and (actual.deployment_version, actual.model_version)
        != (pinned.deployment_version, pinned.model_version)
    ):
        _fail(
            "observed provider identity must agree with the exact resolved deployment pin"
        )


def _validate_actual_deployment(
    actual: ActualProviderIdentity,
    provider_id: str,
    provider_kind: str,
) -> None:
    if (actual.provider_id, actual.provider_kind) != (provider_id, provider_kind):
        _fail("actual provider identity must match selected deployment")


def _check_selection_invocation(
    record: QuestionSelectionRecord,
    invocation: ModelInvocation,
    discovery: ModelInvocation,
    call: SelectionCallPolicy,
    attempt: AttemptDescriptor,
    node_run: NodeRunDescriptor,
    ledger: Mapping[str, ModelInvocation],
) -> None:
    validate_attempt_owner(
        execution_id=record.execution_id,
        node_run_id=record.node_run_id,
        node_id="N02",
        attempt_id=invocation.attempt_ref.record_id,
        node_run=node_run,
        attempt=attempt,
    )
    if (
        invocation.node_id != "N02"
        or invocation.node_run_ref != discovery.node_run_ref
        or record.profile_ref not in invocation.profile_refs
        or "optional_question_proposal" not in invocation.purposes
        or isinstance(invocation.outcome, UndispatchedInvocation)
        or invocation.completed_at > record.created_at
    ):
        _fail("proposal requires an actual associated N02 invocation")
    if isinstance(call, AdditionalSelectionCallPolicy):
        if (
            invocation.model_invocation_id == discovery.model_invocation_id
            or invocation.attempt_ref == discovery.attempt_ref
            or invocation.source_discovery_invocation_ref
            != record.discovery_model_invocation_ref
            or not _same_ref(
                invocation.configured_provider.binding_ref, call.proposal_binding_ref
            )
            or invocation.started_at < discovery.completed_at
            or tuple(invocation.profile_refs) != (record.profile_ref,)
        ):
            _fail(
                "additional proposal needs separate Attempt, exact binding and source discovery"
            )
        admitted_ids = {
            item.model_invocation_id
            for item in ledger.values()
            if item.source_discovery_invocation_ref is not None
            and _same_ref(
                item.configured_provider.binding_ref, call.proposal_binding_ref
            )
            and not isinstance(item.outcome, UndispatchedInvocation)
        }
        if len(admitted_ids) > call.max_additional_invocations_per_execution:
            _fail(
                "supplied additional proposal ledger exceeds the execution call budget"
            )
    elif invocation.model_invocation_id != discovery.model_invocation_id:
        _fail("shared proposal must use the discovery invocation")


def validate_question_selection_record(
    record: QuestionSelectionRecord,
    *,
    profile: ProfileRegistration,
    policy: PolicyRegistration,
    context_snapshot_ref: ContextSnapshotRef,
    context_snapshot_digest: Digest,
    node_run: NodeRunDescriptor,
    attempts: tuple[AttemptDescriptor, ...],
    invocations: tuple[ModelInvocation, ...],
    trusted_discovery_hit_invocation_id: str,
    additional_binding: ModelBindingRegistration | None = None,
    additional_resources: PolicyRegistration | None = None,
) -> None:
    """Verify scoped audit with a trusted discovery hit and caller-complete invocation ledger."""
    if not isinstance(policy.artifact, QuestionSelectionPolicyArtifact) or (
        not _same_ref(record.profile_ref, profile.ref)
        or not _same_ref(
            record.policy_ref, profile.definition.question_selection_policy_ref
        )
        or not _same_ref(record.policy_ref, policy.ref)
        or record.configured_question_refs != profile.definition.question_refs
        or record.context_snapshot_ref != context_snapshot_ref
        or record.context_snapshot_digest != context_snapshot_digest
    ):
        _fail(
            "selection record must use the exact pinned Profile, policy, catalogue and context"
        )
    selection_policy = policy.artifact.policy
    if {reference_identity(ref) for ref in record.mandatory_question_refs} != {
        reference_identity(ref) for ref in selection_policy.mandatory_question_refs
    } or {reference_identity(ref) for ref in record.configured_question_refs} != {
        reference_identity(ref)
        for ref in (
            *selection_policy.mandatory_question_refs,
            *selection_policy.optional_question_refs,
        )
    }:
        _fail("record partitions must agree with the pinned selection policy")
    attempts_by_id = {
        attempt.attempt_ref.record_id: attempt
        for attempt in attempts
        if attempt.execution_id == record.execution_id
    }
    if len(attempts_by_id) != sum(
        attempt.execution_id == record.execution_id for attempt in attempts
    ):
        _fail("duplicate Attempt ID in supplied execution")

    owning_attempt = attempts_by_id.get(record.attempt_id)
    if owning_attempt is None:
        _fail("missing owning N02 Attempt")
    validate_attempt_owner(
        execution_id=record.execution_id,
        node_run_id=record.node_run_id,
        node_id="N02",
        attempt_id=record.attempt_id,
        node_run=node_run,
        attempt=owning_attempt,
    )
    ledger = _model_index(invocations, record.execution_id)
    discovery = _model_by_id(ledger, record.discovery_model_invocation_ref.record_id)
    if (
        discovery.model_invocation_id != trusted_discovery_hit_invocation_id
        or discovery.node_id != "N02"
        or discovery.node_run_ref != node_run.node_run_ref
        or discovery.attempt_ref != owning_attempt.attempt_ref
        or ModelCapability.PROFILE_DISCOVERY not in discovery.purposes
        or record.profile_ref not in discovery.profile_refs
        or not _same_ref(
            discovery.configured_provider.binding_ref,
            profile.definition.discovery_binding_ref,
        )
        or not isinstance(discovery.outcome, SucceededInvocation)
        or discovery.completed_at > record.created_at
    ):
        _fail(
            "selection requires the actual trusted discovery hit for this Profile and Attempt"
        )
    if isinstance(record, AllDeclaredSelectionRecord):
        if selection_policy.optional_source != "all":
            _fail("all_declared selection requires the pinned all policy")
        return
    if isinstance(record, ContextRuleSelectionRecord):
        if selection_policy.context_selection_rules != record.context_selection_rules:
            _fail("context rule trace must match the pinned rules")
        return
    call = selection_policy.selection_call_policy
    extra_call_occurred = isinstance(record, ModelAdvisorySelectionRecord) or (
        isinstance(record, FallbackSelectionRecord)
        and isinstance(record.fallback_evidence, DispatchedSelectionFailure)
    )
    if (
        extra_call_occurred
        and isinstance(call, AdditionalSelectionCallPolicy)
        and (
            additional_binding is None
            or not _same_ref(additional_binding.ref, call.proposal_binding_ref)
            or additional_resources is None
            or not isinstance(
                additional_resources.artifact, ResourceRequirementsArtifact
            )
            or not _same_ref(
                additional_resources.ref, call.additional_resource_requirements_ref
            )
            or additional_binding.definition.capability
            is not ModelCapability.PROFILE_DISCOVERY
        )
    ):
        _fail("additional proposal must resolve its pinned binding and resource budget")
    if isinstance(record, ModelAdvisorySelectionRecord):
        if call is None or record.selection_invocation_mode != call.mode:
            _fail("model advisory invocation mode must match its policy")
        proposal = _model_by_id(ledger, record.model_invocation_ref.record_id)
        proposal_attempt = attempts_by_id.get(proposal.attempt_ref.record_id)
        if (
            not isinstance(proposal.outcome, SucceededInvocation)
            or proposal_attempt is None
        ):
            _fail(
                "accepted advisory choice requires a successful proposal and actual Attempt"
            )
        _check_selection_invocation(
            record, proposal, discovery, call, proposal_attempt, node_run, ledger
        )
    elif isinstance(record, FallbackSelectionRecord):
        evidence = record.fallback_evidence
        if (
            evidence.triggered_method != selection_policy.optional_source
            or record.selection_call_policy != call
            or record.fallback_action != selection_policy.fallback
        ):
            _fail(
                "fallback method, call policy and action must agree with the pinned selection policy"
            )
        trigger = attempts_by_id.get(evidence.trigger_attempt_ref.record_id)
        if trigger is None:
            _fail("missing failed selection operation Attempt")
        validate_attempt_owner(
            execution_id=record.execution_id,
            node_run_id=record.node_run_id,
            node_id="N02",
            attempt_id=evidence.trigger_attempt_ref.record_id,
            node_run=node_run,
            attempt=trigger,
        )
        if isinstance(evidence, DispatchedSelectionFailure):
            if call is None:
                _fail("dispatched fallback requires configured model selection")
            proposal = _model_by_id(ledger, evidence.trigger_invocation_ref.record_id)
            _check_selection_invocation(
                record, proposal, discovery, call, trigger, node_run, ledger
            )
            if evidence.fallback_reason == "invalid_proposal":
                if (
                    not isinstance(proposal.outcome, SucceededInvocation)
                    or not record.validation_issues
                ):
                    _fail(
                        "invalid proposal fallback must preserve successful transport and validation issues"
                    )
            elif not isinstance(
                proposal.outcome, FailedInvocation
            ) or proposal.outcome.error.error_class not in {
                "transport",
                "timeout",
                "rate_limit",
                "unavailable",
            }:
                _fail("transport fallback requires a matching failed model invocation")
        elif evidence.fallback_reason == "selection_transport_failure" and (
            trigger.attempt_ref == discovery.attempt_ref
            or trigger.status not in {AttemptStatus.FAILED, AttemptStatus.TIMED_OUT}
        ):
            _fail(
                "undispatched selection transport failure must preserve the failed operation"
            )


def _payload_at(payload: Value | None, path: tuple[str, ...]) -> Value | None:
    for name in path:
        if not isinstance(payload, RecordValue):
            return None
        payload = payload.value.get(name)
    return payload


def _report_fact_outputs(
    execution: ToolExecution,
    facts: tuple[FactRecord, ...],
    output_values: Mapping[str, Value | None],
    output_states: Mapping[str, str],
    producer_catalogue_ref: ProducerCatalogueContentRef,
    catalogue: ProducerCatalogue,
) -> None:
    origin = execution.invocation_origin
    if isinstance(origin, GroundingBindingOrigin):
        _fail("grounding cannot resolve Tool Fact producers")
    references = {ref.record_id for ref in execution.generated_fact_refs}
    facts_by_id = {fact.fact_id: fact for fact in facts}
    if len(facts_by_id) != len(facts) or set(facts_by_id) != references:
        _fail("generated Tool Fact references must resolve exactly")
    selected = {
        item.declaration_id: item
        for item in catalogue.declarations
        if isinstance(item.producer, ToolStepProducer)
        and item.profile_ref is not None
        and _same_ref(item.profile_ref, execution.profile_ref)
        and item.producer.tool_plan_ref == origin.tool_plan_ref
        and item.producer.step_id == origin.step_id
        and item.producer.tool_ref == execution.tool_ref
    }
    if set(origin.producer_declaration_ids) != set(selected):
        _fail(
            "Tool origin must pin exactly its registered output producer declarations"
        )
    if len({fact.producer_declaration_id for fact in facts}) != len(facts):
        _fail("Tool Attempt must not emit duplicate Facts for an output")
    for fact in facts:
        source = fact.source
        declaration = selected.get(fact.producer_declaration_id)
        if (
            declaration is None
            or not isinstance(source, ToolFactSource)
            or source.tool_execution_ref.record_id != execution.tool_execution_id
            or source.tool_plan_ref != origin.tool_plan_ref
            or source.step_id != origin.step_id
            or source.tool_ref != execution.tool_ref
            or source.output_name != declaration.output_name
            or fact.execution_id != execution.execution_id
            or fact.profile_ref != execution.profile_ref
            or execution.object_ref is None
            or fact.object_id != execution.object_ref.record_id
            or fact.attempt_id != execution.attempt_ref.record_id
            or fact.producer_catalogue_ref != producer_catalogue_ref
            or fact.fact_key != declaration.fact_key
            or fact.fact_schema_ref != declaration.fact_schema_ref
            or fact.status.value != output_states.get(source.output_name)
        ):
            _fail(
                "Tool Fact must agree with its execution, declared step and named result"
            )
        if isinstance(fact, KnownFact) and fact.value != output_values.get(
            source.output_name
        ):
            _fail("known Tool Fact must match the validated named output payload")


def _conflicting_tool_peer(execution: ToolExecution, peer: ToolExecution) -> bool:
    if peer.execution_id != execution.execution_id:
        return False
    if peer.tool_execution_id == execution.tool_execution_id:
        return True
    if peer.attempt_ref != execution.attempt_ref:
        return False
    origin, other = execution.invocation_origin, peer.invocation_origin
    if peer.node_run_ref != execution.node_run_ref or origin.node_id != other.node_id:
        return True
    if isinstance(origin, GroundingBindingOrigin):
        return _same_ref(execution.profile_ref, peer.profile_ref)
    assert isinstance(other, ToolPlanStepOrigin)
    return (
        not _same_ref(execution.profile_ref, peer.profile_ref)
        or execution.object_ref != peer.object_ref
        or origin.tool_plan_ref != other.tool_plan_ref
        or origin.step_id == other.step_id
    )


def validate_tool_execution(
    execution: ToolExecution,
    *,
    node_run: NodeRunDescriptor,
    attempt: AttemptDescriptor,
    profile: ProfileRegistration,
    tool: ToolRegistration,
    object_record: ObjectDescriptor | None = None,
    plan: ToolPlanRegistration | None = None,
    output_schema: ToolResultSchemaDescriptor | None = None,
    producer_catalogue_ref: ProducerCatalogueContentRef | None = None,
    producer_catalogue: ProducerCatalogue | None = None,
    fact_schemas: FactSchemaRegistry | None = None,
    generated_facts: tuple[FactRecord, ...] = (),
    peer_executions: tuple[ToolExecution, ...] = (),
    nested_invocations: tuple[ModelInvocation, ...] = (),
) -> None:
    """Check supplied parents, exact binding/plan, named outputs and emitted Fact associations."""
    origin = execution.invocation_origin
    validate_attempt_owner(
        execution_id=execution.execution_id,
        node_run_id=execution.node_run_ref.record_id,
        node_id=origin.node_id,
        attempt_id=execution.attempt_ref.record_id,
        node_run=node_run,
        attempt=attempt,
    )
    if not _same_ref(execution.profile_ref, profile.ref) or not _same_ref(
        execution.tool_ref, tool.ref
    ):
        _fail("Tool invocation must use the supplied pinned Profile and Tool")
    nested_ids = {
        output.artifact_ref.record_id
        for output in execution.output_refs
        if output.artifact_ref.record_kind is RuntimeRecordKind.MODEL_INVOCATION
    }
    nested_index = _model_index(nested_invocations, execution.execution_id)
    if nested_ids != set(nested_index) or any(
        tool.definition.model_binding_ref is None
        or not _same_ref(
            tool.definition.model_binding_ref, nested.configured_provider.binding_ref
        )
        or nested.node_id != origin.node_id
        or nested.node_run_ref != execution.node_run_ref
        or nested.attempt_ref != execution.attempt_ref
        or nested.profile_refs != (execution.profile_ref,)
        or nested.object_ref != execution.object_ref
        or nested.started_at < execution.started_at
        or nested.completed_at > execution.completed_at
        for nested in nested_index.values()
    ):
        _fail(
            "nested Model Gateway calls must resolve with the exact Tool binding and Attempt"
        )
    actual = execution.actual_implementation_version
    if (
        actual is not None
        and actual
        not in tool.definition.implementation_requirement.allowed_implementation_versions
    ):
        _fail("actual Tool implementation is outside the pinned allowlist")
    if any(_conflicting_tool_peer(execution, peer) for peer in peer_executions):
        _fail("Tool invocation identity or Attempt source is duplicated or mismatched")
    result = (
        execution.attempt_result.result
        if isinstance(execution.attempt_result, ReturnedToolAttempt)
        else None
    )
    payload = (
        result.payload
        if result is not None and not isinstance(result, FailedToolResult)
        else None
    )
    if result is not None:
        if (
            output_schema is None
            or output_schema.schema_ref != tool.definition.output_schema_ref
        ):
            _fail("normalized Tool result must resolve its pinned schema")
        if payload is not None:
            validate_typed_value(output_schema.payload_type, payload)
        evidence_slots = {item.name: item for item in result.evidence}
        declared_slots = {item.name: item for item in output_schema.evidence_fields}
        if not set(evidence_slots) <= set(declared_slots):
            _fail("undeclared Tool evidence slot")
        for name, evidence_declaration in declared_slots.items():
            slot = evidence_slots.get(name)
            if (
                slot is None
                and evidence_declaration.required
                and result.status == "succeeded"
            ):
                _fail("successful result omitted a required Tool evidence slot")
            if (
                slot is not None
                and not evidence_declaration.multiple
                and len(slot.evidence_refs) != 1
            ):
                _fail("single Tool evidence slot cannot hold multiple references")
    if isinstance(origin, GroundingBindingOrigin):
        if (
            origin.grounding_binding != profile.definition.grounding_binding
            or generated_facts
        ):
            _fail(
                "grounding audit must match the pinned Profile Tool binding without Tool Facts"
            )
        return
    if (
        object_record is None
        or object_record.object_ref != execution.object_ref
        or object_record.execution_id != execution.execution_id
        or object_record.profile_ref != execution.profile_ref
        or plan is None
        or not _same_ref(origin.tool_plan_ref, profile.definition.tool_plan_ref)
        or not _same_ref(origin.tool_plan_ref, plan.ref)
    ):
        _fail(
            "Tool Plan step audit must match the exact Profile plan and locally owned Object"
        )
    step = _one(
        (step for step in plan.definition.steps if step.step_id == origin.step_id),
        "Tool Plan step must resolve exactly",
    )
    if step.tool_ref != execution.tool_ref:
        _fail("Tool Plan step does not bind the exact Tool")
    if (
        producer_catalogue is None
        or producer_catalogue_ref is None
        or (producer_catalogue_ref.artifact_id, producer_catalogue_ref.version)
        != (producer_catalogue.catalogue_id, producer_catalogue.version)
    ):
        _fail("Tool Plan step audit must resolve the pinned producer catalogue")
    mappings = {
        item.output_name: item.target_fact_key for item in step.fact_output_mappings
    }
    declarations = {item.output_name: item for item in tool.definition.fact_outputs}
    producers = {
        item.output_name: item
        for item in producer_catalogue.declarations
        if item.declaration_id in origin.producer_declaration_ids
    }
    if set(producers) != set(mappings) or any(
        name not in declarations
        or producers[name].fact_key != target
        or declarations[name].fact_key != target
        or producers[name].fact_schema_ref != declarations[name].fact_schema_ref
        for name, target in mappings.items()
    ):
        _fail("producer declarations and Tool step mappings must agree")
    states = (
        {item.output_name: item.fact_status.value for item in result.output_states}
        if result is not None
        else {}
    )
    output_values: dict[str, Value | None] = {}
    if result is not None:
        if set(states) - set(declarations) or (
            not isinstance(result, FailedToolResult)
            and not set(mappings) <= set(states)
        ):
            _fail("normalized Tool result must contain unique mapped output states")
        assert output_schema is not None
        contract_outputs = {
            item.output_name: item for item in output_schema.output_declarations
        }
        for name in states:
            declaration = declarations[name]
            output = contract_outputs.get(name)
            if (
                output is None
                or output.value_path != declaration.value_path
                or output.required != declaration.required
            ):
                _fail(
                    "named Tool output definition must match the pinned result schema"
                )
            value = _payload_at(payload, declaration.value_path)
            if (states[name] == "known") != (value is not None):
                _fail("only a known Tool output may have its declared value path")
            if (
                result.status == "succeeded"
                and declaration.required
                and states[name] == "error"
            ):
                _fail("successful Tool result cannot hide a required output error")
            if value is not None:
                if fact_schemas is None:
                    _fail("known Tool output requires its exact Fact Schema")
                fact_type = fact_schemas.resolve(declaration.fact_schema_ref)
                if (
                    fact_type.scope is not FactScope.OBJECT
                    or ProducerKind.TOOL not in fact_type.allowed_producer_kinds
                    or fact_type.semantic_key != declaration.fact_key
                ):
                    _fail(
                        "known Tool output requires a Fact Schema compatible with the producer declaration"
                    )
                output_values[name] = fact_schemas.validate_payload(
                    declaration.fact_schema_ref, value
                )
    _report_fact_outputs(
        execution,
        generated_facts,
        output_values,
        states,
        producer_catalogue_ref,
        producer_catalogue,
    )
