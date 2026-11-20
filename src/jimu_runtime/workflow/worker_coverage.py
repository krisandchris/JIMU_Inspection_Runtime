"""Aggregate selected workflow records before atomic finalization."""

from dataclasses import dataclass, field
from datetime import datetime

from jimu_runtime.bundle.producer_rules import producer_activation_contexts
from jimu_runtime.domain.definition_enums import FactScope
from jimu_runtime.domain.definition_support import reference_identity
from jimu_runtime.domain.definitions.capabilities import ProfileDefinition
from jimu_runtime.domain.enums import ArtifactKind, DecisionResult, NodeRunStatus
from jimu_runtime.domain.fact_schemas.canonicalization import canonical_bytes
from jimu_runtime.domain.fact_selectors import ProducerCatalogueContentRef
from jimu_runtime.domain.nodes.context import SuccessfulContextBuild
from jimu_runtime.domain.nodes.discovery import DiscoveryAttemptOutput
from jimu_runtime.domain.policies import EvidencePolicyArtifact, RulePolicyArtifact
from jimu_runtime.domain.producers import (
    AlwaysActivation,
    ProducerCatalogue,
    SystemProducer,
    ToolStepProducer,
)
from jimu_runtime.domain.rules.definitions import RuleDefinition, RuleSetDefinition
from jimu_runtime.domain.runtime.applicability import (
    ApplicabilityDiagnosticRef,
    ApplicabilityEvaluationStatus,
    ApplicabilityLevel,
    ApplicabilityReason,
    ApplicabilityTrace,
    EvaluatedApplicabilityLevel,
    NotEvaluatedApplicabilityLevel,
    RuleApplicabilityEvaluation,
    RuleSetApplicabilityEvaluation,
    RuleTruth,
    UnresolvedObligation,
)
from jimu_runtime.domain.runtime.authority import AuthoritativeResultSelection
from jimu_runtime.domain.runtime.candidates import (
    CandidateParseLimits,
    CandidateSnapshot,
    CandidateSnapshotLimits,
)
from jimu_runtime.domain.runtime.coverage import (
    AncestorNotEvaluatedObligation,
    BlockedCoverageScope,
    CoverageMatch,
    CoverageReason,
    CoverageStatus,
    DependencyNotEvaluatedObligation,
    EvaluatedCoverageScope,
    EvaluatedObligation,
    ExcludedObligation,
    ExecutionCoverage,
    IncludedObligation,
    MandatoryRuleCoverage,
    MandatoryRuleObligation,
    ObligationDisposition,
    ObligationTarget,
    ProfileCoverage,
    ProfileObligationTarget,
    RuleObligationTarget,
    RuleSetObligationTarget,
    SelectedApplicability,
    UnresolvedMandatoryObligation,
)
from jimu_runtime.domain.runtime.decisions import (
    DecisionRecord,
    EvaluatedRuleOutcome,
)
from jimu_runtime.domain.runtime.evidence import EvidenceAsset
from jimu_runtime.domain.runtime.executions import Attempt, NodeId, NodeRun
from jimu_runtime.domain.runtime.facts import (
    AttemptRef,
    FactRecord,
    NodeRunRef,
    ObjectRef,
    PolicyContentRef,
    ProfileContentRef,
    QuestionSelectionRef,
    ToolExecutionRef,
)
from jimu_runtime.domain.runtime.invocations import ModelInvocation
from jimu_runtime.domain.runtime.perception import (
    DiscoveryOutcome,
    GroundingOutcome,
    ObjectRecord,
)
from jimu_runtime.domain.runtime.tool_executions import (
    ToolExecution,
    ToolPlanStepOrigin,
)
from jimu_runtime.domain.validation.applicability import (
    ApplicabilityAttemptBinding,
    ApplicabilityRecordSet,
    ApplicabilityTargetBinding,
)
from jimu_runtime.domain.validation.authority import LocalAuthorityHistoryInput
from jimu_runtime.domain.validation.authority_inputs import (
    AttemptDescriptor,
    AuthorityQuestionInput,
    AuthorityToolPlanInput,
    FactCandidateSubmission,
    NodeRunDescriptor,
    ObjectDescriptor,
    QuestionSelectionDescriptor,
    ToolStepResultDescriptor,
)
from jimu_runtime.domain.validation.coverage_sources import (
    CoverageAuthorityInput,
    CoverageSources,
    validate_decision_coverage_sources,
)
from jimu_runtime.domain.validation.decision_coverage import (
    BoundRule,
    BoundRuleSet,
    DecisionCoverageRecords,
    SelectedApplicabilityRecords,
)
from jimu_runtime.domain.validation.definition_inputs import (
    ProfileRegistration,
    QuestionRegistration,
    RegisteredTool,
    ToolPlanRegistration,
)
from jimu_runtime.domain.validation.producer_authority_inputs import (
    ExecutionDefinitionPin,
    ProducerAuthorityValidationInput,
)
from jimu_runtime.domain.validation.producer_inputs import (
    ProducerCatalogueContext,
    ProducerCatalogueRegistration,
    ProducerValidationContext,
)
from jimu_runtime.domain.validation.runtime_authority import RuntimeAuthorityRecords
from jimu_runtime.domain.validation.tool_plans import ToolRegistration
from jimu_runtime.workflow.engine import (
    CoverageWork,
    ExecutionFinalization,
    PlannerState,
    ProfileCoverageSource,
    ProfilePlan,
    plan_work,
)
from jimu_runtime.workflow.mock_context import (
    SyntheticGateResult,
    applicability_binding,
)
from jimu_runtime.workflow.mock_decision import (
    SyntheticDecisionResult,
    coverage_ref,
    finish_decision,
)
from jimu_runtime.workflow.mock_support import (
    VERSION,
    K,
    VerifiedExecutionScope,
    record_digest,
    synthetic_id,
)
from jimu_runtime.workflow.pins import ResolvedExecutionPins

