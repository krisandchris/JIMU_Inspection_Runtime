"""Immutable applicability results and provenance for precomputed gate records."""

from typing import Annotated, Literal, Self

from pydantic import (
    BeforeValidator,
    ConfigDict,
    Discriminator,
    Field,
    Tag,
    field_validator,
    model_validator,
)
from pydantic.json_schema import JsonSchemaValue

from jimu_runtime.domain.base import ContractModel
from jimu_runtime.domain.definition_support import (
    AbsentOptional,
    ContextFieldSelector,
    OptionalContract,
    UTCDateTime,
    immutable_sequence,
    omit_default_from_schema,
    reference_identity,
    require_unique_references,
)
from jimu_runtime.domain.definitions.capabilities import RuleSetContentRef
from jimu_runtime.domain.enums import ArtifactKind, RuntimeRecordKind, WireEnum
from jimu_runtime.domain.primitives import Digest, RecordId, Version
from jimu_runtime.domain.references import ContentRef
from jimu_runtime.domain.rules import RuleContentRef
from jimu_runtime.domain.runtime.facts import (
    AttemptRef,
    NodeRunRef,
    ObjectRef,
    PolicyContentRef,
    ProfileContentRef,
    TypedRuntimeRef,
    require_unique_runtime_refs,
)
from jimu_runtime.domain.validation.issues import IssueIndex, ValidationIssue


class RuleTruth(WireEnum):
    TRUE = "true"
    FALSE = "false"
    UNKNOWN = "unknown"


class ApplicabilityLevel(WireEnum):
    PROFILE = "profile"
    RULE_SET = "rule_set"
    RULE = "rule"


class ApplicabilityEvaluationStatus(WireEnum):
    EVALUATED = "evaluated"
    NOT_EVALUATED = "not_evaluated"


class ApplicabilityDisposition(WireEnum):
    INCLUDED = "included"
    EXCLUDED = "excluded"
    UNRESOLVED = "unresolved"


class ApplicabilityEvaluationOwner(WireEnum):
    PRE_N02_GATE = "pre_n02_gate"
    N06 = "n06"


class ApplicabilityReason(WireEnum):
    INCLUDED_BY_POLICY = "included_by_policy"
    EXCLUDED_BY_POLICY = "excluded_by_policy"
    METADATA_UNKNOWN = "metadata_unknown"
    METADATA_INVALID = "metadata_invalid"
    ANCESTOR_EXCLUDED = "ancestor_excluded"
    ANCESTOR_UNRESOLVED = "ancestor_unresolved"


class ContextSnapshotRef(TypedRuntimeRef):
    record_kind: Literal[RuntimeRecordKind.CONTEXT_SNAPSHOT]


class ProfileEligibilityEvaluationRef(TypedRuntimeRef):
    record_kind: Literal[RuntimeRecordKind.PROFILE_ELIGIBILITY_EVALUATION]


class RuleSetApplicabilityEvaluationRef(TypedRuntimeRef):
    record_kind: Literal[RuntimeRecordKind.RULE_SET_APPLICABILITY_EVALUATION]


class RuleApplicabilityEvaluationRef(TypedRuntimeRef):
    record_kind: Literal[RuntimeRecordKind.RULE_APPLICABILITY_EVALUATION]


class ApplicabilityTraceRef(TypedRuntimeRef):
    record_kind: Literal[RuntimeRecordKind.APPLICABILITY_TRACE]


class UnresolvedObligationRef(TypedRuntimeRef):
    record_kind: Literal[RuntimeRecordKind.UNRESOLVED_OBLIGATION]


type ApplicabilityAncestorRef = (
    ProfileEligibilityEvaluationRef | RuleSetApplicabilityEvaluationRef
)
type ApplicabilityEvaluationRef = (
    ApplicabilityAncestorRef | RuleApplicabilityEvaluationRef
)
type ApplicabilityTargetRef = ProfileContentRef | RuleSetContentRef | RuleContentRef
type ApplicabilityReasonEvidenceRef = PolicyContentRef | ContextFieldSelector


