"""Scope-keyed PostgreSQL bindings for an isolated durable local filesystem."""

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable, Coroutine
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from functools import partial
from hashlib import sha256
from pathlib import Path
from re import fullmatch
from typing import Literal

from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from jimu_runtime.domain.definition_enums import EvidenceType
from jimu_runtime.domain.enums import FactStatus, RuntimeRecordKind
from jimu_runtime.domain.fact_schemas.canonicalization import canonical_bytes
from jimu_runtime.domain.nodes.envelopes import SucceededNodeOutput
from jimu_runtime.domain.runtime.evidence import (
    EvidenceAsset,
    ObjectEvidenceScope,
    ProjectEvidenceAccess,
    TaskEvidenceAccess,
)
from jimu_runtime.domain.runtime.executions import NodeId
from jimu_runtime.domain.runtime.facts import EvidenceRef
from jimu_runtime.domain.runtime.intake import AcceptedTask, IdempotencyScope
from jimu_runtime.domain.runtime.media import (
    FinalizedAsset,
    ImageDimensions,
    MediaAsset,
    MediaRef,
    StorageReference,
)
from jimu_runtime.domain.runtime.perception import MarkedGroundingFrame, ObjectRecord
from jimu_runtime.domain.runtime.tool_executions import (
    FailedToolResult,
    KnownToolOutputState,
    NonKnownToolOutputState,
    ReturnedToolAttempt,
    ToolPlanStepOrigin,
    ToolResult,
)
from jimu_runtime.domain.values import EnumValue, RecordValue
from jimu_runtime.gateway.http_wire import VerifiedImage, WireError, strict_json
from jimu_runtime.gateway.invocation import TransportResult
from jimu_runtime.gateway.journal import DispatchJournal, StoredDispatch
from jimu_runtime.gateway.journal_records import (
    ModelDispatchResult,
    ModelDispatchTarget,
    NestedModelDispatchTarget,
    ToolDispatchResult,
    ToolDispatchTarget,
)
from jimu_runtime.inspection.media.decoder import verify_image
from jimu_runtime.inspection.media.files import LocalBytes
from jimu_runtime.inspection.media.types import (
    IMAGE_MIME,
    MASK_SLOT,
    RECORD_ID,
    STORE_ID,
    TOOL_SLOT,
    AuditedMaskSidecar,
    ImageMime,
    MaskSidecarRecord,
    MaskSidecarSlot,
    MediaCleanupPage,
    MediaError,
    MediaLimits,
    StoredModelResponse,
    ToolSidecarSlot,
    ToolSidecarSpec,
    VerifiedEvidence,
    VerifiedOriginal,
)
from jimu_runtime.workflow.mock_support import record_digest
from jimu_runtime.workflow.repository import read_output

type _Role = Literal["model_raw", "model_normalized"] | ToolSidecarSlot

_TOOL_STEPS: dict[ToolSidecarSlot, str] = {
    "wheel_mask": "wheel_segmentation",
    "tactile_paving_mask": "tactile_paving_segmentation",
    "geometry": "wheel_boundary_geometry",
}

_ORIGINAL = text("""
    SELECT record::text
    FROM jimu_workflow.original_media
    WHERE (project_id, client_id, media_id) = (:project, :client, :media)
""")
_ARTIFACT = text("""
    SELECT store_id, file_key, content_sha256, byte_size, wire_sha256, wire_size,
           evidence_id, evidence_claim_sha256, staged_at
    FROM jimu_workflow.dispatch_media
    WHERE (project_id, client_id, execution_id, dispatch_id, role)
        = (:project, :client, :execution, :dispatch, :role)
""")
_ARTIFACT_INSERT = text("""
    INSERT INTO jimu_workflow.dispatch_media
        (project_id, client_id, execution_id, dispatch_id, role, store_id, file_key,
         content_sha256, byte_size, wire_sha256, wire_size, evidence_id, evidence_claim_sha256)
    VALUES (:project, :client, :execution, :dispatch, :role, :store_id, :file_key,
        :digest, :size, :wire_digest, :wire_size, :evidence_id, :evidence_claim_digest)
    ON CONFLICT DO NOTHING
    RETURNING staged_at
""")
_OBSERVATION_REQUEST_LIMIT = 1024 * 1024
_OBSERVATION_REQUEST = text("""
    SELECT req.store_id, req.file_key, req.content_sha256, req.byte_size, req.staged_at
    FROM jimu_workflow.observation_request_media req
    WHERE (req.project_id, req.client_id, req.execution_id, req.evidence_id)
        = (:project, :client, :execution, :evidence)
        AND NOT EXISTS (
            SELECT 1 FROM jimu_workflow.observation_request_media_reclaims retired
            WHERE retired.file_key = req.file_key
        )
""")
_RECLAIM_OBSERVATION_REQUEST = text("""
    INSERT INTO jimu_workflow.observation_request_media_reclaims (file_key)
    SELECT req.file_key
    FROM jimu_workflow.observation_request_media req
    JOIN jimu_workflow.executions e USING (project_id, client_id, execution_id)
    WHERE (req.project_id, req.client_id, req.execution_id, req.evidence_id, req.file_key)
        = (:project, :client, :execution, :evidence, :key)
        AND e.status IN ('completed', 'partial_failed', 'failed', 'cancelled')
        AND NOT EXISTS (
            SELECT 1 FROM jimu_workflow.produced_outputs produced
            WHERE (produced.project_id, produced.client_id, produced.execution_id)
                = (:project, :client, :execution)
                AND produced.output_kind = 'evidence'
                AND produced.output_key = 'evidence.' || req.evidence_id
        )
        AND NOT EXISTS (
            SELECT 1 FROM jimu_workflow.dispatch_results seen
            CROSS JOIN LATERAL pg_catalog.json_array_elements(
                seen.record -> 'invocation' -> 'evidence_refs'
            ) AS ref(value)
            WHERE (seen.project_id, seen.client_id, seen.execution_id)
                = (:project, :client, :execution)
                AND ref.value ->> 'record_kind' = 'evidence'
                AND ref.value ->> 'record_id' = req.evidence_id
        )
        AND NOT EXISTS (
            -- An admitted send without a qualified receipt can report late.
            SELECT 1 FROM jimu_workflow.dispatch_intents intent
            WHERE (intent.project_id, intent.client_id, intent.execution_id)
                = (:project, :client, :execution)
                AND intent.send_admitted
                AND intent.record -> 'target' ->> 'node_id' = 'N04'
                AND NOT EXISTS (
                    SELECT 1 FROM jimu_workflow.dispatch_results resolved
                    WHERE (resolved.project_id, resolved.client_id, resolved.execution_id,
                           resolved.dispatch_id)
                        = (intent.project_id, intent.client_id, intent.execution_id,
                           intent.dispatch_id)
                        AND (
                            resolved.boundary = 'not_dispatched'
                            OR resolved.boundary = 'acknowledged'
                                AND pg_catalog.json_typeof(resolved.record -> 'invocation') = 'object'
                        )
                )
        )
    ON CONFLICT DO NOTHING
    RETURNING file_key
""")
_ACTIVE_MARKED_FRAME_ATTEMPT = text("""
    SELECT EXISTS (
        SELECT 1 FROM jimu_workflow.attempts a
        JOIN jimu_workflow.jobs j
            USING (project_id, client_id, execution_id, node_run_id, job_id, fence)
        JOIN jimu_workflow.node_runs n USING (project_id, client_id, execution_id, node_run_id)
        JOIN jimu_workflow.executions e USING (project_id, client_id, execution_id)
        JOIN jimu_workflow.tasks t USING (project_id, client_id, task_id)
        JOIN jimu_workflow.original_media m
            ON (m.project_id, m.client_id, m.media_id) = (t.project_id, t.client_id, :source_id)
        WHERE (a.project_id, a.client_id, a.execution_id, a.node_run_id, a.attempt_id)
            = (:project, :client, :execution, :node, :attempt)
            AND n.node_id = 'N03' AND n.status = 'running'
            AND e.status = 'running' AND e.cancel_requested_at IS NULL
            AND j.status = 'leased' AND j.current_attempt_id = a.attempt_id
            AND j.lease_owner = a.lease_owner
            AND j.lease_until > pg_catalog.clock_timestamp()
            AND j.attempt_deadline > pg_catalog.clock_timestamp()
            AND t.record -> 'media_ref' ->> 'media_id' = :source_id
            AND t.record -> 'media_ref' ->> 'sha256' = :source_digest
            AND m.record -> 'media_ref' ->> 'sha256' = :source_digest
            AND m.record -> 'storage' ->> 'store_id' = :store
            AND m.record -> 'dimensions' ->> 'width' = :width
            AND m.record -> 'dimensions' ->> 'height' = :height
    )
""")
_MARKED_FRAME = text("""
    SELECT store_id, file_key, content_sha256, byte_size, object_sha256, media_id
    FROM jimu_workflow.marked_frame_media
    WHERE (project_id, client_id, execution_id, node_run_id, attempt_id, object_id)
        = (:project, :client, :execution, :node, :attempt, :object)
""")
_AUTHORIZED_MARKED_FRAME = text("""
    SELECT m.node_run_id, m.attempt_id, m.store_id, m.file_key, m.content_sha256,
        m.byte_size, m.object_sha256, m.media_id, a.output_key AS authority_key,
        a.output_digest AS authority_digest
    FROM jimu_workflow.marked_frame_media m
    JOIN jimu_workflow.authority_outputs a
        USING (project_id, client_id, execution_id, node_run_id, attempt_id)
    JOIN jimu_workflow.produced_outputs p
        USING (project_id, client_id, execution_id, node_run_id, attempt_id)
    WHERE (m.project_id, m.client_id, m.execution_id, m.object_id)
        = (:project, :client, :execution, :object)
        AND p.output_kind = 'object' AND p.output_key = 'object.' || m.object_id
        AND p.output_digest = m.object_sha256
    LIMIT 2
""")
_DEFAULT_LIMITS = MediaLimits()
_BLOB_LOCK = text("SELECT pg_catalog.pg_advisory_xact_lock(:key)")