_GROUNDING_GAP = {
    GroundingOutcome.FAILED: CoverageReason.GROUNDING_FAILED,
    GroundingOutcome.NO_TARGET: CoverageReason.GROUNDING_NO_TARGET,
    GroundingOutcome.PARTIAL: CoverageReason.GROUNDING_PARTIAL,
    GroundingOutcome.NOT_EVALUATED: CoverageReason.GROUNDING_NOT_EVALUATED,
}
_DISCOVERY_GAP = {
    DiscoveryOutcome.FAILED: CoverageReason.DISCOVERY_FAILED,
    DiscoveryOutcome.NO_HITS: CoverageReason.DISCOVERY_NO_HITS,
    DiscoveryOutcome.NOT_EVALUATED: CoverageReason.DISCOVERY_NOT_EVALUATED,
}


def _node_ref(node: NodeRun) -> NodeRunRef:
    return NodeRunRef(record_kind=K.NODE_RUN, record_id=node.node_run_id)


def _node_view(node: NodeRun) -> NodeRunDescriptor:
    return NodeRunDescriptor(
        node_run_ref=_node_ref(node),
        execution_id=node.execution_id,
        node_id=node.node_id.value,
        status=node.status,
    )


def _attempt_view(attempt: Attempt) -> AttemptDescriptor:
    return AttemptDescriptor(
        attempt_ref=AttemptRef(record_kind=K.ATTEMPT, record_id=attempt.attempt_id),
        execution_id=attempt.execution_id,
        owner=attempt.owner,
        attempt_number=attempt.attempt_number,
        status=attempt.status,
    )


def _object_view(obj: ObjectRecord) -> ObjectDescriptor:
    return ObjectDescriptor(
        object_ref=ObjectRef(record_kind=K.OBJECT, record_id=obj.object_id),
        execution_id=obj.execution_id,
        profile_ref=obj.profile_ref,
    )


def _blocked(
    reason: CoverageReason, node: NodeRun | None
) -> DependencyNotEvaluatedObligation:
    if node is None:
        raise ValueError(
            f"Coverage blocker is missing its actual NodeRun: {reason.value}"
        )
    return DependencyNotEvaluatedObligation(
        disposition="dependency_not_evaluated",
        reason=reason,
        blocker_node_run_ref=_node_ref(node),
    )


def _targets(
    plan: ProfilePlan,
    obj: ObjectRef | None,
) -> tuple[RuleSetObligationTarget | RuleObligationTarget, ...]:
    object_scope = {} if obj is None else {"object_ref": obj}
    targets: list[RuleSetObligationTarget | RuleObligationTarget] = []
    for group in plan.rules.rule_sets:
        targets.append(
            RuleSetObligationTarget(
                scope="rule_set", rule_set_ref=group.rule_set_ref, **object_scope
            )
        )
        targets.extend(
            RuleObligationTarget(
                scope="rule",
                rule_set_ref=group.rule_set_ref,
                rule_ref=rule.rule_ref,
                **object_scope,
            )
            for rule in group.rules
            if rule.rule_policy.policy.mandatory
        )
    return tuple(targets)


def _policy_ref(
    definition: ProfileDefinition | RuleSetDefinition | RuleDefinition,
) -> PolicyContentRef:
    return PolicyContentRef.model_validate(
        definition.applicability_policy_ref.model_dump()
    )


def _pinned_bindings(
    scope: VerifiedExecutionScope,
    plans: tuple[ProfilePlan, ...],
) -> tuple[ApplicabilityTargetBinding, ...]:
    configured = tuple(
        entry.ref for entry in scope.catalogue.of_kind(ArtifactKind.PROFILE)
    )
    if tuple(plan.rules.profile_ref for plan in plans) != tuple(
        ProfileContentRef.model_validate(ref.model_dump()) for ref in configured
    ):
        raise ValueError("Coverage must exhaust the exact pinned Profile catalogue")
    bindings: dict[tuple[ArtifactKind, str, str, str], ApplicabilityTargetBinding] = {}
    for plan in plans:
        profile = scope.resolve(plan.rules.profile_ref, ProfileDefinition)
        if profile != plan.definition:
            raise ValueError("Coverage Profile view differs from pinned definitions")
        binding = applicability_binding(
            scope, plan.rules.profile_ref, _policy_ref(profile), profile.rule_set_refs
        )
        bindings[reference_identity(binding.target_ref)] = binding
        for group in plan.rules.rule_sets:
            definition = scope.resolve(group.rule_set_ref, RuleSetDefinition)
            if definition != group.definition:
                raise ValueError(
                    "Coverage RuleSet view differs from pinned definitions"
                )
            binding = applicability_binding(
                scope, group.rule_set_ref, _policy_ref(definition), definition.rule_refs
            )
            bindings[reference_identity(binding.target_ref)] = binding
            for rule in group.rules:
                item = scope.resolve(rule.rule_ref, RuleDefinition)
                if (
                    item != rule.definition
                    or scope.resolve(item.rule_policy_ref, RulePolicyArtifact)
                    != rule.rule_policy
                    or scope.resolve(item.evidence_policy_ref, EvidencePolicyArtifact)
                    != rule.evidence_policy
                ):
                    raise ValueError(
                        "Coverage Rule view differs from pinned definitions"
                    )
                binding = applicability_binding(scope, rule.rule_ref, _policy_ref(item))
                bindings[reference_identity(binding.target_ref)] = binding
    return tuple(bindings.values())


