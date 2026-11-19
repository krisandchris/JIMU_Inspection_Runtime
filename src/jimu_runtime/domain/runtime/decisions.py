"""Persisted N06 records. All predicates, guard outcomes and results are supplied."""

from typing import Annotated, Literal, Self

from pydantic import (
    BeforeValidator,
    Field,
    StrictBool,
    field_validator,
    model_validator,
)

from jimu_runtime.domain.base import ContractModel
from jimu_runtime.domain.definition_enums import FactScope
from jimu_runtime.domain.definition_support import (
    AbsentOptional,
    OptionalContract,
    UTCDateTime,
    immutable_sequence,
    omit_default_from_schema,
)
from jimu_runtime.domain.definitions.capabilities import RuleSetContentRef
from jimu_runtime.domain.enums import DecisionResult, RuntimeRecordKind, WireEnum
from jimu_runtime.domain.fact_selectors import FactSelector
from jimu_runtime.domain.primitives import ArtifactId, Digest, RecordId, Version
from jimu_runtime.domain.rules.definitions import RuleContentRef
from jimu_runtime.domain.runtime.applicability import (
    ApplicabilityTraceRef,
    ContextSnapshotRef,
    ProfileEligibilityEvaluationRef,
    RuleApplicabilityEvaluationRef,
    RuleSetApplicabilityEvaluationRef,
    RuleTruth,
)
from jimu_runtime.domain.runtime.facts import (
    AttemptRef,
    AuthoritySelectionRef,
    EvidenceRefs,
    FactRef,
    FactRefs,
    ModelInvocationRef,
    NodeRunRef,
    ObjectRef,
    PolicyContentRef,
    ProfileContentRef,
    TypedRuntimeRef,
    require_unique_runtime_refs,
)
from jimu_runtime.domain.values import FiniteNumber

type RecordList[T] = Annotated[
    tuple[T, ...],
    Field(max_length=1024, json_schema_extra={"uniqueItems": True}),
    BeforeValidator(immutable_sequence),
]


def require_distinct[T](items: tuple[T, ...], label: str) -> None:
    if len(set(items)) != len(items):
        raise ValueError(f"{label} must be unique")


class CandidatePredictionRef(TypedRuntimeRef):
    record_kind: Literal[RuntimeRecordKind.CANDIDATE_PREDICTION]


class DecisionRef(TypedRuntimeRef):
    record_kind: Literal[RuntimeRecordKind.DECISION]


class ExecutionCoverageRef(TypedRuntimeRef):
    record_kind: Literal[RuntimeRecordKind.EXECUTION_COVERAGE]


class RulePath(ContractModel):
    rule_set_ref: RuleSetContentRef
    rule_ref: RuleContentRef


class CandidateCategory(ContractModel):
    category_id: ArtifactId
    score: FiniteNumber


class CandidatePredictionStatus(WireEnum):
    SUCCEEDED = "succeeded"
    FAILED = "failed"


class _DecisionPin(OptionalContract):
    schema_version: Literal["1.0.0"]
    execution_id: RecordId
    release_id: RecordId
    bundle_digest: Digest
    context_snapshot_ref: ContextSnapshotRef


class _ObjectDecisionPin(_DecisionPin):
    scope: Literal[FactScope.OBJECT]
    profile_ref: ProfileContentRef
    object_ref: ObjectRef
    node_run_ref: NodeRunRef
    attempt_ref: AttemptRef

    @field_validator("scope", mode="before")
    @classmethod
    def decode_scope(cls, value: object) -> FactScope:
        return FactScope._decode(value)


class CandidatePrediction(_ObjectDecisionPin):
    prediction_id: RecordId
    status: CandidatePredictionStatus
    categories: RecordList[CandidateCategory]
    model_invocation_ref: ModelInvocationRef
    advisory_only: Literal[True]
    failure_code: AbsentOptional[ArtifactId] = Field(
        default=None,
        validate_default=False,
        exclude_if=lambda value: value is None,
        json_schema_extra=omit_default_from_schema,
    )
    created_at: UTCDateTime

    @property
    def prediction_ref(self) -> CandidatePredictionRef:
        return CandidatePredictionRef(
            record_kind=RuntimeRecordKind.CANDIDATE_PREDICTION,
            record_id=self.prediction_id,
        )

    @model_validator(mode="after")
    def check_result(self) -> Self:
        require_distinct(
            tuple(item.category_id for item in self.categories), "candidate categories"
        )
        if self.status is CandidatePredictionStatus.FAILED:
            if self.categories or self.failure_code is None:
                raise ValueError(
                    "failed prediction requires a failure code and empty categories"
                )
        elif self.failure_code is not None:
            raise ValueError("successful prediction forbids a failure code")
        return self


class RequiredEvidenceReason(WireEnum):
    REQUIRED_FACT_UNAVAILABLE = "required_fact_unavailable"
    EVIDENCE_UNAVAILABLE = "evidence_unavailable"
    PRODUCER_AUTHORITY_UNRESOLVED = "producer_authority_unresolved"


