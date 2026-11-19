"""Execution and per-Profile coverage, independent of whether any Decision exists."""

from typing import Annotated, Literal, Self

from pydantic import Field, StrictInt, model_validator

from jimu_runtime.domain.base import ContractModel
from jimu_runtime.domain.definition_support import (
    AbsentOptional,
    OptionalContract,
    UTCDateTime,
    omit_default_from_schema,
    require_unique_references,
)
from jimu_runtime.domain.definitions.capabilities import RuleSetContentRef
from jimu_runtime.domain.enums import DecisionResult, RuntimeRecordKind, WireEnum
from jimu_runtime.domain.primitives import Digest, RecordId
from jimu_runtime.domain.rules.definitions import RuleContentRef
from jimu_runtime.domain.runtime.applicability import (
    ApplicabilityAncestorRef,
    ApplicabilityEvaluationRef,
    ApplicabilityTraceRef,
    ContextSnapshotRef,
    ProfileEligibilityEvaluationRef,
    UnresolvedObligationRef,
)
from jimu_runtime.domain.runtime.decisions import (
    DecisionRef,
    ExecutionCoverageRef,
    RecordList,
    RulePath,
    require_distinct,
)
from jimu_runtime.domain.runtime.facts import (
    EvidenceRefs,
    ModelInvocationRef,
    NodeRunRef,
    ObjectRef,
    ProfileContentRef,
    require_unique_runtime_refs,
)
from jimu_runtime.domain.runtime.perception import DiscoveryOutcome, GroundingOutcome


class CoverageStatus(WireEnum):
    COMPLETE = "complete"
    PARTIAL = "partial"
    NONE = "none"
    NOT_APPLICABLE = "not_applicable"


class CoverageReason(WireEnum):
    CONTEXT_BUILD_BLOCKED = "context_build_blocked"
    NO_ELIGIBLE_PROFILES = "no_eligible_profiles"
    EMPTY_CONFIGURED_SCOPE = "empty_configured_scope"
    PROFILE_NOT_DISCOVERED = "profile_not_discovered"
    DISCOVERY_NO_HITS = "discovery_no_hits"
    DISCOVERY_FAILED = "discovery_failed"
    DISCOVERY_PARTIAL = "discovery_partial"
    DISCOVERY_NOT_EVALUATED = "discovery_not_evaluated"
    GROUNDING_NO_TARGET = "grounding_no_target"
    GROUNDING_FAILED = "grounding_failed"
    GROUNDING_PARTIAL = "grounding_partial"
    GROUNDING_NOT_EVALUATED = "grounding_not_evaluated"
    DECISION_NOT_EVALUATED = "decision_not_evaluated"
    REQUIRED_EVIDENCE_BLOCKED = "required_evidence_blocked"
    PREDICATE_UNKNOWN = "predicate_unknown"
    APPLICABILITY_UNRESOLVED = "applicability_unresolved"


class ProfileObligationTarget(ContractModel):
    scope: Literal["profile"]


class _LowerObligationTarget(OptionalContract):
    rule_set_ref: RuleSetContentRef
    object_ref: AbsentOptional[ObjectRef] = Field(
        default=None,
        validate_default=False,
        exclude_if=lambda value: value is None,
        json_schema_extra=omit_default_from_schema,
    )


class RuleSetObligationTarget(_LowerObligationTarget):
    scope: Literal["rule_set"]


class RuleObligationTarget(_LowerObligationTarget):
    scope: Literal["rule"]
    rule_ref: RuleContentRef

    @property
    def path(self) -> RulePath:
        return RulePath(rule_set_ref=self.rule_set_ref, rule_ref=self.rule_ref)


type ObligationTarget = Annotated[
    ProfileObligationTarget | RuleSetObligationTarget | RuleObligationTarget,
    Field(discriminator="scope"),
]


class SelectedApplicability(ContractModel):
    evaluation_ref: ApplicabilityEvaluationRef
    trace_ref: ApplicabilityTraceRef


class IncludedObligation(ContractModel):
    disposition: Literal["included"]
    selected_applicability: SelectedApplicability


class ExcludedObligation(ContractModel):
    disposition: Literal["excluded"]
    selected_applicability: SelectedApplicability


class UnresolvedMandatoryObligation(ContractModel):
    disposition: Literal["unresolved"]
    selected_applicability: SelectedApplicability
    unresolved_obligation_ref: UnresolvedObligationRef


class EvaluatedObligation(ContractModel):
    disposition: Literal["evaluated"]
    selected_applicability: SelectedApplicability
    decision_ref: DecisionRef


class GuardBlockedObligation(ContractModel):
    disposition: Literal["blocked"]
    selected_applicability: SelectedApplicability
    decision_ref: DecisionRef