@dataclass(slots=True)
class _Ledger:
    scope: VerifiedExecutionScope
    work: CoverageWork
    now: datetime
    hits: tuple[ProfileContentRef, ...]
    gate: SyntheticGateResult | None
    stages: dict[ObjectRef, SyntheticDecisionResult]
    projected_traces: list[ApplicabilityTrace] = field(default_factory=list)
    projected_sets: list[RuleSetApplicabilityEvaluation] = field(default_factory=list)
    projected_rules: list[RuleApplicabilityEvaluation] = field(default_factory=list)
    unresolved: list[UnresolvedObligation] = field(default_factory=list)

    def entry(
        self,
        profile: ProfileContentRef,
        target: ObligationTarget,
        outcome: ObligationDisposition,
    ) -> MandatoryRuleObligation:
        obligation_id = (
            outcome.unresolved_obligation_ref.record_id
            if isinstance(outcome, UnresolvedMandatoryObligation)
            else self.obligation_id(profile, target)
        )
        return MandatoryRuleObligation(
            obligation_id=obligation_id,
            profile_ref=profile,
            target=target,
            outcome=outcome,
        )

    def obligation_id(
        self, profile: ProfileContentRef, target: ObligationTarget
    ) -> str:
        parts = (
            self.scope.task.task_id,
            self.scope.execution_id,
            profile.stable_json(),
        )
        return (
            synthetic_id("profile-obligation", *parts)
            if isinstance(target, ProfileObligationTarget)
            else synthetic_id("coverage-obligation", *parts, target.stable_json())
        )

    def profile(
        self,
        plan: ProfilePlan,
        source: ProfileCoverageSource,
        trace: ApplicabilityTrace | None,
    ) -> tuple[MandatoryRuleObligation, ...]:
        profile = source.profile_ref
        target = ProfileObligationTarget(scope="profile")
        if self.gate is None:
            blocker = _blocked(
                CoverageReason.CONTEXT_BUILD_BLOCKED, self.work.context_node
            )
            return tuple(
                self.entry(profile, item, blocker)
                for item in (target, *_targets(plan, None))
            )
        if trace is None:
            raise ValueError("Coverage is missing the selected Profile gate trace")
        selection = SelectedApplicability(
            evaluation_ref=self.gate.evaluation.evaluation_ref,
            trace_ref=trace.trace_ref,
        )
        if trace.effective_truth is RuleTruth.FALSE:
            root = self.entry(
                profile,
                target,
                ExcludedObligation(
                    disposition="excluded", selected_applicability=selection
                ),
            )
            return (root, *self.project_profile(plan, trace, root))
        if trace.effective_truth is RuleTruth.UNKNOWN:
            level = trace.levels[-1]
            if not isinstance(
                level, EvaluatedApplicabilityLevel
            ) or level.reason_code not in (
                ApplicabilityReason.METADATA_UNKNOWN,
                ApplicabilityReason.METADATA_INVALID,
            ):
                raise ValueError(
                    "selected unresolved Profile requires its metadata diagnostics"
                )
            unresolved = UnresolvedObligation.model_validate(
                {
                    "schema_version": VERSION,
                    "obligation_id": self.obligation_id(profile, target),
                    "execution_id": self.scope.execution_id,
                    "profile_ref": profile,
                    "scope": ApplicabilityLevel.PROFILE,
                    "target_ref": profile,
                    "applicability_trace_ref": trace.trace_ref,
                    "reason_code": level.reason_code,
                    "diagnostic_refs": tuple(
                        ApplicabilityDiagnosticRef(
                            trace_ref=trace.trace_ref,
                            level=level.level,
                            issue_index=index,
                        )
                        for index in range(len(level.validation_issues))
                    ),
                }
            )
            self.unresolved.append(unresolved)
            root = self.entry(
                profile,
                target,
                UnresolvedMandatoryObligation(
                    disposition="unresolved",
                    selected_applicability=selection,
                    unresolved_obligation_ref=unresolved.obligation_ref,
                ),
            )
            return (root, *self.project_profile(plan, trace, root))
        root = self.entry(
            profile,
            target,
            IncludedObligation(
                disposition="included", selected_applicability=selection
            ),
        )
        if not source.object_refs:
            blocker = (
                _blocked(
                    _GROUNDING_GAP[source.grounding_outcome], self.work.grounding_node
                )
                if profile in self.hits
                else _blocked(
                    _DISCOVERY_GAP.get(
                        self.work.discovery_outcome,
                        CoverageReason.PROFILE_NOT_DISCOVERED,
                    ),
                    self.work.discovery_node,
                )
            )
            return (
                root,
                *(self.entry(profile, item, blocker) for item in _targets(plan, None)),
            )
        rows = [root]
        objects = {item.object_ref: item for item in self.work.objects}
        for obj in source.object_refs:
            targets = _targets(plan, obj)
            stage = self.stages.get(obj)
            if stage is None:
                if targets:
                    blocker = _blocked(
                        CoverageReason.DECISION_NOT_EVALUATED, objects[obj].decision
                    )
                    rows.extend(self.entry(profile, item, blocker) for item in targets)
                continue
            obligations = {item.target: item for item in stage.obligations}
            if (
                len(obligations) != len(stage.obligations)
                or obligations.keys() != set(targets)
                or any(item.profile_ref != profile for item in obligations.values())
            ):
                raise ValueError(
                    "selected N06 draft must account for each configured Object obligation"
                )
            rows.extend(obligations[item] for item in targets)
        return tuple(rows)

    def project_profile(
        self,
        plan: ProfilePlan,
        trace: ApplicabilityTrace,
        root: MandatoryRuleObligation,
    ) -> tuple[MandatoryRuleObligation, ...]:
        """Project the persisted Profile verdict through lower pinned membership."""
        assert self.gate is not None
        profile = root.profile_ref
        ancestor = self.gate.evaluation.evaluation_ref
        reason = (
            ApplicabilityReason.ANCESTOR_EXCLUDED
            if isinstance(root.outcome, ExcludedObligation)
            else ApplicabilityReason.ANCESTOR_UNRESOLVED
        )
        rows = []
        for group in plan.rules.rule_sets:
            target = RuleSetObligationTarget(
                scope="rule_set", rule_set_ref=group.rule_set_ref
            )
            set_trace = self.project_trace(trace, group, target, reason)
            base = {
                "schema_version": VERSION,
                "execution_id": trace.execution_id,
                "release_id": trace.release_id,
                "bundle_digest": trace.bundle_digest,
                "context_snapshot_ref": trace.context_snapshot_ref,
                "profile_eligibility_evaluation_ref": ancestor,
                "profile_ref": profile,
                "rule_set_ref": group.rule_set_ref,
                "recorded_at": self.now,
                "decisive_ancestor_ref": ancestor,
            }
            set_evaluation = RuleSetApplicabilityEvaluation.model_validate(
                {
                    **base,
                    "evaluation_id": synthetic_id(
                        "coverage-projection", set_trace.applicability_trace_id
                    ),
                    "applicability_trace_ref": set_trace.trace_ref,
                }
            )
            self.projected_sets.append(set_evaluation)
            rows.append(
                self.entry(
                    profile,
                    target,
                    AncestorNotEvaluatedObligation(
                        disposition="not_evaluated",
                        decisive_ancestor_ref=ancestor,
                        ancestor_obligation_id=root.obligation_id,
                        selected_applicability=SelectedApplicability(
                            evaluation_ref=set_evaluation.evaluation_ref,
                            trace_ref=set_trace.trace_ref,
                        ),
                    ),
                )
            )
            for rule in group.rules:
                if not rule.rule_policy.policy.mandatory:
                    continue
                rule_target = RuleObligationTarget(
                    scope="rule",
                    rule_set_ref=group.rule_set_ref,
                    rule_ref=rule.rule_ref,
                )
                rule_trace = self.project_trace(set_trace, rule, rule_target, reason)
                rule_evaluation = RuleApplicabilityEvaluation.model_validate(
                    {
                        **base,
                        "rule_ref": rule.rule_ref,
                        "rule_set_applicability_evaluation_ref": set_evaluation.evaluation_ref,
                        "evaluation_id": synthetic_id(
                            "coverage-projection", rule_trace.applicability_trace_id
                        ),
                        "applicability_trace_ref": rule_trace.trace_ref,
                    }
                )
                self.projected_rules.append(rule_evaluation)
                rows.append(
                    self.entry(
                        profile,
                        rule_target,
                        AncestorNotEvaluatedObligation(
                            disposition="not_evaluated",
                            decisive_ancestor_ref=ancestor,
                            ancestor_obligation_id=root.obligation_id,
                            selected_applicability=SelectedApplicability(
                                evaluation_ref=rule_evaluation.evaluation_ref,
                                trace_ref=rule_trace.trace_ref,
                            ),
                        ),
                    )
                )
        return tuple(rows)

    def project_trace(
        self,
        parent: ApplicabilityTrace,
        binding: BoundRuleSet | BoundRule,
        target: RuleSetObligationTarget | RuleObligationTarget,
        reason: ApplicabilityReason,
    ) -> ApplicabilityTrace:
        assert self.gate is not None
        ref, level = (
            (binding.rule_set_ref, ApplicabilityLevel.RULE_SET)
            if isinstance(binding, BoundRuleSet)
            else (binding.rule_ref, ApplicabilityLevel.RULE)
        )
        projected = NotEvaluatedApplicabilityLevel.model_validate(
            {
                "level": level,
                "target_ref": ref,
                "policy_ref": _policy_ref(binding.definition),
                "evaluation_status": ApplicabilityEvaluationStatus.NOT_EVALUATED,
                "reason_code": reason,
                "decisive_ancestor_ref": self.gate.evaluation.evaluation_ref,
            }
        )
        trace = ApplicabilityTrace.model_validate(
            {
                **parent.model_dump(),
                "applicability_trace_id": synthetic_id(
                    "coverage-projection-trace",
                    self.obligation_id(parent.profile_ref, target),
                ),
                "target_ref": ref,
                "levels": (*parent.levels, projected),
                "created_at": self.now,
            }
        )
        self.projected_traces.append(trace)
        return trace


