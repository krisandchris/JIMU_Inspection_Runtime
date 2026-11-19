"""N06 records advisory prediction and deterministic full-scope Rule results."""

from dataclasses import dataclass
from hashlib import sha256
from typing import Annotated, Literal, Self

from pydantic import BeforeValidator, Field, model_validator

from jimu_runtime.domain.base import ContractModel
from jimu_runtime.domain.definition_support import (
    AbsentOptional,
    OptionalContract,
    immutable_sequence,
    omit_default_from_schema,
)
from jimu_runtime.domain.enums import DecisionResult, NodeRunStatus, RuntimeRecordKind
from jimu_runtime.domain.fact_schemas.canonicalization import canonical_bytes
from jimu_runtime.domain.nodes.envelopes import (
    NodeInputEnvelope,
    NodeOutputEnvelope,
    check_envelope_pair,
)
from jimu_runtime.domain.runtime.applicability import (
    ApplicabilityDisposition,
    ApplicabilityEvaluationRef,
    ApplicabilityLevel,
    ApplicabilityTrace,
    ApplicabilityTraceRef,
    ContextSnapshotRef,
    EvaluatedApplicabilityLevel,
    NotEvaluatedApplicabilityLevel,
    ProfileEligibilityEvaluation,
    ProfileEligibilityEvaluationRef,
    RuleApplicabilityEvaluation,
    RuleSetApplicabilityEvaluation,
    RuleTruth,
)
from jimu_runtime.domain.runtime.coverage import (
    AncestorNotEvaluatedObligation,
    ApplicableNotEvaluatedObligation,
    DependencyNotEvaluatedObligation,
    EvaluatedCoverageScope,
    EvaluatedObligation,
    ExcludedObligation,
    ExecutionCoverage,
    GuardBlockedObligation,
    MandatoryRuleCoverage,
    MandatoryRuleObligation,
    RuleObligationTarget,
    RuleSetObligationTarget,
    UnresolvedMandatoryObligation,
)
from jimu_runtime.domain.runtime.decisions import (
    BlockedRuleOutcome,
    CandidatePrediction,
    CandidatePredictionRef,
    DecisionRecord,
    DecisionRef,
    EvaluatedRuleOutcome,
    ExecutionCoverageRef,
    RulePath,
)
from jimu_runtime.domain.runtime.executions import NodeId
from jimu_runtime.domain.runtime.facts import (
    AuthoritySelectionRef,
    FactRefs,
    TypedRuntimeRef,
    require_unique_runtime_refs,
)
from jimu_runtime.domain.runtime.perception import ObjectRecord


