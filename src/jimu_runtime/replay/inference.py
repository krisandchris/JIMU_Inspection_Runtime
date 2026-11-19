"""Authorize and atomically enqueue a new Execution from immutable source pins."""

from asyncio import to_thread
from typing import Protocol
from uuid import uuid4

from pydantic import TypeAdapter
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from jimu_runtime.bundle.archive import BundleLimits
from jimu_runtime.bundle.integrity import sha256_digest
from jimu_runtime.bundle.releases import ReleaseManager
from jimu_runtime.bundle.store import LocalBundleStore
from jimu_runtime.domain.definition_support import ShortText
from jimu_runtime.domain.enums import ExecutionStatus, RuntimeRecordKind
from jimu_runtime.domain.runtime.intake import AcceptedTask, IdempotencyScope
from jimu_runtime.domain.runtime.media import MediaAsset, MediaRef
from jimu_runtime.domain.runtime.replay import (
    ExecutionRef,
    InferenceReplayRequest,
    InferenceReplayRun,
    ReplayKind,
)
from jimu_runtime.replay.inference_repository import (
    StoredInferenceReplay,
    insert_inference_replay,
    read_inference_replay_by_key,
    read_inference_replay_for_target,
)
from jimu_runtime.workflow import repository
from jimu_runtime.workflow.intake import (
    IntakeAuthorizationError,
    IntakeIdentity,
    _database_now,
    initial_records,
)
from jimu_runtime.workflow.mock_support import record_digest
from jimu_runtime.workflow.pins import resolve_execution_pins
from jimu_runtime.workflow.worker_capabilities import read_historical_capabilities
from jimu_runtime.workflow.worker_state import (
    read_context_snapshot,
    read_replay_capture_snapshot,
)

_KEY = TypeAdapter[str](ShortText)
_ACTIVE = frozenset((ExecutionStatus.INITIALIZED, ExecutionStatus.RUNNING))