_LEVELS = (
    ApplicabilityLevel.PROFILE,
    ApplicabilityLevel.RULE_SET,
    ApplicabilityLevel.RULE,
)
_TARGET_KINDS = {
    ApplicabilityLevel.PROFILE: ArtifactKind.PROFILE,
    ApplicabilityLevel.RULE_SET: ArtifactKind.RULE_SET,
    ApplicabilityLevel.RULE: ArtifactKind.RULE,
}
_SOURCE_KINDS = {
    ApplicabilityLevel.PROFILE: RuntimeRecordKind.PROFILE_ELIGIBILITY_EVALUATION,
    ApplicabilityLevel.RULE_SET: RuntimeRecordKind.RULE_SET_APPLICABILITY_EVALUATION,
    ApplicabilityLevel.RULE: RuntimeRecordKind.RULE_APPLICABILITY_EVALUATION,
}
_OWNERS = {
    ApplicabilityLevel.PROFILE: ApplicabilityEvaluationOwner.PRE_N02_GATE,
    ApplicabilityLevel.RULE_SET: ApplicabilityEvaluationOwner.N06,
    ApplicabilityLevel.RULE: ApplicabilityEvaluationOwner.N06,
}
_TRUTH_REASONS = {
    RuleTruth.TRUE: (ApplicabilityReason.INCLUDED_BY_POLICY,),
    RuleTruth.FALSE: (ApplicabilityReason.EXCLUDED_BY_POLICY,),
    RuleTruth.UNKNOWN: (
        ApplicabilityReason.METADATA_UNKNOWN,
        ApplicabilityReason.METADATA_INVALID,
    ),
}
_TRUTH_DISPOSITIONS = {
    RuleTruth.TRUE: ApplicabilityDisposition.INCLUDED,
    RuleTruth.FALSE: ApplicabilityDisposition.EXCLUDED,
    RuleTruth.UNKNOWN: ApplicabilityDisposition.UNRESOLVED,
}


class _LevelTrace(ContractModel):
    level: ApplicabilityLevel
    target_ref: ApplicabilityTargetRef
    policy_ref: PolicyContentRef

    @field_validator("evaluation_status", mode="before", check_fields=False)
    @classmethod
    def decode_status(cls, value: object) -> ApplicabilityEvaluationStatus:
        return ApplicabilityEvaluationStatus._decode(value)

    @model_validator(mode="after")
    def target_matches_level(self) -> Self:
        if self.target_ref.artifact_kind is not _TARGET_KINDS[self.level]:
            raise ValueError("target_ref kind must match applicability level")
        return self


def _evaluated_level_schema(schema: JsonSchemaValue) -> None:
    schema["allOf"] = [
        *(
            {
                "if": {"properties": {"truth": {"const": truth.value}}},
                "then": {
                    "properties": {
                        "reason_code": {"enum": [reason.value for reason in reasons]}
                    }
                },
            }
            for truth, reasons in _TRUTH_REASONS.items()
        ),
        {
            "if": {"properties": {"truth": {"const": "false"}}},
            "then": {"properties": {"reason_evidence_refs": {"minItems": 1}}},
        },
        {
            "if": {"properties": {"truth": {"const": "unknown"}}},
            "then": {"properties": {"validation_issues": {"minItems": 1}}},
        },
        *(
            {
                "if": {"properties": {"level": {"const": level.value}}},
                "then": {
                    "properties": {
                        "evaluation_owner": {"const": _OWNERS[level].value},
                        "source_evaluation_ref": {
                            "properties": {"record_kind": {"const": source.value}}
                        },
                    }
                },
            }
            for level, source in _SOURCE_KINDS.items()
        ),
    ]


