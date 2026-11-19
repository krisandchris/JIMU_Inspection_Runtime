"""Scoped, recovery-safe lifecycle of immutable local evidence bytes."""

import asyncio
from dataclasses import dataclass
from datetime import datetime, timedelta
from hashlib import sha256
from re import fullmatch
from typing import Literal

from pydantic import TypeAdapter
from sqlalchemy import text
from sqlalchemy.engine import RowMapping
from sqlalchemy.ext.asyncio import AsyncConnection

from jimu_runtime.domain.definition_enums import EvidenceType
from jimu_runtime.domain.definition_support import UTCDateTime
from jimu_runtime.domain.enums import RuntimeRecordKind
from jimu_runtime.domain.policies import EvidencePolicyArtifact
from jimu_runtime.domain.runtime.evidence import (
    DerivedImageEvidence,
    EvidenceAsset,
    OriginalImageEvidence,
    TaskEvidenceAccess,
)
from jimu_runtime.domain.runtime.intake import AcceptedTask, IdempotencyScope
from jimu_runtime.domain.runtime.media import FinalizedAsset
from jimu_runtime.domain.runtime.tool_executions import ReturnedToolAttempt
from jimu_runtime.gateway.journal_records import (
    DispatchIntent,
    ModelDispatchResult,
    ToolDispatchResult,
    ToolDispatchTarget,
)
from jimu_runtime.inspection.media.persistence import (
    LocalMediaStore,
    _DispatchKey,
    _durable,
    _evidence_claim_digest,
    _locked,
)
from jimu_runtime.inspection.media.retention_models import (
    AuditResponseAccess,
    DispositionResult,
    EvidenceAccess,
    EvidenceAccessGrant,
    EvidenceDispositionCompletion,
    EvidenceDispositionIntent,
    EvidenceLocator,
    EvidenceRetentionPin,
    EvidenceReviewHold,
    EvidenceReviewRelease,
)
from jimu_runtime.inspection.media.retention_policy import (
    HistoricalEvidencePolicies,
    PolicyResolver,
)
from jimu_runtime.inspection.media.types import MediaError

_UTC = TypeAdapter[UTCDateTime](UTCDateTime)
_TERMINAL = {"completed", "partial_failed", "failed", "cancelled"}
_OWNER = """
    SELECT e.task_id, e.release_id, e.bundle_digest, e.status, t.record::text AS task_record,
           EXISTS (
                SELECT 1 FROM jimu_workflow.jobs AS j
                WHERE (j.project_id, j.client_id, j.execution_id) =
                    (e.project_id, e.client_id, e.execution_id) AND j.status <> 'done'
           ) AS jobs_open
    FROM jimu_workflow.executions AS e JOIN jimu_workflow.tasks AS t
        USING (project_id, client_id, task_id)
    WHERE (e.project_id, e.client_id, e.execution_id) = (:p, :c, :e)
"""
_OUTPUT = text("""
    SELECT record::text AS asset, output_digest
    FROM jimu_workflow.produced_outputs
    WHERE (project_id, client_id, execution_id, node_run_id, attempt_id, output_key)
        = (:p, :c, :e, :n, :a, :output) AND output_kind = 'evidence'
""")
_ALIASES = text("""
    SELECT project_id, client_id, execution_id, node_run_id, attempt_id, output_key,
           record::text AS asset, output_digest
    FROM jimu_workflow.produced_outputs
    WHERE output_kind = 'evidence'
        AND (record -> 'storage' ->> 'store_id', record -> 'storage' ->> 'object_key') = (:store, :path)
    ORDER BY project_id COLLATE "C", client_id COLLATE "C", execution_id COLLATE "C",
             node_run_id COLLATE "C", attempt_id COLLATE "C", output_key COLLATE "C"
    LIMIT 1025
""")
_BINDING = text("""
    SELECT project_id, client_id, NULL::text AS execution_id,
           record -> 'storage' ->> 'store_id' AS store_id,
           record -> 'media_ref' ->> 'sha256' AS content_sha256,
           (record ->> 'byte_size')::bigint AS byte_size, 'original' AS kind,
           NULL::text AS role, false AS retired
    FROM jimu_workflow.original_media WHERE file_key = :key
    UNION ALL
    SELECT project_id, client_id, execution_id, store_id, content_sha256, byte_size,
           'dispatch' AS kind, role, false AS retired
    FROM jimu_workflow.dispatch_media WHERE file_key = :key
    UNION ALL
    SELECT project_id, client_id, execution_id, store_id, content_sha256, byte_size,
           'request' AS kind, NULL::text AS role, EXISTS (
               SELECT 1 FROM jimu_workflow.observation_request_media_reclaims r
               WHERE r.file_key = req.file_key
           ) AS retired
    FROM jimu_workflow.observation_request_media req WHERE file_key = :key
    UNION ALL
    SELECT project_id, client_id, execution_id, store_id, content_sha256, byte_size,
           'marked_frame' AS kind, NULL::text AS role, false AS retired
    FROM jimu_workflow.marked_frame_media WHERE file_key = :key
""")
_PHYSICAL_OWNER = text("""
    SELECT NULL::text AS dispatch_id, NULL::text AS role, evidence_id, staged_at,
           NULL::text AS node_run_id, NULL::text AS attempt_id, NULL::text AS claim_digest,
           NULL::text AS receipt, NULL::text AS intent
    FROM jimu_workflow.observation_request_media
    WHERE (store_id, file_key) = (:store, :key)
        AND (project_id, client_id, execution_id) = (:p, :c, :e)
    UNION ALL
    SELECT m.dispatch_id, m.role, m.evidence_id, m.staged_at, d.node_run_id, d.attempt_id,
           m.evidence_claim_sha256 AS claim_digest, r.record::text AS receipt,
           d.record::text AS intent
    FROM jimu_workflow.dispatch_media AS m
    JOIN jimu_workflow.dispatch_intents AS d USING (project_id, client_id, execution_id, dispatch_id)
    LEFT JOIN jimu_workflow.dispatch_results AS r
        ON (d.project_id, d.client_id, d.execution_id, d.dispatch_id, false) =
           (r.project_id, r.client_id, r.execution_id, r.dispatch_id, r.late)
    WHERE (m.store_id, m.file_key) = (:store, :key) AND d.send_admitted
        AND (m.project_id, m.client_id, m.execution_id) = (:p, :c, :e)
""")
_PENDING = text("""
    SELECT i.project_id, i.client_id, i.execution_id, i.record::text AS intent,
           d.record::text AS completion
    FROM jimu_workflow.evidence_disposition_intents AS i
    LEFT JOIN jimu_workflow.evidence_disposition_completions AS d USING (store_id, file_key)
    WHERE (i.store_id, i.file_key) = (:store, :key)
""")
_HELD = text("""
    SELECT h.hold_id, h.execution_id, h.record::text AS hold, r.record::text AS release
    FROM jimu_workflow.evidence_retention_holds AS h
    LEFT JOIN jimu_workflow.evidence_retention_hold_releases AS r
        USING (project_id, client_id, execution_id, hold_id)
    WHERE (h.store_id, h.file_key) = (:store, :key)
    LIMIT 1025
""")
_HOLD = text("""
    SELECT h.file_key, h.store_id, h.record::text AS hold, r.record::text AS release
    FROM jimu_workflow.evidence_retention_holds AS h
    LEFT JOIN jimu_workflow.evidence_retention_hold_releases AS r
        USING (project_id, client_id, execution_id, hold_id)
    WHERE (h.project_id, h.client_id, h.execution_id, h.hold_id) = (:p, :c, :e, :hold_id)
""")