def _profile_reasons(
    source: ProfileCoverageSource,
    decisions: tuple[DecisionRecord, ...],
    work: CoverageWork,
    obligations: tuple[MandatoryRuleObligation, ...],
) -> tuple[CoverageReason, ...]:
    reasons = list(source.reasons)
    if any(
        isinstance(item.outcome, UnresolvedMandatoryObligation) for item in obligations
    ):
        reasons.append(CoverageReason.APPLICABILITY_UNRESOLVED)
    for item in work.objects:
        if item.object_ref in source.object_refs:
            reasons.extend(item.reasons)
    for decision in decisions:
        if decision.explanation.blocked_rules:
            reasons.append(CoverageReason.REQUIRED_EVIDENCE_BLOCKED)
        if any(
            isinstance(item.result, EvaluatedRuleOutcome)
            and item.result.predicate.truth is RuleTruth.UNKNOWN
            for item in decision.rule_evaluations
        ):
            reasons.append(CoverageReason.PREDICATE_UNKNOWN)
    return tuple(dict.fromkeys(reasons))


def _profile_status(
    source: ProfileCoverageSource,
    obligations: tuple[MandatoryRuleObligation, ...],
    decisions: tuple[DecisionRecord, ...],
) -> CoverageStatus:
    if isinstance(obligations[0].outcome, ExcludedObligation):
        return CoverageStatus.NOT_APPLICABLE
    if not source.object_refs:
        return CoverageStatus.NONE
    excluded = {
        item.obligation_id
        for item in obligations
        if isinstance(item.outcome, ExcludedObligation)
    }
    if source.grounding_outcome is not GroundingOutcome.OBJECTS or len(
        decisions
    ) != len(source.object_refs):
        return CoverageStatus.PARTIAL
    mandatory_paths = {
        (item.target.object_ref, item.target.path)
        for item in obligations
        if isinstance(item.target, RuleObligationTarget)
    }
    if any(
        decision.result is DecisionResult.UNCERTAIN
        or any(
            isinstance(rule.result, EvaluatedRuleOutcome)
            and rule.result.predicate.truth is RuleTruth.UNKNOWN
            and (decision.object_ref, rule.rule) in mandatory_paths
            for rule in decision.rule_evaluations
        )
        for decision in decisions
    ):
        return CoverageStatus.PARTIAL
    for item in obligations:
        outcome = item.outcome
        if isinstance(outcome, AncestorNotEvaluatedObligation):
            if outcome.ancestor_obligation_id not in excluded:
                return CoverageStatus.PARTIAL
        elif not isinstance(
            outcome, IncludedObligation | ExcludedObligation | EvaluatedObligation
        ):
            return CoverageStatus.PARTIAL
    return CoverageStatus.COMPLETE