def _scope(scope: IdempotencyScope) -> dict[str, str]:
    IdempotencyScope.model_validate(scope)
    return {"project": scope.project_id, "client": scope.client_id}


def _key(kind: str, *parts: str) -> str:
    return sha256(canonical_bytes(["jimu.media.v1", kind, *parts])).hexdigest()


def _observation_request_binding(
    scope: IdempotencyScope, execution_id: str, evidence_id: str
) -> tuple[dict[str, str], str]:
    params = {
        **_scope(scope),
        "execution": RECORD_ID.validate_python(execution_id),
        "evidence": RECORD_ID.validate_python(evidence_id),
    }
    return params, _key(
        "observation-request",
        scope.project_id,
        scope.client_id,
        execution_id,
        evidence_id,
    )


@dataclass(frozen=True, slots=True)
class _DispatchKey:
    scope: IdempotencyScope
    execution_id: str
    dispatch_id: str

    def __post_init__(self) -> None:
        IdempotencyScope.model_validate(self.scope)
        RECORD_ID.validate_python(self.execution_id)
        RECORD_ID.validate_python(self.dispatch_id)

    def key(self, role: _Role) -> str:
        return _key(
            "dispatch",
            self.scope.project_id,
            self.scope.client_id,
            self.execution_id,
            self.dispatch_id,
            role,
        )

    def params(self) -> dict[str, str]:
        return {
            **_scope(self.scope),
            "execution": self.execution_id,
            "dispatch": self.dispatch_id,
        }


@dataclass(frozen=True, slots=True)
class _Blob:
    store_id: str
    digest: str
    size: int
    wire_digest: str | None = None
    wire_size: int | None = None
    evidence_id: str | None = None
    evidence_claim_digest: str | None = None

    def params(self) -> dict[str, str | int | None]:
        return {
            "store_id": self.store_id,
            "digest": self.digest,
            "size": self.size,
            "wire_digest": self.wire_digest,
            "wire_size": self.wire_size,
            "evidence_id": self.evidence_id,
            "evidence_claim_digest": self.evidence_claim_digest,
        }


@dataclass(frozen=True, slots=True)
class _ToolContext:
    binding: _DispatchKey
    dispatch: StoredDispatch
    target: ToolDispatchTarget
    task: AcceptedTask
    original: VerifiedOriginal


async def _locked(connection: AsyncConnection, key: str) -> None:
    value = int.from_bytes(bytes.fromhex(key[:16]), byteorder="big", signed=True)
    await connection.execute(_BLOB_LOCK, {"key": value})


async def _durable[T](operation: Coroutine[object, object, T]) -> T:
    """Keep DB ownership until the off-thread filesystem mutation has stopped."""
    task = asyncio.create_task(operation)

    def observe(completed: asyncio.Task[T]) -> None:
        if not completed.cancelled():
            completed.exception()

    task.add_done_callback(observe)
    return await asyncio.shield(task)


def _check_size(content: bytes, limit: int) -> None:
    if type(content) is not bytes or not 1 <= len(content) <= limit:
        raise MediaError("media_size_limit")


def _check_json_object(content: bytes) -> dict[str, object]:
    try:
        validated = strict_json(content, exact_numbers=True)
    except WireError:
        raise MediaError("media_json_invalid") from None
    if not isinstance(validated, dict):
        raise MediaError("media_json_object_required")
    return dict(validated)