class EvidenceLifecycleError(MediaError):
    """Fixed retention diagnostic."""


class _EvidenceDigestMismatch(EvidenceLifecycleError):
    """The caller's pin does not identify the durable evidence record."""


@dataclass(frozen=True, slots=True)
class _Owner:
    task: AcceptedTask
    status: str
    jobs_open: bool

    @property
    def inactive(self) -> bool:
        return self.status in _TERMINAL and not self.jobs_open


@dataclass(frozen=True, slots=True)
class _Reference:
    locator: EvidenceLocator
    asset: EvidenceAsset


@dataclass(frozen=True, slots=True)
class _Plan:
    references: tuple[_Reference, ...]
    recorded_start: datetime
    pins: tuple[EvidenceRetentionPin, ...]


def _params(scope: IdempotencyScope, eid: str) -> dict[str, str]:
    return {"p": scope.project_id, "c": scope.client_id, "e": eid}


def _asset(body: str, digest: str) -> EvidenceAsset:
    if sha256(body.encode("utf-8")).hexdigest() != digest:
        raise EvidenceLifecycleError("evidence_output_digest_mismatch")
    parsed = EvidenceAsset.model_validate_json(body)
    if parsed.stable_json() != body:
        raise EvidenceLifecycleError("evidence_output_representation_mismatch")
    return parsed


def _authorized(
    grant: EvidenceAccessGrant, task: AcceptedTask, asset: EvidenceAsset
) -> bool:
    access = asset.access_scope
    return (
        grant.scope == task.idempotency_scope
        and (grant.task_id is None or grant.task_id == task.task_id)
        and access.project_id == grant.scope.project_id
        and (
            not isinstance(access, TaskEvidenceAccess)
            or (access.task_id == task.task_id and grant.task_id == access.task_id)
        )
    )


async def _owner(
    connection: AsyncConnection,
    grant: EvidenceAccessGrant,
    eid: str,
    *,
    lock: bool = False,
) -> _Owner | None:
    row = (
        await connection.execute(
            text(_OWNER + (" FOR UPDATE OF e" if lock else "")),
            _params(grant.scope, eid),
        )
    ).one_or_none()
    if row is None or (grant.task_id is not None and row.task_id != grant.task_id):
        return None
    task = AcceptedTask.model_validate_json(row.task_record)
    if (task.idempotency_scope, task.task_id, task.release_id, task.bundle_digest) != (
        grant.scope,
        row.task_id,
        row.release_id,
        row.bundle_digest,
    ):
        raise EvidenceLifecycleError("evidence_task_identity_mismatch")
    return _Owner(task, row.status, row.jobs_open)


def _same_source(task: AcceptedTask, asset: EvidenceAsset) -> bool:
    return (
        not isinstance(asset.spatial, (OriginalImageEvidence, DerivedImageEvidence))
        or asset.spatial.original_media_ref == task.media_ref
    )


def _checked_record[
    T: (
        EvidenceDispositionIntent,
        EvidenceDispositionCompletion,
        EvidenceReviewHold,
        EvidenceReviewRelease,
    )
](model: type[T], body: str) -> T:
    row = model.model_validate_json(body)
    if row.stable_json() != body:
        raise ValueError("retention_record_representation_mismatch")
    return row


async def _resolve(
    connection: AsyncConnection,
    grant: EvidenceAccessGrant,
    locator: EvidenceLocator,
    *,
    lock: bool = False,
) -> tuple[_Owner, EvidenceAsset] | None:
    row = (
        await connection.execute(
            _OUTPUT,
            {
                **_params(grant.scope, locator.execution_id),
                "n": locator.node_run_id,
                "a": locator.attempt_id,
                "output": f"evidence.{locator.evidence_id}",
            },
        )
    ).one_or_none()
    if row is None:
        return None
    if row.output_digest != locator.output_digest:
        raise _EvidenceDigestMismatch("evidence_output_digest_unverified")
    owner = await _owner(connection, grant, locator.execution_id, lock=lock)
    if owner is None:
        return None
    asset = _asset(row.asset, row.output_digest)
    if not _authorized(grant, owner.task, asset):
        return None
    if (
        asset.evidence_id != locator.evidence_id
        or asset.scope.execution_id != locator.execution_id
        or not _same_source(owner.task, asset)
    ):
        raise EvidenceLifecycleError("evidence_locator_mismatch")
    return owner, asset