def _summary(
    profiles: tuple[ProfileCoverage, ...],
    decisions: tuple[DecisionRecord, ...],
    outcome: DiscoveryOutcome,
    matches: tuple[CoverageMatch, ...],
) -> tuple[CoverageStatus, DecisionResult]:
    statuses = {item.coverage_status for item in profiles}
    if statuses == {CoverageStatus.NOT_APPLICABLE}:
        return CoverageStatus.NOT_APPLICABLE, DecisionResult.NOT_APPLICABLE
    if (
        CoverageStatus.COMPLETE in statuses
        and statuses
        <= {
            CoverageStatus.COMPLETE,
            CoverageStatus.NOT_APPLICABLE,
        }
        and outcome is DiscoveryOutcome.HITS
    ):
        if matches:
            return CoverageStatus.COMPLETE, DecisionResult.VIOLATION
        return CoverageStatus.COMPLETE, (
            DecisionResult.NO_VIOLATION
            if any(item.result is DecisionResult.NO_VIOLATION for item in decisions)
            else DecisionResult.NOT_APPLICABLE
        )
    status = (
        CoverageStatus.PARTIAL
        if statuses & {CoverageStatus.COMPLETE, CoverageStatus.PARTIAL}
        else CoverageStatus.NONE
    )
    return status, DecisionResult.VIOLATION if matches else DecisionResult.UNCERTAIN


def _check_perception_inputs(work: CoverageWork, sources: CoverageSources) -> None:
    if isinstance(work.context, SuccessfulContextBuild) and (
        sources.context_snapshot is None
        or record_digest(sources.context_snapshot)
        != work.context.context_snapshot_digest
    ):
        raise ValueError(
            "Coverage must use the exact selected N01 ContextSnapshot digest"
        )
    if isinstance(work.discovery, DiscoveryAttemptOutput):
        selections = {
            item.ref.record_id: item.digest
            for item in work.discovery.envelope.output_artifacts
            if item.ref.record_kind is K.QUESTION_SELECTION_RECORD
        }
        if selections != {
            item.selection_id: record_digest(item)
            for item in sources.question_selections
        }:
            raise ValueError(
                "Coverage must retain exact selected N02 QuestionSelection digests"
            )
    elif sources.question_selections:
        raise ValueError(
            "Coverage cannot consume Question selections without an N02 Attempt"
        )


def _check_grounding_snapshot(work: CoverageWork, sources: CoverageSources) -> None:
    output = work.grounding
    if output is None:
        if sources.grounding_records:
            raise ValueError(
                "Coverage cannot consume grounding without its selected envelope"
            )
        return
    pins = {
        item.ref.record_id: item.digest
        for item in output.envelope.output_artifacts
        if item.ref.record_kind is K.GROUNDING_RECORD
    }
    if tuple(
        item.as_profile_result() for item in sources.grounding_records
    ) != output.profile_results or any(
        (item.node_run_id, item.attempt_id, record_digest(item))
        != (
            output.envelope.node_run_id,
            output.envelope.attempt_id,
            pins.get(item.grounding_record_id),
        )
        for item in sources.grounding_records
    ):
        raise ValueError(
            "Coverage grounding must match each selected N03 result digest and Attempt"
        )
    if {
        item.ref.record_id: item.digest
        for item in output.envelope.output_artifacts
        if item.ref.record_kind is K.OBJECT
    } != {item.object_id: record_digest(item) for item in sources.objects}:
        raise ValueError("Coverage must retain exact selected N03 Object digests")