class EvaluatedApplicabilityLevel(_LevelTrace):
    model_config = ConfigDict(json_schema_extra=_evaluated_level_schema)

    evaluation_status: Literal[ApplicabilityEvaluationStatus.EVALUATED]
    truth: RuleTruth
    evaluation_owner: ApplicabilityEvaluationOwner
    source_evaluation_ref: ApplicabilityEvaluationRef
    declared_input_refs: Annotated[
        tuple[ContextFieldSelector, ...],
        Field(max_length=1024, json_schema_extra={"uniqueItems": True}),
        BeforeValidator(immutable_sequence),
    ]
    reason_code: ApplicabilityReason
    reason_evidence_refs: Annotated[
        tuple[ApplicabilityReasonEvidenceRef, ...],
        Field(max_length=1024, json_schema_extra={"uniqueItems": True}),
        BeforeValidator(immutable_sequence),
    ]
    validation_issues: Annotated[
        tuple[ValidationIssue, ...],
        Field(max_length=64),
        BeforeValidator(immutable_sequence),
    ]

    @model_validator(mode="after")
    def verify_evaluation_evidence(self) -> Self:
        if self.evaluation_owner is not _OWNERS[self.level]:
            raise ValueError("evaluation owner must match applicability level")
        if self.source_evaluation_ref.record_kind is not _SOURCE_KINDS[self.level]:
            raise ValueError("source evaluation kind must match applicability level")
        if self.reason_code not in _TRUTH_REASONS[self.truth]:
            raise ValueError("reason code must match evaluated truth")
        if self.truth is RuleTruth.FALSE and not self.reason_evidence_refs:
            raise ValueError("FALSE requires decisive policy or input evidence")
        if self.truth is RuleTruth.UNKNOWN and not self.validation_issues:
            raise ValueError("UNKNOWN requires metadata diagnostics")
        declared = set(self.declared_input_refs)
        if len(declared) != len(self.declared_input_refs):
            raise ValueError("declared input refs must be unique")
        if len(set(self.reason_evidence_refs)) != len(self.reason_evidence_refs):
            raise ValueError("reason evidence refs must be unique")
        for item in self.reason_evidence_refs:
            if isinstance(item, ContentRef):
                if reference_identity(item) != reference_identity(self.policy_ref):
                    raise ValueError("reason must cite the pinned applicability policy")
            elif item not in declared:
                raise ValueError("reason evidence must cite a declared input")
        allowed_refs = {reference_identity(self.policy_ref)} | {
            reference_identity(item.schema_ref) for item in declared
        }
        for issue in self.validation_issues:
            if any(
                reference_identity(ref) not in allowed_refs
                for ref in issue.related_refs
            ):
                raise ValueError(
                    "metadata diagnostics must use pinned policy or declared schema refs"
                )
        return self


class NotEvaluatedApplicabilityLevel(_LevelTrace):
    model_config = ConfigDict(
        json_schema_extra={
            "allOf": [
                {"properties": {"level": {"not": {"const": "profile"}}}},
                {
                    "if": {"properties": {"level": {"const": "rule_set"}}},
                    "then": {
                        "properties": {
                            "decisive_ancestor_ref": {
                                "properties": {
                                    "record_kind": {
                                        "const": "profile_eligibility_evaluation"
                                    }
                                }
                            }
                        }
                    },
                },
            ]
        }
    )

    evaluation_status: Literal[ApplicabilityEvaluationStatus.NOT_EVALUATED]
    reason_code: Literal[
        ApplicabilityReason.ANCESTOR_EXCLUDED, ApplicabilityReason.ANCESTOR_UNRESOLVED
    ]
    decisive_ancestor_ref: ApplicabilityAncestorRef

    @field_validator("reason_code", mode="before")
    @classmethod
    def decode_reason(cls, value: object) -> ApplicabilityReason:
        return ApplicabilityReason._decode(value)

    @model_validator(mode="after")
    def verify_possible_ancestor(self) -> Self:
        if self.level is ApplicabilityLevel.PROFILE or (
            self.level is ApplicabilityLevel.RULE_SET
            and not isinstance(
                self.decisive_ancestor_ref, ProfileEligibilityEvaluationRef
            )
        ):
            raise ValueError(
                "not_evaluated must cite a possible applicability ancestor"
            )
        return self


def _evaluation_tag(value: object) -> object:
    if isinstance(value, _LevelTrace):
        status = getattr(value, "evaluation_status", None)
    elif isinstance(value, dict):
        status = value.get("evaluation_status")
    else:
        return None
    if isinstance(status, ApplicabilityEvaluationStatus):
        return status.value
    return status if type(status) is str else None


type _EvaluatedBranch = Annotated[
    EvaluatedApplicabilityLevel, Tag(ApplicabilityEvaluationStatus.EVALUATED.value)
]
type _NotEvaluatedBranch = Annotated[
    NotEvaluatedApplicabilityLevel,
    Tag(ApplicabilityEvaluationStatus.NOT_EVALUATED.value),
]
type ApplicabilityLevelTrace = Annotated[
    _EvaluatedBranch | _NotEvaluatedBranch, Discriminator(_evaluation_tag)
]


