"""Address an original image already verified by the media boundary."""

from hashlib import sha256

from jimu_runtime.domain.definition_enums import EvidenceType
from jimu_runtime.domain.policies import EvidencePolicy
from jimu_runtime.domain.runtime.evidence import (
    EvidenceAsset,
    ExecutionEvidenceScope,
    OriginalImageEvidence,
    ProjectEvidenceAccess,
    RetentionPolicyRef,
    TaskEvidenceAccess,
)
from jimu_runtime.domain.runtime.facts import PolicyContentRef
from jimu_runtime.domain.runtime.intake import AcceptedTask
from jimu_runtime.domain.runtime.media import CoordinateSystem, MediaAsset


def original_image_evidence(
    task: AcceptedTask,
    execution_id: str,
    media: MediaAsset,
    coordinates: CoordinateSystem,
    policy_ref: PolicyContentRef,
    policy: EvidencePolicy,
) -> EvidenceAsset:
    """Describe the finalized original without duplicating its stored bytes."""
    task.validate_media(media)
    project_id = task.idempotency_scope.project_id
    material = f"{execution_id}:{media.media_ref.sha256}:{policy_ref.digest}"
    return EvidenceAsset(
        schema_version="1.0.0",
        evidence_id=f"input.original.{sha256(material.encode()).hexdigest()}",
        evidence_type=EvidenceType.ORIGINAL_IMAGE,
        scope=ExecutionEvidenceScope(scope="execution", execution_id=execution_id),
        storage=media.storage,
        sha256=media.media_ref.sha256,
        mime_type=media.mime_type,
        byte_size=media.byte_size,
        stage=media.stage,
        spatial=OriginalImageEvidence(
            kind="original_image",
            original_media_ref=media.media_ref,
            coordinate_system=coordinates,
        ),
        producer_id="jimu.media.intake",
        producer_version="1.0.0",
        access_scope=(
            TaskEvidenceAccess(
                scope="task", project_id=project_id, task_id=task.task_id
            )
            if policy.access_scope == "task"
            else ProjectEvidenceAccess(scope="project", project_id=project_id)
        ),
        retention=RetentionPolicyRef(
            evidence_policy_ref=policy_ref, retention_class=policy.retention_class
        ),
        created_at=media.stage.staged_at,
    )