def _check_decision_inputs(
    stages: tuple[SyntheticDecisionResult, ...],
    facts: tuple[FactRecord, ...],
    authorities: tuple[AuthoritativeResultSelection, ...],
    evidence: tuple[EvidenceAsset, ...],
    invocations: tuple[ModelInvocation, ...],
) -> None:
    fact_by_id = {item.fact_id: item for item in facts}
    revisions = {item.selection_id: item for item in authorities}
    evidence_by_id = {item.evidence_id: item for item in evidence}
    invocation_by_id = {item.model_invocation_id: item for item in invocations}
    for stage in stages:
        inputs = stage.draft.inputs
        pins = {
            (item.ref.record_kind, item.ref.record_id): item.digest
            for item in inputs.envelope.upstream_artifacts
        }
        for refs, directory in (
            (inputs.fact_refs, fact_by_id),
            (inputs.authority_selection_refs, revisions),
        ):
            for ref in refs:
                row = directory.get(ref.record_id)
                if row is None or pins.get(
                    (ref.record_kind, ref.record_id)
                ) != record_digest(row):
                    raise ValueError(
                        "N06 inputs must retain exact persisted Facts and authority revisions"
                    )
        if (
            stage.invocation is not None
            and invocation_by_id.get(stage.invocation.model_invocation_id)
            != stage.invocation
        ) or any(
            evidence_by_id.get(item.evidence_id) != item for item in stage.evidence
        ):
            raise ValueError(
                "N06 outputs must retain exact persisted invocation and evidence records"
            )