class LocalEvidenceLifecycle:
    """Trusted maintenance adapter. The host must authenticate the access grant."""

    def __init__(self, media: LocalMediaStore, resolve_policy: PolicyResolver) -> None:
        self._media = media
        self._policy = resolve_policy

    def _key(self, asset: EvidenceAsset) -> str | None:
        path = asset.storage.object_key
        key = path.removeprefix("media-v1/")
        if (
            not isinstance(asset.stage, FinalizedAsset)
            or asset.stage.object_version != f"sha256:{asset.sha256}"
            or asset.storage.store_id != self._media._store_id
            or not path.startswith("media-v1/")
            or not fullmatch(r"[0-9a-f]{64}", key)
        ):
            return None
        return key

    async def _anchor(
        self, grant: EvidenceAccessGrant, locator: EvidenceLocator
    ) -> tuple[_Owner, EvidenceAsset] | None:
        await self._media._ensure_database()
        async with self._media._engine.connect() as connection:
            return await _resolve(connection, grant, locator)

    async def _binding(
        self,
        connection: AsyncConnection,
        grant: EvidenceAccessGrant,
        asset: EvidenceAsset,
        key: str,
    ) -> Literal["managed", "original", "unmanaged", "corrupt", "denied", "retired"]:
        rows = (await connection.execute(_BINDING, {"key": key})).all()
        if not rows:
            return "unmanaged"
        if len(rows) != 1:
            return "corrupt"
        (row,) = rows
        if (row.project_id, row.client_id) != (
            grant.scope.project_id,
            grant.scope.client_id,
        ) or (
            row.execution_id is not None
            and row.execution_id != asset.scope.execution_id
        ):
            return "denied"
        if (row.store_id, row.content_sha256, row.byte_size) != (
            asset.storage.store_id,
            asset.sha256,
            asset.byte_size,
        ):
            return "corrupt"
        if row.kind == "original":
            return (
                "original"
                if asset.evidence_type is EvidenceType.ORIGINAL_IMAGE
                else "corrupt"
            )
        if row.kind == "request":
            if row.retired:
                return "retired"
            return (
                "managed"
                if asset.evidence_type is EvidenceType.MODEL_REQUEST
                else "corrupt"
            )
        expected = {
            "wheel_mask": EvidenceType.MASK,
            "tactile_paving_mask": EvidenceType.MASK,
            "geometry": EvidenceType.GEOMETRY,
        }.get(row.role)
        if expected is None:
            # Model wire and marked frames use their own authority checks.
            return "unmanaged"
        return "managed" if asset.evidence_type is expected else "corrupt"

    @staticmethod
    def _values(
        grant: EvidenceAccessGrant,
        locator: EvidenceLocator,
        asset: EvidenceAsset,
        key: str,
    ) -> dict[str, str]:
        return {
            **_params(grant.scope, locator.execution_id),
            "store": asset.storage.store_id,
            "key": key,
        }

    @staticmethod
    async def _primary_access(
        connection: AsyncConnection,
        grant: EvidenceAccessGrant,
        owner: _Owner,
        locator: EvidenceLocator,
        asset: EvidenceAsset,
        key: str,
    ) -> bool:
        if isinstance(asset.spatial, OriginalImageEvidence):
            return grant.task_id == owner.task.task_id
        values = LocalEvidenceLifecycle._values(grant, locator, asset, key)
        source = (
            (await connection.execute(_PHYSICAL_OWNER, values)).mappings().one_or_none()
        )
        if source is None or source.evidence_id is None:
            return False
        rows = (
            (
                await connection.execute(
                    _ALIASES,
                    {
                        "store": asset.storage.store_id,
                        "path": asset.storage.object_key,
                    },
                )
            )
            .mappings()
            .all()
        )
        if len(rows) > 1024:
            return False
        matched = False
        for row in rows:
            if (row.project_id, row.client_id, row.execution_id, row.output_key) != (
                grant.scope.project_id,
                grant.scope.client_id,
                locator.execution_id,
                f"evidence.{source.evidence_id}",
            ):
                continue
            if source.dispatch_id is not None and (row.node_run_id, row.attempt_id) != (
                source.node_run_id,
                source.attempt_id,
            ):
                continue
            primary = _asset(row.asset, row.output_digest)
            if (
                not _authorized(grant, owner.task, primary)
                or not _same_source(owner.task, primary)
                or (primary.scope.execution_id, primary.evidence_id)
                != (locator.execution_id, source.evidence_id)
                or (
                    primary.storage,
                    primary.sha256,
                    primary.byte_size,
                    primary.evidence_type,
                )
                != (asset.storage, asset.sha256, asset.byte_size, asset.evidence_type)
            ):
                return False
            matched = True
        return matched

    @staticmethod
    def _existing(
        row: RowMapping,
        grant: EvidenceAccessGrant,
        locator: EvidenceLocator,
        asset: EvidenceAsset,
        clock: datetime,
    ) -> DispositionResult:
        try:
            intent = _checked_record(EvidenceDispositionIntent, row.intent)
            if (
                (row.project_id, row.client_id, row.execution_id)
                != (grant.scope.project_id, grant.scope.client_id, locator.execution_id)
                or (intent.storage, intent.sha256, intent.byte_size)
                != (asset.storage, asset.sha256, asset.byte_size)
                or not any(
                    pin.locator == locator and pin.retention == asset.retention
                    for pin in intent.references
                )
                or any(
                    pin.locator.execution_id != locator.execution_id
                    for pin in intent.references
                )
                or intent.requested_at > clock
            ):
                return DispositionResult("corrupt")
            completion = (
                _checked_record(EvidenceDispositionCompletion, row.completion)
                if row.completion is not None
                else None
            )
            if (
                completion is not None
                and not intent.requested_at <= completion.completed_at <= clock
            ):
                return DispositionResult("corrupt")
        except ValueError:
            return DispositionResult("corrupt")
        return DispositionResult("disposed", intent, completion)

    async def _physical(
        self, key: str, asset: EvidenceAsset, *, max_bytes: int | None = None
    ) -> tuple[Literal["available", "missing", "corrupt"], bytes | None]:
        limit = max(
            self._media._limits.max_original_bytes,
            self._media._limits.max_response_bytes,
            self._media._limits.max_sidecar_bytes,
            1024 * 1024,
        )
        if max_bytes is not None:
            limit = min(limit, max_bytes)
        try:
            data = await asyncio.to_thread(
                self._media._bytes.read,
                key,
                asset.sha256,
                asset.byte_size,
                limit,
            )
        except MediaError:
            path = self._media._bytes._directory / key
            try:
                await asyncio.to_thread(path.lstat)
            except FileNotFoundError:
                return "missing", None
            except OSError:
                pass
            return "corrupt", None
        return "available", data

    async def read_evidence(
        self,
        grant: EvidenceAccessGrant,
        locator: EvidenceLocator,
        *,
        max_bytes: int | None = None,
    ) -> EvidenceAccess:
        _validate_read_limit(max_bytes)
        try:
            anchor = await self._anchor(grant, locator)
        except _EvidenceDigestMismatch:
            return EvidenceAccess("denied", "evidence_scope_unavailable")
        except ValueError:
            return EvidenceAccess("corrupt", "evidence_output_mismatch")
        if anchor is None:
            return EvidenceAccess("denied", "evidence_scope_unavailable")
        owner, asset = anchor
        key = self._key(asset)
        if key is None:
            return EvidenceAccess("unmanaged", "evidence_storage_unmanaged", asset)

        async def read() -> EvidenceAccess:
            async with (
                self._media._file_locks((key,)),
                self._media._engine.connect() as connection,
            ):
                binding = await self._binding(connection, grant, asset, key)
                if (
                    binding == "unmanaged"
                    or binding == "corrupt"
                    or binding == "denied"
                ):
                    return EvidenceAccess(
                        binding,
                        "evidence_storage_binding_" + binding,
                        None if binding == "denied" else asset,
                    )
                if binding == "retired":
                    return EvidenceAccess(
                        "not_retained", "request_binding_reclaimed", asset
                    )
                try:
                    allowed = await self._primary_access(
                        connection, grant, owner, locator, asset, key
                    )
                except ValueError:
                    return EvidenceAccess("corrupt", "evidence_primary_mismatch")
                if not allowed:
                    return EvidenceAccess(
                        "denied", "evidence_primary_scope_unavailable"
                    )
                pending = (
                    (
                        await connection.execute(
                            _PENDING,
                            self._values(grant, locator, asset, key),
                        )
                    )
                    .mappings()
                    .one_or_none()
                )
                if pending is not None:
                    result = self._existing(
                        pending, grant, locator, asset, await _clock(connection)
                    )
                    if result.intent is None:
                        return EvidenceAccess(
                            "corrupt", "evidence_disposition_mismatch", asset
                        )
                    if result.completion is None:
                        return EvidenceAccess(
                            "disposition_pending",
                            "evidence_disposition_committed",
                            asset,
                        )
                    return EvidenceAccess(
                        "expired", "evidence_disposition_completed", asset
                    )
                if max_bytes is not None and asset.byte_size > max_bytes:
                    return EvidenceAccess(
                        "too_large", "evidence_read_limit_exceeded", asset
                    )
                status, contents = await self._physical(key, asset, max_bytes=max_bytes)
                return EvidenceAccess(
                    status, "evidence_bytes_" + status, asset, contents
                )

        return await _durable(read())

    async def read_model_audit(
        self,
        grant: EvidenceAccessGrant,
        execution_id: str,
        dispatch_id: str,
        *,
        late: bool,
        role: Literal["raw", "normalized"],
        expected_digest: str | None = None,
        max_bytes: int | None = None,
    ) -> AuditResponseAccess:
        _validate_read_limit(max_bytes)
        if role not in {"raw", "normalized"}:
            raise EvidenceLifecycleError("response_role_invalid")
        pin_denied = AuditResponseAccess("denied", "response_digest_unverified")
        try:
            async with self._media._engine.connect() as connection:
                owner = await _owner(connection, grant, execution_id)
        except ValueError:
            if expected_digest is not None:
                return pin_denied
            raise
        # Journal bytes have no EvidenceAsset access pin. Require an explicit
        # grant for the owning Task's forensic evidence.
        if owner is None or grant.task_id != owner.task.task_id:
            if expected_digest is not None:
                return pin_denied
            return AuditResponseAccess("denied", "response_scope_unavailable")
        try:
            dispatch = await self._media._journal.get_dispatch(
                grant.scope, execution_id, dispatch_id
            )
        except ValueError:
            if expected_digest is not None:
                return pin_denied
            raise
        receipt = (
            next((row.result for row in dispatch.results if row.late is late), None)
            if dispatch is not None
            else None
        )
        if not isinstance(receipt, ModelDispatchResult):
            if expected_digest is not None:
                return pin_denied
            return AuditResponseAccess("denied", "response_receipt_unavailable")
        assert dispatch is not None
        digest, size = (
            (receipt.response_digest, receipt.observed_response_bytes)
            if role == "raw"
            else (receipt.normalized_response_digest, receipt.normalized_response_bytes)
        )
        if expected_digest is not None and digest != expected_digest:
            return pin_denied
        omission = receipt.response_bytes_omission_reason
        if role == "raw" and omission is not None:
            return AuditResponseAccess("not_retained", omission, receipt, late=late)
        if role == "normalized" and receipt.normalized_response_digest is None:
            return AuditResponseAccess(
                "not_retained",
                omission or "normalized_proof_unavailable",
                receipt,
                late=late,
            )
        if (
            not dispatch.send_admitted
            or receipt.acknowledgement != "http_headers"
            or receipt.response_digest is None
            or receipt.observed_response_bytes <= 0
        ):
            return AuditResponseAccess(
                "not_retained", "response_body_unobserved", receipt, late=late
            )
        if role == "raw" and dispatch.intent.retain_complete_response is not True:
            return AuditResponseAccess(
                "not_retained", "raw_retention_pin_unverified", receipt, late=late
            )
        if receipt.response_evidence_error is not None:
            return AuditResponseAccess(
                "missing", receipt.response_evidence_error, receipt, late=late
            )
        if max_bytes is not None and size is not None and size > max_bytes:
            return AuditResponseAccess(
                "too_large",
                "response_read_limit_exceeded",
                receipt,
                late=late,
            )
        storage_role: Literal["model_raw", "model_normalized"] = (
            "model_raw" if role == "raw" else "model_normalized"
        )
        binding = _DispatchKey(grant.scope, execution_id, dispatch_id)
        try:
            async with self._media._engine.connect() as connection:
                data = await self._media._read_model_role(
                    connection,
                    binding,
                    storage_role,
                    receipt,
                    max_bytes=max_bytes,
                )
        except MediaError as error:
            reason = str(error)
            status: Literal["missing", "corrupt"] = (
                "missing"
                if reason
                in {
                    "model_response_raw_unavailable",
                    "model_response_normalized_unavailable",
                    "media_file_unavailable",
                }
                else "corrupt"
            )
            return AuditResponseAccess(status, reason, receipt, late=late)
        return AuditResponseAccess(
            "late" if late else "available",
            "verified_http_receipt",
            receipt,
            data,
            late=late,
        )

    async def place_hold(
        self,
        grant: EvidenceAccessGrant,
        locator: EvidenceLocator,
        hold_id: str,
        *,
        reason: str,
        at: datetime,
    ) -> EvidenceReviewHold:
        hold = EvidenceReviewHold(
            hold_id=hold_id,
            locator=locator,
            actor=grant.actor,
            reason=reason,
            placed_at=at,
        )
        return await _durable(self._place_hold(grant, hold))

    async def _place_hold(
        self, grant: EvidenceAccessGrant, hold: EvidenceReviewHold
    ) -> EvidenceReviewHold:
        anchor = (
            await self._anchor(grant, hold.locator) if grant.manage_retention else None
        )
        if anchor is None:
            raise EvidenceLifecycleError("retention_scope_unavailable")
        key = self._key(anchor[1])
        if key is None:
            raise EvidenceLifecycleError("retention_storage_unmanaged")
        owner, asset = anchor
        values = {
            **self._values(grant, hold.locator, asset, key),
            "hold_id": hold.hold_id,
        }
        async with (
            self._media._file_locks((key,)),
            self._media._transaction() as connection,
        ):
            await _locked(connection, key)
            await _validate_clock(connection, hold.placed_at)
            existing = (await connection.execute(_HOLD, values)).one_or_none()
            if existing is not None:
                current = _checked_record(EvidenceReviewHold, existing.hold)
                if current != hold or (existing.store_id, existing.file_key) != (
                    values["store"],
                    key,
                ):
                    raise EvidenceLifecycleError("retention_hold_identity_conflict")
                if existing.release is not None:
                    raise EvidenceLifecycleError("retention_hold_already_released")
                return current
            if (await connection.execute(_PENDING, values)).one_or_none() is not None:
                raise EvidenceLifecycleError("retention_disposition_already_committed")
            if await self._binding(connection, grant, anchor[1], key) not in {
                "managed",
                "original",
            }:
                raise EvidenceLifecycleError("retention_storage_unmanaged")
            if not await self._primary_access(
                connection, grant, owner, hold.locator, asset, key
            ):
                raise EvidenceLifecycleError("retention_scope_unavailable")
            await connection.execute(
                text("""
                    INSERT INTO jimu_workflow.evidence_retention_holds
                        (project_id, client_id, execution_id, store_id, file_key, hold_id, record)
                    VALUES (:p, :c, :e, :store, :key, :hold_id, CAST(:record AS json))
                    ON CONFLICT DO NOTHING
                """),
                {**values, "record": hold.stable_json()},
            )
            saved = (await connection.execute(_HOLD, values)).one()
            if saved.file_key != key or saved.hold != hold.stable_json():
                raise EvidenceLifecycleError("retention_hold_identity_conflict")
            return hold

    async def release_hold(
        self,
        grant: EvidenceAccessGrant,
        execution_id: str,
        hold_id: str,
        *,
        reason: str,
        at: datetime,
    ) -> EvidenceReviewRelease:
        release = EvidenceReviewRelease(
            hold_id=hold_id,
            actor=grant.actor,
            reason=reason,
            released_at=at,
        )
        return await _durable(self._release_hold(grant, execution_id, release))

    async def _release_hold(
        self, grant: EvidenceAccessGrant, eid: str, release: EvidenceReviewRelease
    ) -> EvidenceReviewRelease:
        if not grant.manage_retention:
            raise EvidenceLifecycleError("retention_scope_unavailable")
        await self._media._ensure_database()
        values = {**_params(grant.scope, eid), "hold_id": release.hold_id}
        async with self._media._engine.connect() as connection:
            existing = (await connection.execute(_HOLD, values)).one_or_none()
            if existing is None:
                raise EvidenceLifecycleError("retention_scope_unavailable")
            hold = _checked_record(EvidenceReviewHold, existing.hold)
            anchor = await _resolve(connection, grant, hold.locator)
            if anchor is None or hold.locator.execution_id != eid:
                raise EvidenceLifecycleError("retention_scope_unavailable")
            owner, asset = anchor
            key = self._key(asset)
            if (
                key is None
                or key != existing.file_key
                or asset.storage.store_id != existing.store_id
                or hold.hold_id != release.hold_id
                or not await self._primary_access(
                    connection, grant, owner, hold.locator, asset, key
                )
            ):
                raise EvidenceLifecycleError("retention_hold_identity_conflict")
        async with (
            self._media._file_locks((key,)),
            self._media._transaction() as connection,
        ):
            await _locked(connection, key)
            await _validate_clock(connection, release.released_at)
            if release.released_at < hold.placed_at:
                raise EvidenceLifecycleError("retention_hold_release_precedes_hold")
            await connection.execute(
                text("""
                    INSERT INTO jimu_workflow.evidence_retention_hold_releases
                        (project_id, client_id, execution_id, hold_id, record)
                    VALUES (:p, :c, :e, :hold_id, CAST(:record AS json))
                    ON CONFLICT DO NOTHING
                """),
                {**values, "record": release.stable_json()},
            )
            saved = (await connection.execute(_HOLD, values)).one()
            if saved.release != release.stable_json():
                raise EvidenceLifecycleError("retention_hold_release_conflict")
            return release

    async def dispose(
        self, grant: EvidenceAccessGrant, locator: EvidenceLocator, *, at: datetime
    ) -> DispositionResult:
        """Authorize once, then remove bytes under the same root/file lock."""
        _UTC.validate_python(at)
        return await _durable(self._dispose(grant, locator, at))

    async def _dispose(
        self, grant: EvidenceAccessGrant, locator: EvidenceLocator, at: datetime
    ) -> DispositionResult:
        if not grant.manage_retention:
            return DispositionResult("denied")
        try:
            anchor = await self._anchor(grant, locator)
            if anchor is None:
                return DispositionResult("denied")
            owner, asset = anchor
            key = self._key(asset)
            if key is None:
                return DispositionResult("unmanaged")
            existing = await self._resume_without_policy(
                grant, owner, locator, asset, key, at
            )
            if existing is not None:
                return existing
            plan = await self._plan(grant, owner, locator, asset, key, at)
        except EvidenceLifecycleError as error:
            if str(error) == "retention_clock_is_in_future":
                raise
            return DispositionResult("corrupt")
        except ValueError:
            return DispositionResult("corrupt")
        if isinstance(plan, DispositionResult):
            return plan
        async with self._media._file_locks((key,)):
            prepared = await self._prepare(grant, locator, key, at, plan)
            intent = prepared.intent
            if (
                prepared.status != "disposed"
                or intent is None
                or prepared.completion is not None
            ):
                return prepared
            status, _ = await self._physical(key, asset)
            if status == "corrupt":
                return DispositionResult("corrupt", intent)
            if status == "available":
                await asyncio.to_thread(self._media._bytes.remove_orphan, key)
            completion = await self._complete(
                grant, locator.execution_id, key, removed=status == "available"
            )
            return DispositionResult("disposed", intent, completion)

    async def _resume_without_policy(
        self,
        grant: EvidenceAccessGrant,
        owner: _Owner,
        locator: EvidenceLocator,
        asset: EvidenceAsset,
        key: str,
        at: datetime,
    ) -> DispositionResult | None:
        values = self._values(grant, locator, asset, key)
        async with self._media._engine.connect() as connection:
            if (await connection.execute(_PENDING, values)).one_or_none() is None:
                return None
        async with self._media._file_locks((key,)):
            async with self._media._engine.connect() as connection:
                clock = await _validate_clock(connection, at)
                row = (await connection.execute(_PENDING, values)).mappings().one()
                result = self._existing(row, grant, locator, asset, clock)
                if result.intent is None:
                    return result
                if not await self._primary_access(
                    connection, grant, owner, locator, asset, key
                ):
                    return DispositionResult("denied")
                refs = await self._reference_records(
                    connection, grant, owner, locator, asset
                )
                if isinstance(refs, DispositionResult):
                    return refs
                if {(item.locator, item.asset.retention) for item in refs} != {
                    (pin.locator, pin.retention) for pin in result.intent.references
                }:
                    return DispositionResult("corrupt")
                if result.completion is not None:
                    return result
            status, _ = await self._physical(key, asset)
            if status != "missing":
                return None
            completion = await self._complete(
                grant, locator.execution_id, key, removed=False
            )
            return DispositionResult("disposed", result.intent, completion)

    async def _plan(
        self,
        grant: EvidenceAccessGrant,
        owner: _Owner,
        locator: EvidenceLocator,
        asset: EvidenceAsset,
        key: str,
        at: datetime,
    ) -> _Plan | DispositionResult:
        values = self._values(grant, locator, asset, key)
        async with self._media._engine.connect() as connection:
            clock = await _validate_clock(connection, at)
            binding = await self._binding(connection, grant, asset, key)
            if binding != "managed":
                return DispositionResult(self._unavailable_binding(binding))
            if not owner.inactive:
                return DispositionResult("active_execution")
            if await _has_hold(connection, values, clock):
                return DispositionResult("review_hold")
            refs = await self._reference_records(
                connection, grant, owner, locator, asset
            )
            if isinstance(refs, DispositionResult):
                return refs
            source = (
                (await connection.execute(_PHYSICAL_OWNER, values))
                .mappings()
                .one_or_none()
            )
            if source is None or source.staged_at is None:
                return DispositionResult("policy_unverified")
            start = _UTC.validate_python(source.staged_at)
        # Archive I/O and policy parsing must never hold a PostgreSQL row or file lock.
        pins = await self._policy_pins(owner.task, refs, start)
        if isinstance(pins, DispositionResult):
            return pins
        return _Plan(refs, start, pins)

    @staticmethod
    def _unavailable_binding(
        binding: Literal["original", "unmanaged", "corrupt", "denied", "retired"],
    ) -> Literal["shared_reference", "unmanaged", "corrupt", "denied"]:
        if binding == "original":
            return "shared_reference"
        return "unmanaged" if binding == "retired" else binding

    async def _prepare(
        self,
        grant: EvidenceAccessGrant,
        locator: EvidenceLocator,
        key: str,
        at: datetime,
        plan: _Plan,
    ) -> DispositionResult:
        async with self._media._transaction() as connection:
            # Writers can already own their Execution row when the trigger
            # acquires the advisory lock. Use that same ordering here.
            anchor = await _resolve(connection, grant, locator, lock=True)
            if anchor is None:
                return DispositionResult("denied")
            owner, asset = anchor
            values = self._values(grant, locator, asset, key)
            await _locked(connection, key)
            clock = await _validate_clock(connection, at)
            binding = await self._binding(connection, grant, asset, key)
            if binding != "managed":
                return DispositionResult(self._unavailable_binding(binding))
            if not owner.inactive:
                return DispositionResult("active_execution")
            if await _has_hold(connection, values, clock):
                return DispositionResult("review_hold")
            refs = await self._reference_records(
                connection, grant, owner, locator, asset
            )
            if isinstance(refs, DispositionResult):
                return refs
            source = (
                (await connection.execute(_PHYSICAL_OWNER, values))
                .mappings()
                .one_or_none()
            )
            if (
                refs != plan.references
                or source is None
                or source.staged_at != plan.recorded_start
            ):
                return DispositionResult("shared_reference")
            if not await self._verify_physical_owner(
                connection, grant, values, plan.pins
            ):
                return DispositionResult("unmanaged")
            existing = (
                (await connection.execute(_PENDING, values)).mappings().one_or_none()
            )
            if existing is not None:
                authorized = self._existing(existing, grant, locator, asset, clock)
                if (
                    authorized.intent is None
                    or authorized.intent.references != plan.pins
                ):
                    return DispositionResult("corrupt")
                return authorized
            if any(at < item.expires_at for item in plan.pins):
                return DispositionResult("retention_pending")
            status, _ = await self._physical(key, asset)
            if status != "available":
                return DispositionResult(status)
            intent = EvidenceDispositionIntent(
                storage=asset.storage,
                sha256=asset.sha256,
                byte_size=asset.byte_size,
                requested_at=at,
                actor=grant.actor,
                references=plan.pins,
            )
            await connection.execute(
                text("""
                    INSERT INTO jimu_workflow.evidence_disposition_intents
                        (project_id, client_id, execution_id, store_id, file_key, record)
                    VALUES (:p, :c, :e, :store, :key, CAST(:record AS json))
                """),
                {**values, "record": intent.stable_json()},
            )
            return DispositionResult("disposed", intent)

    async def _verify_physical_owner(
        self,
        connection: AsyncConnection,
        grant: EvidenceAccessGrant,
        values: dict[str, str],
        aliases: tuple[EvidenceRetentionPin, ...],
    ) -> bool:
        source = (await connection.execute(_PHYSICAL_OWNER, values)).one_or_none()
        if source is None or source.evidence_id is None:
            # Model transport bytes have receipts but no EvidencePolicy artefact.
            return False
        candidates = [
            pin.locator
            for pin in aliases
            if pin.locator.evidence_id == source.evidence_id
        ]
        if source.dispatch_id is None:
            return bool(candidates)
        exact = next(
            (
                entry
                for entry in candidates
                if (
                    entry.node_run_id == source.node_run_id
                    and entry.attempt_id == source.attempt_id
                )
            ),
            None,
        )
        if exact is None:
            return False
        if source.receipt is None or source.claim_digest is None:
            return False
        try:
            primary = await _resolve(connection, grant, exact)
            if primary is None:
                return False
            _, asset = primary
            recorded = ToolDispatchResult.model_validate_json(source.receipt)
            intent = DispatchIntent.model_validate_json(source.intent)
            target = intent.target
            tool = recorded.execution
            implementation = recorded.implementation
        except ValueError:
            return False
        return (
            _evidence_claim_digest(asset) == source.claim_digest
            and isinstance(target, ToolDispatchTarget)
            and recorded.acknowledgement == "tool_process"
            and recorded.response_digest is not None
            and recorded.observed_response_bytes > 0
            and implementation is not None
            and implementation.implementation_version == target.implementation_version
            and implementation.proof_ref == target.implementation_proof_ref
            and tool is not None
            and tool.tool_execution_id == source.dispatch_id == intent.dispatch_id
            and tool.execution_id == exact.execution_id
            and tool.attempt_ref.record_id == exact.attempt_id
            and tool.node_run_ref.record_id == exact.node_run_id
            and tool.profile_ref == target.profile_ref
            and tool.object_ref == target.object_ref
            and isinstance(tool.attempt_result, ReturnedToolAttempt)
            and any(
                item.name == source.role
                and tuple(ref.record_id for ref in item.evidence_refs)
                == (asset.evidence_id,)
                for item in tool.attempt_result.result.evidence
            )
            and any(
                item.artifact_ref.record_kind is RuntimeRecordKind.EVIDENCE
                and item.artifact_ref.record_id == asset.evidence_id
                and item.digest == asset.sha256
                for item in tool.output_refs
            )
        )

    @staticmethod
    async def _reference_records(
        connection: AsyncConnection,
        grant: EvidenceAccessGrant,
        owner: _Owner,
        locator: EvidenceLocator,
        asset: EvidenceAsset,
    ) -> tuple[_Reference, ...] | DispositionResult:
        aliases = (
            await connection.execute(
                _ALIASES,
                {
                    "store": asset.storage.store_id,
                    "path": asset.storage.object_key,
                },
            )
        ).all()
        if len(aliases) > 1024:
            return DispositionResult("shared_reference")
        refs = []
        for row in aliases:
            if (row.project_id, row.client_id, row.execution_id) != (
                grant.scope.project_id,
                grant.scope.client_id,
                locator.execution_id,
            ):
                return DispositionResult("shared_reference")
            try:
                member = _asset(row.asset, row.output_digest)
            except ValueError:
                return DispositionResult("corrupt")
            if (
                not _authorized(grant, owner.task, member)
                or not _same_source(owner.task, member)
                or member.scope.execution_id != row.execution_id
                or row.output_key != f"evidence.{member.evidence_id}"
                or (
                    member.storage,
                    member.sha256,
                    member.byte_size,
                    member.evidence_type,
                )
                != (asset.storage, asset.sha256, asset.byte_size, asset.evidence_type)
                or not isinstance(member.stage, FinalizedAsset)
                or member.stage.object_version != f"sha256:{member.sha256}"
            ):
                return DispositionResult("shared_reference")
            refs.append(
                _Reference(
                    EvidenceLocator(
                        execution_id=row.execution_id,
                        node_run_id=row.node_run_id,
                        attempt_id=row.attempt_id,
                        evidence_id=member.evidence_id,
                        output_digest=row.output_digest,
                    ),
                    member,
                )
            )
        if locator not in {item.locator for item in refs}:
            return DispositionResult("corrupt")
        return tuple(refs)

    async def _policy_pins(
        self,
        task: AcceptedTask,
        refs: tuple[_Reference, ...],
        recorded_start: datetime,
    ) -> tuple[EvidenceRetentionPin, ...] | DispositionResult:
        pins: list[EvidenceRetentionPin] = []
        cache: dict[str, EvidencePolicyArtifact] = {}
        bodies = None
        if isinstance(self._policy, HistoricalEvidencePolicies):
            identities = tuple(
                dict.fromkeys(row.asset.retention.evidence_policy_ref for row in refs)
            )
            try:
                bodies = await self._policy.resolve_many(task, identities)
            except ValueError:
                return DispositionResult("policy_unverified")
        for reference in refs:
            member = reference.asset
            if not isinstance(member.stage, FinalizedAsset):
                return DispositionResult("policy_unverified")
            ref = member.retention.evidence_policy_ref
            try:
                policy = cache.get(ref.digest)
                if policy is None:
                    body = (
                        await self._policy(task, ref) if bodies is None else bodies[ref]
                    )
                    if sha256(body).hexdigest() != ref.digest:
                        return DispositionResult("policy_unverified")
                    policy = EvidencePolicyArtifact.model_validate_json(body)
                    cache[ref.digest] = policy
                if (
                    (policy.policy_id, policy.version) != (ref.artifact_id, ref.version)
                    or policy.policy.retention_class != member.retention.retention_class
                    or policy.policy.access_scope != member.access_scope.scope
                    or member.evidence_type not in policy.policy.allowed_evidence_types
                ):
                    return DispositionResult("policy_unverified")
                expires = max(recorded_start, member.stage.verified_at) + timedelta(
                    seconds=policy.policy.retention_duration_seconds
                )
            except (ValueError, OverflowError):
                return DispositionResult("policy_unverified")
            pins.append(
                EvidenceRetentionPin(
                    locator=reference.locator,
                    retention=member.retention,
                    expires_at=expires,
                )
            )
        return tuple(pins)

    async def _complete(
        self, grant: EvidenceAccessGrant, eid: str, key: str, *, removed: bool
    ) -> EvidenceDispositionCompletion:
        async with self._media._transaction() as connection:
            await _locked(connection, key)
            at = await connection.scalar(text("SELECT pg_catalog.clock_timestamp()"))
            completion = EvidenceDispositionCompletion(
                outcome="removed" if removed else "absent_on_recovery",
                completed_at=_UTC.validate_python(at),
            )
            await connection.execute(
                text("""
                    INSERT INTO jimu_workflow.evidence_disposition_completions
                        (project_id, client_id, execution_id, store_id, file_key, record)
                    VALUES (:p, :c, :e, :store, :key, CAST(:record AS json))
                """),
                {
                    **_params(grant.scope, eid),
                    "key": key,
                    "store": self._media._store_id,
                    "record": completion.stable_json(),
                },
            )
            return completion