class _PinnedRecord(OptionalContract):
    schema_version: Literal["1.0.0"]
    execution_id: RecordId
    release_id: RecordId
    bundle_digest: Digest
    context_snapshot_ref: ContextSnapshotRef


class ApplicabilityTrace(_PinnedRecord):
    applicability_trace_id: RecordId
    target_ref: ApplicabilityTargetRef
    profile_ref: ProfileContentRef
    context_snapshot_digest: Digest
    levels: Annotated[
        tuple[ApplicabilityLevelTrace, ...],
        Field(min_length=1, max_length=3),
        BeforeValidator(immutable_sequence),
    ]
    effective_truth: RuleTruth
    disposition: ApplicabilityDisposition
    decisive_level: ApplicabilityLevel
    created_at: UTCDateTime

    @property
    def trace_ref(self) -> ApplicabilityTraceRef:
        return ApplicabilityTraceRef(
            record_kind=RuntimeRecordKind.APPLICABILITY_TRACE,
            record_id=self.applicability_trace_id,
        )

    @model_validator(mode="after")
    def verify_path(self) -> Self:
        if tuple(item.level for item in self.levels) != _LEVELS[: len(self.levels)]:
            raise ValueError(
                "applicability levels must be the ordered Profile / RuleSet / Rule path"
            )
        if reference_identity(self.levels[0].target_ref) != reference_identity(
            self.profile_ref
        ):
            raise ValueError("Profile prefix must match profile_ref")
        if reference_identity(self.levels[-1].target_ref) != reference_identity(
            self.target_ref
        ):
            raise ValueError("trace target must match the last level")
        decisive: EvaluatedApplicabilityLevel | None = None
        for item in self.levels:
            if decisive is not None and decisive.truth is not RuleTruth.TRUE:
                reason = (
                    ApplicabilityReason.ANCESTOR_EXCLUDED
                    if decisive.truth is RuleTruth.FALSE
                    else ApplicabilityReason.ANCESTOR_UNRESOLVED
                )
                if not isinstance(item, NotEvaluatedApplicabilityLevel) or (
                    item.decisive_ancestor_ref != decisive.source_evaluation_ref
                    or item.reason_code is not reason
                ):
                    raise ValueError(
                        "stopped descendant must project the first decisive ancestor"
                    )
            else:
                if not isinstance(item, EvaluatedApplicabilityLevel):
                    raise ValueError(
                        "not_evaluated requires a FALSE or UNKNOWN ancestor"
                    )
                decisive = item
        assert decisive is not None
        if (
            self.effective_truth is not decisive.truth
            or self.decisive_level is not decisive.level
        ):
            raise ValueError(
                "effective truth and decisive level must reflect the decisive record"
            )
        if self.disposition is not _TRUTH_DISPOSITIONS[decisive.truth]:
            raise ValueError("disposition must match effective applicability truth")
        return self


type ProfileEligibilityScopeRefs = Annotated[
    tuple[ProfileContentRef, ...],
    Field(max_length=1024, json_schema_extra={"uniqueItems": True}),
    BeforeValidator(immutable_sequence),
]


class ProfileEligibilityEvaluation(_PinnedRecord):
    evaluation_id: RecordId
    workflow_version: Version
    evaluated_at: UTCDateTime
    configured_profile_refs: ProfileEligibilityScopeRefs
    eligible_profiles: ProfileEligibilityScopeRefs
    excluded_profiles: ProfileEligibilityScopeRefs
    unresolved_profiles: ProfileEligibilityScopeRefs
    applicability_traces: Annotated[
        tuple[ApplicabilityTraceRef, ...],
        Field(max_length=1024, json_schema_extra={"uniqueItems": True}),
        BeforeValidator(immutable_sequence),
    ]

    @property
    def evaluation_ref(self) -> ProfileEligibilityEvaluationRef:
        return ProfileEligibilityEvaluationRef(
            record_kind=RuntimeRecordKind.PROFILE_ELIGIBILITY_EVALUATION,
            record_id=self.evaluation_id,
        )

    @model_validator(mode="after")
    def verify_partition(self) -> Self:
        require_unique_references(self.configured_profile_refs)
        partition = (
            self.eligible_profiles,
            self.excluded_profiles,
            self.unresolved_profiles,
        )
        flattened = tuple(profile for subset in partition for profile in subset)
        require_unique_references(flattened)
        configured = {
            reference_identity(item): index
            for index, item in enumerate(self.configured_profile_refs)
        }
        if {reference_identity(item) for item in flattened} != configured.keys():
            raise ValueError(
                "Profile partition must exhaust the configured scope with exact refs"
            )
        for subset in partition:
            indexes = [configured[reference_identity(item)] for item in subset]
            if indexes != sorted(indexes):
                raise ValueError("each Profile partition must follow configured order")
        require_unique_runtime_refs(self.applicability_traces)
        if len(self.applicability_traces) != len(self.configured_profile_refs):
            raise ValueError("exactly one trace per configured Profile is required")
        return self


