"""Versioned evidence disposition records and host access inputs."""

from dataclasses import dataclass, field
from typing import Annotated, Literal, Self

from pydantic import Field, model_validator

from jimu_runtime.domain.base import ContractModel
from jimu_runtime.domain.definition_support import UTCDateTime
from jimu_runtime.domain.primitives import ArtifactId, Digest, RecordId
from jimu_runtime.domain.runtime.authority import ActorRef
from jimu_runtime.domain.runtime.evidence import EvidenceAsset, RetentionPolicyRef
from jimu_runtime.domain.runtime.intake import IdempotencyScope
from jimu_runtime.domain.runtime.media import StorageReference
from jimu_runtime.gateway.journal_records import ModelDispatchResult

type Availability = Literal[
    "available",
    "denied",
    "not_retained",
    "missing",
    "corrupt",
    "unmanaged",
    "late",
    "expired",
    "disposition_pending",
    "too_large",
]
type DispositionStatus = Literal[
    "denied",
    "unmanaged",
    "shared_reference",
    "active_execution",
    "review_hold",
    "policy_unverified",
    "retention_pending",
    "missing",
    "corrupt",
    "disposed",
]


class EvidenceAccessGrant(ContractModel):
    """Trusted host authorization. A task pin is required for task-scoped reads."""

    scope: IdempotencyScope
    actor: ActorRef
    task_id: RecordId | None
    manage_retention: bool = False


class EvidenceLocator(ContractModel):
    execution_id: RecordId
    node_run_id: RecordId
    attempt_id: RecordId
    evidence_id: RecordId
    output_digest: Digest


class EvidenceReviewHold(ContractModel):
    schema_version: Literal["1.0.0"] = "1.0.0"
    hold_id: RecordId
    locator: EvidenceLocator
    actor: ActorRef
    reason: ArtifactId
    placed_at: UTCDateTime


class EvidenceReviewRelease(ContractModel):
    schema_version: Literal["1.0.0"] = "1.0.0"
    hold_id: RecordId
    actor: ActorRef
    reason: ArtifactId
    released_at: UTCDateTime


class EvidenceRetentionPin(ContractModel):
    locator: EvidenceLocator
    retention: RetentionPolicyRef
    expires_at: UTCDateTime


class EvidenceDispositionIntent(ContractModel):
    schema_version: Literal["1.0.0"] = "1.0.0"
    storage: StorageReference
    sha256: Digest
    byte_size: Annotated[int, Field(gt=0)]
    requested_at: UTCDateTime
    actor: ActorRef
    reason: Literal["retention_period_elapsed"] = "retention_period_elapsed"
    references: Annotated[
        tuple[EvidenceRetentionPin, ...], Field(min_length=1, max_length=1024)
    ]

    @model_validator(mode="after")
    def check_cutoff(self) -> Self:
        if any(pin.expires_at > self.requested_at for pin in self.references) or len(
            {pin.locator for pin in self.references}
        ) != len(self.references):
            raise ValueError("disposition requires elapsed, unique reference pins")
        return self


class EvidenceDispositionCompletion(ContractModel):
    schema_version: Literal["1.0.0"] = "1.0.0"
    outcome: Literal["removed", "absent_on_recovery"]
    completed_at: UTCDateTime


@dataclass(frozen=True, slots=True)
class EvidenceAccess:
    status: Availability
    reason: str
    asset: EvidenceAsset | None = None
    content: bytes | None = field(default=None, repr=False)


@dataclass(frozen=True, slots=True)
class AuditResponseAccess:
    status: Availability
    reason: str
    receipt: ModelDispatchResult | None = None
    content: bytes | None = field(default=None, repr=False)
    late: bool = False


@dataclass(frozen=True, slots=True)
class DispositionResult:
    status: DispositionStatus
    intent: EvidenceDispositionIntent | None = None
    completion: EvidenceDispositionCompletion | None = None