async def _has_hold(
    connection: AsyncConnection, values: dict[str, str], clock: datetime
) -> bool:
    rows = (await connection.execute(_HELD, values)).all()
    if len(rows) > 1024:
        return True
    for row in rows:
        if row.release is None:
            return True
        try:
            hold = _checked_record(EvidenceReviewHold, row.hold)
            release = _checked_record(EvidenceReviewRelease, row.release)
            if (
                hold.hold_id != row.hold_id
                or hold.locator.execution_id != row.execution_id
                or release.hold_id != hold.hold_id
                or not hold.placed_at <= release.released_at <= clock
            ):
                return True
        except ValueError:
            return True
    return False


def _validate_read_limit(max_bytes: int | None) -> None:
    if max_bytes is not None and (type(max_bytes) is not int or max_bytes < 0):
        raise EvidenceLifecycleError("evidence_read_limit_invalid")


async def _clock(connection: AsyncConnection) -> datetime:
    return _UTC.validate_python(
        await connection.scalar(text("SELECT pg_catalog.clock_timestamp()"))
    )


async def _validate_clock(connection: AsyncConnection, at: datetime) -> datetime:
    clock = await _clock(connection)
    if at > clock:
        raise EvidenceLifecycleError("retention_clock_is_in_future")
    return clock
