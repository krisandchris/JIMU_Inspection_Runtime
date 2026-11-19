"""Append-only N02 Question selection audit, separate from Profile discovery."""

from typing import Annotated, Literal, Self

from pydantic import BeforeValidator, Field, field_validator, model_validator

from jimu_runtime.domain.base import ContractModel
from jimu_runtime.domain.definition_support import (
    AbsentOptional,
    Description,
    OptionalContract,
    UTCDateTime,
    immutable_sequence,
    omit_default_from_schema,
    reference_identity,
    require_unique_references,
)
from jimu_runtime.domain.definitions.models import PolicyContentRef
from jimu_runtime.domain.fact_selectors import QuestionContentRef
from jimu_runtime.domain.policies import ContextSelectionRule, SelectionCallPolicy
from jimu_runtime.domain.primitives import ArtifactId, Digest, RecordId
from jimu_runtime.domain.runtime.applicability import ContextSnapshotRef
from jimu_runtime.domain.runtime.facts import (
    AttemptRef,
    EvidenceRefs,
    ModelInvocationRef,
    ProfileContentRef,
    require_unique_runtime_refs,
)
from jimu_runtime.domain.validation.issues import ValidationIssue

type SelectionInvocationMode = Literal["shared_n02", "explicit_additional"]
type FallbackReason = Literal[
    "context_unknown",
    "selection_review_unavailable",
    "invalid_proposal",
    "selection_transport_failure",
    "additional_budget_unavailable",
]
type QuestionSelectionRefs = Annotated[
    tuple[QuestionContentRef, ...],
    Field(max_length=1024, json_schema_extra={"uniqueItems": True}),
    BeforeValidator(immutable_sequence),
]


class ExcludedQuestionReason(OptionalContract):
    question_ref: QuestionContentRef
    reason_code: ArtifactId
    reported_reason: AbsentOptional[Description] = Field(
        default=None,
        validate_default=False,
        exclude_if=lambda value: value is None,
        json_schema_extra=omit_default_from_schema,
    )


def _explicit_bool(value: object) -> object:
    if type(value) is not bool:
        raise ValueError("expected an explicit boolean")
    return value


class _FallbackSelectionEvidence(ContractModel):
    @model_validator(mode="before")
    @classmethod
    def require_boolean_dispatch(cls, raw: object) -> object:
        if isinstance(raw, dict) and "dispatched" in raw:
            _explicit_bool(raw["dispatched"])
        return raw

    triggered_method: Literal["context_rules", "model_advisory"]
    trigger_attempt_ref: AttemptRef
    fallback_reason: FallbackReason
    diagnostic_refs: Annotated[EvidenceRefs, Field(min_length=1)]

    @model_validator(mode="after")
    def reason_matches_operation(self) -> Self:
        if (self.fallback_reason == "context_unknown") != (
            self.triggered_method == "context_rules"
        ):
            raise ValueError(
                "fallback reason must match the failed selection operation"
            )
        require_unique_runtime_refs(self.diagnostic_refs)
        return self


class UndispatchedSelectionFailure(_FallbackSelectionEvidence):
    dispatched: Literal[False]
    fallback_reason: Literal[
        "context_unknown",
        "selection_review_unavailable",
        "additional_budget_unavailable",
        "selection_transport_failure",
    ]


class DispatchedSelectionFailure(_FallbackSelectionEvidence):
    dispatched: Literal[True]
    triggered_method: Literal["model_advisory"]
    fallback_reason: Literal["invalid_proposal", "selection_transport_failure"]
    trigger_invocation_ref: ModelInvocationRef


type FallbackSelectionEvidence = Annotated[
    UndispatchedSelectionFailure | DispatchedSelectionFailure,
    Field(discriminator="dispatched"),
]


class _QuestionSelectionRecord(ContractModel):
    @field_validator("fallback_applied", mode="before", check_fields=False)
    @classmethod
    def require_boolean_fallback(cls, value: object) -> object:
        return _explicit_bool(value)

    schema_version: Literal["1.0.0"]
    selection_id: RecordId
    execution_id: RecordId
    profile_ref: ProfileContentRef
    policy_ref: PolicyContentRef
    configured_question_refs: Annotated[QuestionSelectionRefs, Field(min_length=1)]
    mandatory_question_refs: QuestionSelectionRefs
    proposed_optional_question_refs: QuestionSelectionRefs
    selected_optional_question_refs: QuestionSelectionRefs
    effective_question_refs: QuestionSelectionRefs
    excluded_question_reasons: Annotated[
        tuple[ExcludedQuestionReason, ...],
        Field(max_length=1024),
        BeforeValidator(immutable_sequence),
    ]
    context_snapshot_ref: ContextSnapshotRef
    context_snapshot_digest: Digest
    node_run_id: RecordId
    attempt_id: RecordId
    discovery_model_invocation_ref: ModelInvocationRef
    validation_issues: Annotated[
        tuple[ValidationIssue, ...],
        Field(max_length=1024),
        BeforeValidator(immutable_sequence),
    ]
    created_at: UTCDateTime

    @model_validator(mode="after")
    def validate_sets(self) -> Self:
        for refs in (
            self.configured_question_refs,
            self.mandatory_question_refs,
            self.selected_optional_question_refs,
            self.effective_question_refs,
            tuple(item.question_ref for item in self.excluded_question_reasons),
        ):
            require_unique_references(refs)
        if len(
            set(reference_identity(ref) for ref in self.proposed_optional_question_refs)
        ) != len(self.proposed_optional_question_refs):
            raise ValueError("proposed Question references must be unique")
        configured = {reference_identity(ref) for ref in self.configured_question_refs}
        mandatory = {reference_identity(ref) for ref in self.mandatory_question_refs}
        selected = {
            reference_identity(ref) for ref in self.selected_optional_question_refs
        }
        excluded = {
            reference_identity(item.question_ref)
            for item in self.excluded_question_reasons
        }
        if (
            not mandatory <= configured
            or not selected <= configured
            or selected & mandatory
        ):
            raise ValueError(
                "selected optional and mandatory Questions must be disjoint configured sets"
            )
        expected_effective = tuple(
            ref
            for ref in self.configured_question_refs
            if reference_identity(ref) in mandatory | selected
        )
        if (
            isinstance(self, FallbackSelectionRecord)
            and self.fallback_action == "block_profile"
        ):
            expected_effective = ()
            if selected:
                raise ValueError("blocked Profile cannot select optional Questions")
        if self.effective_question_refs != expected_effective:
            raise ValueError(
                "effective Questions must follow the configured order and preserve mandatory Questions"
            )
        if excluded != configured - {
            reference_identity(ref) for ref in expected_effective
        }:
            raise ValueError("excluded Question reasons must exactly cover omissions")
        if not expected_effective and not (
            isinstance(self, FallbackSelectionRecord)
            and self.fallback_action == "block_profile"
        ):
            raise ValueError("hit Profile requires at least one effective Question")
        return self