class LocalMediaStore:
    """Trusted host adapter; use one private persistent root per PostgreSQL DB.

    Authenticate and authorize scopes in the host before calling. Read methods
    independently bind bytes to scope and durable identity.
    """

    def __init__(
        self,
        root: Path,
        engine: AsyncEngine,
        *,
        store_id: str = "jimu.local-media",
        limits: MediaLimits = _DEFAULT_LIMITS,
    ) -> None:
        self._store_id = STORE_ID.validate_python(store_id)
        self._bytes = LocalBytes(root)
        self._engine = engine
        self._journal = DispatchJournal(engine)
        self._limits = limits
        self._decode_slots = asyncio.Semaphore(limits.max_parallel_decodes)
        self._database_lock = asyncio.Lock()
        self._database_verified = False

    @asynccontextmanager
    async def _file_locks(self, keys: tuple[str, ...]) -> AsyncIterator[None]:
        descriptors: list[int] = []
        try:
            try:
                async with asyncio.timeout(5):
                    for shard in sorted({key[:2] for key in keys}):
                        fd = self._bytes.try_lock(shard)
                        while fd is None:
                            await asyncio.sleep(0.02)
                            fd = self._bytes.try_lock(shard)
                        descriptors.append(fd)
            except TimeoutError:
                raise MediaError("media_binding_lock_timeout") from None
            except OSError:
                raise MediaError("media_binding_lock_unavailable") from None
            yield
        finally:
            for fd in reversed(descriptors):
                self._bytes.release_lock(fd)

    async def _ensure_database(self) -> None:
        if self._database_verified:
            return
        async with self._database_lock:
            if self._database_verified:
                return
            async with self._engine.connect() as connection:
                identity = await connection.scalar(
                    text("""
                    SELECT pg_catalog.shobj_description(oid, 'pg_database')
                    FROM pg_catalog.pg_database WHERE datname = pg_catalog.current_database()
                """)
                )
            if not isinstance(identity, str) or not fullmatch(
                r"jimu-workflow-instance:[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}",
                identity,
            ):
                raise MediaError("media_database_identity_unverified")

            async def bind() -> None:
                async with self._file_locks(("00",)):
                    await asyncio.to_thread(self._bytes.bind_database, identity)

            await _durable(bind())
            self._database_verified = True

    @asynccontextmanager
    async def _transaction(self) -> AsyncIterator[AsyncConnection]:
        async with self._engine.connect() as connection:
            await connection.execution_options(isolation_level="READ COMMITTED")
            async with connection.begin():
                await connection.exec_driver_sql("SET LOCAL lock_timeout TO '5s'")
                yield connection

    @asynccontextmanager
    async def _publication(
        self, keys: tuple[str, ...], *reclaim: Callable[[], Awaitable[bool]]
    ) -> AsyncIterator[AsyncConnection]:
        await self._ensure_database()
        try:
            async with self._file_locks(keys), self._transaction() as connection:
                yield connection
        except (MediaError, SQLAlchemyError):
            # A fresh transaction rechecks the binding, including ambiguous
            # commit outcomes and a successful concurrent retry.
            for cleanup in reclaim:
                with suppress(MediaError, SQLAlchemyError):
                    await cleanup()
            raise

    async def _decode(
        self,
        content: bytes,
        mime_type: ImageMime,
        dimensions: ImageDimensions,
        *,
        encoding: Literal["rgb", "binary_mask"],
    ) -> None:
        await self._decode_slots.acquire()

        async def run() -> None:
            try:
                await asyncio.to_thread(
                    verify_image,
                    content,
                    mime_type,
                    dimensions,
                    self._limits,
                    encoding=encoding,
                )
            finally:
                self._decode_slots.release()

        await _durable(run())

    def _storage(self, key: str) -> StorageReference:
        return StorageReference(store_id=self._store_id, object_key=f"media-v1/{key}")

    @staticmethod
    def _finalized(digest: str, at: datetime) -> FinalizedAsset:
        return FinalizedAsset(
            stage="finalized",
            staged_at=at,
            verified_at=at,
            verified_sha256=digest,
            object_version=f"sha256:{digest}",
        )

    async def publish_original(
        self,
        scope: IdempotencyScope,
        media_ref: MediaRef,
        *,
        mime_type: ImageMime,
        dimensions: ImageDimensions,
        content: bytes,
    ) -> MediaAsset:
        """Publish a scope-owned original ahead of Task intake."""
        params = {**_scope(scope), "media": MediaRef.model_validate(media_ref).media_id}
        key = _key("original", scope.project_id, scope.client_id, media_ref.media_id)
        mime_type = IMAGE_MIME.validate_python(mime_type)
        dimensions = ImageDimensions.model_validate(dimensions)
        _check_size(content, self._limits.max_original_bytes)
        if sha256(content).hexdigest() != media_ref.sha256:
            raise MediaError("media_content_mismatch")
        await self._decode(content, mime_type, dimensions, encoding="rgb")
        return await _durable(
            self._publish_original(
                params, key, media_ref, mime_type, dimensions, content
            )
        )

    async def _publish_original(
        self,
        params: dict[str, str],
        key: str,
        media_ref: MediaRef,
        mime_type: ImageMime,
        dimensions: ImageDimensions,
        content: bytes,
    ) -> MediaAsset:
        async with self._publication(
            (key,), partial(self._cleanup_original, key, params)
        ) as connection:
            await _locked(connection, key)
            row = (await connection.execute(_ORIGINAL, params)).scalar_one_or_none()
            common = MediaAsset(
                schema_version="1.0.0",
                media_ref=media_ref,
                storage=self._storage(key),
                stage=self._finalized(media_ref.sha256, datetime.now(UTC)),
                mime_type=mime_type,
                dimensions=dimensions,
                byte_size=len(content),
                color_space="RGB",
            )
            if row is not None:
                old = MediaAsset.model_validate_json(row)
                if (
                    not isinstance(old.stage, FinalizedAsset)
                    or old.stage
                    != self._finalized(media_ref.sha256, old.stage.staged_at)
                    or old != common.model_copy(update={"stage": old.stage})
                ):
                    raise MediaError("original_media_identity_conflict")
            else:
                old = common
                await connection.execute(
                    text("""
                        INSERT INTO jimu_workflow.original_media
                            (project_id, client_id, media_id, record)
                        VALUES (:project, :client, :media, CAST(:record AS json))
                    """),
                    {**params, "record": common.stable_json()},
                )
            await asyncio.to_thread(
                self._bytes.publish,
                key,
                content,
                media_ref.sha256,
                recover_orphan=row is None,
            )
            return old

    async def scoped_original_metadata(
        self, scope: IdempotencyScope, media_ref: MediaRef
    ) -> MediaAsset:
        """Read the pinned original binding without making a byte availability claim."""
        params = {**_scope(scope), "media": MediaRef.model_validate(media_ref).media_id}
        key = _key("original", scope.project_id, scope.client_id, media_ref.media_id)
        await self._ensure_database()
        async with self._engine.connect() as connection:
            row = (await connection.execute(_ORIGINAL, params)).scalar_one_or_none()
        if row is None:
            raise MediaError("original_media_unavailable")
        asset = MediaAsset.model_validate_json(row)
        if (
            asset.media_ref != media_ref
            or asset.storage != self._storage(key)
            or not isinstance(asset.stage, FinalizedAsset)
            or asset.stage != self._finalized(media_ref.sha256, asset.stage.staged_at)
        ):
            raise MediaError("original_media_identity_conflict")
        return asset

    async def read_original(
        self, scope: IdempotencyScope, media_ref: MediaRef
    ) -> VerifiedOriginal:
        asset = await self.scoped_original_metadata(scope, media_ref)
        content = await asyncio.to_thread(
            self._bytes.read,
            asset.storage.object_key.removeprefix("media-v1/"),
            asset.media_ref.sha256,
            asset.byte_size,
            self._limits.max_original_bytes,
        )
        await self._decode(
            content,
            IMAGE_MIME.validate_python(asset.mime_type),
            asset.dimensions,
            encoding="rgb",
        )
        return VerifiedOriginal(asset, content)

    async def verified_original(
        self, scope: IdempotencyScope, media_ref: MediaRef
    ) -> MediaAsset:
        return (await self.read_original(scope, media_ref)).asset

    async def verified_task_original(
        self, scope: IdempotencyScope, task: AcceptedTask
    ) -> VerifiedOriginal:
        task = AcceptedTask.model_validate(task)
        if task.idempotency_scope != scope:
            raise MediaError("original_media_scope_mismatch")
        result = await self.read_original(scope, task.media_ref)
        task.validate_media(result.asset)
        return result

    @staticmethod
    def _marked_frame(obj: ObjectRecord) -> MarkedGroundingFrame:
        frame = ObjectRecord.model_validate(obj).marked_frame
        if (
            frame is None
            or obj.coordinate_system.coordinate_system_id
            != obj.source_media_ref.media_id
        ):
            raise MediaError("marked_frame_identity_mismatch")
        return frame

    async def stage_marked_frame(
        self,
        scope: IdempotencyScope,
        obj: ObjectRecord,
        image: VerifiedImage,
        *,
        node_run_id: str,
        attempt_id: str,
    ) -> None:
        """Stage trusted renderer bytes for the exact live N03 Attempt and Object."""
        frame = self._marked_frame(obj)
        params = {
            **_scope(scope),
            "execution": RECORD_ID.validate_python(obj.execution_id),
            "node": RECORD_ID.validate_python(node_run_id),
            "attempt": RECORD_ID.validate_python(attempt_id),
            "object": RECORD_ID.validate_python(obj.object_id),
            "store": self._store_id,
            "source_id": obj.source_media_ref.media_id,
            "source_digest": obj.source_media_ref.sha256,
            "width": str(obj.coordinate_system.dimensions.width),
            "height": str(obj.coordinate_system.dimensions.height),
        }
        key = _key(
            "marked-frame",
            scope.project_id,
            scope.client_id,
            obj.execution_id,
            node_run_id,
            attempt_id,
            obj.object_id,
        )
        _check_size(image.content, self._limits.max_sidecar_bytes)
        if (
            image.media_type != frame.mime_type
            or image.sha256 != frame.media_ref.sha256
            or len(image.content) != frame.byte_size
            or sha256(image.content).hexdigest() != image.sha256
        ):
            raise MediaError("marked_frame_content_mismatch")
        await self._decode(
            image.content,
            frame.mime_type,
            obj.coordinate_system.dimensions,
            encoding="rgb",
        )
        await _durable(self._stage_marked_frame(params, key, obj, frame, image.content))

    async def _stage_marked_frame(
        self,
        params: dict[str, str],
        key: str,
        obj: ObjectRecord,
        frame: MarkedGroundingFrame,
        content: bytes,
    ) -> None:
        storage_digest = sha256(obj.stable_json().encode("utf-8")).hexdigest()
        async with self._publication(
            (key,), partial(self._cleanup_file_candidate, key, 0)
        ) as connection:
            await _locked(connection, key)
            if not await connection.scalar(_ACTIVE_MARKED_FRAME_ATTEMPT, params):
                raise MediaError("marked_frame_attempt_unavailable")
            existing = (await connection.execute(_MARKED_FRAME, params)).one_or_none()
            if existing is None:
                await connection.execute(
                    text("""
                        INSERT INTO jimu_workflow.marked_frame_media
                            (project_id, client_id, execution_id, node_run_id, attempt_id,
                             object_id, object_sha256, media_id, store_id, file_key,
                             content_sha256, byte_size)
                        VALUES (:project, :client, :execution, :node, :attempt, :object,
                                :claim, :media, :store, :key, :digest, :size)
                    """),
                    {
                        **params,
                        "claim": storage_digest,
                        "media": frame.media_ref.media_id,
                        "key": key,
                        "digest": frame.media_ref.sha256,
                        "size": frame.byte_size,
                    },
                )
            elif tuple(existing) != (
                self._store_id,
                key,
                frame.media_ref.sha256,
                frame.byte_size,
                storage_digest,
                frame.media_ref.media_id,
            ):
                raise MediaError("marked_frame_identity_conflict")
            await asyncio.to_thread(
                self._bytes.publish,
                key,
                content,
                frame.media_ref.sha256,
                recover_orphan=existing is None,
            )

    async def read_marked_frame(
        self, scope: IdempotencyScope, obj: ObjectRecord
    ) -> VerifiedImage:
        """Read the exact frame of an authoritative, scope-owned N03 Object."""
        frame = self._marked_frame(obj)
        params = {
            **_scope(scope),
            "execution": RECORD_ID.validate_python(obj.execution_id),
            "object": RECORD_ID.validate_python(obj.object_id),
        }
        await self._ensure_database()
        async with self._engine.connect() as connection:
            rows = (await connection.execute(_AUTHORIZED_MARKED_FRAME, params)).all()
            if not rows:
                raise MediaError("marked_frame_unavailable")
            if len(rows) != 1:
                raise MediaError("marked_frame_identity_conflict")
            (row,) = rows
            key = _key(
                "marked-frame",
                scope.project_id,
                scope.client_id,
                obj.execution_id,
                row.node_run_id,
                row.attempt_id,
                obj.object_id,
            )
            if tuple(row)[2:8] != (
                self._store_id,
                key,
                frame.media_ref.sha256,
                frame.byte_size,
                sha256(obj.stable_json().encode("utf-8")).hexdigest(),
                frame.media_ref.media_id,
            ):
                raise MediaError("marked_frame_identity_conflict")
            actual = await read_output(
                connection,
                scope,
                obj.execution_id,
                row.node_run_id,
                row.attempt_id,
                f"object.{obj.object_id}",
                ObjectRecord,
            )
            authority = await read_output(
                connection,
                scope,
                obj.execution_id,
                row.node_run_id,
                row.attempt_id,
                row.authority_key,
                SucceededNodeOutput,
            )
            # Produced outputs hash stored JSON. Envelope pins hash canonical JSON.
            artifact_digest = record_digest(obj)
            if (
                actual != obj
                or authority is None
                or authority.node_id is not NodeId.N03
                or sha256(authority.stable_json().encode("utf-8")).hexdigest()
                != row.authority_digest
                or not any(
                    pin.ref.record_kind is RuntimeRecordKind.OBJECT
                    and pin.ref.record_id == obj.object_id
                    and pin.digest == artifact_digest
                    for pin in authority.output_artifacts
                )
            ):
                raise MediaError("marked_frame_identity_conflict")
        content = await asyncio.to_thread(
            self._bytes.read,
            key,
            frame.media_ref.sha256,
            frame.byte_size,
            self._limits.max_sidecar_bytes,
        )
        await self._decode(
            content, frame.mime_type, obj.coordinate_system.dimensions, encoding="rgb"
        )
        return VerifiedImage(content, frame.mime_type, frame.media_ref.sha256)

    async def stage_observation_request(
        self,
        scope: IdempotencyScope,
        execution_id: str,
        evidence_id: str,
        content: bytes,
    ) -> tuple[StorageReference, datetime]:
        """Publish one generated N04 request artifact against its exact Execution."""
        _check_size(content, _OBSERVATION_REQUEST_LIMIT)
        params, key = _observation_request_binding(scope, execution_id, evidence_id)
        return await _durable(self._stage_observation_request(params, key, content))

    async def _stage_observation_request(
        self, params: dict[str, str], key: str, content: bytes
    ) -> tuple[StorageReference, datetime]:
        digest = sha256(content).hexdigest()
        async with self._publication(
            (key,), partial(self._cleanup_file_candidate, key, 0)
        ) as connection:
            await _locked(connection, key)
            existing = (
                await connection.execute(_OBSERVATION_REQUEST, params)
            ).one_or_none()
            if existing is None:
                recorded = await connection.scalar(
                    text("""
                        INSERT INTO jimu_workflow.observation_request_media
                            (project_id, client_id, execution_id, evidence_id, store_id,
                             file_key, content_sha256, byte_size)
                        SELECT project_id, client_id, execution_id,
                               :evidence, :store, :key, :digest, :size
                        FROM jimu_workflow.executions
                        WHERE (project_id, client_id, execution_id)
                            = (:project, :client, :execution)
                                AND status IN ('initialized', 'running')
                        RETURNING staged_at
                    """),
                    {
                        **params,
                        "store": self._store_id,
                        "key": key,
                        "digest": digest,
                        "size": len(content),
                    },
                )
                if not isinstance(recorded, datetime):
                    raise MediaError("observation_request_media_unavailable")
            else:
                if tuple(existing)[:4] != (self._store_id, key, digest, len(content)):
                    raise MediaError("observation_request_media_identity_conflict")
                recorded = existing.staged_at
            await asyncio.to_thread(
                self._bytes.publish,
                key,
                content,
                digest,
                recover_orphan=existing is None,
            )
        return self._storage(key), recorded.astimezone(UTC)

    async def read_observation_request(
        self,
        scope: IdempotencyScope,
        execution_id: str,
        evidence_id: str,
        storage: StorageReference,
        digest: str,
        byte_size: int,
    ) -> bytes:
        """Verify an N04 request EvidenceAsset's immutable binding and physical bytes."""
        params, key = _observation_request_binding(scope, execution_id, evidence_id)
        await self._ensure_database()
        async with self._engine.connect() as connection:
            existing = (
                await connection.execute(_OBSERVATION_REQUEST, params)
            ).one_or_none()
        if existing is None:
            raise MediaError("observation_request_media_unavailable")
        if storage != self._storage(key) or tuple(existing)[:4] != (
            self._store_id,
            key,
            digest,
            byte_size,
        ):
            raise MediaError("observation_request_media_identity_conflict")
        return await asyncio.to_thread(
            self._bytes.read, key, digest, byte_size, _OBSERVATION_REQUEST_LIMIT
        )

    async def cleanup_unreferenced_observation_request(
        self, scope: IdempotencyScope, execution_id: str, evidence_id: str
    ) -> bool:
        """Reclaim a terminal unreferenced request while retaining its binding history."""
        params, key = _observation_request_binding(scope, execution_id, evidence_id)
        return await _durable(self._reclaim_observation_request(params, key))

    async def cleanup_unreferenced_observation_requests(
        self,
        scope: IdempotencyScope,
        execution_id: str,
        *,
        limit: int = 100,
        after_evidence_id: str | None = None,
    ) -> MediaCleanupPage:
        """Inspect a bounded page of terminal request bindings with no caller-side inventory."""
        if type(limit) is not int or not 1 <= limit <= 1024:
            raise MediaError("media_cleanup_limits_invalid")
        await self._ensure_database()
        params = {
            **_scope(scope),
            "execution": RECORD_ID.validate_python(execution_id),
            "after": ""
            if after_evidence_id is None
            else RECORD_ID.validate_python(after_evidence_id),
            "limit": limit + 1,
        }
        async with self._engine.connect() as connection:
            names = tuple(
                (
                    await connection.scalars(
                        text("""
                    SELECT req.evidence_id
                    FROM jimu_workflow.observation_request_media req
                    JOIN jimu_workflow.executions e USING (project_id, client_id, execution_id)
                    WHERE (req.project_id, req.client_id, req.execution_id)
                        = (:project, :client, :execution)
                        AND e.status IN ('completed', 'partial_failed', 'failed', 'cancelled')
                        AND req.evidence_id COLLATE "C" > :after
                        AND NOT EXISTS (
                            SELECT 1 FROM jimu_workflow.observation_request_media_reclaims retired
                            WHERE retired.file_key = req.file_key
                        )
                    ORDER BY req.evidence_id COLLATE "C"
                    LIMIT :limit
                """),
                        params,
                    )
                ).all()
            )
        page = names[:limit]
        removed = 0
        for evidence_id in page:
            if await self.cleanup_unreferenced_observation_request(
                scope, execution_id, evidence_id
            ):
                removed += 1
        return MediaCleanupPage(
            scanned=len(page),
            removed=removed,
            next_key=page[-1] if len(names) > limit else None,
        )

    async def _reclaim_observation_request(
        self, params: dict[str, str], key: str
    ) -> bool:
        await self._ensure_database()
        async with self._file_locks((key,)), self._transaction() as connection:
            await _locked(connection, key)
            retired = await connection.scalar(
                _RECLAIM_OBSERVATION_REQUEST, {**params, "key": key}
            )
        if retired is None:
            return False
        await self._cleanup_file_candidate(key, 0)
        return True

    async def _require_dispatch(self, binding: _DispatchKey) -> StoredDispatch:
        dispatch = await self._journal.get_dispatch(
            binding.scope, binding.execution_id, binding.dispatch_id
        )
        if dispatch is None or not dispatch.send_admitted:
            raise MediaError("media_dispatch_unavailable")
        return dispatch

    async def _stage_blob(
        self,
        connection: AsyncConnection,
        binding: _DispatchKey,
        role: _Role,
        content: bytes,
        blob: _Blob,
    ) -> datetime:
        key = binding.key(role)
        params = {**binding.params(), "role": role}
        await _locked(connection, key)
        inserted = (
            await connection.execute(
                _ARTIFACT_INSERT, {**params, **blob.params(), "file_key": key}
            )
        ).scalar_one_or_none()
        if inserted is None:
            current = await self._get_blob(connection, binding, role)
            if current is None or current[0] != blob:
                raise MediaError("dispatch_media_identity_conflict")
            staged_at = current[1]
        else:
            staged_at = inserted.astimezone(UTC)
        await asyncio.to_thread(
            self._bytes.publish,
            key,
            content,
            blob.digest,
            recover_orphan=inserted is not None,
        )
        return staged_at

    @staticmethod
    async def _get_blob(
        connection: AsyncConnection, binding: _DispatchKey, role: _Role
    ) -> tuple[_Blob, datetime] | None:
        row = (
            await connection.execute(_ARTIFACT, {**binding.params(), "role": role})
        ).one_or_none()
        if row is None:
            return None
        if row.file_key != binding.key(role):
            raise MediaError("dispatch_media_identity_conflict")
        return (
            _Blob(
                row.store_id,
                row.content_sha256,
                row.byte_size,
                row.wire_sha256,
                row.wire_size,
                row.evidence_id,
                row.evidence_claim_sha256,
            ),
            row.staged_at.astimezone(UTC),
        )

    async def _read_blob(
        self, binding: _DispatchKey, role: _Role, blob: _Blob, limit: int
    ) -> bytes:
        await self._ensure_database()
        if blob.store_id != self._store_id:
            raise MediaError("media_store_identity_mismatch")
        return await asyncio.to_thread(
            self._bytes.read, binding.key(role), blob.digest, blob.size, limit
        )

    async def cleanup_unbound_original(
        self, scope: IdempotencyScope, media_id: str
    ) -> bool:
        """Reclaim a crashed/rolled-back publication whose binding was never committed."""
        params = {**_scope(scope), "media": RECORD_ID.validate_python(media_id)}
        key = _key("original", scope.project_id, scope.client_id, media_id)
        return await _durable(self._cleanup_original(key, params))

    async def _cleanup_original(self, key: str, params: dict[str, str]) -> bool:
        await self._ensure_database()
        async with self._file_locks((key,)), self._transaction() as connection:
            await _locked(connection, key)
            if (
                await connection.execute(_ORIGINAL, params)
            ).scalar_one_or_none() is not None:
                return False
            await asyncio.to_thread(self._bytes.remove_orphan, key)
            return True

    async def cleanup_unbound_dispatch(
        self, scope: IdempotencyScope, execution_id: str, dispatch_id: str, role: _Role
    ) -> bool:
        """Reclaim only bytes without a committed role binding."""
        if role not in {"model_raw", "model_normalized"}:
            TOOL_SLOT.validate_python(role)
        binding = _DispatchKey(scope, execution_id, dispatch_id)
        return await _durable(self._cleanup_dispatch(binding, role))

    async def _cleanup_dispatch(self, binding: _DispatchKey, role: _Role) -> bool:
        key = binding.key(role)
        await self._ensure_database()
        async with self._file_locks((key,)), self._transaction() as connection:
            await _locked(connection, key)
            if await self._get_blob(connection, binding, role) is not None:
                return False
            await asyncio.to_thread(self._bytes.remove_orphan, key)
            return True

    async def cleanup_unbound_files(
        self,
        *,
        minimum_age: int = 3600,
        scan_limit: int = 100,
        after_key: str | None = None,
    ) -> MediaCleanupPage:
        """Reclaim old files without caller IDs; continue with the returned cursor."""
        if (
            type(minimum_age) is not int
            or minimum_age < 1
            or type(scan_limit) is not int
            or not 1 <= scan_limit <= 1024
            or (after_key is not None and not fullmatch(r"[0-9a-f]{64}", after_key))
        ):
            raise MediaError("media_cleanup_limits_invalid")
        await self._ensure_database()
        candidates = await asyncio.to_thread(
            self._bytes.orphan_candidates, minimum_age, scan_limit + 1, after_key or ""
        )
        page = candidates[:scan_limit]
        removed = await _durable(self._cleanup_file_candidates(page, minimum_age))
        return MediaCleanupPage(
            scanned=len(page),
            removed=removed,
            next_key=page[-1] if len(candidates) > scan_limit else None,
        )

    async def _cleanup_file_candidates(
        self, candidates: tuple[str, ...], minimum_age: int
    ) -> int:
        removed = 0
        for key in candidates:
            removed += await self._cleanup_file_candidate(key, minimum_age)
        return removed

    async def _cleanup_file_candidate(self, key: str, minimum_age: int) -> bool:
        async with self._file_locks((key,)), self._transaction() as connection:
            await _locked(connection, key)
            bound = await connection.scalar(
                text("""
                SELECT EXISTS (
                    SELECT file_key FROM jimu_workflow.original_media WHERE file_key = :key
                    UNION ALL
                    SELECT file_key FROM jimu_workflow.dispatch_media WHERE file_key = :key
                    UNION ALL
                    SELECT req.file_key FROM jimu_workflow.observation_request_media req
                        WHERE req.file_key = :key AND NOT EXISTS (
                            SELECT 1 FROM jimu_workflow.observation_request_media_reclaims retired
                            WHERE retired.file_key = req.file_key
                        )
                    UNION ALL
                    SELECT m.file_key FROM jimu_workflow.marked_frame_media m
                    WHERE m.file_key = :key AND (
                        NOT EXISTS (
                            SELECT 1 FROM jimu_workflow.attempt_results a
                            WHERE (a.project_id, a.client_id, a.execution_id,
                                   a.node_run_id, a.attempt_id)
                                = (m.project_id, m.client_id, m.execution_id,
                                   m.node_run_id, m.attempt_id)
                        ) OR EXISTS (
                            SELECT 1 FROM jimu_workflow.produced_outputs p
                            WHERE (p.project_id, p.client_id, p.execution_id,
                                   p.node_run_id, p.attempt_id, p.output_key)
                                = (m.project_id, m.client_id, m.execution_id,
                                   m.node_run_id, m.attempt_id, 'object.' || m.object_id)
                                AND p.output_kind = 'object' AND p.output_digest = m.object_sha256
                        )
                    )
                )
            """),
                {"key": key},
            )
            return not bound and await asyncio.to_thread(
                self._bytes.remove_orphan, key, minimum_age
            )

    async def stage_model_response(
        self,
        scope: IdempotencyScope,
        execution_id: str,
        dispatch_id: str,
        receipt: TransportResult,
    ) -> None:
        """Stage a trusted HTTP observation; historical reads also require its journal receipt."""
        binding = _DispatchKey(scope, execution_id, dispatch_id)
        dispatch = await self._require_dispatch(binding)
        if not isinstance(
            dispatch.intent.target, (ModelDispatchTarget, NestedModelDispatchTarget)
        ):
            raise MediaError("media_dispatch_kind_mismatch")
        raw, normalized = receipt.response_bytes, receipt.parsed_content
        if raw is None and normalized is None:
            return
        if receipt.response_dispatch_id != dispatch_id:
            raise MediaError("model_response_dispatch_mismatch")
        if raw is not None and dispatch.intent.retain_complete_response is not True:
            raise MediaError("model_response_retention_mismatch")
        if raw is not None and receipt.response_bytes_omission_reason is not None:
            raise MediaError("model_response_retention_mismatch")
        if (
            normalized is not None
            and raw is None
            and receipt.response_bytes_omission_reason != "policy_disabled"
        ):
            raise MediaError("model_response_retention_mismatch")
        digest, size = receipt.response_digest, receipt.observed_response_bytes
        if (
            not receipt.dispatched
            or receipt.response_status is None
            or digest is None
            or size is None
            or not 1 <= size <= self._limits.max_response_bytes
        ):
            raise MediaError("model_response_incomplete")

        for entry in dispatch.results:
            recorded = entry.result
            if (
                not isinstance(recorded, ModelDispatchResult)
                or recorded.response_digest is None
            ):
                continue
            if (
                recorded.response_digest,
                recorded.observed_response_bytes,
                recorded.response_status,
            ) != (digest, size, receipt.response_status):
                raise MediaError("model_response_wire_mismatch")
            if raw is not None and recorded.response_bytes_omission_reason is not None:
                raise MediaError("model_response_retention_mismatch")
            if (
                normalized is not None
                and recorded.response_bytes_omission_reason
                not in {None, "policy_disabled"}
            ):
                raise MediaError("model_response_retention_mismatch")
            if (
                normalized is not None
                and recorded.normalized_response_digest is not None
                and (
                    recorded.normalized_response_digest,
                    recorded.normalized_response_bytes,
                )
                != (
                    receipt.normalized_response_digest,
                    receipt.normalized_response_bytes,
                )
            ):
                raise MediaError("model_response_normalized_mismatch")
        roles: list[tuple[_Role, bytes, _Blob]] = []
        if raw is not None:
            _check_size(raw, self._limits.max_response_bytes)
            if len(raw) != size or sha256(raw).hexdigest() != digest:
                raise MediaError("model_response_wire_mismatch")
            roles.append(
                ("model_raw", raw, _Blob(self._store_id, digest, size, digest, size))
            )
        if normalized is not None:
            _check_size(normalized, self._limits.max_response_bytes)
            normalized_digest = sha256(normalized).hexdigest()
            if (normalized_digest, len(normalized)) != (
                receipt.normalized_response_digest,
                receipt.normalized_response_bytes,
            ):
                raise MediaError("model_response_normalized_mismatch")
            if (
                receipt.error is not None
                or not 200 <= receipt.response_status <= 299
                or receipt.validated_response_contract_digest
                != dispatch.intent.response_contract_digest
            ):
                raise MediaError("model_response_contract_mismatch")
            await asyncio.to_thread(_check_json_object, normalized)
            if raw is not None:
                await asyncio.to_thread(_check_chat_content, raw, normalized)
            roles.append(
                (
                    "model_normalized",
                    normalized,
                    _Blob(
                        self._store_id,
                        normalized_digest,
                        len(normalized),
                        digest,
                        size,
                    ),
                )
            )
        await _durable(self._stage_model_blobs(binding, roles))

    async def _stage_model_blobs(
        self, binding: _DispatchKey, roles: list[tuple[_Role, bytes, _Blob]]
    ) -> None:
        reclaim = tuple(
            partial(self._cleanup_dispatch, binding, role) for role, _, _ in roles
        )
        keys = tuple(binding.key(role) for role, _, _ in roles)
        async with self._publication(keys, *reclaim) as connection:
            for role, content, blob in roles:
                await self._stage_blob(connection, binding, role, content, blob)

    async def read_model_response(
        self,
        scope: IdempotencyScope,
        execution_id: str,
        dispatch_id: str,
        *,
        late: bool,
    ) -> StoredModelResponse:
        if type(late) is not bool:
            raise MediaError("model_response_receipt_invalid")
        binding = _DispatchKey(scope, execution_id, dispatch_id)
        # Undispatched/rejected and interrupted results may legitimately have
        # journal diagnostics but no bytes.
        dispatch = await self._journal.get_dispatch(scope, execution_id, dispatch_id)
        if dispatch is None:
            raise MediaError("media_dispatch_unavailable")
        receipt = next(
            (entry.result for entry in dispatch.results if entry.late is late), None
        )
        if not isinstance(receipt, ModelDispatchResult):
            raise MediaError("model_response_receipt_unavailable")
        raw: bytes | None = None
        normalized: bytes | None = None
        if receipt.response_evidence_error is not None:
            return StoredModelResponse(receipt, late, raw, normalized)
        if (
            dispatch.send_admitted
            and receipt.acknowledgement == "http_headers"
            and receipt.response_digest is not None
        ):
            async with self._engine.connect() as connection:
                if (
                    receipt.response_bytes_omission_reason is None
                    and receipt.observed_response_bytes > 0
                ):
                    if dispatch.intent.retain_complete_response is not True:
                        raise MediaError("model_response_raw_policy_unverified")
                    raw = await self._read_model_role(
                        connection, binding, "model_raw", receipt
                    )
                if (
                    receipt.normalized_response_digest is not None
                    and receipt.error is None
                    and receipt.response_status is not None
                    and 200 <= receipt.response_status <= 299
                    and receipt.response_bytes_omission_reason
                    in {None, "policy_disabled"}
                ):
                    normalized = await self._read_model_role(
                        connection, binding, "model_normalized", receipt
                    )
        if raw is not None and normalized is not None:
            await asyncio.to_thread(_check_chat_content, raw, normalized)
        return StoredModelResponse(receipt, late, raw, normalized)

    async def _read_model_role(
        self,
        connection: AsyncConnection,
        binding: _DispatchKey,
        role: Literal["model_raw", "model_normalized"],
        receipt: ModelDispatchResult,
        *,
        max_bytes: int | None = None,
    ) -> bytes:
        row = await self._get_blob(connection, binding, role)
        if row is None:
            code = (
                "model_response_raw_unavailable"
                if role == "model_raw"
                else "model_response_normalized_unavailable"
            )
            raise MediaError(code)
        blob, _ = row
        if (blob.wire_digest, blob.wire_size) != (
            receipt.response_digest,
            receipt.observed_response_bytes,
        ):
            raise MediaError("model_response_wire_mismatch")
        if role == "model_raw":
            if (blob.digest, blob.size) != (
                receipt.response_digest,
                receipt.observed_response_bytes,
            ):
                raise MediaError("model_response_wire_mismatch")
        elif (blob.digest, blob.size) != (
            receipt.normalized_response_digest,
            receipt.normalized_response_bytes,
        ):
            raise MediaError("model_response_normalized_mismatch")
        limit = self._limits.max_response_bytes
        if max_bytes is not None:
            limit = min(limit, max_bytes)
        return await self._read_blob(binding, role, blob, limit)

    async def _tool_context(
        self, binding: _DispatchKey, slot: ToolSidecarSlot
    ) -> _ToolContext:
        dispatch = await self._require_dispatch(binding)
        target = dispatch.intent.target
        if (
            not isinstance(target, ToolDispatchTarget)
            or target.object_ref is None
            or not isinstance(target.invocation_origin, ToolPlanStepOrigin)
            or target.invocation_origin.step_id != _TOOL_STEPS[slot]
        ):
            raise MediaError("tool_sidecar_slot_mismatch")
        async with self._engine.connect() as connection:
            row = (
                await connection.execute(
                    text("""
                        SELECT t.record::text, e.task_id
                        FROM jimu_workflow.executions AS e
                        JOIN jimu_workflow.tasks AS t USING (project_id, client_id, task_id)
                        WHERE (e.project_id, e.client_id, e.execution_id)
                            = (:project, :client, :execution)
                    """),
                    binding.params(),
                )
            ).one_or_none()
        if row is None:
            raise MediaError("tool_sidecar_task_unavailable")
        task = AcceptedTask.model_validate_json(row[0])
        if task.task_id != row.task_id:
            raise MediaError("tool_sidecar_task_mismatch")
        original = await self.verified_task_original(binding.scope, task)
        return _ToolContext(binding, dispatch, target, task, original)

    async def stage_tool_sidecar(
        self,
        scope: IdempotencyScope,
        execution_id: str,
        dispatch_id: str,
        slot: ToolSidecarSlot,
        spec: ToolSidecarSpec,
        content: bytes,
    ) -> EvidenceAsset:
        """Verify and stage one named CPU sidecar before ToolWorkerResponse is returned."""
        TOOL_SLOT.validate_python(slot)
        binding = _DispatchKey(scope, execution_id, dispatch_id)
        _check_size(content, self._limits.max_sidecar_bytes)
        context = await self._tool_context(binding, slot)
        blob = _Blob(
            self._store_id,
            sha256(content).hexdigest(),
            len(content),
            evidence_id=RECORD_ID.validate_python(spec.evidence_id),
        )
        draft = self._tool_asset(context, slot, spec, blob, datetime.now(UTC))
        await self._verify_sidecar(context, slot, draft, content)
        if slot == "geometry":
            if spec.mask_record is not None:
                raise MediaError("tool_sidecar_slot_mismatch")
            if spec.geometry_digest != blob.digest:
                raise MediaError("geometry_sidecar_digest_mismatch")
        elif spec.geometry_digest is not None:
            raise MediaError("tool_sidecar_slot_mismatch")
        elif spec.mask_record is None:
            raise MediaError("mask_sidecar_record_required")
        else:
            spec.mask_record.validate_evidence(draft)
        blob = replace(blob, evidence_claim_digest=_evidence_claim_digest(draft))
        return await _durable(self._publish_sidecar(context, slot, spec, blob, content))

    def _tool_asset(
        self,
        context: _ToolContext,
        slot: ToolSidecarSlot,
        spec: ToolSidecarSpec,
        blob: _Blob,
        at: datetime,
    ) -> EvidenceAsset:
        target, binding = context.target, context.binding
        assert target.object_ref is not None
        project = binding.scope.project_id
        access: ProjectEvidenceAccess | TaskEvidenceAccess
        if spec.access == "project":
            access = ProjectEvidenceAccess(scope="project", project_id=project)
        elif spec.access == "task":
            access = TaskEvidenceAccess(
                scope="task", project_id=project, task_id=context.task.task_id
            )
        else:
            raise MediaError("tool_sidecar_access_mismatch")
        return EvidenceAsset(
            schema_version="1.0.0",
            evidence_id=spec.evidence_id,
            evidence_type=EvidenceType.GEOMETRY
            if slot == "geometry"
            else EvidenceType.MASK,
            scope=ObjectEvidenceScope(
                scope="object",
                execution_id=binding.execution_id,
                object_ref=target.object_ref,
                profile_ref=target.profile_ref,
            ),
            storage=self._storage(binding.key(slot)),
            sha256=blob.digest,
            mime_type="application/json" if slot == "geometry" else "image/png",
            byte_size=blob.size,
            stage=self._finalized(blob.digest, at),
            spatial=spec.spatial,
            producer_id=target.tool_ref.artifact_id,
            producer_version=target.implementation_version,
            access_scope=access,
            retention=spec.retention,
            created_at=at,
        )

    async def _publish_sidecar(
        self,
        context: _ToolContext,
        slot: ToolSidecarSlot,
        spec: ToolSidecarSpec,
        blob: _Blob,
        content: bytes,
    ) -> EvidenceAsset:
        reclaim = partial(self._cleanup_dispatch, context.binding, slot)
        binding = context.binding
        evidence_id = spec.evidence_id
        identity = _key(
            "tool-evidence",
            binding.scope.project_id,
            binding.scope.client_id,
            binding.execution_id,
            evidence_id,
        )
        keys = (identity, binding.key(slot))
        async with self._publication(keys, reclaim) as connection:
            await _locked(connection, identity)
            await self._require_recoverable_evidence_id(
                connection, context, slot, evidence_id
            )
            staged_at = await self._stage_blob(
                connection, context.binding, slot, content, blob
            )
        return self._tool_asset(context, slot, spec, blob, staged_at)

    @staticmethod
    async def _require_recoverable_evidence_id(
        connection: AsyncConnection,
        context: _ToolContext,
        slot: ToolSidecarSlot,
        evidence_id: str,
    ) -> None:
        # Allow a later Attempt of the same operation to restage a private sidecar.
        # The abandoned binding stays immutable and is never treated as output.
        blocked = await connection.scalar(
            text("""
                SELECT EXISTS (
                    SELECT 1 FROM jimu_workflow.dispatch_media AS m
                    JOIN jimu_workflow.dispatch_intents AS d
                        USING (project_id, client_id, execution_id, dispatch_id)
                    LEFT JOIN jimu_workflow.attempt_results AS a
                        USING (project_id, client_id, execution_id, node_run_id, attempt_id)
                    WHERE (m.project_id, m.client_id, m.execution_id, m.evidence_id)
                        = (:project, :client, :execution, :evidence_id)
                      AND m.dispatch_id <> :dispatch
                      AND (
                          m.role <> :role OR d.operation_id <> :operation
                          OR (d.record ->> 'input_digest') IS DISTINCT FROM :input_digest
                          OR a.status IS NULL OR a.status = 'succeeded'
                          OR EXISTS (
                              SELECT 1 FROM jimu_workflow.dispatch_results AS r
                              WHERE (r.project_id, r.client_id, r.execution_id, r.dispatch_id)
                                  = (m.project_id, m.client_id, m.execution_id, m.dispatch_id)
                          )
                          OR EXISTS (
                              SELECT 1 FROM jimu_workflow.produced_outputs AS p
                              WHERE (p.project_id, p.client_id, p.execution_id, p.node_run_id,
                                     p.attempt_id, p.output_key)
                                  = (m.project_id, m.client_id, m.execution_id, d.node_run_id,
                                     d.attempt_id, 'evidence.' || m.evidence_id)
                          )
                      )
                )
            """),
            {
                **context.binding.params(),
                "role": slot,
                "operation": context.dispatch.intent.operation_id,
                "input_digest": context.dispatch.intent.input_digest,
                "evidence_id": evidence_id,
            },
        )
        if blocked:
            raise MediaError("dispatch_media_evidence_in_use")

    async def _verify_sidecar(
        self,
        context: _ToolContext,
        slot: ToolSidecarSlot,
        evidence: EvidenceAsset,
        content: bytes,
    ) -> dict[str, object] | None:
        evidence.validate_original_media(context.original.asset)
        spatial = evidence.spatial
        if (
            spatial.kind != "derived_image"
            or spatial.transforms[0].source != context.original.coordinate_system
        ):
            raise MediaError("tool_sidecar_original_frame_mismatch")
        if slot == "geometry":
            return await asyncio.to_thread(_geometry_document, context, content)
        if (
            len(spatial.transforms) != 1
            or spatial.transforms[0].affine != (1, 0, 0, 0, 1, 0)
            or spatial.coordinate_system.dimensions != context.original.asset.dimensions
        ):
            raise MediaError("mask_sidecar_original_grid_required")
        await self._decode(
            content,
            "image/png",
            spatial.coordinate_system.dimensions,
            encoding="binary_mask",
        )
        return None

    async def read_tool_sidecar(
        self,
        scope: IdempotencyScope,
        execution_id: str,
        dispatch_id: str,
        slot: ToolSidecarSlot,
        *,
        expected_asset: EvidenceAsset | None = None,
    ) -> VerifiedEvidence:
        """Resolve a sidecar through its exact Tool receipt and produced Attempt output.

        A downstream Tool can pass the EvidenceAsset from the bound ancestor
        result to require the exact Object, Profile, and producing payload.
        """
        result, _ = await self._read_tool_sidecar(
            scope,
            execution_id,
            dispatch_id,
            slot,
            expected_asset=expected_asset,
            allow_diagnostic_mask=False,
        )
        return result

    async def read_tool_sidecar_audit(
        self,
        scope: IdempotencyScope,
        execution_id: str,
        dispatch_id: str,
        slot: MaskSidecarSlot,
    ) -> AuditedMaskSidecar:
        """Read verified physical mask evidence and its historical Fact status."""
        MASK_SLOT.validate_python(slot)
        result, fact_status = await self._read_tool_sidecar(
            scope,
            execution_id,
            dispatch_id,
            slot,
            expected_asset=None,
            allow_diagnostic_mask=True,
        )
        assert fact_status is not None
        return AuditedMaskSidecar(result.asset, fact_status, result.content)

    async def _read_tool_sidecar(
        self,
        scope: IdempotencyScope,
        execution_id: str,
        dispatch_id: str,
        slot: ToolSidecarSlot,
        *,
        expected_asset: EvidenceAsset | None,
        allow_diagnostic_mask: bool,
    ) -> tuple[VerifiedEvidence, FactStatus | None]:
        TOOL_SLOT.validate_python(slot)
        binding = _DispatchKey(scope, execution_id, dispatch_id)
        context = await self._tool_context(binding, slot)
        async with self._engine.connect() as connection:
            stored = await self._get_blob(connection, binding, slot)
            if stored is None or stored[0].evidence_id is None:
                raise MediaError("tool_sidecar_unavailable")
            blob, staged_at = stored
            evidence_id = blob.evidence_id
            assert evidence_id is not None
            output_key = f"evidence.{evidence_id}"
            evidence_kind = await connection.scalar(
                text("""
                    SELECT output_kind FROM jimu_workflow.produced_outputs
                    WHERE (project_id, client_id, execution_id, node_run_id, attempt_id, output_key)
                        = (:project, :client, :execution, :node, :attempt, :output_key)
                """),
                {
                    **binding.params(),
                    "node": context.dispatch.node_run_id,
                    "attempt": context.dispatch.attempt_id,
                    "output_key": output_key,
                },
            )
            if evidence_kind != "evidence":
                raise MediaError("tool_sidecar_output_unavailable")
            asset = await read_output(
                connection,
                scope,
                execution_id,
                context.dispatch.node_run_id,
                context.dispatch.attempt_id,
                output_key,
                EvidenceAsset,
            )
        receipt = next(
            (row.result for row in context.dispatch.results if not row.late), None
        )
        if asset is None or not isinstance(receipt, ToolDispatchResult):
            raise MediaError("tool_sidecar_output_unavailable")
        if expected_asset is not None and asset != expected_asset:
            raise MediaError("tool_sidecar_bound_asset_mismatch")
        execution = receipt.execution
        observed = receipt.implementation
        if (
            receipt.acknowledgement != "tool_process"
            or observed is None
            or observed.implementation_version != context.target.implementation_version
            or observed.proof_ref != context.target.implementation_proof_ref
            or execution is None
            or execution.tool_execution_id != dispatch_id
            or execution.execution_id != execution_id
            or execution.attempt_ref.record_id != context.dispatch.attempt_id
            or execution.node_run_ref.record_id != context.dispatch.node_run_id
            or execution.object_ref != context.target.object_ref
            or execution.profile_ref != context.target.profile_ref
            or execution.tool_ref != context.target.tool_ref
            or execution.invocation_origin != context.target.invocation_origin
            or execution.actual_implementation_version
            != context.target.implementation_version
            or not isinstance(execution.attempt_result, ReturnedToolAttempt)
        ):
            raise MediaError("tool_sidecar_output_mismatch")
        result = execution.attempt_result.result
        ref = EvidenceRef(record_kind=RuntimeRecordKind.EVIDENCE, record_id=evidence_id)
        named = next((item for item in result.evidence if item.name == slot), None)
        if (
            named is None
            or ref not in named.evidence_refs
            or ref not in execution.evidence_refs
        ):
            raise MediaError("tool_sidecar_output_mismatch")
        fact_status = None
        if slot == "geometry":
            if result.status not in {"succeeded", "partial"}:
                raise MediaError("tool_sidecar_output_mismatch")
        else:
            if named.evidence_refs != (ref,):
                raise MediaError("tool_sidecar_output_mismatch")
            fact_status = _mask_output_status(
                result, slot, ref, asset, allow_diagnostic_mask=allow_diagnostic_mask
            )
        if asset.spatial.kind != "derived_image":
            raise MediaError("tool_sidecar_original_frame_mismatch")
        expected = self._tool_asset(
            context,
            slot,
            ToolSidecarSpec(
                evidence_id, asset.spatial, asset.retention, asset.access_scope.scope
            ),
            blob,
            staged_at,
        )
        if asset != expected or blob.evidence_claim_digest != _evidence_claim_digest(
            asset
        ):
            raise MediaError("tool_sidecar_output_mismatch")
        content = await self._read_blob(
            binding, slot, blob, self._limits.max_sidecar_bytes
        )
        document = await self._verify_sidecar(context, slot, asset, content)
        if document is not None:
            if (
                document["response_sha256"] != receipt.response_digest
                or receipt.observed_response_bytes <= 0
                or named.evidence_refs != (ref,)
                or not any(
                    item.artifact_ref.record_kind is RuntimeRecordKind.EVIDENCE
                    and item.artifact_ref.record_id == evidence_id
                    and item.digest == asset.sha256
                    for item in execution.output_refs
                )
            ):
                raise MediaError("geometry_sidecar_output_mismatch")
            _verify_geometry_result(document, result, ref)
        return VerifiedEvidence(asset, content), fact_status


