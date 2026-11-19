"""Scoped, bounded storage for generated joint-observation request artifacts."""

from jimu_runtime.bundle.integrity import sha256_digest
from jimu_runtime.domain.definition_enums import EvidenceType
from jimu_runtime.domain.fact_schemas.canonicalization import canonical_bytes
from jimu_runtime.domain.policies import EvidencePolicyArtifact
from jimu_runtime.domain.runtime.evidence import (
    EvidenceAsset,
    NonSpatialEvidence,
    ObjectEvidenceScope,
    ProjectEvidenceAccess,
    RetentionPolicyRef,
    TaskEvidenceAccess,
)
from jimu_runtime.domain.runtime.executions import NodeId
from jimu_runtime.domain.runtime.facts import ObjectRef, PolicyContentRef
from jimu_runtime.domain.runtime.intake import AcceptedTask
from jimu_runtime.domain.runtime.media import FinalizedAsset
from jimu_runtime.domain.runtime.perception import ObjectRecord
from jimu_runtime.inspection.media import LocalMediaStore
from jimu_runtime.inspection.perception import RequestEvidenceKind
from jimu_runtime.workflow.mock_support import VERSION, K
from jimu_runtime.workflow.worker import AdapterRejected, ClaimedNode

_STORE_ID = "jimu.local-request"
_MAX_REQUEST_EVIDENCE = 1024 * 1024
_MIMES = {
    "compiled_prompt": "text/plain",
    "response_schema": "application/schema+json",
}


def _identity(
    task: AcceptedTask,
    execution_id: str,
    object_id: str,
    kind: RequestEvidenceKind,
    digest: str,
) -> str:
    return "request." + sha256_digest(
        canonical_bytes(
            [
                task.idempotency_scope.project_id,
                task.idempotency_scope.client_id,
                task.task_id,
                execution_id,
                object_id,
                kind,
                digest,
            ]
        )
    )


class LocalRequestEvidence:
    """Content-addressed files are discoverable through the fenced N04 message."""

    def __init__(self, media: LocalMediaStore) -> None:
        self._media = media

    async def stage(
        self,
        claimed: ClaimedNode,
        obj: ObjectRecord,
        kind: RequestEvidenceKind,
        content: bytes,
        policy_ref: PolicyContentRef,
    ) -> EvidenceAsset:
        scope = claimed.opened.scope
        at = claimed.attempt.started_at
        if (
            claimed.node.node_id is not NodeId.N04
            or obj != claimed.previous.object_record(claimed.branch)
            or claimed.claim.scope != scope.task.idempotency_scope
            or obj.execution_id != scope.execution_id
            or at is None
            or kind not in _MIMES
            or not 0 < len(content) <= _MAX_REQUEST_EVIDENCE
        ):
            raise AdapterRejected("observation_request_scope_invalid")
        policy = scope.resolve(policy_ref, EvidencePolicyArtifact).policy
        digest = sha256_digest(content)
        evidence_id = _identity(
            scope.task, obj.execution_id, obj.object_id, kind, digest
        )
        previous = claimed.previous.observation_retry
        if previous is not None:
            retained = next(
                (
                    row
                    for row in previous.message.evidence
                    if row.evidence_id == evidence_id
                ),
                None,
            )
            if retained is None or await self.read(scope.task, retained) != content:
                raise AdapterRejected("observation_retry_request_evidence_mismatch")
            return retained
        storage, recorded_at = await self._media.stage_observation_request(
            claimed.claim.scope, obj.execution_id, evidence_id, content
        )
        project_id = scope.task.idempotency_scope.project_id
        return EvidenceAsset(
            schema_version=VERSION,
            evidence_id=evidence_id,
            evidence_type=EvidenceType.MODEL_REQUEST,
            scope=ObjectEvidenceScope(
                scope="object",
                execution_id=obj.execution_id,
                profile_ref=obj.profile_ref,
                object_ref=ObjectRef(record_kind=K.OBJECT, record_id=obj.object_id),
            ),
            storage=storage,
            sha256=digest,
            mime_type=_MIMES[kind],
            byte_size=len(content),
            stage=FinalizedAsset(
                stage="finalized",
                staged_at=recorded_at,
                verified_at=recorded_at,
                object_version=f"sha256:{digest}",
                verified_sha256=digest,
            ),
            spatial=NonSpatialEvidence(kind="non_spatial"),
            producer_id=_STORE_ID,
            producer_version=VERSION,
            access_scope=(
                TaskEvidenceAccess(
                    scope="task", project_id=project_id, task_id=scope.task.task_id
                )
                if policy.access_scope == "task"
                else ProjectEvidenceAccess(scope="project", project_id=project_id)
            ),
            retention=RetentionPolicyRef(
                evidence_policy_ref=policy_ref, retention_class=policy.retention_class
            ),
            created_at=recorded_at,
        )

    async def read(self, task: AcceptedTask, asset: EvidenceAsset) -> bytes:
        kind: RequestEvidenceKind | None = (
            "compiled_prompt"
            if asset.mime_type == _MIMES["compiled_prompt"]
            else "response_schema"
            if asset.mime_type == _MIMES["response_schema"]
            else None
        )
        access = asset.access_scope
        if (
            kind is None
            or asset.evidence_type is not EvidenceType.MODEL_REQUEST
            or not isinstance(asset.scope, ObjectEvidenceScope)
            or not isinstance(asset.stage, FinalizedAsset)
            or asset.stage.object_version != f"sha256:{asset.sha256}"
            or asset.evidence_id
            != _identity(
                task,
                asset.scope.execution_id,
                asset.scope.object_ref.record_id,
                kind,
                asset.sha256,
            )
            or access.project_id != task.idempotency_scope.project_id
            or isinstance(access, TaskEvidenceAccess)
            and access.task_id != task.task_id
        ):
            raise AdapterRejected("observation_request_storage_pin_invalid")
        return await self._media.read_observation_request(
            task.idempotency_scope,
            asset.scope.execution_id,
            asset.evidence_id,
            asset.storage,
            asset.sha256,
            asset.byte_size,
        )
