"""Immutable admission, approval and activation records for a pinned Bundle."""

from enum import unique
from typing import Annotated, Literal, Self

from pydantic import BeforeValidator, Field, model_validator

from jimu_runtime.domain.base import ContractModel
from jimu_runtime.domain.definition_support import (
    AbsentOptional,
    ExternalCapabilityRequirement,
    OptionalContract,
    UTCDateTime,
    VocabularyMemberRef,
    immutable_sequence,
    omit_default_from_schema,
)
from jimu_runtime.domain.enums import RuntimeRecordKind, WireEnum
from jimu_runtime.domain.primitives import ArtifactId, RecordId, Version
from jimu_runtime.domain.runtime.authority import ActorRef
from jimu_runtime.domain.runtime.bundle_manifest import (
    MAX_BUNDLE_CAPABILITIES,
    BundleRef,
)
from jimu_runtime.domain.runtime.facts import TypedRuntimeRef
from jimu_runtime.domain.validation.issues import ValidationIssue


class ReleaseRef(TypedRuntimeRef):
    record_kind: Literal[RuntimeRecordKind.RELEASE]


class ActivationRef(TypedRuntimeRef):
    record_kind: Literal[RuntimeRecordKind.ACTIVATION]


class SupportedCapability(ContractModel):
    status: Literal["supported"]
    requirement: ExternalCapabilityRequirement
    implementation_version: Version

    @model_validator(mode="after")
    def allowed_implementation_version(self) -> Self:
        if (
            self.implementation_version
            not in self.requirement.allowed_implementation_versions
        ):
            raise ValueError(
                "supported capability requires an allowed implementation version"
            )
        return self


class UnavailableCapability(ContractModel):
    status: Literal["unsupported", "not_evaluated"]
    requirement: ExternalCapabilityRequirement


type CapabilityAssessment = Annotated[
    SupportedCapability | UnavailableCapability, Field(discriminator="status")
]
type CapabilityAssessments = Annotated[
    tuple[CapabilityAssessment, ...],
    Field(max_length=MAX_BUNDLE_CAPABILITIES),
    BeforeValidator(immutable_sequence),
]
# Receipt diagnostics are per severity, at the same scale as ValidationReport
# check entries. They retain independent budgets for errors and warnings.
type AdmissionIssues = Annotated[
    tuple[ValidationIssue, ...],
    Field(max_length=128),
    BeforeValidator(immutable_sequence),
]


class CapabilityReport(ContractModel):
    required: CapabilityAssessments
    optional: CapabilityAssessments

    @model_validator(mode="after")
    def unique_assessments(self) -> Self:
        assessments = (*self.required, *self.optional)
        identities = {
            (
                item.requirement.family,
                item.requirement.capability_id,
                item.requirement.contract_version,
            )
            for item in assessments
        }
        if len(identities) != len(assessments):
            raise ValueError("capability assessment identities must be unique")
        return self


class AdmissionReceipt(BundleRef):
    """Runtime technical decision. The receipt carries no business approval."""

    schema_version: Literal["1.0.0"]
    receipt_id: RecordId
    runtime_version: Version
    runtime_identity: ActorRef
    accepted: bool
    capability_report: CapabilityReport
    errors: AdmissionIssues
    warnings: AdmissionIssues
    timestamp: UTCDateTime

    @model_validator(mode="after")
    def outcome_matches_report(self) -> Self:
        if self.accepted == bool(self.errors):
            raise ValueError(
                "accepted receipts forbid errors and rejected receipts require errors"
            )
        if self.accepted and any(
            item.status != "supported" for item in self.capability_report.required
        ):
            raise ValueError(
                "accepted receipts require all reported required capabilities to be supported"
            )
        return self


class ApprovalReceipt(ContractModel):
    """Evidence of business approval for one Release and its admission receipt."""

    schema_version: Literal["1.0.0"]
    receipt_id: RecordId
    release_ref: ReleaseRef
    bundle_ref: BundleRef
    admission_receipt_id: RecordId
    approved_by: ActorRef
    approved_at: UTCDateTime


