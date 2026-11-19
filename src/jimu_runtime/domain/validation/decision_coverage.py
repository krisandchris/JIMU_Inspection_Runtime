"""Finite association checks over supplied records and Bundle views."""

from collections.abc import Callable
from dataclasses import dataclass
from typing import Self

from pydantic import model_validator

from jimu_runtime.domain.base import ContractModel
from jimu_runtime.domain.definition_enums import (
    EvidenceType,
    FactScope,
    ModelCapability,
)
from jimu_runtime.domain.definition_support import reference_identity
from jimu_runtime.domain.definitions.capabilities import RuleSetContentRef
from jimu_runtime.domain.enums import AttemptStatus, DecisionResult, NodeRunStatus
from jimu_runtime.domain.fact_selectors import (
    QuestionProducerSelector,
    SystemProducerSelector,
    ToolProducerSelector,
)
from jimu_runtime.domain.policies import EvidencePolicyArtifact, RulePolicyArtifact
from jimu_runtime.domain.primitives import RecordId
from jimu_runtime.domain.rules.definitions import (
    RuleContentRef,
    RuleDefinition,
    RuleSetDefinition,
)
from jimu_runtime.domain.runtime.applicability import (
    ApplicabilityTrace,
    NotEvaluatedApplicabilityLevel,
    RuleApplicabilityEvaluation,
    RuleApplicabilityEvaluationRef,
    RuleSetApplicabilityEvaluation,
    RuleSetApplicabilityEvaluationRef,
    RuleTruth,
)
from jimu_runtime.domain.runtime.authority import (
    AuthoritativeResultSelection,
    ObservationBatchSelection,
    SystemOutputSelection,
    ToolStepSelection,
)
from jimu_runtime.domain.runtime.coverage import (
    AncestorNotEvaluatedObligation,
    ApplicableNotEvaluatedObligation,
    BlockedCoverageScope,
    CoverageMatch,
    CoverageReason,
    CoverageStatus,
    DependencyNotEvaluatedObligation,
    EvaluatedObligation,
    ExcludedObligation,
    ExecutionCoverage,
    GuardBlockedObligation,
    IncludedObligation,
    MandatoryRuleObligation,
    ObligationTarget,
    ProfileObligationTarget,
    RuleObligationTarget,
    RuleSetObligationTarget,
    UnresolvedMandatoryObligation,
)
from jimu_runtime.domain.runtime.decisions import (
    BlockedRuleOutcome,
    CandidatePrediction,
    CandidatePredictionStatus,
    CompleteRequiredEvidenceGuard,
    DecisionRecord,
    EvaluatedRuleOutcome,
    RecordList,
    RequiredEvidenceReason,
    RequiredFactProof,
    RuleEvaluation,
    RulePath,
    RuleSkipReason,
    SkippedRuleOutcome,
)
from jimu_runtime.domain.runtime.evidence import EvidenceAsset, ObjectEvidenceScope
from jimu_runtime.domain.runtime.facts import (
    AttemptRef,
    FactRecord,
    KnownFact,
    NodeRunRef,
    ObjectRef,
    ProfileContentRef,
    QuestionFactSource,
    SystemFactSource,
    ToolFactSource,
)
from jimu_runtime.domain.runtime.invocations import ModelInvocation
from jimu_runtime.domain.runtime.media import FinalizedAsset
from jimu_runtime.domain.runtime.perception import (
    DiscoveryOutcome,
    GroundingOutcome,
    GroundingRecordRef,
)
from jimu_runtime.domain.validation.applicability import (
    ApplicabilityRecordSet,
    ApplicabilityScope,
    validate_applicability_records,
)
from jimu_runtime.domain.validation.authority_inputs import (
    AttemptDescriptor,
    NodeAttemptOwner,
    NodeRunDescriptor,
    ObjectDescriptor,
)
from jimu_runtime.domain.values import RecordValue

type _TargetKey = tuple[ProfileContentRef, ObligationTarget]


class BoundRule(ContractModel):
    rule_ref: RuleContentRef
    definition: RuleDefinition
    rule_policy: RulePolicyArtifact
    evidence_policy: EvidencePolicyArtifact

    @model_validator(mode="after")
    def check_identity(self) -> Self:
        if (self.rule_ref.artifact_id, self.rule_ref.version) != (
            self.definition.rule_id,
            self.definition.version,
        ):
            raise ValueError("pinned Rule identity mismatch")
        for ref, policy in (
            (self.definition.rule_policy_ref, self.rule_policy),
            (self.definition.evidence_policy_ref, self.evidence_policy),
        ):
            if (ref.artifact_id, ref.version) != (policy.policy_id, policy.version):
                raise ValueError("pinned Rule Policy identity mismatch")
        return self


class BoundRuleSet(ContractModel):
    rule_set_ref: RuleSetContentRef
    definition: RuleSetDefinition
    rules: RecordList[BoundRule]

    @model_validator(mode="after")
    def check_identity(self) -> Self:
        if (self.rule_set_ref.artifact_id, self.rule_set_ref.version) != (
            self.definition.rule_set_id,
            self.definition.version,
        ):
            raise ValueError("pinned RuleSet identity mismatch")
        if tuple(item.rule_ref for item in self.rules) != self.definition.rule_refs:
            raise ValueError("Rule view must exhaust pinned RuleSet membership")
        return self


class ProfileRuleScope(ContractModel):
    """Exact Profile forward membership as supplied by the pinned Bundle view."""

    profile_ref: ProfileContentRef
    rule_sets: RecordList[BoundRuleSet]

    @model_validator(mode="after")
    def check_rules(self) -> Self:
        _index(self.rule_sets, lambda item: item.rule_set_ref)
        return self


class SelectedApplicabilityRecords(ContractModel):
    scope: ApplicabilityScope
    records: ApplicabilityRecordSet


class DecisionCoverageRecords(ContractModel):
    """Caller-selected records for checking one coverage revision."""

    profile_rule_scope: RecordList[ProfileRuleScope]
    applicability: SelectedApplicabilityRecords | None = None
    objects: RecordList[ObjectDescriptor]
    node_runs: RecordList[NodeRunDescriptor]
    attempts: RecordList[AttemptDescriptor]
    decisions: RecordList[DecisionRecord]
    predictions: RecordList[CandidatePrediction]
    model_invocations: RecordList[ModelInvocation]
    facts: RecordList[FactRecord]
    authoritative_selections: RecordList[AuthoritativeResultSelection]
    evidence: RecordList[EvidenceAsset]


