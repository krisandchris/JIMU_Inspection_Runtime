"""N02 discovery consumes only the Workflow-owned eligible Profile set."""

from typing import Annotated, Literal, Self

from pydantic import BeforeValidator, Field, field_validator, model_validator

from jimu_runtime.domain.base import ContractModel
from jimu_runtime.domain.definition_enums import ModelCapability
from jimu_runtime.domain.definition_support import (
    AbsentOptional,
    immutable_sequence,
    omit_default_from_schema,
    reference_identity,
    require_unique_references,
)
from jimu_runtime.domain.definitions.capabilities import (
    ModelBindingContentRef,
    ProfileDefinition,
)
from jimu_runtime.domain.definitions.prompts import PromptTemplateContentRef
from jimu_runtime.domain.enums import RuntimeRecordKind
from jimu_runtime.domain.nodes.envelopes import (
    NodeError,
    NodeInputEnvelope,
    NodeIssues,
    NodeOutputEnvelope,
    check_envelope_pair,
)
from jimu_runtime.domain.primitives import Digest, RecordId
from jimu_runtime.domain.runtime.applicability import (
    ContextSnapshotRef,
    ProfileEligibilityEvaluation,
    ProfileEligibilityEvaluationRef,
    ProfileEligibilityScopeRefs,
)
from jimu_runtime.domain.runtime.executions import NodeId
from jimu_runtime.domain.runtime.facts import (
    EvidenceRef,
    ModelInvocationRef,
    PolicyContentRef,
    ProfileContentRef,
)
from jimu_runtime.domain.runtime.invocations import ModelInvocation
from jimu_runtime.domain.runtime.media import MediaRef
from jimu_runtime.domain.runtime.perception import (
    DiscoveryOutcome,
    EffectiveQuestionSelection,
)
from jimu_runtime.domain.runtime.selection import QuestionSelectionRecord
from jimu_runtime.domain.values import FiniteNumber


class DiscoveryProfileBinding(ContractModel):
    profile_ref: ProfileContentRef
    discovery_binding_ref: ModelBindingContentRef
    prompt_template_ref: PromptTemplateContentRef
    question_selection_policy_ref: PolicyContentRef


class DiscoveryInput(ContractModel):
    schema_version: Literal["1.0.0"]
    envelope: NodeInputEnvelope
    original_media_ref: MediaRef
    context_snapshot_ref: ContextSnapshotRef
    context_snapshot_digest: Digest
    profile_eligibility_evaluation_ref: ProfileEligibilityEvaluationRef
    eligible_profiles: Annotated[ProfileEligibilityScopeRefs, Field(min_length=1)]
    bindings: Annotated[
        tuple[DiscoveryProfileBinding, ...],
        Field(min_length=1, max_length=1024),
        BeforeValidator(immutable_sequence),
    ]

    @model_validator(mode="after")
    def check_input(self) -> Self:
        require_unique_references(self.eligible_profiles)
        if (
            self.envelope.node_id is not NodeId.N02
            or tuple(binding.profile_ref for binding in self.bindings)
            != self.eligible_profiles
        ):
            raise ValueError(
                "N02 bindings must exactly match the eligible Profile order"
            )
        return self

    def check_gate(self, eligibility: ProfileEligibilityEvaluation) -> None:
        if (
            self.envelope.execution_id,
            self.envelope.release_id,
            self.envelope.bundle_digest,
            self.context_snapshot_ref,
            self.profile_eligibility_evaluation_ref,
            self.eligible_profiles,
        ) != (
            eligibility.execution_id,
            eligibility.release_id,
            eligibility.bundle_digest,
            eligibility.context_snapshot_ref,
            eligibility.evaluation_ref,
            eligibility.eligible_profiles,
        ):
            raise ValueError(
                "N02 must consume the eligible partition of the selected Workflow gate"
            )

    def check_profiles(self, profiles: tuple[ProfileDefinition, ...]) -> None:
        if len(self.bindings) != len(profiles):
            raise ValueError("N02 requires a resolved definition per eligible Profile")
        for binding, profile in zip(self.bindings, profiles, strict=True):
            if (
                binding.profile_ref.artifact_id,
                binding.profile_ref.version,
                binding.discovery_binding_ref,
                reference_identity(binding.question_selection_policy_ref),
            ) != (
                profile.profile_id,
                profile.version,
                profile.discovery_binding_ref,
                reference_identity(profile.question_selection_policy_ref),
            ):
                raise ValueError(
                    "N02 discovery/selection bindings must match the pinned Profile"
                )


class ProfileHit(EffectiveQuestionSelection):
    model_invocation_ref: ModelInvocationRef
    confidence: AbsentOptional[FiniteNumber] = Field(
        default=None,
        validate_default=False,
        exclude_if=lambda value: value is None,
        json_schema_extra=omit_default_from_schema,
    )


class DiscoveryProfileDiagnostic(ContractModel):
    schema_version: Literal["1.0.0"]
    diagnostic_id: RecordId
    execution_id: RecordId
    model_invocation_ref: ModelInvocationRef
    raw_response_ref: EvidenceRef
    validation_issues: Annotated[NodeIssues, Field(min_length=1)]
    reason_code: Literal["invalid_profile_proposal", "noneligible_profile_proposal"]