class RequiredFactProof(ContractModel):
    selector: FactSelector
    fact_ref: FactRef
    authority_selection_ref: AuthoritySelectionRef


class _RequiredEvidenceGuard(ContractModel):
    required_fact_refs: FactRefs
    required_evidence_refs: EvidenceRefs
    authoritative_result_selection_refs: RecordList[AuthoritySelectionRef]
    fact_proofs: RecordList[RequiredFactProof]

    @model_validator(mode="after")
    def check_proofs(self) -> Self:
        require_unique_runtime_refs(self.required_fact_refs)
        require_unique_runtime_refs(self.required_evidence_refs)
        require_unique_runtime_refs(self.authoritative_result_selection_refs)
        require_distinct(
            tuple(proof.selector for proof in self.fact_proofs), "guard proof selectors"
        )
        if set(self.required_fact_refs) != {
            proof.fact_ref for proof in self.fact_proofs
        }:
            raise ValueError("guard Fact refs must match the supplied proofs")
        if set(self.authoritative_result_selection_refs) != {
            proof.authority_selection_ref for proof in self.fact_proofs
        }:
            raise ValueError("guard selection refs must match the supplied proofs")
        return self


class CompleteRequiredEvidenceGuard(_RequiredEvidenceGuard):
    outcome: Literal["complete"]


class BlockedRequiredEvidenceGuard(_RequiredEvidenceGuard):
    outcome: Literal["blocked"]
    reasons: Annotated[
        tuple[RequiredEvidenceReason, ...],
        Field(min_length=1, max_length=3, json_schema_extra={"uniqueItems": True}),
        BeforeValidator(immutable_sequence),
    ]

    @model_validator(mode="after")
    def check_reasons(self) -> Self:
        require_distinct(self.reasons, "guard reasons")
        return self


type RequiredEvidenceGuard = Annotated[
    CompleteRequiredEvidenceGuard | BlockedRequiredEvidenceGuard,
    Field(discriminator="outcome"),
]


class RuleSkipReason(WireEnum):
    APPLICABILITY_EXCLUDED = "applicability_excluded"
    OPTIONAL_NOT_SELECTED = "optional_not_selected"


class PredicateRecord(ContractModel):
    truth: RuleTruth
    diagnostic_codes: RecordList[ArtifactId]

    @model_validator(mode="after")
    def preserve_diagnostics(self) -> Self:
        require_distinct(self.diagnostic_codes, "predicate diagnostics")
        if self.truth is RuleTruth.UNKNOWN and not self.diagnostic_codes:
            raise ValueError("UNKNOWN predicate requires diagnostics")
        return self


class _RuleOutcome(OptionalContract):
    outcome: str


class EvaluatedRuleOutcome(_RuleOutcome):
    outcome: Literal["evaluated"]
    predicate: PredicateRecord
    guard: CompleteRequiredEvidenceGuard
    matched: StrictBool

    @model_validator(mode="after")
    def check_match(self) -> Self:
        if self.matched != (self.predicate.truth is RuleTruth.TRUE):
            raise ValueError(
                "only an evaluated TRUE predicate with complete guard matches"
            )
        return self


class BlockedRuleOutcome(_RuleOutcome):
    outcome: Literal["blocked"]
    guard: BlockedRequiredEvidenceGuard
    predicate: AbsentOptional[PredicateRecord] = Field(
        default=None,
        validate_default=False,
        exclude_if=lambda value: value is None,
        json_schema_extra=omit_default_from_schema,
    )


class SkippedRuleOutcome(_RuleOutcome):
    outcome: Literal["skipped"]
    reason: RuleSkipReason


type RuleEvaluationOutcome = Annotated[
    EvaluatedRuleOutcome | BlockedRuleOutcome | SkippedRuleOutcome,
    Field(discriminator="outcome"),
]


class RuleEvaluation(ContractModel):
    rule: RulePath
    selected_rule_set_evaluation_ref: RuleSetApplicabilityEvaluationRef
    selected_rule_evaluation_ref: RuleApplicabilityEvaluationRef
    applicability_trace_ref: ApplicabilityTraceRef
    result: RuleEvaluationOutcome
    optional_fact_proofs: RecordList[RequiredFactProof] = ()
    fact_refs: FactRefs
    evidence_refs: EvidenceRefs

    @model_validator(mode="after")
    def check_refs(self) -> Self:
        require_unique_runtime_refs(self.fact_refs)
        require_unique_runtime_refs(self.evidence_refs)
        optional = self.optional_fact_proofs
        require_distinct(
            tuple(proof.selector for proof in optional), "optional Fact selectors"
        )
        if isinstance(self.result, EvaluatedRuleOutcome | BlockedRuleOutcome):
            guard = self.result.guard
            if set(self.fact_refs) != set(guard.required_fact_refs) | {
                proof.fact_ref for proof in optional
            }:
                raise ValueError(
                    "Rule Fact refs must exactly match required and optional operand proofs"
                )
            if not set(guard.required_evidence_refs) <= set(self.evidence_refs):
                raise ValueError(
                    "guard Evidence refs must appear in the Rule evaluation trace"
                )
        elif self.fact_refs or self.evidence_refs or optional:
            raise ValueError(
                "a skipped Rule cannot claim condition Fact or Evidence refs"
            )
        return self