def finalize_execution(
    state: PlannerState,
    work: CoverageWork,
    *,
    scope: VerifiedExecutionScope,
    sources: CoverageSources,
    gate: SyntheticGateResult | None,
    stages: tuple[SyntheticDecisionResult, ...],
    facts: tuple[FactRecord, ...],
    authorities: tuple[AuthoritativeResultSelection, ...],
    evidence: tuple[EvidenceAsset, ...],
    invocations: tuple[ModelInvocation, ...],
    authority: CoverageAuthorityInput | None,
    now: datetime,
    tool_step_nodes: tuple[NodeRun, ...] = (),
    additional_unresolved: tuple[UnresolvedObligation, ...] = (),
) -> ExecutionFinalization:
    """Check one settled, caller-supplied transaction snapshot and derive its result."""
    execution = state.execution
    if (
        work != plan_work(state).coverage
        or not work.ready_to_finalize
        or sources.execution != execution
        or sources.context_output != work.context
        or sources.discovery_output != work.discovery
        or sources.objects != state.objects
        or {item.run for item in state.runs} | set(tool_step_nodes)
        != set(sources.node_runs)
        or any(item.node_id is not NodeId.N05 for item in tool_step_nodes)
        or (gate.evaluation if gate is not None else None) != work.eligibility
        or (execution.execution_id, execution.task_id, execution.pins)
        != (
            scope.execution_id,
            scope.task.task_id,
            scope.pins,
        )
    ):
        raise ValueError(
            "Coverage requires a single settled pinned planning and persistence snapshot"
        )
    _check_perception_inputs(work, sources)
    _check_grounding_snapshot(work, sources)
    supplied = {stage.decision.object_ref: stage for stage in stages}
    selected_drafts = tuple(
        item.decision_draft for item in work.objects if item.decision_draft is not None
    )
    if (
        len(supplied) != len(stages)
        or {stage.draft for stage in stages} != set(selected_drafts)
        or any(
            item.decision_draft is not None
            and (
                item.decision is None
                or item.decision.status is not NodeRunStatus.SUCCEEDED
            )
            for item in work.objects
        )
    ):
        raise ValueError(
            "Coverage can only use the exact selected successful N06 drafts"
        )
    stages = tuple(supplied[draft.decision.object_ref] for draft in selected_drafts)
    _check_decision_inputs(stages, facts, authorities, evidence, invocations)
    decisions = tuple(stage.decision for stage in stages)
    hits = (
        tuple(item.profile_ref for item in work.discovery.profile_hits)
        if isinstance(
            work.discovery,
            DiscoveryAttemptOutput,
        )
        else ()
    )
    bindings = _pinned_bindings(scope, state.profiles)
    profile_traces = {trace.profile_ref: trace for trace in gate.traces} if gate else {}
    configured = tuple(item.rules.profile_ref for item in state.profiles)
    ledger = _Ledger(scope, work, now, hits, gate, supplied)
    pin = {
        "schema_version": VERSION,
        "execution_id": execution.execution_id,
        "release_id": execution.pins.release_id,
        "bundle_digest": execution.pins.bundle_digest,
    }
    obligations: list[MandatoryRuleObligation] = []
    profile_records: list[ProfileCoverage] = []
    for plan, profile in zip(state.profiles, work.profiles, strict=True):
        rows = ledger.profile(plan, profile, profile_traces.get(profile.profile_ref))
        obligations.extend(rows)
        profile_decisions = tuple(
            item for item in decisions if item.profile_ref == profile.profile_ref
        )
        profile_records.append(
            ProfileCoverage.model_validate(
                {
                    **pin,
                    "profile_ref": profile.profile_ref,
                    "grounding_outcome": profile.grounding_outcome,
                    "object_refs": profile.object_refs,
                    "object_count": len(profile.object_refs),
                    "obligation_ids": tuple(item.obligation_id for item in rows),
                    "coverage_status": _profile_status(
                        profile, rows, profile_decisions
                    ),
                    "reasons": _profile_reasons(profile, profile_decisions, work, rows),
                    "missing_evidence_refs": (),
                }
            )
        )
    selected = tuple(
        item.outcome.selected_applicability
        for item in obligations
        if not isinstance(item.outcome, DependencyNotEvaluatedObligation)
    )
    applicability_scope: BlockedCoverageScope | EvaluatedCoverageScope
    if gate is None:
        applicability_scope = BlockedCoverageScope(
            scope_status="blocked",
            configured_profile_refs=configured,
            blocker_node_run_ref=_blocked(
                CoverageReason.CONTEXT_BUILD_BLOCKED,
                work.context_node,
            ).blocker_node_run_ref,
        )
    else:
        applicability_scope = EvaluatedCoverageScope(
            scope_status="evaluated",
            configured_profile_refs=configured,
            context_snapshot_ref=gate.evaluation.context_snapshot_ref,
            profile_eligibility_evaluation_ref=gate.evaluation.evaluation_ref,
        )
    matches = tuple(
        CoverageMatch(
            object_ref=decision.object_ref,
            profile_ref=decision.profile_ref,
            decision_ref=decision.decision_ref,
            rule=rule,
        )
        for decision in decisions
        for rule in decision.conflict_resolution.matched_rules
    )
    profiles = tuple(profile_records)
    unresolved = (*ledger.unresolved, *additional_unresolved)
    status, result = _summary(profiles, decisions, work.discovery_outcome, matches)
    discovery_reason = _DISCOVERY_GAP.get(work.discovery_outcome)
    reasons = tuple(
        dict.fromkeys(
            (
                *work.reasons,
                *((discovery_reason,) if discovery_reason is not None else ()),
                *(reason for item in profiles for reason in item.reasons),
            )
        )
    )
    coverage = ExecutionCoverage.model_validate(
        {
            **pin,
            "coverage_id": coverage_ref(scope).record_id,
            "applicability_scope": applicability_scope,
            "discovery_outcome": work.discovery_outcome,
            "discovery_model_invocation_refs": work.discovery.model_invocation_refs
            if isinstance(
                work.discovery,
                DiscoveryAttemptOutput,
            )
            else (),
            "hit_profile_refs": hits,
            "profiles": profiles,
            "mandatory_rule_coverage": MandatoryRuleCoverage(
                scan="all_mandatory", obligations=tuple(obligations)
            ),
            "selected_applicability_evaluation_refs": tuple(
                dict.fromkeys(item.evaluation_ref for item in selected)
            ),
            "applicability_trace_refs": tuple(
                dict.fromkeys(item.trace_ref for item in selected)
            ),
            "unresolved_obligation_refs": tuple(
                item.obligation_ref for item in unresolved
            ),
            "object_count": len(state.objects),
            "coverage_status": status,
            "reasons": reasons,
            "missing_evidence_refs": (),
            "summary_result": result,
            "matches": matches,
            "created_at": now,
        }
    )
    objects = tuple(_object_view(item) for item in sources.objects)
    nodes = tuple(_node_view(item) for item in sources.node_runs)
    attempts = tuple(_attempt_view(item) for item in sources.attempts)
    app = None
    if gate is not None:
        attempts_by_ref = {item.attempt_ref: item for item in attempts}
        app = SelectedApplicabilityRecords(
            scope=gate.scope.model_copy(update={"target_bindings": bindings}),
            records=ApplicabilityRecordSet(
                profile_eligibility=gate.evaluation,
                traces=(
                    *gate.traces,
                    *(trace for stage in stages for trace in stage.traces),
                    *ledger.projected_traces,
                ),
                rule_set_evaluations=(
                    *(
                        item
                        for stage in stages
                        for item in stage.draft.rule_set_evaluations
                    ),
                    *ledger.projected_sets,
                ),
                rule_evaluations=(
                    *(
                        item
                        for stage in stages
                        for item in stage.draft.rule_evaluations
                    ),
                    *ledger.projected_rules,
                ),
                unresolved_obligations=unresolved,
                node_runs=nodes,
                attempts=tuple(
                    ApplicabilityAttemptBinding(
                        attempt=attempts_by_ref[decision.attempt_ref],
                        object_ref=decision.object_ref,
                    )
                    for decision in decisions
                ),
                objects=objects,
            ),
        )
    records = DecisionCoverageRecords(
        profile_rule_scope=tuple(item.rules for item in state.profiles),
        applicability=app,
        objects=objects,
        node_runs=nodes,
        attempts=attempts,
        decisions=decisions,
        predictions=tuple(
            stage.draft.candidate
            for stage in stages
            if stage.draft.candidate is not None
        ),
        model_invocations=invocations,
        facts=facts,
        authoritative_selections=authorities,
        evidence=evidence,
    )
    if (
        state.grounding_gaps
        and state.grounding_output is not None
        and state.grounding_output.envelope.status is NodeRunStatus.SUCCEEDED
    ):
        validate_decision_coverage_sources(
            coverage,
            records,
            sources,
            authority=authority,
            grounding_gaps=state.grounding_gaps,
        )
    else:
        validate_decision_coverage_sources(
            coverage, records, sources, authority=authority
        )
    if gate is not None:
        for stage in stages:
            finish_decision(scope, stage, coverage, gate)
    return ExecutionFinalization(
        schema_version=VERSION,
        execution_id=execution.execution_id,
        coverage=coverage,
        decisions=decisions,
        projected_traces=tuple(ledger.projected_traces),
        projected_rule_set_evaluations=tuple(ledger.projected_sets),
        projected_rule_evaluations=tuple(ledger.projected_rules),
        unresolved_obligations=unresolved,
    )


def _tool_view(tool: ToolExecution) -> ToolStepResultDescriptor | None:
    origin, observation = tool.invocation_origin, tool.authority_observation
    if not isinstance(origin, ToolPlanStepOrigin) or observation is None:
        return None
    if tool.object_ref is None or tool.actual_implementation_version is None:
        raise ValueError(
            "Tool authority requires actual Object and implementation version"
        )
    return ToolStepResultDescriptor(
        execution_id=tool.execution_id,
        scope=FactScope.OBJECT,
        object_id=tool.object_ref.record_id,
        profile_ref=tool.profile_ref,
        attempt_ref=tool.attempt_ref,
        arrival_qualification=observation.arrival_qualification,
        result_ref=ToolExecutionRef(
            record_kind=K.TOOL_EXECUTION, record_id=tool.tool_execution_id
        ),
        invocation_origin="tool_plan_step",
        tool_plan_ref=origin.tool_plan_ref,
        step_id=origin.step_id,
        tool_ref=tool.tool_ref,
        actual_implementation_version=tool.actual_implementation_version,
        envelope_validation=observation.envelope_validation,
        association_validation=observation.association_validation,
        mapped_outputs=observation.mapped_outputs,
    )