class _DiscoveryGroupFailure(ContractModel):
    """Internal worker record for a scoped, journal-backed N02 group diagnostic."""

    schema_version: Literal["1.0.0"]
    execution_id: RecordId
    node_run_id: RecordId
    attempt_id: RecordId
    binding_ref: ModelBindingContentRef
    profile_refs: Annotated[ProfileEligibilityScopeRefs, Field(min_length=1)]
    dispatch_id: RecordId
    boundary: Literal["not_dispatched", "sent_unknown", "acknowledged"]
    model_invocation_ref: ModelInvocationRef | None
    error: NodeError

    @model_validator(mode="after")
    def check_identity(self) -> Self:
        require_unique_references(self.profile_refs)
        if (
            self.model_invocation_ref is not None
            and self.model_invocation_ref.record_id != self.dispatch_id
        ):
            raise ValueError("discovery failure must pin its own dispatch")
        return self


class DiscoveryAttemptOutput(ContractModel):
    schema_version: Literal["1.0.0"]
    outcome: Literal["hits", "no_hits", "failed", "partial"]
    execution_id: RecordId
    envelope: NodeOutputEnvelope
    profile_hits: Annotated[
        tuple[ProfileHit, ...],
        Field(max_length=1024),
        BeforeValidator(immutable_sequence),
    ]
    diagnostics: Annotated[
        tuple[DiscoveryProfileDiagnostic, ...],
        Field(max_length=1024),
        BeforeValidator(immutable_sequence),
    ]
    model_invocation_refs: Annotated[
        tuple[ModelInvocationRef, ...],
        Field(max_length=1024, json_schema_extra={"uniqueItems": True}),
        BeforeValidator(immutable_sequence),
    ]

    @model_validator(mode="after")
    def check_outcome(self) -> Self:
        if self.envelope.node_id is not NodeId.N02:
            raise ValueError("discovery output requires an N02 invocation")
        if any(
            item.ref.record_kind
            not in {
                RuntimeRecordKind.QUESTION_SELECTION_RECORD,
                RuntimeRecordKind.EVIDENCE,
                RuntimeRecordKind.MODEL_INVOCATION,
            }
            for item in self.envelope.output_artifacts
        ):
            raise ValueError(
                "discovery artifacts are limited to selection and invocation evidence"
            )

        require_unique_references(tuple(item.profile_ref for item in self.profile_hits))
        invocation_ids = {ref.record_id for ref in self.model_invocation_refs}
        if len(invocation_ids) != len(self.model_invocation_refs):
            raise ValueError("discovery invocation references must be unique")
        if (
            self.outcome == DiscoveryOutcome.HITS.value
            and not self.profile_hits
            or self.outcome
            in {DiscoveryOutcome.NO_HITS.value, DiscoveryOutcome.FAILED.value}
            and self.profile_hits
        ):
            raise ValueError("only a discovery hits outcome requires Profile hits")
        success = self.envelope.status.value == "succeeded"
        if success != (self.outcome != DiscoveryOutcome.FAILED.value) or (
            success and not invocation_ids
        ):
            raise ValueError(
                "discovery hits and no_hits require actual successful invocation"
            )
        if any(
            item.model_invocation_ref.record_id not in invocation_ids
            for item in self.profile_hits
        ):
            raise ValueError("Profile hit must reference discovery invocation evidence")
        if any(
            item.model_invocation_ref.record_id not in invocation_ids
            or item.execution_id != self.execution_id
            for item in self.diagnostics
        ):
            raise ValueError(
                "discovery diagnostic must belong to this Execution and invocation"
            )
        if len({item.diagnostic_id for item in self.diagnostics}) != len(
            self.diagnostics
        ):
            raise ValueError("discovery diagnostics must have distinct identities")
        return self

    def check_records(
        self,
        inputs: DiscoveryInput,
        selections: tuple[QuestionSelectionRecord, ...],
        invocations: tuple[ModelInvocation, ...],
        *,
        group_failures: tuple[_DiscoveryGroupFailure, ...] = (),
    ) -> None:
        check_envelope_pair(inputs.envelope, self.envelope)
        if self.execution_id != inputs.envelope.execution_id:
            raise ValueError("discovery output belongs to a different Execution")
        if tuple(inv.model_invocation_id for inv in invocations) != tuple(
            ref.record_id for ref in self.model_invocation_refs
        ):
            raise ValueError("resolve exactly the recorded discovery invocations")
        allowed = {
            binding.profile_ref.stable_json(): binding for binding in inputs.bindings
        }
        completed_profiles: set[str] = set()
        by_invocation = {inv.model_invocation_id: inv for inv in invocations}
        rejected: set[str] = set()
        failed_dispatches: set[str] = set()
        for failure in group_failures:
            group = tuple(
                row.profile_ref
                for row in inputs.bindings
                if row.discovery_binding_ref == failure.binding_ref
            )
            invocation = by_invocation.get(failure.dispatch_id)
            if (
                (failure.execution_id, failure.node_run_id, failure.attempt_id)
                != (
                    self.execution_id,
                    self.envelope.node_run_id,
                    self.envelope.attempt_id,
                )
                or not group
                or group != failure.profile_refs
                or failure.dispatch_id in failed_dispatches
                or (failure.model_invocation_ref is None) != (invocation is None)
                or invocation is not None
                and (
                    invocation.profile_refs != group
                    or invocation.outcome.status != "succeeded"
                    and invocation.outcome.error.code != failure.error.code
                )
            ):
                raise ValueError(
                    "discovery failure must pin the exact Profile group and Attempt"
                )
            keys = {ref.stable_json() for ref in group}
            if keys & rejected:
                raise ValueError("discovery failure groups must be disjoint")
            rejected.update(keys)
            failed_dispatches.add(failure.dispatch_id)
        for inv in invocations:
            if (
                inv.execution_id != self.execution_id
                or inv.node_id != NodeId.N02.value
                or inv.node_run_ref.record_id != self.envelope.node_run_id
                or inv.attempt_ref.record_id != self.envelope.attempt_id
                or inv.object_ref is not None
                or ModelCapability.PROFILE_DISCOVERY not in inv.purposes
                or not inv.profile_refs
            ):
                raise ValueError(
                    "discovery evidence must resolve to this N02 discovery Attempt"
                )
            for profile in inv.profile_refs:
                key = profile.stable_json()
                if key not in allowed or (
                    allowed[key].discovery_binding_ref.stable_json()
                    != inv.configured_provider.binding_ref.stable_json()
                ):
                    raise ValueError(
                        "invocation discovery Profiles and bindings must be eligible and pinned"
                    )
                if inv.outcome.status == "succeeded" and key not in rejected:
                    completed_profiles.add(key)
        if group_failures and (
            self.outcome
            not in {DiscoveryOutcome.FAILED.value, DiscoveryOutcome.PARTIAL.value}
            or (self.outcome == DiscoveryOutcome.PARTIAL.value)
            != bool(completed_profiles)
        ):
            raise ValueError(
                "discovery group failures must preserve other completed branches"
            )
        if (
            self.outcome
            in {DiscoveryOutcome.NO_HITS.value, DiscoveryOutcome.HITS.value}
            and completed_profiles != allowed.keys()
        ):
            raise ValueError(
                "complete discovery must cover the nonempty eligible catalogue"
            )
        if self.outcome == DiscoveryOutcome.PARTIAL.value and (
            not completed_profiles
            or completed_profiles == allowed.keys()
            and not self.diagnostics
        ):
            raise ValueError(
                "partial discovery requires successful work and an uncovered or diagnosed branch"
            )
        if len(selections) != len(self.profile_hits):
            raise ValueError(
                "resolve exactly one QuestionSelectionRecord per accepted Profile hit"
            )
        for hit, record in zip(self.profile_hits, selections, strict=True):
            hit.check_selection(self.execution_id, record)
            inv = by_invocation[hit.model_invocation_ref.record_id]
            if (
                hit.profile_ref not in inv.profile_refs
                or hit.profile_ref.stable_json() in rejected
                or inv.outcome.status != "succeeded"
                or record.discovery_model_invocation_ref != hit.model_invocation_ref
                or record.node_run_id != self.envelope.node_run_id
                or record.attempt_id != self.envelope.attempt_id
                or record.context_snapshot_ref.record_id
                != inputs.context_snapshot_ref.record_id
                or record.context_snapshot_digest != inputs.context_snapshot_digest
                or reference_identity(record.policy_ref)
                != reference_identity(
                    allowed[hit.profile_ref.stable_json()].question_selection_policy_ref
                )
            ):
                raise ValueError(
                    "hit selection must retain actual eligible discovery and pinned context"
                )
        for diagnostic in self.diagnostics:
            invocation = by_invocation[diagnostic.model_invocation_ref.record_id]
            if diagnostic.raw_response_ref not in invocation.evidence_refs:
                raise ValueError(
                    "diagnostic must pin response evidence from its invocation"
                )


class SkippedDiscoveryOutput(ContractModel):
    schema_version: Literal["1.0.0"]
    outcome: Literal["not_evaluated"]
    node_id: Literal[NodeId.N02]
    status: Literal["skipped"]
    reason_code: Literal["no_eligible_profiles"]
    node_run_id: RecordId
    execution_id: RecordId
    profile_eligibility_evaluation_ref: ProfileEligibilityEvaluationRef

    @field_validator("node_id", mode="before")
    @classmethod
    def decode_node_id(cls, value: object) -> NodeId:
        return NodeId._decode(value)

    def check_gate(self, eligibility: ProfileEligibilityEvaluation) -> None:
        if eligibility.eligible_profiles or (
            self.execution_id,
            self.profile_eligibility_evaluation_ref,
        ) != (eligibility.execution_id, eligibility.evaluation_ref):
            raise ValueError(
                "skipped N02 requires the matching empty eligible partition"
            )


type DiscoveryOutput = Annotated[
    DiscoveryAttemptOutput | SkippedDiscoveryOutput, Field(discriminator="outcome")
]