@dataclass(frozen=True, slots=True)
class _ExclusionRecords:
    obligations: dict[str, MandatoryRuleObligation]
    evaluations: dict[
        ApplicabilityEvaluationRef,
        RuleSetApplicabilityEvaluation | RuleApplicabilityEvaluation,
    ]
    traces: dict[ApplicabilityTraceRef, ApplicabilityTrace]
    output_digests: dict[tuple[RuntimeRecordKind, str], str]
    context_digest: str | None

    def matches_output(self, ref: TypedRuntimeRef, record: ContractModel) -> bool:
        expected = self.output_digests.get((ref.record_kind, ref.record_id))
        return (
            expected is not None
            and expected
            == sha256(canonical_bytes(record.model_dump(mode="json"))).hexdigest()
        )

    def proves_exclusion(
        self,
        decision: DecisionRecord,
        target: RuleObligationTarget,
        outcome: AncestorNotEvaluatedObligation,
    ) -> bool:
        parent = self.obligations.get(outcome.ancestor_obligation_id)
        if parent is None or not isinstance(parent.outcome, ExcludedObligation):
            return False
        selected_parent = parent.outcome.selected_applicability
        selected_child = outcome.selected_applicability
        parent_eval = self.evaluations.get(selected_parent.evaluation_ref)
        child_eval = self.evaluations.get(selected_child.evaluation_ref)
        parent_trace = self.traces.get(selected_parent.trace_ref)
        child_trace = self.traces.get(selected_child.trace_ref)
        explanation = decision.explanation
        if (
            parent.target
            != RuleSetObligationTarget(
                scope="rule_set",
                rule_set_ref=target.rule_set_ref,
                object_ref=decision.object_ref,
            )
            or parent.profile_ref != decision.profile_ref
            or parent.obligation_id not in explanation.obligation_ids
            or selected_parent.evaluation_ref != outcome.decisive_ancestor_ref
            or not isinstance(parent_eval, RuleSetApplicabilityEvaluation)
            or not isinstance(child_eval, RuleApplicabilityEvaluation)
            or parent_trace is None
            or child_trace is None
            or parent_trace.target_ref != target.rule_set_ref
            or child_trace.target_ref != target.rule_ref
            or selected_parent.trace_ref not in explanation.applicability_trace_refs
            or selected_child.trace_ref not in explanation.applicability_trace_refs
        ):
            return False
        if (
            parent_eval.rule_set_ref != target.rule_set_ref
            or parent_eval.applicability_trace_ref != parent_trace.trace_ref
            or parent_eval.decisive_ancestor_ref is not None
            or (
                parent_eval.object_ref,
                parent_eval.node_run_ref,
                parent_eval.attempt_ref,
            )
            != (decision.object_ref, decision.node_run_ref, decision.attempt_ref)
            or child_eval.rule_ref != target.rule_ref
            or child_eval.rule_set_ref != target.rule_set_ref
            or child_eval.applicability_trace_ref != child_trace.trace_ref
            or child_eval.rule_set_applicability_evaluation_ref
            != parent_eval.evaluation_ref
            or child_eval.decisive_ancestor_ref != parent_eval.evaluation_ref
            or any(
                ref is not None
                for ref in (
                    child_eval.object_ref,
                    child_eval.node_run_ref,
                    child_eval.attempt_ref,
                )
            )
        ):
            return False
        pin = (
            decision.execution_id,
            decision.release_id,
            decision.bundle_digest,
            decision.context_snapshot_ref,
            decision.profile_ref,
        )
        if any(
            (
                record.execution_id,
                record.release_id,
                record.bundle_digest,
                record.context_snapshot_ref,
                record.profile_ref,
            )
            != pin
            for record in (parent_eval, child_eval, parent_trace, child_trace)
        ) or any(
            record.profile_eligibility_evaluation_ref
            != decision.profile_eligibility_evaluation_ref
            for record in (parent_eval, child_eval)
        ):
            return False
        top, decisive = parent_trace.levels
        successor = child_trace.levels[-1]
        return (
            isinstance(top, EvaluatedApplicabilityLevel)
            and top.truth is RuleTruth.TRUE
            and top.source_evaluation_ref == decision.profile_eligibility_evaluation_ref
            and isinstance(decisive, EvaluatedApplicabilityLevel)
            and decisive.source_evaluation_ref == parent_eval.evaluation_ref
            and decisive.truth is RuleTruth.FALSE
            and isinstance(successor, NotEvaluatedApplicabilityLevel)
            and successor.decisive_ancestor_ref == parent_eval.evaluation_ref
            and child_trace.levels[:-1] == parent_trace.levels
            and parent_trace.context_snapshot_digest
            == child_trace.context_snapshot_digest
            == self.context_digest
            and all(
                row.disposition is ApplicabilityDisposition.EXCLUDED
                and row.effective_truth is RuleTruth.FALSE
                and row.decisive_level is ApplicabilityLevel.RULE_SET
                for row in (parent_trace, child_trace)
            )
            and all(
                self.matches_output(ref, record)
                for ref, record in (
                    (parent_eval.evaluation_ref, parent_eval),
                    (child_eval.evaluation_ref, child_eval),
                    (parent_trace.trace_ref, parent_trace),
                    (child_trace.trace_ref, child_trace),
                )
            )
        )