class _N06ApplicabilityEvaluation(_PinnedRecord):
    model_config = ConfigDict(
        json_schema_extra={
            "oneOf": [
                {
                    "required": ["node_run_ref", "attempt_ref"],
                    "properties": {"decisive_ancestor_ref": False},
                },
                {
                    "required": ["decisive_ancestor_ref"],
                    "properties": {
                        "node_run_ref": False,
                        "attempt_ref": False,
                        "object_ref": False,
                    },
                },
            ]
        }
    )

    evaluation_id: RecordId
    profile_eligibility_evaluation_ref: ProfileEligibilityEvaluationRef
    profile_ref: ProfileContentRef
    rule_set_ref: RuleSetContentRef
    applicability_trace_ref: ApplicabilityTraceRef
    recorded_at: UTCDateTime
    object_ref: AbsentOptional[ObjectRef] = Field(
        default=None,
        validate_default=False,
        exclude_if=lambda v: v is None,
        json_schema_extra=omit_default_from_schema,
    )
    node_run_ref: AbsentOptional[NodeRunRef] = Field(
        default=None,
        validate_default=False,
        exclude_if=lambda v: v is None,
        json_schema_extra=omit_default_from_schema,
    )
    attempt_ref: AbsentOptional[AttemptRef] = Field(
        default=None,
        validate_default=False,
        exclude_if=lambda v: v is None,
        json_schema_extra=omit_default_from_schema,
    )
    decisive_ancestor_ref: AbsentOptional[ApplicabilityAncestorRef] = Field(
        default=None,
        validate_default=False,
        exclude_if=lambda v: v is None,
        json_schema_extra=omit_default_from_schema,
    )

    @model_validator(mode="after")
    def verify_origin(self) -> Self:
        if self.decisive_ancestor_ref is None:
            if self.node_run_ref is None or self.attempt_ref is None:
                raise ValueError(
                    "evaluated N06 level requires node run and attempt refs"
                )
        elif any(
            ref is not None
            for ref in (self.node_run_ref, self.attempt_ref, self.object_ref)
        ):
            raise ValueError("ancestor projection forbids local N06 invocation refs")
        return self


class RuleSetApplicabilityEvaluation(_N06ApplicabilityEvaluation):
    @property
    def evaluation_ref(self) -> RuleSetApplicabilityEvaluationRef:
        return RuleSetApplicabilityEvaluationRef(
            record_kind=RuntimeRecordKind.RULE_SET_APPLICABILITY_EVALUATION,
            record_id=self.evaluation_id,
        )

    @model_validator(mode="after")
    def verify_possible_ancestor(self) -> Self:
        if (
            self.decisive_ancestor_ref is not None
            and self.decisive_ancestor_ref != self.profile_eligibility_evaluation_ref
        ):
            raise ValueError(
                "RuleSet projection must cite the selected Profile evaluation"
            )
        return self


class RuleApplicabilityEvaluation(_N06ApplicabilityEvaluation):
    rule_ref: RuleContentRef
    rule_set_applicability_evaluation_ref: RuleSetApplicabilityEvaluationRef

    @property
    def evaluation_ref(self) -> RuleApplicabilityEvaluationRef:
        return RuleApplicabilityEvaluationRef(
            record_kind=RuntimeRecordKind.RULE_APPLICABILITY_EVALUATION,
            record_id=self.evaluation_id,
        )

    @model_validator(mode="after")
    def verify_possible_ancestor(self) -> Self:
        if (
            self.decisive_ancestor_ref is not None
            and self.decisive_ancestor_ref
            not in (
                self.profile_eligibility_evaluation_ref,
                self.rule_set_applicability_evaluation_ref,
            )
        ):
            raise ValueError("Rule projection must cite a selected parent evaluation")
        return self