class AncestorNotEvaluatedObligation(ContractModel):
    disposition: Literal["not_evaluated"]
    selected_applicability: SelectedApplicability
    decisive_ancestor_ref: ApplicabilityAncestorRef
    ancestor_obligation_id: RecordId


class ApplicableNotEvaluatedObligation(ContractModel):
    disposition: Literal["applicable_not_evaluated"]
    selected_applicability: SelectedApplicability
    blocker_node_run_ref: NodeRunRef


class DependencyNotEvaluatedObligation(ContractModel):
    disposition: Literal["dependency_not_evaluated"]
    reason: CoverageReason
    blocker_node_run_ref: NodeRunRef


type ObligationDisposition = Annotated[
    IncludedObligation
    | ExcludedObligation
    | UnresolvedMandatoryObligation
    | EvaluatedObligation
    | GuardBlockedObligation
    | AncestorNotEvaluatedObligation
    | ApplicableNotEvaluatedObligation
    | DependencyNotEvaluatedObligation,
    Field(discriminator="disposition"),
]


class MandatoryRuleObligation(ContractModel):
    obligation_id: RecordId
    profile_ref: ProfileContentRef
    target: ObligationTarget
    outcome: ObligationDisposition

    @model_validator(mode="after")
    def verify_target(self) -> Self:
        target = self.target
        outcome = self.outcome
        if isinstance(outcome, IncludedObligation) and isinstance(
            target, RuleObligationTarget
        ):
            raise ValueError("a mandatory Rule must report work beyond inclusion")
        if isinstance(outcome, EvaluatedObligation | GuardBlockedObligation) and (
            not isinstance(target, RuleObligationTarget) or target.object_ref is None
        ):
            raise ValueError(
                "condition evaluation or guard blocker requires a real Object Rule"
            )
        if isinstance(outcome, AncestorNotEvaluatedObligation) and isinstance(
            target, ProfileObligationTarget
        ):
            raise ValueError("Profile obligations have no applicability ancestor")
        if isinstance(outcome, UnresolvedMandatoryObligation) and (
            outcome.unresolved_obligation_ref.record_id != self.obligation_id
        ):
            raise ValueError(
                "the unresolved branch must share the ledger obligation ID"
            )
        return self


class MandatoryRuleCoverage(ContractModel):
    scan: Literal["all_mandatory"]
    obligations: RecordList[MandatoryRuleObligation]

    @model_validator(mode="after")
    def ensure_unique(self) -> Self:
        require_distinct(
            tuple(item.obligation_id for item in self.obligations), "obligation IDs"
        )
        require_distinct(
            tuple((item.profile_ref, item.target) for item in self.obligations),
            "obligation targets",
        )
        return self


class CoverageMatch(ContractModel):
    object_ref: ObjectRef
    profile_ref: ProfileContentRef
    decision_ref: DecisionRef
    rule: RulePath


class _CoveragePin(ContractModel):
    schema_version: Literal["1.0.0"]
    execution_id: RecordId
    release_id: RecordId
    bundle_digest: Digest


class ProfileCoverage(_CoveragePin):
    profile_ref: ProfileContentRef
    grounding_outcome: GroundingOutcome
    object_refs: RecordList[ObjectRef]
    object_count: Annotated[StrictInt, Field(ge=0, le=1024)]
    obligation_ids: RecordList[RecordId]
    coverage_status: CoverageStatus
    reasons: RecordList[CoverageReason]
    missing_evidence_refs: EvidenceRefs

    @model_validator(mode="after")
    def verify_grounding(self) -> Self:
        require_unique_runtime_refs(self.object_refs)
        require_distinct(self.obligation_ids, "Profile obligation IDs")
        require_distinct(self.reasons, "Profile coverage reasons")
        require_unique_runtime_refs(self.missing_evidence_refs)
        if self.object_count != len(self.object_refs):
            raise ValueError("Profile Object count must match Object refs")
        if self.grounding_outcome is GroundingOutcome.OBJECTS:
            if not self.object_refs:
                raise ValueError("objects grounding requires a nonempty Object list")
        elif (
            self.grounding_outcome is not GroundingOutcome.PARTIAL and self.object_refs
        ):
            raise ValueError(
                "no_target, failed or not_evaluated grounding forbids Objects"
            )
        expected_reason = {
            GroundingOutcome.NO_TARGET: CoverageReason.GROUNDING_NO_TARGET,
            GroundingOutcome.FAILED: CoverageReason.GROUNDING_FAILED,
            GroundingOutcome.PARTIAL: CoverageReason.GROUNDING_PARTIAL,
            GroundingOutcome.NOT_EVALUATED: CoverageReason.GROUNDING_NOT_EVALUATED,
        }.get(self.grounding_outcome)
        if expected_reason is not None and expected_reason not in self.reasons:
            raise ValueError("Profile must retain the typed grounding reason")
        if self.coverage_status is CoverageStatus.COMPLETE and (
            self.grounding_outcome is not GroundingOutcome.OBJECTS
            or self.missing_evidence_refs
        ):
            raise ValueError(
                "complete Profile requires successful nonempty grounding and evidence"
            )
        return self