def build_coverage_authority(
    resolved: ResolvedExecutionPins,
    records: RuntimeAuthorityRecords,
) -> CoverageAuthorityInput:
    """Project admitted Producer claims from the selected runtime records."""
    if (
        records.execution.pins != resolved.pins
        or records.manifest != resolved.files.manifest
    ):
        raise ValueError(
            "Coverage authority must use the actual pinned Bundle catalogue"
        )
    (entry,) = resolved.catalogue.of_kind(ArtifactKind.FACT_PRODUCER_CATALOG)
    if not isinstance(entry.value, ProducerCatalogue):
        raise ValueError("workflow authority requires the pinned Producer Catalogue")
    conditional = tuple(
        item
        for item in entry.value.declarations
        if not isinstance(item.activation_condition, AlwaysActivation)
    )
    conditional_steps = {
        (item.profile_ref, producer.tool_plan_ref, producer.step_id)
        for item in conditional
        if isinstance(producer := item.producer, ToolStepProducer)
    }
    executed_steps = {
        (tool.profile_ref, origin.tool_plan_ref, origin.step_id)
        for tool in records.tool_executions
        if isinstance(origin := tool.invocation_origin, ToolPlanStepOrigin)
    }
    # This projection validates declared condition scopes but cannot attest the
    # truth of an invoked step's activation condition.
    if (
        conditional_steps & executed_steps
        or {item.declaration_id for item in conditional}
        & {item.producer_declaration_id for item in records.facts}
        or any(
            isinstance(item.producer, SystemProducer)
            for item in entry.value.declarations
        )
    ):
        raise ValueError(
            "workflow authority currently requires unconditional active Question or Tool declarations"
        )
    catalogue_ref = ProducerCatalogueContentRef.model_validate(entry.ref.model_dump())
    registration = ProducerCatalogueRegistration(
        ref=catalogue_ref, definition=entry.value
    )
    definitions = resolved.definition_inputs
    profiles = tuple(
        item
        for item in definitions.definitions
        if isinstance(item, ProfileRegistration)
    )
    questions = tuple(
        AuthorityQuestionInput(ref=item.ref, definition=item.definition)
        for item in definitions.definitions
        if isinstance(item, QuestionRegistration)
    )
    plans = tuple(
        AuthorityToolPlanInput(ref=item.ref, definition=item.definition)
        for item in definitions.definitions
        if isinstance(item, ToolPlanRegistration)
    )
    tools = tuple(
        ToolRegistration(ref=item.ref, definition=item.definition)
        for item in definitions.definitions
        if isinstance(item, RegisteredTool)
    )
    context = ProducerCatalogueContext(
        registration=registration,
        context=ProducerValidationContext(
            profiles=profiles,
            questions=questions,
            plans=plans,
            tools=tools,
            policies=definitions.policies,
            system_capabilities=(),
            condition_contexts=producer_activation_contexts(
                resolved.catalogue, registration, definitions
            ),
        ),
    )
    history = LocalAuthorityHistoryInput(
        candidates=CandidateSnapshot(
            entries=tuple(
                FactCandidateSubmission(
                    fact=item, raw_bytes=canonical_bytes(item.model_dump(mode="json"))
                )
                for item in records.facts
            )
        ),
        prior_selections=records.authority_selections,
        proposed_selections=(),
        objects=tuple(_object_view(item) for item in records.objects),
        node_runs=tuple(_node_view(item) for item in records.node_runs),
        attempts=tuple(_attempt_view(item) for item in records.attempts),
        question_selections=tuple(
            QuestionSelectionDescriptor(
                question_selection_ref=QuestionSelectionRef(
                    record_kind=K.QUESTION_SELECTION_RECORD,
                    record_id=item.selection_id,
                ),
                execution_id=item.execution_id,
                profile_ref=item.profile_ref,
                eligibility_qualification="validated",
                effective_question_refs=item.effective_question_refs,
            )
            for item in records.question_selections
            if item.effective_question_refs
        ),
        observation_results=tuple(
            item.to_authority_descriptor() for item in records.observations
        ),
        tool_results=tuple(
            view
            for item in records.tool_executions
            if (view := _tool_view(item)) is not None
        ),
        system_results=(),
        questions=questions,
        plans=plans,
        tools=tools,
    )
    claims = ProducerAuthorityValidationInput(
        history=history,
        producer_catalogues=(context,),
        execution_pins=(
            ExecutionDefinitionPin(
                execution_id=records.execution.execution_id,
                release_id=resolved.pins.release_id,
                bundle_digest=resolved.pins.bundle_digest,
                producer_catalogue_ref=catalogue_ref,
                configured_profile_refs=tuple(item.ref for item in profiles),
            ),
        ),
    )
    return CoverageAuthorityInput(
        claims=claims,
        records=records,
        fact_schemas=resolved.fact_schemas,
        parse_limits=CandidateParseLimits(
            max_bytes=16 * 1024 * 1024,
            max_json_depth=32,
            max_object_members=1024,
            max_array_items=1024,
            max_number_chars=4096,
        ),
        snapshot_limits=CandidateSnapshotLimits(
            max_entries=1024,
            max_batch_entries=1024,
            max_total_raw_bytes=64 * 1024 * 1024,
        ),
    )