class InferenceReplayError(RuntimeError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class InferenceReplayHost(Protocol):
    async def authorize_inference_replay(
        self,
        principal: object,
        scope: IdempotencyScope,
        request: InferenceReplayRequest,
        source_task: AcceptedTask,
    ) -> IntakeIdentity:
        """Authorize the exact source or raise IntakeAuthorizationError."""

    async def verified_original(
        self, scope: IdempotencyScope, media_ref: MediaRef
    ) -> MediaAsset:
        """Authorize the source scope's original media and verify its bytes."""


async def _find_accepted(
    connection: AsyncConnection,
    scope: IdempotencyScope,
    key: bytes,
    request: InferenceReplayRequest,
    request_digest: str,
    identity: IntakeIdentity,
) -> InferenceReplayRun | None:
    saved = await read_inference_replay_by_key(connection, scope, key)
    if saved is None:
        return None
    if (
        saved.identity != identity
        or saved.request_digest != request_digest
        or saved.run.request != request
    ):
        raise InferenceReplayError("inference_replay_key_conflict")
    target = await repository.get_execution(
        connection, scope, saved.run.target_execution_ref.record_id
    )
    if target is None or not target.is_inference_replay:
        raise InferenceReplayError("inference_replay_target_missing")
    await read_replay_capture_snapshot(connection, scope, target, saved)
    return saved.run


class InferenceReplayService:
    def __init__(
        self,
        engine: AsyncEngine,
        release_manager: ReleaseManager,
        bundle_store: LocalBundleStore,
        *,
        host: InferenceReplayHost,
        bundle_limits: BundleLimits,
    ) -> None:
        self._engine = engine
        self._releases = release_manager
        self._store = bundle_store
        self._host = host
        self._limits = bundle_limits

    async def submit(
        self,
        scope: IdempotencyScope,
        request: InferenceReplayRequest,
        *,
        idempotency_key: str,
        principal: object,
    ) -> InferenceReplayRun:
        scope = IdempotencyScope.model_validate(scope)
        request = InferenceReplayRequest.model_validate(request)
        key = _KEY.validate_python(idempotency_key)
        try:
            encoded_key = key.encode("utf-8")
        except UnicodeEncodeError:
            raise InferenceReplayError("inference_replay_key_invalid") from None
        request_digest = sha256_digest(request.stable_json().encode("utf-8"))
        source_id = request.source.execution_ref.record_id
        async with self._engine.connect() as connection:
            source = await repository.get_execution(connection, scope, source_id)
        if source is None:
            raise IntakeAuthorizationError()
        try:
            identity = IntakeIdentity.model_validate(
                await self._host.authorize_inference_replay(
                    principal, scope, request, source.task
                )
            )
        except IntakeAuthorizationError:
            raise IntakeAuthorizationError() from None
        try:
            request.source.validate_execution(source.execution)
        except ValueError:
            raise InferenceReplayError("inference_replay_source_pin_mismatch") from None
        if request.target_pins != source.execution.pins:
            raise InferenceReplayError("inference_replay_target_pins_unsupported")

        async with self._engine.connect() as connection:
            existing = await _find_accepted(
                connection, scope, encoded_key, request, request_digest, identity
            )
            if existing is not None:
                return existing
            snapshot = await read_context_snapshot(connection, scope, source)
            capture_execution_id = source_id
            if source.is_inference_replay:
                replay = await read_inference_replay_for_target(
                    connection, scope, source_id
                )
                if replay is None:
                    raise InferenceReplayError(
                        "inference_replay_source_lineage_missing"
                    )
                replay.run.validate_target_execution(source.execution)
                if snapshot is None:
                    snapshot = await read_replay_capture_snapshot(
                        connection, scope, source, replay
                    )
                    capture_execution_id = replay.capture_execution_id
            historical = await read_historical_capabilities(
                connection, scope, source.execution
            )
        if snapshot is None:
            code = (
                "inference_replay_source_context_pending"
                if source.execution.status in _ACTIVE
                else "inference_replay_source_context_unavailable"
            )
            raise InferenceReplayError(code)
        if historical is None:
            raise InferenceReplayError(
                "inference_replay_historical_capabilities_missing"
            )
        source.task.validate_media(
            await self._host.verified_original(scope, source.task.media_ref)
        )
        resolved = await to_thread(
            resolve_execution_pins,
            self._releases,
            self._store,
            source.task,
            scope=scope,
            host=historical.snapshot,
            validation_at=historical.validation_at,
            limits=self._limits,
        )
        if (resolved.pins, resolved.policy_ref) != (
            source.execution.pins,
            source.execution_policy_ref,
        ):
            raise InferenceReplayError("inference_replay_reopened_pin_mismatch")

        async with self._engine.connect() as connection:
            # Apply isolation after checkout. The statement after a contended
            # advisory lock needs a fresh snapshot of the winner's receipt.
            await connection.execution_options(isolation_level="READ COMMITTED")
            async with connection.begin():
                # Serialize writers for this exact request key. A 64-bit hash collision
                # can only delay unrelated keys, which are matched by their full bytes.
                lock_digest = sha256_digest(
                    scope.stable_json().encode("utf-8") + b"\x00" + encoded_key
                )
                lock_key = int.from_bytes(
                    bytes.fromhex(lock_digest[:16]), "big", signed=True
                )
                await connection.execute(
                    text(
                        "SELECT pg_catalog.pg_advisory_xact_lock(CAST(:key AS bigint))"
                    ),
                    {"key": lock_key},
                )
                existing = await _find_accepted(
                    connection, scope, encoded_key, request, request_digest, identity
                )
                if existing is not None:
                    return existing
                now = await _database_now(connection)
                suffix = f"replay.{uuid4().hex}"
                target, root = initial_records(
                    source.task, resolved, now, execution_suffix=suffix
                )
                run = InferenceReplayRun(
                    schema_version="1.0.0",
                    kind=ReplayKind.INFERENCE,
                    replay_run_id=suffix,
                    request=request,
                    target_execution_ref=ExecutionRef(
                        record_kind=RuntimeRecordKind.EXECUTION,
                        record_id=target.execution_id,
                    ),
                    recorded_at=now,
                )
                run.validate_target_execution(target)
                await repository.insert_initial_execution(
                    connection,
                    scope,
                    target,
                    resolved.policy_ref,
                    source.scheduling_limits,
                    root,
                    historical_capabilities=historical,
                )
                await insert_inference_replay(
                    connection,
                    scope,
                    encoded_key,
                    StoredInferenceReplay(
                        run,
                        request_digest,
                        identity,
                        capture_execution_id,
                        record_digest(snapshot),
                    ),
                )
                await repository.ready_root_job(
                    connection,
                    scope,
                    root,
                    repository.NewJob(
                        job_id=f"job.{root.node_run_id}",
                        ready_at=now,
                        budget=source.scheduling_limits.for_node(root.node_id),
                    ),
                )
                return run