_GEOMETRY_FIELDS = {
    "schema_version",
    "tool_execution_id",
    "execution_id",
    "attempt_id",
    "object_ref",
    "profile_ref",
    "request_sha256",
    "response_sha256",
    "implementation",
    "parking_area",
    "original_sha256",
    "inputs",
    "metrics",
    "relation",
    "reason",
}
_GEOMETRY_MASKS = {"wheel_mask", "tactile_paving_mask"}
_GEOMETRY_METRICS = {
    "wheel",
    "tactile",
    "overlap",
    "inside",
    "outside",
    "crossing",
    "touching",
}


def _geometry_document(context: _ToolContext, content: bytes) -> dict[str, object]:
    document = _check_json_object(content)
    dispatch, target = context.dispatch, context.target
    assert target.object_ref is not None
    exact = {
        "schema_version": "1.0.0",
        "tool_execution_id": dispatch.intent.dispatch_id,
        "execution_id": context.binding.execution_id,
        "attempt_id": dispatch.attempt_id,
        "object_ref": target.object_ref.model_dump(mode="json"),
        "profile_ref": target.profile_ref.model_dump(mode="json"),
        "request_sha256": dispatch.intent.request_digest,
        "implementation": target.implementation_proof_ref.model_dump(mode="json"),
        "original_sha256": context.original.asset.media_ref.sha256,
    }
    if set(document) != _GEOMETRY_FIELDS or any(
        document[key] != value for key, value in exact.items()
    ):
        raise MediaError("geometry_sidecar_dispatch_mismatch")
    raw_digest, inputs = document["response_sha256"], document["inputs"]
    metrics, parking_area = document["metrics"], document["parking_area"]
    dimensions = context.original.asset.dimensions
    pixels = dimensions.width * dimensions.height
    if (
        not isinstance(raw_digest, str)
        or not fullmatch(r"[0-9a-f]{64}", raw_digest)
        or not isinstance(parking_area, dict)
        or not isinstance(metrics, dict)
        or set(metrics) != _GEOMETRY_METRICS
        or any(
            type(value) is not int or not 0 <= value <= pixels
            for value in metrics.values()
        )
        or not isinstance(inputs, dict)
        or set(inputs) != _GEOMETRY_MASKS
    ):
        raise MediaError("geometry_sidecar_document_invalid")
    if metrics["overlap"] > min(metrics["wheel"], metrics["tactile"]) or metrics[
        "wheel"
    ] != sum(metrics[key] for key in ("inside", "outside", "crossing", "touching")):
        raise MediaError("geometry_sidecar_document_invalid")
    allowed_inputs = {
        (item.artifact_ref.record_id, item.digest)
        for item in target.input_refs or ()
        if item.artifact_ref.record_kind is RuntimeRecordKind.EVIDENCE
    }
    seen = set()
    for source in inputs.values():
        if (
            not isinstance(source, dict)
            or set(source) != {"evidence_id", "sha256"}
            or not isinstance(evidence_id := source["evidence_id"], str)
            or not isinstance(digest := source["sha256"], str)
            or (evidence_id, digest) not in allowed_inputs
            or evidence_id in seen
        ):
            raise MediaError("geometry_sidecar_input_mismatch")
        seen.add(evidence_id)
    relation, reason = document["relation"], document["reason"]
    if not (
        (isinstance(relation, str) and reason is None)
        or (relation is None and isinstance(reason, str) and bool(reason))
    ):
        raise MediaError("geometry_sidecar_document_invalid")
    return document