class DecisionInput(ContractModel):
    schema_version: Literal["1.0.0"]
    envelope: NodeInputEnvelope
    object_record: ObjectRecord
    context_snapshot_ref: ContextSnapshotRef
    profile_eligibility_evaluation_ref: ProfileEligibilityEvaluationRef
    mandatory_rule_paths: Annotated[
        tuple[RulePath, ...],
        Field(max_length=1024, json_schema_extra={"uniqueItems": True}),
        BeforeValidator(immutable_sequence),
    ]
    fact_refs: FactRefs
    authority_selection_refs: Annotated[
        tuple[AuthoritySelectionRef, ...],
        Field(max_length=1024, json_schema_extra={"uniqueItems": True}),
        BeforeValidator(immutable_sequence),
    ]

    @model_validator(mode="after")
    def check_object_scope(self) -> Self:
        if (
            self.envelope.node_id is not NodeId.N06
            or self.object_record.execution_id != self.envelope.execution_id
        ):
            raise ValueError("N06 requires an Object in the pinned Execution")
        if len(set(self.mandatory_rule_paths)) != len(self.mandatory_rule_paths):
            raise ValueError("mandatory Rule paths must be unique")
        require_unique_runtime_refs(self.fact_refs)
        require_unique_runtime_refs(self.authority_selection_refs)
        if self.fact_refs and not self.authority_selection_refs:
            raise ValueError(
                "Rule Fact inputs require explicit Producer Authority revisions"
            )
        return self

    def check_gate(self, gate: ProfileEligibilityEvaluation) -> None:
        env = self.envelope
        if (
            env.execution_id,
            env.release_id,
            env.bundle_digest,
            self.context_snapshot_ref,
            self.profile_eligibility_evaluation_ref,
        ) != (
            gate.execution_id,
            gate.release_id,
            gate.bundle_digest,
            gate.context_snapshot_ref,
            gate.evaluation_ref,
        ) or self.object_record.profile_ref not in gate.eligible_profiles:
            raise ValueError(
                "N06 must inherit this Object Profile's actual Workflow eligibility gate"
            )

    def check_mandatory_scope(self, configured_paths: tuple[RulePath, ...]) -> None:
        if self.mandatory_rule_paths != configured_paths:
            raise ValueError(
                "mandatory scan must retain the complete resolved Profile scope"
            )