class AllDeclaredSelectionRecord(_QuestionSelectionRecord):
    method: Literal["all_declared"]
    fallback_applied: Literal[False]

    @model_validator(mode="after")
    def entire_catalogue(self) -> Self:
        if self.effective_question_refs != self.configured_question_refs:
            raise ValueError("all_declared requires the full catalogue")
        if self.proposed_optional_question_refs:
            raise ValueError("all_declared cannot carry model proposals")
        return self


class ContextRuleSelectionRecord(_QuestionSelectionRecord):
    method: Literal["context_rules"]
    fallback_applied: Literal[False]
    context_selection_rules: Annotated[
        tuple[ContextSelectionRule, ...],
        Field(max_length=1024),
        BeforeValidator(immutable_sequence),
    ]

    @model_validator(mode="after")
    def covered_optional_catalogue(self) -> Self:
        refs = tuple(rule.question_ref for rule in self.context_selection_rules)
        require_unique_references(refs)
        if set(reference_identity(ref) for ref in refs) != (
            {reference_identity(ref) for ref in self.configured_question_refs}
            - {reference_identity(ref) for ref in self.mandatory_question_refs}
        ):
            raise ValueError(
                "context rule references must cover the configured optional catalogue"
            )
        if self.proposed_optional_question_refs:
            raise ValueError("context_rules cannot carry model proposals")
        return self


class ModelAdvisorySelectionRecord(_QuestionSelectionRecord):
    method: Literal["model_advisory"]
    fallback_applied: Literal[False]
    selection_invocation_mode: SelectionInvocationMode
    model_invocation_ref: ModelInvocationRef

    @model_validator(mode="after")
    def accepted_proposal_matches_selection(self) -> Self:
        if (self.model_invocation_ref == self.discovery_model_invocation_ref) != (
            self.selection_invocation_mode == "shared_n02"
        ):
            raise ValueError(
                "shared selection must use the discovery call; additional selection must use a distinct call"
            )
        require_unique_references(self.proposed_optional_question_refs)
        if {
            reference_identity(ref) for ref in self.proposed_optional_question_refs
        } != {reference_identity(ref) for ref in self.selected_optional_question_refs}:
            raise ValueError(
                "accepted model proposals must exactly match selected optional Questions"
            )
        return self


class FallbackSelectionRecord(_QuestionSelectionRecord, OptionalContract):
    method: Literal["fallback"]
    fallback_applied: Literal[True]
    fallback_action: Literal["include_all_optional", "block_profile"]
    fallback_evidence: FallbackSelectionEvidence
    selection_call_policy: AbsentOptional[SelectionCallPolicy] = Field(
        default=None,
        validate_default=False,
        exclude_if=lambda value: value is None,
        json_schema_extra=omit_default_from_schema,
    )

    @model_validator(mode="after")
    def preserve_failed_operation(self) -> Self:
        evidence = self.fallback_evidence
        if (self.selection_call_policy is not None) != (
            evidence.triggered_method == "model_advisory"
        ):
            raise ValueError(
                "fallback preserves the configured call policy for model selection"
            )
        if (
            evidence.triggered_method == "context_rules"
            and self.proposed_optional_question_refs
        ):
            raise ValueError("context fallback cannot carry model proposals")
        if self.fallback_action == "include_all_optional" and (
            self.effective_question_refs != self.configured_question_refs
        ):
            raise ValueError(
                "include_all_optional fallback requires the entire catalogue"
            )
        if isinstance(evidence, DispatchedSelectionFailure) and (
            evidence.trigger_invocation_ref == self.discovery_model_invocation_ref
        ) != (
            self.selection_call_policy is not None
            and self.selection_call_policy.mode == "shared_n02"
        ):
            raise ValueError(
                "actual failed selection invocation must match its configured call mode"
            )
        if evidence.fallback_reason == "additional_budget_unavailable" and (
            self.selection_call_policy is None
            or self.selection_call_policy.mode != "explicit_additional"
        ):
            raise ValueError("budget failure requires configured additional selection")
        return self


type QuestionSelectionRecord = Annotated[
    AllDeclaredSelectionRecord
    | ContextRuleSelectionRecord
    | ModelAdvisorySelectionRecord
    | FallbackSelectionRecord,
    Field(discriminator="method"),
]