class ConflictResolutionTrace(ContractModel):
    mode: Literal["collect_all"]
    conflict_policy_refs: RecordList[PolicyContentRef]
    matched_rules: RecordList[RulePath]
    matched_category_ids: RecordList[ArtifactId]

    @model_validator(mode="after")
    def unique_matches(self) -> Self:
        require_distinct(self.conflict_policy_refs, "conflict policy refs")
        require_distinct(self.matched_rules, "matched Rule paths")
        require_distinct(self.matched_category_ids, "matched category IDs")
        if bool(self.matched_category_ids) != bool(self.matched_rules):
            raise ValueError("matched category set must reflect the matched Rule set")
        return self


class ExplanationTrace(ContractModel):
    applicable_rules: RecordList[RulePath]
    evaluated_rules: RecordList[RulePath]
    skipped_rules: RecordList[RulePath]
    blocked_rules: RecordList[RulePath]
    not_evaluated_rules: RecordList[RulePath]
    applicability_trace_refs: RecordList[ApplicabilityTraceRef]
    obligation_ids: RecordList[RecordId]
    fact_refs: FactRefs
    evidence_refs: EvidenceRefs

    @model_validator(mode="after")
    def verify_partition(self) -> Self:
        require_distinct(self.applicable_rules, "applicable Rule paths")
        require_distinct(
            self.evaluated_rules
            + self.skipped_rules
            + self.blocked_rules
            + self.not_evaluated_rules,
            "Rule outcome refs",
        )
        require_unique_runtime_refs(self.applicability_trace_refs)
        require_distinct(self.obligation_ids, "Decision obligation IDs")
        require_unique_runtime_refs(self.fact_refs)
        require_unique_runtime_refs(self.evidence_refs)
        if not set(self.evaluated_rules + self.blocked_rules) <= set(
            self.applicable_rules
        ):
            raise ValueError("evaluated or guard-blocked Rule must be applicable")
        return self


class DecisionRecord(_ObjectDecisionPin):
    decision_id: RecordId
    profile_eligibility_evaluation_ref: ProfileEligibilityEvaluationRef
    candidate_prediction_ref: AbsentOptional[CandidatePredictionRef] = Field(
        default=None,
        validate_default=False,
        exclude_if=lambda value: value is None,
        json_schema_extra=omit_default_from_schema,
    )
    execution_coverage_ref: ExecutionCoverageRef
    rule_engine_version: Version
    rule_evaluations: RecordList[RuleEvaluation]
    result: DecisionResult
    explanation: ExplanationTrace
    conflict_resolution: ConflictResolutionTrace
    created_at: UTCDateTime

    @property
    def decision_ref(self) -> DecisionRef:
        return DecisionRef(
            record_kind=RuntimeRecordKind.DECISION, record_id=self.decision_id
        )

    @model_validator(mode="after")
    def verify_report(self) -> Self:
        require_distinct(
            tuple(item.rule for item in self.rule_evaluations), "Rule evaluation paths"
        )
        trace = self.explanation
        matched: set[RulePath] = set()
        for item in self.rule_evaluations:
            if item.applicability_trace_ref not in trace.applicability_trace_refs:
                raise ValueError(
                    "explanation must retain the selected Rule applicability trace"
                )
            if isinstance(item.result, EvaluatedRuleOutcome):
                if item.result.matched:
                    matched.add(item.rule)
        for outcome, expected_paths in (
            ("evaluated", trace.evaluated_rules),
            ("skipped", trace.skipped_rules),
            ("blocked", trace.blocked_rules),
        ):
            if set(expected_paths) != {
                item.rule
                for item in self.rule_evaluations
                if item.result.outcome == outcome
            }:
                raise ValueError("Decision outcome buckets must match Rule evaluations")
        if matched != set(self.conflict_resolution.matched_rules):
            raise ValueError(
                "collect_all must retain every evidence-valid matched Rule"
            )
        if bool(matched) != (self.result is DecisionResult.VIOLATION):
            raise ValueError("violation must reflect the supplied valid matches")
        if {ref for item in self.rule_evaluations for ref in item.fact_refs} != set(
            trace.fact_refs
        ) or {
            ref for item in self.rule_evaluations for ref in item.evidence_refs
        } != set(trace.evidence_refs):
            raise ValueError(
                "Decision Fact and Evidence refs must exhaust Rule evaluation refs"
            )
        return self