@unique
class ReleaseStatus(WireEnum):
    COMPILED = "compiled"
    SUBMITTED = "submitted"
    ACCEPTED = "accepted"
    REJECTED = "rejected"
    APPROVED = "approved"
    DEPRECATED = "deprecated"
    REVOKED = "revoked"


_PRE_ADMISSION = frozenset({ReleaseStatus.COMPILED, ReleaseStatus.SUBMITTED})
_WITH_APPROVAL = frozenset(
    {ReleaseStatus.APPROVED, ReleaseStatus.DEPRECATED, ReleaseStatus.REVOKED}
)


class ReleaseRecord(OptionalContract):
    """Record for a compiled Bundle pin. Each activation is recorded separately."""

    schema_version: Literal["1.0.0"]
    release_id: RecordId
    bundle_ref: BundleRef
    status: ReleaseStatus
    admission_receipt: AbsentOptional[AdmissionReceipt] = Field(
        default=None,
        validate_default=False,
        exclude_if=lambda value: value is None,
        json_schema_extra=omit_default_from_schema,
    )
    approval_receipt: AbsentOptional[ApprovalReceipt] = Field(
        default=None,
        validate_default=False,
        exclude_if=lambda value: value is None,
        json_schema_extra=omit_default_from_schema,
    )
    recorded_at: UTCDateTime

    @model_validator(mode="after")
    def pinned_receipts(self) -> Self:
        admission = self.admission_receipt
        approval = self.approval_receipt
        if self.status in _PRE_ADMISSION:
            if admission is not None or approval is not None:
                raise ValueError("pre-admission Release records forbid receipts")
            return self
        if admission is None or admission.accepted == (
            self.status is ReleaseStatus.REJECTED
        ):
            raise ValueError(
                "Release requires an admission receipt matching its outcome"
            )
        if (
            admission.bundle_id != self.bundle_ref.bundle_id
            or admission.bundle_version != self.bundle_ref.bundle_version
            or admission.bundle_digest != self.bundle_ref.bundle_digest
        ):
            raise ValueError("admission receipt must pin the Release Bundle")
        if admission.timestamp > self.recorded_at:
            raise ValueError("admission receipt must precede the Release record")
        if self.status not in _WITH_APPROVAL and approval is not None:
            raise ValueError("Release status does not permit business approval")
        if approval is None:
            if self.status in _WITH_APPROVAL:
                raise ValueError("Release status requires a business approval receipt")
            return self
        if (
            approval.release_ref.record_id != self.release_id
            or approval.bundle_ref != self.bundle_ref
            or approval.admission_receipt_id != admission.receipt_id
        ):
            raise ValueError(
                "approval receipt must pin the same Release, Bundle and admission"
            )
        if not admission.timestamp <= approval.approved_at <= self.recorded_at:
            raise ValueError(
                "business approval must follow admission and precede the Release record"
            )
        return self


class GlobalActivationScope(ContractModel):
    scope: Literal["global"]


class SceneActivationScope(ContractModel):
    scope: Literal["scene"]
    scene_type: VocabularyMemberRef


class RegionActivationScope(ContractModel):
    scope: Literal["region"]
    region_id: ArtifactId


class DeviceActivationScope(ContractModel):
    scope: Literal["device"]
    device_id: RecordId


type ActivationScope = Annotated[
    GlobalActivationScope
    | SceneActivationScope
    | RegionActivationScope
    | DeviceActivationScope,
    Field(discriminator="scope"),
]


class ActivationRecord(OptionalContract):
    """Append-only selection of a Release for a scope, including rollback."""

    schema_version: Literal["1.0.0"]
    activation_id: RecordId
    release_ref: ReleaseRef
    bundle_ref: BundleRef
    scope: ActivationScope
    activated_by: ActorRef
    activated_at: UTCDateTime
    previous_activation_ref: AbsentOptional[ActivationRef] = Field(
        default=None,
        validate_default=False,
        exclude_if=lambda value: value is None,
        json_schema_extra=omit_default_from_schema,
    )

    @model_validator(mode="after")
    def distinct_previous_activation(self) -> Self:
        previous = self.previous_activation_ref
        if previous is not None and previous.record_id == self.activation_id:
            raise ValueError("previous activation must be a distinct record")
        return self