def _index[K, T](items: tuple[T, ...], key: Callable[[T], K]) -> dict[K, T]:
    result = {key(item): item for item in items}
    if len(result) != len(items):
        raise ValueError("finite records contain duplicate identities")
    return result


def _get[K, T](items: dict[K, T], key: K, label: str) -> T:
    if key not in items:
        raise ValueError(f"missing or unselected {label}")
    return items[key]


@dataclass(frozen=True, slots=True)
class _GroundingFailureWitness:
    """A selected failed Profile result within a completed aggregate N03 call."""

    profile_ref: ProfileContentRef
    grounding_ref: GroundingRecordRef
    node_run_ref: NodeRunRef
    attempt_ref: AttemptRef


@dataclass(frozen=True, slots=True)
class _GroundingNotEvaluatedWitness:
    """A journal-verified Profile gap within a successful aggregate N03."""

    profile_ref: ProfileContentRef
    node_run_ref: NodeRunRef
    attempt_ref: AttemptRef
    dispatch_id: RecordId


class _CoverageCheck:
    def __init__(
        self,
        coverage: ExecutionCoverage,
        records: DecisionCoverageRecords,
        grounding_failures: tuple[_GroundingFailureWitness, ...],
        omitted_grounding: tuple[_GroundingNotEvaluatedWitness, ...],
    ) -> None:
        self.grounding_omissions = _index(
            omitted_grounding, lambda item: (item.profile_ref, item.node_run_ref)
        )
        self.grounding_failures = {
            (item.profile_ref, item.node_run_ref): item for item in grounding_failures
        }
        self.coverage = coverage
        self.records = records
        self.objects = _index(records.objects, lambda item: item.object_ref)
        self.nodes = _index(records.node_runs, lambda item: item.node_run_ref)
        self.attempts = _index(records.attempts, lambda item: item.attempt_ref)
        self.decisions = _index(records.decisions, lambda item: item.decision_ref)
        self.predictions = _index(records.predictions, lambda item: item.prediction_ref)
        self.invocations = _index(
            records.model_invocations, lambda item: item.model_invocation_id
        )
        self.facts = _index(records.facts, lambda item: item.fact_id)
        self.selections = _index(
            records.authoritative_selections, lambda item: item.selection_id
        )
        self.evidence = _index(records.evidence, lambda item: item.evidence_id)
        self.ledger = _index(
            coverage.mandatory_rule_coverage.obligations,
            lambda item: (item.profile_ref, item.target),
        )
        selected = records.applicability
        self.traces = (
            _index(selected.records.traces, lambda item: item.trace_ref)
            if selected
            else {}
        )
        self.rule_sets = (
            _index(
                selected.records.rule_set_evaluations, lambda item: item.evaluation_ref
            )
            if selected
            else {}
        )
        self.rules = (
            _index(selected.records.rule_evaluations, lambda item: item.evaluation_ref)
            if selected
            else {}
        )
        self.runs_by_object = _index(records.decisions, lambda item: item.object_ref)
        self.paths = {
            item.profile_ref: {
                RulePath(
                    rule_set_ref=rule_set.rule_set_ref, rule_ref=rule.rule_ref
                ): rule
                for rule_set in item.rule_sets
                for rule in rule_set.rules
            }
            for item in records.profile_rule_scope
        }
        self.pin = (coverage.execution_id, coverage.release_id, coverage.bundle_digest)

    def check_scope(self) -> None:
        scope, selected = self.coverage.applicability_scope, self.records.applicability
        if (
            tuple(item.profile_ref for item in self.records.profile_rule_scope)
            != scope.configured_profile_refs
        ):
            raise ValueError(
                "finite forward membership must exhaust configured Profiles"
            )
        if object_profiles := {
            ref: profile.profile_ref
            for profile in self.coverage.profiles
            for ref in profile.object_refs
        }:
            if self.objects.keys() != object_profiles.keys() or any(
                (item.execution_id, item.profile_ref)
                != (self.coverage.execution_id, object_profiles[ref])
                for ref, item in self.objects.items()
            ):
                raise ValueError(
                    "Objects must exhaust the exact Execution/Profile coverage"
                )
        elif self.objects:
            raise ValueError("coverage did not retain supplied Objects")
        if isinstance(scope, BlockedCoverageScope):
            if selected is not None or self.decisions or self.predictions:
                raise ValueError("blocked N01 cannot supply gate or N06 records")
            self.check_blocker(scope.blocker_node_run_ref, "N01", require_failure=True)
            return
        if selected is None:
            raise ValueError("selected Profile gate records are required")
        app = selected.scope
        evaluated = selected.records
        validate_applicability_records(app, evaluated)
        if (
            (app.execution_id, app.release_id, app.bundle_digest) != self.pin
            or app.context_snapshot_ref != scope.context_snapshot_ref
            or app.configured_profile_refs != scope.configured_profile_refs
            or evaluated.profile_eligibility.evaluation_ref
            != scope.profile_eligibility_evaluation_ref
        ):
            raise ValueError(
                "Coverage must use the pinned selected applicability scope"
            )
        members = {item.target_ref: item.child_refs for item in app.target_bindings}
        for item in self.records.profile_rule_scope:
            if tuple(group.rule_set_ref for group in item.rule_sets) != members.get(
                item.profile_ref
            ):
                raise ValueError(
                    "Profile RuleSets must exhaust pinned forward membership"
                )
            for group in item.rule_sets:
                if group.definition.rule_refs != members.get(group.rule_set_ref):
                    raise ValueError(
                        "RuleSet Rules must exhaust pinned forward membership"
                    )
        gate = evaluated.profile_eligibility
        if not set(self.coverage.hit_profile_refs) <= set(gate.eligible_profiles):
            raise ValueError("discovery hits must be eligible Profiles")
        if not gate.eligible_profiles:
            if self.coverage.discovery_outcome is not DiscoveryOutcome.NOT_EVALUATED:
                raise ValueError("no eligible Profiles forbids actual discovery")
            reason = (
                CoverageReason.NO_ELIGIBLE_PROFILES
                if gate.configured_profile_refs
                else CoverageReason.EMPTY_CONFIGURED_SCOPE
            )
            if reason not in self.coverage.reasons:
                raise ValueError(
                    "empty eligible scope requires its typed coverage reason"
                )
        successful_profiles: set[ProfileContentRef] = set()
        eligible_profiles = set(gate.eligible_profiles)
        for ref in self.coverage.discovery_model_invocation_refs:
            invocation = _get(self.invocations, ref.record_id, "discovery invocation")
            profiles = set(invocation.profile_refs)
            if (
                ModelCapability.PROFILE_DISCOVERY not in invocation.purposes
                or invocation.execution_id != self.coverage.execution_id
                or invocation.node_id != "N02"
                or not profiles <= eligible_profiles
            ):
                raise ValueError(
                    "discovery invocation must concern eligible Profiles in this Execution"
                )
            if invocation.outcome.status == "succeeded":
                if successful_profiles & profiles:
                    raise ValueError(
                        "selected discovery success groups must not overlap"
                    )
                successful_profiles.update(profiles)
        if not set(self.coverage.hit_profile_refs) <= successful_profiles:
            raise ValueError("hit Profiles require successful discovery invocations")
        if (
            self.coverage.discovery_outcome
            in (DiscoveryOutcome.HITS, DiscoveryOutcome.NO_HITS)
            and successful_profiles != eligible_profiles
        ):
            raise ValueError(
                "successful discovery binding groups must cover all eligible Profiles"
            )
        if set(self.coverage.unresolved_obligation_refs) != {
            item.obligation_ref for item in evaluated.unresolved_obligations
        }:
            raise ValueError("Coverage must retain all selected unresolved obligations")

    def check_blocker(
        self, ref: NodeRunRef, expected_node: str, *, require_failure: bool
    ) -> None:
        node = _get(self.nodes, ref, "upstream lifecycle blocker")
        if (
            node.execution_id != self.coverage.execution_id
            or node.node_id != expected_node
            or node.status
            in (NodeRunStatus.PENDING, NodeRunStatus.READY, NodeRunStatus.RUNNING)
            or (require_failure and node.status is NodeRunStatus.SUCCEEDED)
        ):
            raise ValueError(
                "lifecycle blocker must cite the corresponding resolved dependency"
            )

    def expected_obligations(self) -> set[_TargetKey]:
        expected: set[_TargetKey] = set()
        profiles = {item.profile_ref: item for item in self.coverage.profiles}
        for item in self.records.profile_rule_scope:
            refs = profiles[item.profile_ref].object_refs
            object_scopes: tuple[ObjectRef | None, ...] = refs or (None,)
            expected.add((item.profile_ref, ProfileObligationTarget(scope="profile")))
            for group in item.rule_sets:
                for obj in object_scopes:
                    object_data = {} if obj is None else {"object_ref": obj}
                    expected.add(
                        (
                            item.profile_ref,
                            RuleSetObligationTarget(
                                scope="rule_set",
                                rule_set_ref=group.rule_set_ref,
                                **object_data,
                            ),
                        )
                    )
                    for rule in group.rules:
                        if rule.rule_policy.policy.mandatory:
                            expected.add(
                                (
                                    item.profile_ref,
                                    RuleObligationTarget(
                                        scope="rule",
                                        rule_set_ref=group.rule_set_ref,
                                        rule_ref=rule.rule_ref,
                                        **object_data,
                                    ),
                                )
                            )
        return expected

    def check_ledger(self) -> None:
        if self.ledger.keys() != self.expected_obligations():
            raise ValueError(
                "ledger must exhaust configured Profile, RuleSet and mandatory Rule work"
            )
        by_id = {entry.obligation_id: entry for entry in self.ledger.values()}
        for entry in self.ledger.values():
            result = entry.outcome
            if isinstance(result, DependencyNotEvaluatedObligation):
                self.check_dependency(entry, result)
                continue
            trace = self.check_selected_trace(entry)
            if isinstance(result, ExcludedObligation):
                self.check_truth(trace, RuleTruth.FALSE)
            elif isinstance(result, UnresolvedMandatoryObligation):
                self.check_truth(trace, RuleTruth.UNKNOWN)
            elif isinstance(result, AncestorNotEvaluatedObligation):
                level = trace.levels[-1]
                parent = _get(
                    by_id, result.ancestor_obligation_id, "decisive ancestor obligation"
                )
                parent_outcome = parent.outcome
                if (
                    not isinstance(level, NotEvaluatedApplicabilityLevel)
                    or level.decisive_ancestor_ref != result.decisive_ancestor_ref
                    or parent.profile_ref != entry.profile_ref
                    or not isinstance(
                        parent_outcome,
                        ExcludedObligation | UnresolvedMandatoryObligation,
                    )
                    or parent_outcome.selected_applicability.evaluation_ref
                    != result.decisive_ancestor_ref
                    or self.traces[
                        parent_outcome.selected_applicability.trace_ref
                    ].levels[-1]
                    != trace.levels[
                        len(
                            self.traces[
                                parent_outcome.selected_applicability.trace_ref
                            ].levels
                        )
                        - 1
                    ]
                ):
                    raise ValueError(
                        "projected obligation must link to its precise decisive ancestor"
                    )
                if isinstance(parent.target, RuleSetObligationTarget) and (
                    not isinstance(entry.target, RuleObligationTarget)
                    or parent.target.rule_set_ref != entry.target.rule_set_ref
                    or parent.target.object_ref != entry.target.object_ref
                ):
                    raise ValueError(
                        "decisive RuleSet ancestor must share RuleSet and Object scope"
                    )
            else:
                self.check_truth(trace, RuleTruth.TRUE)
                if isinstance(result, ApplicableNotEvaluatedObligation):
                    self.check_blocker(
                        result.blocker_node_run_ref, "N06", require_failure=True
                    )
                elif isinstance(result, EvaluatedObligation | GuardBlockedObligation):
                    self.check_recorded_rule(entry, result)

    def check_dependency(
        self, entry: MandatoryRuleObligation, result: DependencyNotEvaluatedObligation
    ) -> None:
        coverage = self.coverage
        if isinstance(coverage.applicability_scope, BlockedCoverageScope):
            if (result.blocker_node_run_ref, result.reason) != (
                coverage.applicability_scope.blocker_node_run_ref,
                CoverageReason.CONTEXT_BUILD_BLOCKED,
            ):
                raise ValueError("unreached obligations must retain the N01 blocker")
            return
        target = entry.target
        if isinstance(target, ProfileObligationTarget):
            raise ValueError("evaluated gate requires one selected Profile trace")
        parent_target = _parent(target)
        parent = self.ledger[entry.profile_ref, parent_target]
        if not isinstance(
            parent.outcome, IncludedObligation | DependencyNotEvaluatedObligation
        ):
            raise ValueError(
                "an applicability ancestor requires a projection, not a dependency blocker"
            )
        profile = next(
            item for item in coverage.profiles if item.profile_ref == entry.profile_ref
        )
        reason = result.reason
        expected_node, allowed = {
            CoverageReason.DISCOVERY_FAILED: (
                "N02",
                coverage.discovery_outcome is DiscoveryOutcome.FAILED,
            ),
            CoverageReason.DISCOVERY_NO_HITS: (
                "N02",
                coverage.discovery_outcome is DiscoveryOutcome.NO_HITS,
            ),
            CoverageReason.DISCOVERY_NOT_EVALUATED: (
                "N02",
                coverage.discovery_outcome is DiscoveryOutcome.NOT_EVALUATED,
            ),
            CoverageReason.PROFILE_NOT_DISCOVERED: (
                "N02",
                entry.profile_ref not in coverage.hit_profile_refs,
            ),
            CoverageReason.GROUNDING_FAILED: (
                "N03",
                profile.grounding_outcome is GroundingOutcome.FAILED,
            ),
            CoverageReason.GROUNDING_NO_TARGET: (
                "N03",
                profile.grounding_outcome is GroundingOutcome.NO_TARGET,
            ),
            CoverageReason.GROUNDING_PARTIAL: (
                "N03",
                profile.grounding_outcome is GroundingOutcome.PARTIAL
                and target.object_ref is None,
            ),
            CoverageReason.GROUNDING_NOT_EVALUATED: (
                "N03",
                profile.grounding_outcome is GroundingOutcome.NOT_EVALUATED,
            ),
            CoverageReason.DECISION_NOT_EVALUATED: (
                "N06",
                target.object_ref is not None
                and target.object_ref not in self.runs_by_object,
            ),
        }.get(reason, ("", False))
        if not allowed:
            raise ValueError(
                "dependency reason must match the observed discovery, grounding or Decision gap"
            )
        witness: _GroundingFailureWitness | _GroundingNotEvaluatedWitness | None = None
        key = (entry.profile_ref, result.blocker_node_run_ref)
        if reason is CoverageReason.GROUNDING_FAILED:
            witness = self.grounding_failures.get(key)
        elif reason is CoverageReason.GROUNDING_NOT_EVALUATED:
            witness = self.grounding_omissions.get(key)
        checked_profile_gap = False
        if witness is not None:
            attempt = _get(
                self.attempts, witness.attempt_ref, "grounding witness Attempt"
            )
            checked_profile_gap = (
                attempt.status is AttemptStatus.SUCCEEDED
                and attempt.execution_id == self.coverage.execution_id
                and isinstance(attempt.owner, NodeAttemptOwner)
                and attempt.owner.node_run_ref == witness.node_run_ref
            )
        self.check_blocker(
            result.blocker_node_run_ref,
            expected_node,
            require_failure=not checked_profile_gap
            and reason
            in (
                CoverageReason.DISCOVERY_FAILED,
                CoverageReason.DISCOVERY_NOT_EVALUATED,
                CoverageReason.GROUNDING_FAILED,
                CoverageReason.GROUNDING_NOT_EVALUATED,
                CoverageReason.DECISION_NOT_EVALUATED,
            ),
        )

    def check_truth(self, trace: ApplicabilityTrace, expected: RuleTruth) -> None:
        if (
            isinstance(trace.levels[-1], NotEvaluatedApplicabilityLevel)
            or trace.effective_truth is not expected
        ):
            raise ValueError(
                "ledger disposition must reflect the actual target applicability"
            )

    def check_selected_trace(
        self, entry: MandatoryRuleObligation
    ) -> ApplicabilityTrace:
        outcome = entry.outcome
        assert not isinstance(outcome, DependencyNotEvaluatedObligation)
        selection = outcome.selected_applicability
        trace = _get(self.traces, selection.trace_ref, "applicability trace")
        target = entry.target
        if trace.profile_ref != entry.profile_ref:
            raise ValueError("selected applicability trace belongs to another Profile")
        if isinstance(target, ProfileObligationTarget):
            app = self.records.applicability
            if (
                app is None
                or selection.evaluation_ref
                != app.records.profile_eligibility.evaluation_ref
                or trace.target_ref != entry.profile_ref
            ):
                raise ValueError(
                    "Profile obligation must retain its actual selected gate trace"
                )
            self.check_unresolved(entry, trace, None)
            return trace
        if isinstance(target, RuleSetObligationTarget):
            if (
                not isinstance(
                    selection.evaluation_ref, RuleSetApplicabilityEvaluationRef
                )
                or selection.evaluation_ref not in self.rule_sets
            ):
                raise ValueError(
                    "RuleSet obligation requires a selected RuleSet evaluation"
                )
            wrapper: RuleSetApplicabilityEvaluation | RuleApplicabilityEvaluation = (
                self.rule_sets[selection.evaluation_ref]
            )
        else:
            if (
                not isinstance(selection.evaluation_ref, RuleApplicabilityEvaluationRef)
                or selection.evaluation_ref not in self.rules
            ):
                raise ValueError("Rule obligation requires a selected Rule evaluation")
            wrapper = self.rules[selection.evaluation_ref]
            if wrapper.rule_ref != target.rule_ref:
                raise ValueError(
                    "selected Rule applicability has another Rule identity"
                )
            parent = self.ledger[entry.profile_ref, _parent(target)].outcome
            if isinstance(parent, DependencyNotEvaluatedObligation) or (
                wrapper.rule_set_applicability_evaluation_ref
                != parent.selected_applicability.evaluation_ref
            ):
                raise ValueError(
                    "Rule applicability must pin its ledger-selected RuleSet revision"
                )
        if (
            wrapper.profile_ref,
            wrapper.rule_set_ref,
            wrapper.applicability_trace_ref,
        ) != (entry.profile_ref, target.rule_set_ref, selection.trace_ref) or (
            wrapper.object_ref is not None and wrapper.object_ref != target.object_ref
        ):
            raise ValueError(
                "selected applicability revision must match its exact ledger scope"
            )
        self.check_unresolved(entry, trace, target.object_ref)
        return trace

    def check_unresolved(
        self,
        entry: MandatoryRuleObligation,
        trace: ApplicabilityTrace,
        object_ref: ObjectRef | None,
    ) -> None:
        outcome = entry.outcome
        if not isinstance(outcome, UnresolvedMandatoryObligation):
            return
        assert self.records.applicability is not None
        if not any(
            item.obligation_ref == outcome.unresolved_obligation_ref
            and item.applicability_trace_ref == trace.trace_ref
            and item.object_ref == object_ref
            for item in self.records.applicability.records.unresolved_obligations
        ):
            raise ValueError(
                "unresolved record must describe the selected target and scope"
            )

    def check_recorded_rule(
        self,
        entry: MandatoryRuleObligation,
        result: EvaluatedObligation | GuardBlockedObligation,
    ) -> None:
        decision = _get(self.decisions, result.decision_ref, "Object Decision")
        target = entry.target
        assert isinstance(target, RuleObligationTarget)
        if (decision.profile_ref, decision.object_ref) != (
            entry.profile_ref,
            target.object_ref,
        ):
            raise ValueError("Rule obligation points to another Object Decision")
        item = next(
            (row for row in decision.rule_evaluations if row.rule == target.path), None
        )
        if item is None or (
            item.selected_rule_evaluation_ref,
            item.applicability_trace_ref,
        ) != (
            result.selected_applicability.evaluation_ref,
            result.selected_applicability.trace_ref,
        ):
            raise ValueError(
                "Object Decision did not retain the ledger-selected Rule revision"
            )
        expected = (
            EvaluatedRuleOutcome
            if isinstance(result, EvaluatedObligation)
            else BlockedRuleOutcome
        )
        if not isinstance(item.result, expected):
            raise ValueError("Rule outcome contradicts the recorded ledger branch")

    def check_decisions(self) -> None:
        matches: set[CoverageMatch] = set()
        predictions = set()
        scope = self.coverage.applicability_scope
        if isinstance(scope, BlockedCoverageScope):
            return
        for decision in self.decisions.values():
            obj = _get(self.objects, decision.object_ref, "Decision Object")
            if (
                (decision.execution_id, decision.release_id, decision.bundle_digest)
                != self.pin
                or decision.profile_ref != obj.profile_ref
                or decision.context_snapshot_ref != scope.context_snapshot_ref
                or decision.profile_eligibility_evaluation_ref
                != scope.profile_eligibility_evaluation_ref
                or decision.execution_coverage_ref != self.coverage.coverage_ref
            ):
                raise ValueError(
                    "Decision must keep its exact Execution, Object, Gate and Coverage pins"
                )
            node = _get(self.nodes, decision.node_run_ref, "Decision node")
            attempt = _get(self.attempts, decision.attempt_ref, "Decision attempt")
            if (
                node.node_id != "N06"
                or node.execution_id != self.coverage.execution_id
                or attempt.execution_id != node.execution_id
                or attempt.status is not AttemptStatus.SUCCEEDED
                or not isinstance(attempt.owner, NodeAttemptOwner)
                or attempt.owner.node_run_ref != decision.node_run_ref
            ):
                raise ValueError(
                    "Decision requires its actual N06 node and selected successful attempt"
                )
            if decision.candidate_prediction_ref is not None:
                self.check_prediction(
                    decision,
                    _get(
                        self.predictions,
                        decision.candidate_prediction_ref,
                        "candidate prediction",
                    ),
                )
                predictions.add(decision.candidate_prediction_ref)
            matches.update(self.check_decision_rules(decision))
        if set(self.coverage.matches) != matches:
            raise ValueError(
                "Execution summary must retain every Object's valid matches"
            )
        if predictions != self.predictions.keys():
            raise ValueError("candidate records must belong to the supplied Decisions")

    def check_prediction(
        self, decision: DecisionRecord, prediction: CandidatePrediction
    ) -> None:
        if (
            prediction.execution_id,
            prediction.release_id,
            prediction.bundle_digest,
        ) != self.pin or (
            prediction.profile_ref,
            prediction.object_ref,
            prediction.context_snapshot_ref,
            prediction.node_run_ref,
            prediction.attempt_ref,
        ) != (
            decision.profile_ref,
            decision.object_ref,
            decision.context_snapshot_ref,
            decision.node_run_ref,
            decision.attempt_ref,
        ):
            raise ValueError(
                "candidate must retain its exact Object and N06 invocation scope"
            )
        invocation = _get(
            self.invocations,
            prediction.model_invocation_ref.record_id,
            "candidate model invocation",
        )
        if (
            invocation.node_id != "N06"
            or (
                invocation.execution_id,
                invocation.object_ref,
                invocation.node_run_ref,
                invocation.attempt_ref,
            )
            != (
                decision.execution_id,
                decision.object_ref,
                decision.node_run_ref,
                decision.attempt_ref,
            )
            or invocation.profile_refs != (decision.profile_ref,)
            or (
                prediction.status is CandidatePredictionStatus.SUCCEEDED
                and invocation.outcome.status != "succeeded"
            )
        ):
            raise ValueError(
                "CandidatePrediction must cite the exact attributed model invocation"
            )

    def check_decision_rules(self, decision: DecisionRecord) -> set[CoverageMatch]:
        bindings = self.paths[decision.profile_ref]
        evaluated = {item.rule: item for item in decision.rule_evaluations}
        ledger = tuple(
            item
            for (profile_ref, target), item in self.ledger.items()
            if profile_ref == decision.profile_ref
            and (
                isinstance(target, ProfileObligationTarget)
                or target.object_ref == decision.object_ref
            )
        )
        if set(decision.explanation.obligation_ids) != {
            item.obligation_id for item in ledger
        }:
            raise ValueError("Decision must retain all its mandatory obligation IDs")
        trace_refs = {
            entry.outcome.selected_applicability.trace_ref
            for entry in ledger
            if not isinstance(entry.outcome, DependencyNotEvaluatedObligation)
        }
        expected_ne = set()
        mandatory_truths: list[RuleTruth] = []
        mandatory_gap = any(
            isinstance(entry.target, RuleSetObligationTarget)
            and isinstance(entry.outcome, UnresolvedMandatoryObligation)
            for entry in ledger
        )
        for entry in ledger:
            target = entry.target
            if not isinstance(target, RuleObligationTarget):
                continue
            result = entry.outcome
            item = evaluated.get(target.path)
            if isinstance(result, ExcludedObligation):
                if item is None or not isinstance(item.result, SkippedRuleOutcome):
                    raise ValueError(
                        "proven Rule exclusion must remain in the skipped Rule refs"
                    )
            elif isinstance(
                result,
                AncestorNotEvaluatedObligation
                | DependencyNotEvaluatedObligation
                | ApplicableNotEvaluatedObligation
                | UnresolvedMandatoryObligation,
            ):
                expected_ne.add(target.path)
                mandatory_gap |= not _proven_excluded(entry, self.ledger)
            elif isinstance(result, GuardBlockedObligation):
                mandatory_gap = True
            elif item and isinstance(item.result, EvaluatedRuleOutcome):
                mandatory_truths.append(item.result.predicate.truth)
                mandatory_gap |= item.result.predicate.truth is RuleTruth.UNKNOWN
        if expected_ne != set(decision.explanation.not_evaluated_rules):
            raise ValueError("Decision must retain every not_evaluated mandatory Rule")
        categories = set()
        applicable_paths = {
            entry.target.path
            for entry in ledger
            if isinstance(entry.target, RuleObligationTarget)
            and isinstance(entry.outcome, ApplicableNotEvaluatedObligation)
        }
        for item in evaluated.values():
            binding = _get(bindings, item.rule, "configured Rule path")
            trace = self.check_decision_rule(decision, item, binding)
            trace_refs.add(trace.trace_ref)
            if trace.effective_truth is RuleTruth.TRUE:
                applicable_paths.add(item.rule)
            if isinstance(item.result, EvaluatedRuleOutcome) and item.result.matched:
                categories.add(binding.definition.violation_category)
        if (
            set(decision.explanation.applicability_trace_refs) != trace_refs
            or set(decision.explanation.applicable_rules) != applicable_paths
        ):
            raise ValueError(
                "Decision explanation must retain applicable Rules and selected trace refs"
            )
        if categories != set(decision.conflict_resolution.matched_category_ids):
            raise ValueError(
                "collect_all must retain the set of all matched Rule categories"
            )
        policy_refs = {
            reference_identity(rule_set.definition.conflict_policy_ref)
            for profile in self.records.profile_rule_scope
            if profile.profile_ref == decision.profile_ref
            for rule_set in profile.rule_sets
        }
        if {
            reference_identity(ref)
            for ref in decision.conflict_resolution.conflict_policy_refs
        } != policy_refs:
            raise ValueError(
                "collect_all must pin the configured RuleSet conflict policies"
            )
        matches = {
            CoverageMatch(
                object_ref=decision.object_ref,
                profile_ref=decision.profile_ref,
                decision_ref=decision.decision_ref,
                rule=rule,
            )
            for rule in decision.conflict_resolution.matched_rules
        }
        if not matches:
            required_result = (
                DecisionResult.UNCERTAIN
                if mandatory_gap
                else DecisionResult.NO_VIOLATION
                if mandatory_truths
                else DecisionResult.NOT_APPLICABLE
            )
            if decision.result is not required_result:
                raise ValueError("Decision result contradicts supplied mandatory work")
        return matches

    def check_decision_rule(
        self, decision: DecisionRecord, item: RuleEvaluation, binding: BoundRule
    ) -> ApplicabilityTrace:
        wrapper = _get(
            self.rules, item.selected_rule_evaluation_ref, "Decision Rule evaluation"
        )
        trace = _get(self.traces, item.applicability_trace_ref, "Decision Rule trace")
        if (
            (
                wrapper.profile_ref,
                wrapper.rule_set_ref,
                wrapper.rule_ref,
                wrapper.applicability_trace_ref,
                wrapper.rule_set_applicability_evaluation_ref,
            )
            != (
                decision.profile_ref,
                item.rule.rule_set_ref,
                item.rule.rule_ref,
                item.applicability_trace_ref,
                item.selected_rule_set_evaluation_ref,
            )
            or wrapper.object_ref != decision.object_ref
            or (wrapper.node_run_ref, wrapper.attempt_ref)
            != (decision.node_run_ref, decision.attempt_ref)
        ):
            raise ValueError(
                "Decision must use the selected Rule revision from its N06 Object Attempt"
            )
        obligation = self.ledger.get(
            (
                decision.profile_ref,
                RuleObligationTarget(
                    scope="rule",
                    rule_set_ref=item.rule.rule_set_ref,
                    rule_ref=item.rule.rule_ref,
                    object_ref=decision.object_ref,
                ),
            )
        )
        if binding.rule_policy.policy.mandatory and (
            obligation is None
            or isinstance(obligation.outcome, DependencyNotEvaluatedObligation)
            or obligation.outcome.selected_applicability.evaluation_ref
            != item.selected_rule_evaluation_ref
        ):
            raise ValueError(
                "mandatory Rule must use the ledger-selected applicability revision"
            )
        parent = self.ledger[
            decision.profile_ref,
            RuleSetObligationTarget(
                scope="rule_set",
                rule_set_ref=item.rule.rule_set_ref,
                object_ref=decision.object_ref,
            ),
        ].outcome
        if isinstance(parent, DependencyNotEvaluatedObligation) or (
            parent.selected_applicability.evaluation_ref
            != item.selected_rule_set_evaluation_ref
        ):
            raise ValueError(
                "Decision Rule must use the ledger-selected RuleSet revision"
            )
        if isinstance(item.result, SkippedRuleOutcome):
            if item.result.reason is RuleSkipReason.OPTIONAL_NOT_SELECTED:
                if binding.rule_policy.policy.mandatory:
                    raise ValueError("candidate priority cannot skip a mandatory Rule")
                self.check_truth(trace, RuleTruth.TRUE)
            else:
                self.check_truth(trace, RuleTruth.FALSE)
            return trace
        self.check_truth(trace, RuleTruth.TRUE)
        self.check_guard(decision, item, binding, item.result)
        return trace

    def check_guard(
        self,
        decision: DecisionRecord,
        item: RuleEvaluation,
        binding: BoundRule,
        outcome: EvaluatedRuleOutcome | BlockedRuleOutcome,
    ) -> None:
        guard = outcome.guard
        declared = set(binding.definition.required_fact_selectors)
        proven = {proof.selector for proof in guard.fact_proofs}
        if not proven <= declared:
            raise ValueError("guard proof is not a declared required Fact")
        if not {proof.selector for proof in item.optional_fact_proofs} <= set(
            binding.definition.optional_fact_selectors
        ):
            raise ValueError("optional Fact proof is not a declared optional selector")
        for ref in item.fact_refs:
            fact = _get(self.facts, ref.record_id, "Rule Fact")
            if fact.execution_id != decision.execution_id or (
                fact.scope is FactScope.OBJECT
                and (fact.object_id, fact.profile_ref)
                != (decision.object_ref.record_id, decision.profile_ref)
            ):
                raise ValueError(
                    "Rule Fact belongs to another Execution or Object/Profile"
                )
        for proof in guard.fact_proofs:
            self.check_proof(proof, require_known=True)
        for proof in item.optional_fact_proofs:
            self.check_proof(proof, require_known=False)
        evidence_types = set()
        policy = binding.evidence_policy.policy
        for evidence_ref in item.evidence_refs:
            asset = _get(self.evidence, evidence_ref.record_id, "Rule Evidence")
            if asset.scope.execution_id != decision.execution_id or (
                isinstance(asset.scope, ObjectEvidenceScope)
                and (asset.scope.object_ref, asset.scope.profile_ref)
                != (decision.object_ref, decision.profile_ref)
            ):
                raise ValueError(
                    "Rule Evidence belongs to another Execution or Object/Profile"
                )
            if asset.evidence_type not in policy.allowed_evidence_types:
                raise ValueError(
                    "Rule Evidence type is outside the pinned EvidencePolicy"
                )
            if evidence_ref in guard.required_evidence_refs:
                if not isinstance(asset.stage, FinalizedAsset):
                    raise ValueError(
                        "required evidence proof must cite a finalized asset"
                    )
                evidence_types.add(asset.evidence_type)
        expected_types = set(policy.required_evidence_types)
        if policy.original_media_required:
            expected_types.add(EvidenceType.ORIGINAL_IMAGE)
        missing_facts = bool(declared - proven)
        missing_evidence = bool(expected_types - evidence_types)
        if isinstance(guard, CompleteRequiredEvidenceGuard):
            if missing_facts or missing_evidence:
                raise ValueError(
                    "complete guard must retain every required Fact proof and Evidence type"
                )
        else:
            reports_fact_gap = any(
                reason in guard.reasons
                for reason in (
                    RequiredEvidenceReason.REQUIRED_FACT_UNAVAILABLE,
                    RequiredEvidenceReason.PRODUCER_AUTHORITY_UNRESOLVED,
                )
            )
            reports_evidence_gap = (
                RequiredEvidenceReason.EVIDENCE_UNAVAILABLE in guard.reasons
            )
            if (missing_facts, missing_evidence) != (
                reports_fact_gap,
                reports_evidence_gap,
            ):
                raise ValueError(
                    "guard blockers must describe the missing required input proofs"
                )

    def check_proof(self, proof: RequiredFactProof, *, require_known: bool) -> None:
        fact = _get(self.facts, proof.fact_ref.record_id, "Rule operand Fact")
        if require_known and not isinstance(fact, KnownFact):
            raise ValueError("required guard proof requires a known Fact")
        selector = proof.selector
        if (selector.scope, selector.fact_key, selector.fact_schema_ref) != (
            fact.scope,
            fact.fact_key,
            fact.fact_schema_ref,
        ):
            raise ValueError("guard Fact must match selector key, Schema and scope")
        if require_known and isinstance(fact, KnownFact):
            value = fact.value
            for field in selector.field_path:
                if not isinstance(value, RecordValue) or field not in value.value:
                    raise ValueError("required Fact Record field is unavailable")
                value = value.value[field]
        selection = _get(
            self.selections,
            proof.authority_selection_ref.record_id,
            "authoritative selection revision",
        )
        if (
            (
                selection.execution_id,
                selection.scope,
                selection.object_id,
                selection.profile_ref,
            )
            != (fact.execution_id, fact.scope, fact.object_id, fact.profile_ref)
            or proof.fact_ref not in selection.selected_fact_refs
            or fact.attempt_id != selection.selected_attempt_id
        ):
            raise ValueError(
                "guard must cite the exact authoritative Fact and Attempt revision"
            )
        producer, source = selector.producer_selector, fact.source
        if isinstance(producer, QuestionProducerSelector) and isinstance(
            source, QuestionFactSource
        ):
            valid = (
                producer.question_ref == source.question_ref
                and isinstance(selection, ObservationBatchSelection)
                and selection.selected_result_ref == source.observation_batch_ref
            )
        elif isinstance(producer, ToolProducerSelector) and isinstance(
            source, ToolFactSource
        ):
            valid = (
                (
                    producer.tool_plan_ref,
                    producer.step_id,
                    producer.tool_ref,
                    producer.output_name,
                )
                == (
                    source.tool_plan_ref,
                    source.step_id,
                    source.tool_ref,
                    source.output_name,
                )
                and isinstance(selection, ToolStepSelection)
                and (
                    selection.selected_result_ref,
                    selection.tool_plan_ref,
                    selection.step_id,
                )
                == (source.tool_execution_ref, source.tool_plan_ref, source.step_id)
            )
        elif isinstance(producer, SystemProducerSelector) and isinstance(
            source, SystemFactSource
        ):
            valid = (
                (producer.requirement, producer.output_name)
                == (source.requirement, source.output_name)
                and isinstance(selection, SystemOutputSelection)
                and (
                    selection.selected_result_ref,
                    selection.requirement,
                    selection.output_name,
                )
                == (source.system_output_ref, source.requirement, source.output_name)
            )
        else:
            valid = False
        if not valid:
            raise ValueError(
                "guard authority must match the declared producer and exact source result"
            )

    def check_summary(self) -> None:
        profile_statuses: list[CoverageStatus] = []
        applicable_false = False
        eligible = {item.profile_ref for item in self.coverage.profiles}
        if self.records.applicability is not None:
            eligible = set(
                self.records.applicability.records.profile_eligibility.eligible_profiles
            )
        for profile in self.coverage.profiles:
            root = self.ledger[
                profile.profile_ref, ProfileObligationTarget(scope="profile")
            ]
            if isinstance(root.outcome, ExcludedObligation):
                expected = CoverageStatus.NOT_APPLICABLE
            elif not profile.object_refs:
                expected = CoverageStatus.NONE
            else:
                by_object = tuple(
                    self.runs_by_object.get(ref) for ref in profile.object_refs
                )
                no_gaps = (
                    profile.grounding_outcome is GroundingOutcome.OBJECTS
                    and not profile.missing_evidence_refs
                    and all(
                        item is not None and item.result is not DecisionResult.UNCERTAIN
                        for item in by_object
                    )
                    and not any(
                        self.is_required_gap(entry)
                        for (ref, _), entry in self.ledger.items()
                        if ref == profile.profile_ref
                    )
                )
                expected = (
                    CoverageStatus.COMPLETE if no_gaps else CoverageStatus.PARTIAL
                )
                applicable_false |= any(
                    item is not None and item.result is DecisionResult.NO_VIOLATION
                    for item in by_object
                )
            if profile.coverage_status is not expected:
                raise ValueError(
                    "Profile coverage contradicts its grounded Objects and mandatory obligations"
                )
            if (
                isinstance(root.outcome, UnresolvedMandatoryObligation)
                and CoverageReason.APPLICABILITY_UNRESOLVED not in profile.reasons
            ):
                raise ValueError(
                    "Profile coverage must retain its unresolved applicability reason"
                )
            if (
                profile.profile_ref in eligible
                and profile.profile_ref not in self.coverage.hit_profile_refs
                and self.coverage.discovery_outcome is DiscoveryOutcome.HITS
                and CoverageReason.PROFILE_NOT_DISCOVERED not in profile.reasons
            ):
                raise ValueError(
                    "eligible Profile omitted by discovery must remain uncovered"
                )
            profile_statuses.append(expected)
        if profile_statuses and all(
            item is CoverageStatus.NOT_APPLICABLE for item in profile_statuses
        ):
            expected = CoverageStatus.NOT_APPLICABLE
        elif (
            self.coverage.discovery_outcome is DiscoveryOutcome.HITS
            and bool(self.coverage.object_count)
            and all(
                item in (CoverageStatus.COMPLETE, CoverageStatus.NOT_APPLICABLE)
                for item in profile_statuses
            )
        ):
            expected = CoverageStatus.COMPLETE
        elif any(
            item in (CoverageStatus.COMPLETE, CoverageStatus.PARTIAL)
            for item in profile_statuses
        ):
            expected = CoverageStatus.PARTIAL
        else:
            expected = CoverageStatus.NONE
        if self.coverage.coverage_status is not expected:
            raise ValueError(
                "Execution coverage contradicts the supplied Profile and discovery coverage"
            )
        if self.coverage.matches:
            result = DecisionResult.VIOLATION
        elif expected is CoverageStatus.NOT_APPLICABLE or (
            expected is CoverageStatus.COMPLETE and not applicable_false
        ):
            result = DecisionResult.NOT_APPLICABLE
        elif expected is CoverageStatus.COMPLETE:
            result = DecisionResult.NO_VIOLATION
        else:
            result = DecisionResult.UNCERTAIN
        if self.coverage.summary_result is not result:
            raise ValueError(
                "Execution summary contradicts mandatory coverage and retained matches"
            )

    def is_required_gap(self, entry: MandatoryRuleObligation) -> bool:
        outcome = entry.outcome
        if isinstance(outcome, IncludedObligation):
            return False
        if isinstance(outcome, EvaluatedObligation):
            decision = self.decisions[outcome.decision_ref]
            target = entry.target
            assert isinstance(target, RuleObligationTarget)
            return any(
                item.rule == target.path
                and isinstance(item.result, EvaluatedRuleOutcome)
                and item.result.predicate.truth is RuleTruth.UNKNOWN
                for item in decision.rule_evaluations
            )
        return not _proven_excluded(entry, self.ledger)