class EvaluatedCoverageScope(ContractModel):
    scope_status: Literal["evaluated"]
    context_snapshot_ref: ContextSnapshotRef
    profile_eligibility_evaluation_ref: ProfileEligibilityEvaluationRef
    configured_profile_refs: RecordList[ProfileContentRef]

    @model_validator(mode="after")
    def check_scope(self) -> Self:
        require_unique_references(self.configured_profile_refs)
        return self


class BlockedCoverageScope(ContractModel):
    scope_status: Literal["blocked"]
    blocker_node_run_ref: NodeRunRef
    configured_profile_refs: RecordList[ProfileContentRef]

    @model_validator(mode="after")
    def check_scope(self) -> Self:
        require_unique_references(self.configured_profile_refs)
        return self


type CoverageScope = Annotated[
    EvaluatedCoverageScope | BlockedCoverageScope, Field(discriminator="scope_status")
]


class ExecutionCoverage(_CoveragePin, OptionalContract):
    coverage_id: RecordId
    applicability_scope: CoverageScope
    discovery_outcome: DiscoveryOutcome
    discovery_model_invocation_refs: RecordList[ModelInvocationRef]
    hit_profile_refs: RecordList[ProfileContentRef]
    profiles: RecordList[ProfileCoverage]
    mandatory_rule_coverage: MandatoryRuleCoverage
    selected_applicability_evaluation_refs: RecordList[ApplicabilityEvaluationRef]
    applicability_trace_refs: RecordList[ApplicabilityTraceRef]
    unresolved_obligation_refs: RecordList[UnresolvedObligationRef]
    object_count: Annotated[StrictInt, Field(ge=0, le=1024)]
    coverage_status: CoverageStatus
    reasons: RecordList[CoverageReason]
    missing_evidence_refs: EvidenceRefs
    summary_result: DecisionResult
    matches: RecordList[CoverageMatch]
    created_at: UTCDateTime

    @property
    def coverage_ref(self) -> ExecutionCoverageRef:
        return ExecutionCoverageRef(
            record_kind=RuntimeRecordKind.EXECUTION_COVERAGE, record_id=self.coverage_id
        )

    @model_validator(mode="after")
    def check_coverage(self) -> Self:
        self._check_profiles()
        self._check_discovery()
        self._check_ledger()
        self._check_summary()
        return self

    def _check_profiles(self) -> None:
        require_unique_references(self.hit_profile_refs)
        refs = tuple(item.profile_ref for item in self.profiles)
        if refs != self.applicability_scope.configured_profile_refs:
            raise ValueError("coverage must follow the exact configured Profile scope")
        if not set(self.hit_profile_refs) <= set(refs):
            raise ValueError("discovery hits must be configured Profiles")
        objects = tuple(ref for item in self.profiles for ref in item.object_refs)
        require_unique_runtime_refs(objects)
        if len(objects) != self.object_count:
            raise ValueError(
                "Execution Object count must match distinct Profile Object refs"
            )
        for item in self.profiles:
            if (item.execution_id, item.release_id, item.bundle_digest) != (
                self.execution_id,
                self.release_id,
                self.bundle_digest,
            ):
                raise ValueError("Profile coverage must share the Execution pin")
            if item.grounding_outcome is not GroundingOutcome.NOT_EVALUATED and (
                item.profile_ref not in self.hit_profile_refs
            ):
                raise ValueError("grounding requires a discovered Profile")
        require_unique_runtime_refs(self.missing_evidence_refs)
        if set(self.missing_evidence_refs) != {
            ref for profile in self.profiles for ref in profile.missing_evidence_refs
        }:
            raise ValueError(
                "Execution missing evidence refs must exhaust Profile refs"
            )

    def _check_discovery(self) -> None:
        require_distinct(self.reasons, "Execution coverage reasons")
        require_unique_runtime_refs(self.discovery_model_invocation_refs)
        if (
            self.discovery_outcome is DiscoveryOutcome.NOT_EVALUATED
            and self.discovery_model_invocation_refs
        ):
            raise ValueError("not_evaluated discovery forbids invocations")
        if (
            self.discovery_outcome
            in (
                DiscoveryOutcome.HITS,
                DiscoveryOutcome.NO_HITS,
                DiscoveryOutcome.PARTIAL,
            )
            and not self.discovery_model_invocation_refs
        ):
            raise ValueError("usable discovery requires ModelInvocation refs")
        if self.discovery_outcome is DiscoveryOutcome.HITS:
            if not self.hit_profile_refs:
                raise ValueError("discovery hits requires hit Profile refs")
        elif (
            self.discovery_outcome is not DiscoveryOutcome.PARTIAL
            and self.hit_profile_refs
        ):
            raise ValueError("discovery status cannot report hits")
        expected_reason = {
            DiscoveryOutcome.NO_HITS: CoverageReason.DISCOVERY_NO_HITS,
            DiscoveryOutcome.FAILED: CoverageReason.DISCOVERY_FAILED,
            DiscoveryOutcome.PARTIAL: CoverageReason.DISCOVERY_PARTIAL,
            DiscoveryOutcome.NOT_EVALUATED: CoverageReason.DISCOVERY_NOT_EVALUATED,
        }.get(self.discovery_outcome)
        if expected_reason is not None and expected_reason not in self.reasons:
            raise ValueError("Execution must retain the typed discovery reason")

    def _check_ledger(self) -> None:
        ledger = self.mandatory_rule_coverage.obligations
        require_unique_runtime_refs(self.selected_applicability_evaluation_refs)
        require_unique_runtime_refs(self.applicability_trace_refs)
        require_unique_runtime_refs(self.unresolved_obligation_refs)
        selected = {
            entry.outcome.selected_applicability
            for entry in ledger
            if not isinstance(entry.outcome, DependencyNotEvaluatedObligation)
        }
        if {item.evaluation_ref for item in selected} != set(
            self.selected_applicability_evaluation_refs
        ) or {item.trace_ref for item in selected} != set(
            self.applicability_trace_refs
        ):
            raise ValueError("Execution applicability refs must match the ledger")
        if {
            item.outcome.unresolved_obligation_ref
            for item in ledger
            if isinstance(item.outcome, UnresolvedMandatoryObligation)
        } != set(self.unresolved_obligation_refs):
            raise ValueError("Execution unresolved refs must match the ledger")
        if not {entry.profile_ref for entry in ledger} <= {
            profile.profile_ref for profile in self.profiles
        }:
            raise ValueError("ledger contains unconfigured Profiles")
        for item in self.profiles:
            if {
                entry.obligation_id
                for entry in ledger
                if entry.profile_ref == item.profile_ref
            } != set(item.obligation_ids):
                raise ValueError(
                    "Profile must retain all and only its ledger obligations"
                )
        if isinstance(self.applicability_scope, BlockedCoverageScope):
            if self.discovery_outcome is not DiscoveryOutcome.NOT_EVALUATED or selected:
                raise ValueError(
                    "blocked context forbids discovery and applicability records"
                )
            if CoverageReason.CONTEXT_BUILD_BLOCKED not in self.reasons:
                raise ValueError("blocked context requires its own coverage reason")

    def _check_summary(self) -> None:
        require_distinct(self.matches, "matched Object and Rule paths")
        object_profiles = {
            ref: profile.profile_ref
            for profile in self.profiles
            for ref in profile.object_refs
        }
        if any(
            object_profiles.get(item.object_ref) != item.profile_ref
            for item in self.matches
        ):
            raise ValueError("matched Object must belong to the stated Profile")
        if bool(self.matches) != (self.summary_result is DecisionResult.VIOLATION):
            raise ValueError("violation must reflect the supplied valid matches")
        if self.coverage_status is CoverageStatus.COMPLETE and (
            not self.object_count
            or self.discovery_outcome is not DiscoveryOutcome.HITS
            or self.missing_evidence_refs
            or any(
                profile.coverage_status
                not in (CoverageStatus.COMPLETE, CoverageStatus.NOT_APPLICABLE)
                for profile in self.profiles
            )
        ):
            raise ValueError(
                "complete coverage requires nonempty Objects, complete discovery and all Profile obligations"
            )
        if self.coverage_status is CoverageStatus.NOT_APPLICABLE and (
            not self.profiles
            or any(
                profile.coverage_status is not CoverageStatus.NOT_APPLICABLE
                for profile in self.profiles
            )
        ):
            raise ValueError(
                "not_applicable coverage requires deterministic exclusions"
            )
        if self.coverage_status is CoverageStatus.NONE and self.matches:
            raise ValueError("a valid match constitutes completed work")
        if (
            self.summary_result is DecisionResult.NO_VIOLATION
            and self.coverage_status is not CoverageStatus.COMPLETE
        ):
            raise ValueError("no_violation requires complete coverage")
        if (
            self.summary_result is DecisionResult.NOT_APPLICABLE
            and self.coverage_status
            not in (CoverageStatus.COMPLETE, CoverageStatus.NOT_APPLICABLE)
        ):
            raise ValueError(
                "not_applicable summary requires complete or excluded scope"
            )
        if (
            self.coverage_status is CoverageStatus.NOT_APPLICABLE
            and self.summary_result is not DecisionResult.NOT_APPLICABLE
        ):
            raise ValueError("excluded scope must summarize as not_applicable")