def _verify_geometry_result(
    document: dict[str, object], result: ToolResult, ref: EvidenceRef
) -> None:
    output_name = "wheel_boundary_relation"
    state = next(
        (item for item in result.output_states if item.output_name == output_name), None
    )
    payload = None if isinstance(result, FailedToolResult) else result.payload
    value = payload.value.get(output_name) if isinstance(payload, RecordValue) else None
    if (
        state is None
        or ref not in state.evidence_refs
        or (
            isinstance(state, KnownToolOutputState)
            and (
                not isinstance(value, EnumValue) or value.value != document["relation"]
            )
        )
        or (
            isinstance(state, NonKnownToolOutputState)
            and (
                value is not None
                or document["relation"] is not None
                or ref not in state.reason.diagnostic_refs
            )
        )
    ):
        raise MediaError("geometry_sidecar_output_mismatch")
    sources = document["inputs"]
    assert isinstance(sources, dict)
    named = {item.name: item.evidence_refs for item in result.evidence}
    for name, item in sources.items():
        assert isinstance(item, dict)
        refs = named.get(name, ())
        if len(refs) != 1 or refs[0].record_id != item["evidence_id"]:
            raise MediaError("geometry_sidecar_input_mismatch")


def _mask_output_status(
    result: ToolResult,
    slot: MaskSidecarSlot,
    ref: EvidenceRef,
    asset: EvidenceAsset,
    *,
    allow_diagnostic_mask: bool,
) -> FactStatus:
    output = next(
        (item for item in result.output_states if item.output_name == slot), None
    )
    if output is None or ref not in output.evidence_refs:
        raise MediaError("mask_sidecar_output_state_mismatch")
    payload = None if isinstance(result, FailedToolResult) else result.payload
    if isinstance(output, KnownToolOutputState):
        if not isinstance(payload, RecordValue):
            raise MediaError("mask_sidecar_record_required")
        MaskSidecarRecord.from_typed_output(payload.value.get(slot)).validate_evidence(
            asset
        )
    elif isinstance(output, NonKnownToolOutputState):
        if not allow_diagnostic_mask:
            raise MediaError("mask_sidecar_output_not_known")
        if ref not in output.reason.diagnostic_refs or (
            payload is not None
            and (not isinstance(payload, RecordValue) or slot in payload.value)
        ):
            raise MediaError("mask_sidecar_diagnostic_mismatch")
    return output.fact_status


def _evidence_claim_digest(asset: EvidenceAsset) -> str:
    # Digest the immutable claim excluding publication timestamps. The owning
    # Attempt's produced_outputs retains the sole canonical EvidenceAsset.
    claim = asset.model_dump(mode="json", exclude={"created_at", "stage"})
    return sha256(canonical_bytes(claim)).hexdigest()


def _check_chat_content(raw: bytes, normalized: bytes) -> None:
    try:
        envelope = strict_json(raw)
    except WireError:
        raise MediaError("model_response_wire_mismatch") from None
    if isinstance(envelope, dict):
        choices = envelope.get("choices")
        if (
            isinstance(choices, list)
            and len(choices) == 1
            and isinstance(choices[0], dict)
        ):
            message = choices[0].get("message")
            if isinstance(message, dict):
                content = message.get("content")
                if isinstance(content, str) and content.encode("utf-8") == normalized:
                    return
    raise MediaError("model_response_normalized_mismatch")