def _parent(target: RuleSetObligationTarget | RuleObligationTarget) -> ObligationTarget:
    if isinstance(target, RuleSetObligationTarget):
        return ProfileObligationTarget(scope="profile")
    return RuleSetObligationTarget(
        scope="rule_set",
        rule_set_ref=target.rule_set_ref,
        **({} if target.object_ref is None else {"object_ref": target.object_ref}),
    )


def _proven_excluded(
    entry: MandatoryRuleObligation,
    ledger: dict[_TargetKey, MandatoryRuleObligation],
) -> bool:
    if isinstance(entry.outcome, ExcludedObligation):
        return True
    if isinstance(entry.outcome, AncestorNotEvaluatedObligation):
        return any(
            ancestor.obligation_id == entry.outcome.ancestor_obligation_id
            and isinstance(ancestor.outcome, ExcludedObligation)
            for ancestor in ledger.values()
        )
    return False


def validate_decision_coverage(
    coverage: ExecutionCoverage, records: DecisionCoverageRecords
) -> None:
    """Check only supplied records against one explicit revision of finite inputs."""
    _validate_decision_coverage(coverage, records, ())


def _validate_decision_coverage(
    coverage: ExecutionCoverage,
    records: DecisionCoverageRecords,
    grounding_failures: tuple[_GroundingFailureWitness, ...],
    *,
    omitted_grounding: tuple[_GroundingNotEvaluatedWitness, ...] = (),
) -> None:
    checker = _CoverageCheck(
        ExecutionCoverage.model_validate(coverage),
        DecisionCoverageRecords.model_validate(records),
        grounding_failures,
        omitted_grounding,
    )
    checker.check_scope()
    checker.check_ledger()
    checker.check_decisions()
    checker.check_summary()