class ApplicabilityDiagnosticRef(ContractModel):
    """Location of a diagnostic in an evaluated level of a persisted trace."""

    trace_ref: ApplicabilityTraceRef
    level: ApplicabilityLevel
    issue_index: IssueIndex


class UnresolvedObligation(OptionalContract):
    model_config = ConfigDict(
        json_schema_extra={
            "allOf": [
                {
                    "if": {"properties": {"scope": {"const": "rule"}}},
                    "then": {"required": ["rule_set_ref"]},
                    "else": {"properties": {"rule_set_ref": False}},
                },
                {
                    "if": {
                        "properties": {"reason_code": {"const": "ancestor_unresolved"}}
                    },
                    "then": {
                        "required": ["decisive_ancestor_ref"],
                        "properties": {"scope": {"not": {"const": "profile"}}},
                    },
                    "else": {"properties": {"decisive_ancestor_ref": False}},
                },
            ]
        }
    )

    schema_version: Literal["1.0.0"]
    obligation_id: RecordId
    execution_id: RecordId
    profile_ref: ProfileContentRef
    scope: ApplicabilityLevel
    target_ref: ApplicabilityTargetRef
    rule_set_ref: AbsentOptional[RuleSetContentRef] = Field(
        default=None,
        validate_default=False,
        exclude_if=lambda v: v is None,
        json_schema_extra=omit_default_from_schema,
    )
    object_ref: AbsentOptional[ObjectRef] = Field(
        default=None,
        validate_default=False,
        exclude_if=lambda v: v is None,
        json_schema_extra=omit_default_from_schema,
    )
    applicability_trace_ref: ApplicabilityTraceRef
    reason_code: Literal[
        ApplicabilityReason.METADATA_UNKNOWN,
        ApplicabilityReason.METADATA_INVALID,
        ApplicabilityReason.ANCESTOR_UNRESOLVED,
    ]
    diagnostic_refs: Annotated[
        tuple[ApplicabilityDiagnosticRef, ...],
        Field(min_length=1, max_length=64, json_schema_extra={"uniqueItems": True}),
        BeforeValidator(immutable_sequence),
    ]
    decisive_ancestor_ref: AbsentOptional[ApplicabilityAncestorRef] = Field(
        default=None,
        validate_default=False,
        exclude_if=lambda v: v is None,
        json_schema_extra=omit_default_from_schema,
    )

    @field_validator("reason_code", mode="before")
    @classmethod
    def decode_reason(cls, value: object) -> ApplicabilityReason:
        return ApplicabilityReason._decode(value)

    @property
    def obligation_ref(self) -> UnresolvedObligationRef:
        return UnresolvedObligationRef(
            record_kind=RuntimeRecordKind.UNRESOLVED_OBLIGATION,
            record_id=self.obligation_id,
        )

    @model_validator(mode="after")
    def verify_obligation_shape(self) -> Self:
        if self.target_ref.artifact_kind is not _TARGET_KINDS[self.scope]:
            raise ValueError("obligation target kind must match scope")
        if self.scope is ApplicabilityLevel.PROFILE and reference_identity(
            self.target_ref
        ) != reference_identity(self.profile_ref):
            raise ValueError("Profile obligation target must match profile_ref")
        if (self.scope is ApplicabilityLevel.RULE) != (self.rule_set_ref is not None):
            raise ValueError(
                "exact parent RuleSet ref is required only for a Rule obligation"
            )
        inherited = self.reason_code is ApplicabilityReason.ANCESTOR_UNRESOLVED
        if inherited != (self.decisive_ancestor_ref is not None):
            raise ValueError(
                "ancestor_unresolved requires exactly one decisive ancestor"
            )
        if inherited and self.scope is ApplicabilityLevel.PROFILE:
            raise ValueError("a Profile obligation cannot inherit an ancestor")
        if len(set(self.diagnostic_refs)) != len(self.diagnostic_refs):
            raise ValueError("diagnostic refs must be unique")
        return self