class DecisionOutput(OptionalContract):
    schema_version: Literal["1.0.0"]
    envelope: NodeOutputEnvelope
    decision_ref: DecisionRef
    execution_coverage_ref: ExecutionCoverageRef
    mandatory_rule_coverage: MandatoryRuleCoverage
    candidate_prediction_ref: AbsentOptional[CandidatePredictionRef] = Field(
        default=None,
        validate_default=False,
        exclude_if=lambda value: value is None,
        json_schema_extra=omit_default_from_schema,
    )

    @model_validator(mode="after")
    def check_output(self) -> Self:
        if (
            self.envelope.node_id is not NodeId.N06
            or self.envelope.status is not NodeRunStatus.SUCCEEDED
        ):
            raise ValueError(
                "Decision output requires an actually completed N06 invocation"
            )
        return self

    def check_records(
        self,
        inputs: DecisionInput,
        decision: DecisionRecord,
        coverage: ExecutionCoverage,
        candidate: CandidatePrediction | None,
        gate: ProfileEligibilityEvaluation,
        rule_set_results: tuple[RuleSetApplicabilityEvaluation, ...],
        rule_results: tuple[RuleApplicabilityEvaluation, ...],
        configured_paths: tuple[RulePath, ...],
        *,
        applicability_traces: tuple[ApplicabilityTrace, ...] = (),
    ) -> None:
        check_envelope_pair(inputs.envelope, self.envelope)
        inputs.check_gate(gate)
        inputs.check_mandatory_scope(configured_paths)
        env, obj = inputs.envelope, inputs.object_record
        expected = (
            env.execution_id,
            env.release_id,
            env.bundle_digest,
            inputs.context_snapshot_ref,
            obj.profile_ref,
            obj.object_id,
            env.node_run_id,
            env.attempt_id,
        )
        if (
            decision.execution_id,
            decision.release_id,
            decision.bundle_digest,
            decision.context_snapshot_ref,
            decision.profile_ref,
            decision.object_ref.record_id,
            decision.node_run_ref.record_id,
            decision.attempt_ref.record_id,
        ) != expected or (
            decision.decision_ref,
            decision.execution_coverage_ref,
            decision.profile_eligibility_evaluation_ref,
            decision.candidate_prediction_ref,
        ) != (
            self.decision_ref,
            self.execution_coverage_ref,
            inputs.profile_eligibility_evaluation_ref,
            self.candidate_prediction_ref,
        ):
            raise ValueError(
                "Decision must retain its actual N06 Object scope and selected revisions"
            )
        if (candidate is None) != (self.candidate_prediction_ref is None):
            raise ValueError("resolve the optional Advisory Candidate record")
        if candidate is not None and (
            candidate.prediction_ref != self.candidate_prediction_ref
            or (
                candidate.execution_id,
                candidate.release_id,
                candidate.bundle_digest,
                candidate.context_snapshot_ref,
                candidate.profile_ref,
                candidate.object_ref.record_id,
                candidate.node_run_ref.record_id,
                candidate.attempt_ref.record_id,
            )
            != expected
        ):
            raise ValueError(
                "Advisory Candidate must belong to this exact Object and N06 invocation"
            )
        scope = coverage.applicability_scope
        if not isinstance(scope, EvaluatedCoverageScope) or (
            coverage.execution_id,
            coverage.release_id,
            coverage.bundle_digest,
            scope.context_snapshot_ref,
            scope.profile_eligibility_evaluation_ref,
            scope.configured_profile_refs,
            coverage.coverage_ref,
            coverage.mandatory_rule_coverage,
        ) != (
            env.execution_id,
            env.release_id,
            env.bundle_digest,
            inputs.context_snapshot_ref,
            gate.evaluation_ref,
            gate.configured_profile_refs,
            self.execution_coverage_ref,
            self.mandatory_rule_coverage,
        ):
            raise ValueError(
                "N06 must retain the execution-wide Coverage ledger and actual eligibility scope"
            )
        if not any(
            profile.profile_ref == obj.profile_ref
            and decision.object_ref in profile.object_refs
            for profile in coverage.profiles
        ):
            raise ValueError("N06 Object must belong to the recorded Profile coverage")
        traces = {
            trace.trace_ref: ApplicabilityTrace.model_validate(trace)
            for trace in applicability_traces
        }
        if len(traces) != len(applicability_traces):
            raise ValueError("selected applicability traces must be unique")
        evaluations: tuple[
            RuleSetApplicabilityEvaluation | RuleApplicabilityEvaluation, ...
        ] = (
            *rule_set_results,
            *rule_results,
        )
        exclusions = _ExclusionRecords(
            obligations={
                row.obligation_id: row
                for row in self.mandatory_rule_coverage.obligations
            },
            evaluations={row.evaluation_ref: row for row in evaluations},
            traces=traces,
            output_digests={
                (item.ref.record_kind, item.ref.record_id): item.digest
                for item in self.envelope.output_artifacts
            },
            context_digest=next(
                (
                    item.digest
                    for item in env.upstream_artifacts
                    if (item.ref.record_kind, item.ref.record_id)
                    == (
                        RuntimeRecordKind.CONTEXT_SNAPSHOT,
                        inputs.context_snapshot_ref.record_id,
                    )
                ),
                None,
            ),
        )
        self._check_scan(inputs, decision, exclusions)
        self._check_fact_inputs(inputs, decision)
        self._check_applicability(
            inputs, decision, coverage, rule_set_results, rule_results
        )

    def _check_scan(
        self,
        inputs: DecisionInput,
        decision: DecisionRecord,
        exclusions: _ExclusionRecords,
    ) -> None:
        trace = decision.explanation
        recorded_paths = set(
            trace.evaluated_rules
            + trace.blocked_rules
            + trace.skipped_rules
            + trace.not_evaluated_rules
        )
        object_obligations = tuple(
            item
            for item in self.mandatory_rule_coverage.obligations
            if item.profile_ref == decision.profile_ref
            and isinstance(item.target, RuleObligationTarget)
            and item.target.object_ref == decision.object_ref
        )
        ledger_paths = {
            item.target.path
            for item in object_obligations
            if isinstance(item.target, RuleObligationTarget)
        }
        if (
            not set(inputs.mandatory_rule_paths) <= recorded_paths
            or set(inputs.mandatory_rule_paths) != ledger_paths
            or not {item.obligation_id for item in object_obligations}
            <= set(trace.obligation_ids)
        ):
            raise ValueError(
                "mandatory Rule accounting must cover the actual Object's full configured scope"
            )
        recorded_rules = {item.rule: item for item in decision.rule_evaluations}
        for obligation in object_obligations:
            target, outcome = obligation.target, obligation.outcome
            if not isinstance(target, RuleObligationTarget):
                continue
            path = target.path
            if isinstance(
                outcome,
                EvaluatedObligation | GuardBlockedObligation | ExcludedObligation,
            ):
                entry = recorded_rules.get(path)
                expected = (
                    "evaluated"
                    if isinstance(outcome, EvaluatedObligation)
                    else "blocked"
                    if isinstance(outcome, GuardBlockedObligation)
                    else "skipped"
                )
                if entry is None or (
                    entry.result.outcome,
                    entry.selected_rule_evaluation_ref,
                    entry.applicability_trace_ref,
                ) != (
                    expected,
                    outcome.selected_applicability.evaluation_ref,
                    outcome.selected_applicability.trace_ref,
                ):
                    raise ValueError(
                        "Decision Rule outcome and selected revisions must agree with its mandatory obligation"
                    )
                if (
                    isinstance(outcome, EvaluatedObligation | GuardBlockedObligation)
                    and outcome.decision_ref != self.decision_ref
                ):
                    raise ValueError(
                        "mandatory obligation must identify the actual Object Decision"
                    )
            elif isinstance(
                outcome,
                UnresolvedMandatoryObligation
                | AncestorNotEvaluatedObligation
                | ApplicableNotEvaluatedObligation
                | DependencyNotEvaluatedObligation,
            ):
                if path not in trace.not_evaluated_rules:
                    raise ValueError(
                        "unresolved mandatory Rule must remain not_evaluated in the Decision"
                    )
            if decision.result is DecisionResult.NO_VIOLATION:
                complete = isinstance(outcome, EvaluatedObligation | ExcludedObligation)
                proven_excluded = isinstance(
                    outcome, AncestorNotEvaluatedObligation
                ) and exclusions.proves_exclusion(decision, target, outcome)
                if not complete and not proven_excluded:
                    raise ValueError(
                        "unresolved mandatory scope precludes no_violation"
                    )

    def _check_fact_inputs(
        self, inputs: DecisionInput, decision: DecisionRecord
    ) -> None:
        if not set(decision.explanation.fact_refs) <= set(inputs.fact_refs):
            raise ValueError("Decision may only evaluate supplied Fact references")
        for evaluation in decision.rule_evaluations:
            outcome = evaluation.result
            if isinstance(outcome, EvaluatedRuleOutcome | BlockedRuleOutcome) and not (
                set(outcome.guard.authoritative_result_selection_refs)
                <= set(inputs.authority_selection_refs)
            ):
                raise ValueError(
                    "Required Evidence Guard must consume supplied Producer Authority revisions"
                )
            if decision.result is DecisionResult.NO_VIOLATION and (
                isinstance(outcome, BlockedRuleOutcome)
                or isinstance(outcome, EvaluatedRuleOutcome)
                and outcome.predicate.truth is RuleTruth.UNKNOWN
            ):
                raise ValueError(
                    "blocked evidence or an UNKNOWN Rule predicate cannot establish no_violation"
                )

    def _check_applicability(
        self,
        inputs: DecisionInput,
        decision: DecisionRecord,
        coverage: ExecutionCoverage,
        rule_sets: tuple[RuleSetApplicabilityEvaluation, ...],
        rules: tuple[RuleApplicabilityEvaluation, ...],
    ) -> None:
        rule_by_ref = {rule.evaluation_ref.record_id: rule for rule in rules}
        rule_set_by_ref = {
            rule_set.evaluation_ref.record_id: rule_set for rule_set in rule_sets
        }
        if len(rule_by_ref) != len(rules) or len(rule_set_by_ref) != len(rule_sets):
            raise ValueError("selected Rule applicability references must be unique")
        for result in decision.rule_evaluations:
            rule = rule_by_ref.get(result.selected_rule_evaluation_ref.record_id)
            rule_set = rule_set_by_ref.get(
                result.selected_rule_set_evaluation_ref.record_id
            )
            if (
                rule is None
                or rule_set is None
                or (
                    rule.rule_set_applicability_evaluation_ref
                    != rule_set.evaluation_ref
                    or rule.applicability_trace_ref != result.applicability_trace_ref
                    or rule.rule_ref != result.rule.rule_ref
                    or rule.rule_set_ref != result.rule.rule_set_ref
                    or rule_set.rule_set_ref != result.rule.rule_set_ref
                )
            ):
                raise ValueError(
                    "Decision Rule result must retain its exact selected RuleSet and Rule revisions"
                )
            for evaluation in (rule, rule_set):
                if (
                    (
                        evaluation.execution_id,
                        evaluation.release_id,
                        evaluation.bundle_digest,
                        evaluation.context_snapshot_ref,
                        evaluation.profile_eligibility_evaluation_ref,
                        evaluation.profile_ref,
                    )
                    != (
                        decision.execution_id,
                        decision.release_id,
                        decision.bundle_digest,
                        inputs.context_snapshot_ref,
                        inputs.profile_eligibility_evaluation_ref,
                        decision.profile_ref,
                    )
                    or evaluation.evaluation_ref
                    not in coverage.selected_applicability_evaluation_refs
                    or (
                        evaluation.node_run_ref is not None
                        and (
                            evaluation.object_ref,
                            evaluation.node_run_ref,
                            evaluation.attempt_ref,
                        )
                        != (
                            decision.object_ref,
                            decision.node_run_ref,
                            decision.attempt_ref,
                        )
                    )
                ):
                    raise ValueError(
                        "selected applicability belongs to a different Object, invocation or scope"
                    )
