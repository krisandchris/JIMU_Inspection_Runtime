"""Durable Task intake across PostgreSQL and the WP-02 Release ledger."""

from asyncio import to_thread
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Annotated, Protocol, cast

from pydantic import StringConstraints, TypeAdapter, ValidationError
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from jimu_runtime.bundle.archive import BundleLimits
from jimu_runtime.bundle.capabilities import CapabilitySnapshot
from jimu_runtime.bundle.integrity import sha256_digest
from jimu_runtime.bundle.releases import ReleaseManager, ReleaseOperationError
from jimu_runtime.bundle.store import LocalBundleStore, StoreBusy, StoreError
from jimu_runtime.domain.base import ContractModel
from jimu_runtime.domain.enums import ExecutionStatus, NodeRunStatus
from jimu_runtime.domain.primitives import RecordId
from jimu_runtime.domain.runtime.executions import ExecutionRecord, NodeId, NodeRun
from jimu_runtime.domain.runtime.intake import (
    AcceptedTask,
    IdempotencyScope,
    TaskSubmission,
)
from jimu_runtime.domain.runtime.media import FinalizedAsset, MediaAsset, MediaRef
from jimu_runtime.gateway.routing import DeploymentOwnership
from jimu_runtime.workflow import repository
from jimu_runtime.workflow.pins import (
    ExecutionPinError,
    ResolvedExecutionPins,
    resolve_execution_pins,
)
from jimu_runtime.workflow.scheduling import SchedulingLimits
from jimu_runtime.workflow.worker_capabilities import (
    HistoricalCapabilities,
    ToolOwnershipPin,
)

type IdentityComponent = Annotated[
    str,
    StringConstraints(
        strict=True, min_length=1, max_length=1024, pattern=r"^[^\x00]+$"
    ),
]


_REQUEST_ID = TypeAdapter[str](RecordId)
_MAX_PENDING_PAGE_SIZE = 64

type HistoricalOwnershipSource = Callable[
    [IdempotencyScope, CapabilitySnapshot], Awaitable[tuple[DeploymentOwnership, ...]]
]
type HistoricalToolOwnershipSource = Callable[
    [IdempotencyScope, ResolvedExecutionPins], Awaitable[tuple[ToolOwnershipPin, ...]]
]


class IntakeIdentity(ContractModel):
    """The exact authenticated identity as recovered from the host registry."""

    issuer: IdentityComponent
    subject: IdentityComponent


class IntakeError(RuntimeError):
    """An intake failure with a machine-readable code."""

    def __init__(self, code: str, *, retryable: bool = False) -> None:
        self.code = code
        self.retryable = retryable
        super().__init__(code)


class IntakeConflict(IntakeError):
    """A scoped request key is already bound to a different intake."""


class IntakeAuthorizationError(IntakeError):
    """The trusted registry authoritatively denied access to this submission."""

    def __init__(self) -> None:
        super().__init__("intake_authorization_denied")


class IntakeHostUnavailable(IntakeError):
    """The host cannot currently authenticate, authorize or resolve trusted inputs."""

    def __init__(self) -> None:
        super().__init__("intake_host_unavailable", retryable=True)


class IntakeHost(Protocol):
    """Trusted host callbacks shared by interactive intake and recovery."""

    async def authorize_submission(
        self, principal: object, submission: TaskSubmission
    ) -> IntakeIdentity:
        """Authenticate and authorize the project/client and device/camera/region."""

    async def restore_principal(
        self, identity: IntakeIdentity, submission: TaskSubmission
    ) -> object:
        """Re-resolve identity in the host registry with its current permissions."""

    async def verified_original(
        self, scope: IdempotencyScope, media_ref: MediaRef
    ) -> MediaAsset:
        """Return the scope-owned original after its immutable bytes were verified."""

    async def capabilities(self, scope: IdempotencyScope) -> CapabilitySnapshot:
        """Resolve the current trusted runtime capabilities for first finalization."""


@dataclass(frozen=True, slots=True)
class IntakeResult:
    task: AcceptedTask
    execution: ExecutionRecord


class PendingIntakeCursor(ContractModel):
    scope: IdempotencyScope
    after_request_id: RecordId


@dataclass(frozen=True, slots=True)
class PendingIntakes:
    request_ids: tuple[str, ...]
    next_cursor: PendingIntakeCursor | None


@dataclass(frozen=True, slots=True)
class IntakeRecoveryFailure:
    request_id: str
    code: str


@dataclass(frozen=True, slots=True)
class PendingIntakeRecovery:
    results: tuple[IntakeResult | IntakeRecoveryFailure, ...]
    next_cursor: PendingIntakeCursor | None


def _check_accepted(task: AcceptedTask, submission: TaskSubmission) -> None:
    if (
        task.request_id,
        task.idempotency_scope,
        task.idempotency_key,
        task.media_ref,
        task.context,
        task.trace,
    ) != (
        submission.request_id,
        submission.idempotency_scope,
        submission.idempotency_key,
        submission.media_ref,
        submission.context,
        submission.trace,
    ) or submission.requested_release_id not in (None, task.release_id):
        raise IntakeError("intake_task_pin_mismatch")


def initial_records(
    task: AcceptedTask,
    resolved: ResolvedExecutionPins,
    created_at: datetime,
    *,
    execution_suffix: str | None = None,
) -> tuple[ExecutionRecord, NodeRun]:
    task_suffix = execution_suffix or sha256_digest(task.task_id.encode("utf-8"))
    execution = ExecutionRecord(
        schema_version="1.0.0",
        execution_id=f"execution.{task_suffix}",
        task_id=task.task_id,
        pins=resolved.pins,
        trace=task.trace,
        status=ExecutionStatus.INITIALIZED,
        created_at=created_at,
        model_invocation_refs=(),
    )
    root = NodeRun(
        schema_version="1.0.0",
        node_run_id=f"node.n01.{task_suffix}",
        execution_id=execution.execution_id,
        node_id=NodeId.N01,
        trace=task.trace,
        config_ref=resolved.policy_ref,
        upstream_inputs=(),
        input_digest=resolved.pins.input_digest,
        status=NodeRunStatus.PENDING,
        created_at=created_at,
        attempt_history=(),
        output_refs=(),
    )
    return execution, root


async def _database_now(connection: AsyncConnection) -> datetime:
    row = await connection.execute(text("SELECT pg_catalog.clock_timestamp()"))
    return cast(datetime, row.scalar_one()).astimezone(UTC)


class IntakeService:
    """Reserve intake before choosing a Release, then finalize in PostgreSQL."""

    def __init__(
        self,
        engine: AsyncEngine,
        release_manager: ReleaseManager,
        bundle_store: LocalBundleStore,
        *,
        host: IntakeHost,
        bundle_limits: BundleLimits,
        scheduling_limits: SchedulingLimits,
        max_submission_bytes: int,
        historical_ownership: HistoricalOwnershipSource | None = None,
        historical_tool_ownership: HistoricalToolOwnershipSource | None = None,
    ) -> None:
        if type(max_submission_bytes) is not int or max_submission_bytes <= 0:
            raise ValueError("max_submission_bytes must be a positive integer")
        self._engine = engine
        self._releases = release_manager
        self._store = bundle_store
        self._host = host
        self._bundle_limits = bundle_limits
        self._scheduling_limits = SchedulingLimits.model_validate(scheduling_limits)
        self._max_submission_bytes = max_submission_bytes
        self._historical_ownership = historical_ownership
        self._historical_tool_ownership = historical_tool_ownership

    async def submit(
        self, submission: TaskSubmission, *, principal: object
    ) -> IntakeResult:
        """Accept a new request or return the authorized original acceptance."""
        submission = TaskSubmission.model_validate(submission)
        try:
            payload_size = len(submission.stable_json().encode("utf-8"))
        except UnicodeEncodeError:
            raise IntakeError("invalid_intake_payload") from None
        if payload_size > self._max_submission_bytes:
            raise IntakeError("intake_submission_too_large")
        identity = await self._authorize(principal, submission)
        try:
            async with self._engine.begin() as connection:
                intent = await repository.reserve_intent(
                    connection,
                    submission.idempotency_scope,
                    submission,
                    issuer=identity.issuer,
                    subject=identity.subject,
                )
        except repository.IntentConflict as error:
            raise IntakeConflict(error.code) from None
        return await self._complete(intent, principal)

    async def resume(self, scope: IdempotencyScope, request_id: str) -> IntakeResult:
        """Recover a durable intent through the trusted host's reconciliation entry."""
        scope = IdempotencyScope.model_validate(scope)
        request_id = _REQUEST_ID.validate_python(request_id)
        async with self._engine.connect() as connection:
            intent = await repository.get_intent(connection, scope, request_id)
        if intent is None:
            raise IntakeError("intake_intent_not_found")

        identity = IntakeIdentity(issuer=intent.issuer, subject=intent.subject)
        try:
            principal = await self._host.restore_principal(identity, intent.submission)
            restored = await self._authorize(principal, intent.submission)
        except IntakeAuthorizationError as error:
            await self._record_failure(intent, error.code)
            raise
        if restored != identity:
            changed = IntakeError("intake_principal_changed")
            await self._record_failure(intent, changed.code)
            raise changed
        return await self._complete(intent, principal)

    async def list_pending(
        self,
        scope: IdempotencyScope,
        *,
        limit: int,
        cursor: PendingIntakeCursor | None = None,
    ) -> PendingIntakes:
        """List pending request IDs within one authorized host scope."""
        scope = IdempotencyScope.model_validate(scope)
        if type(limit) is not int or not 1 <= limit <= _MAX_PENDING_PAGE_SIZE:
            raise ValueError(f"limit must be between 1 and {_MAX_PENDING_PAGE_SIZE}")
        if cursor is not None:
            cursor = PendingIntakeCursor.model_validate(cursor)
            if cursor.scope != scope:
                raise IntakeError("intake_cursor_scope_mismatch")
        async with self._engine.connect() as connection:
            candidates = await repository.list_pending_request_ids(
                connection,
                scope,
                after_request_id=cursor.after_request_id
                if cursor is not None
                else None,
                limit=limit + 1,
            )
        page = candidates[:limit]
        next_cursor = (
            PendingIntakeCursor(scope=scope, after_request_id=page[-1])
            if len(candidates) > limit
            else None
        )
        return PendingIntakes(page, next_cursor)

    async def recover_pending(
        self,
        scope: IdempotencyScope,
        *,
        limit: int,
        cursor: PendingIntakeCursor | None = None,
    ) -> PendingIntakeRecovery:
        """Resume a host-scoped page, reauthorizing each stored identity.

        Resume with the returned cursor even if an item failed. A later scan
        starts at the beginning to revisit pending items and discover new rows.
        Concurrent scans may observe the same persisted successful result.
        """
        pending = await self.list_pending(scope, limit=limit, cursor=cursor)
        results: list[IntakeResult | IntakeRecoveryFailure] = []
        for request_id in pending.request_ids:
            try:
                outcome = await self.resume(scope, request_id)
            except (
                IntakeError,
                ExecutionPinError,
                repository.RepositoryError,
            ) as error:
                results.append(IntakeRecoveryFailure(request_id, error.code))
            except StoreBusy:
                results.append(
                    IntakeRecoveryFailure(request_id, "intake_release_store_busy")
                )
            except StoreError:
                results.append(
                    IntakeRecoveryFailure(request_id, "intake_release_store_error")
                )
            else:
                results.append(outcome)
        return PendingIntakeRecovery(tuple(results), pending.next_cursor)

    async def _authorize(
        self, principal: object, submission: TaskSubmission
    ) -> IntakeIdentity:
        return IntakeIdentity.model_validate(
            await self._host.authorize_submission(principal, submission)
        )

    async def _record_failure(self, intent: repository.IntakeIntent, code: str) -> None:
        if intent.status is repository.IntentStatus.PENDING:
            async with self._engine.begin() as connection:
                await repository.fail_intent(
                    connection,
                    intent.submission.idempotency_scope,
                    intent,
                    failure_code=code,
                )

    async def _verify_media(self, scope: IdempotencyScope, media_ref: MediaRef) -> None:
        original = await self._host.verified_original(scope, media_ref)
        try:
            asset = MediaAsset.model_validate(original)
        except ValidationError:
            raise IntakeError("intake_original_mismatch") from None
        if asset.media_ref != media_ref or not isinstance(asset.stage, FinalizedAsset):
            raise IntakeError("intake_original_mismatch")

    async def _original_result(
        self, intent: repository.IntakeIntent, task: AcceptedTask
    ) -> IntakeResult | None:
        submission = intent.submission
        scope = submission.idempotency_scope
        async with self._engine.connect() as connection:
            current = await repository.get_intent(
                connection, scope, submission.request_id
            )
            if current is None:
                raise IntakeError("intake_intent_not_found")
            if current.status is repository.IntentStatus.FAILED:
                raise IntakeError(current.failure_code or "intake_intent_failed")
            if current.status is repository.IntentStatus.PENDING:
                return None
            if current.task_id != task.task_id or current.execution_id is None:
                raise IntakeError("intake_result_mismatch")
            result = await repository.get_execution(
                connection, scope, current.execution_id
            )
            if (
                result is None
                or result.task != task
                or result.execution.task_id != task.task_id
                or result.execution.pins.release_id != task.release_id
                or result.execution.pins.bundle_digest != task.bundle_digest
            ):
                raise IntakeError("intake_result_mismatch")
            return IntakeResult(task=result.task, execution=result.execution)

    async def _complete(
        self, intent: repository.IntakeIntent, principal: object
    ) -> IntakeResult:
        submission = intent.submission
        scope = submission.idempotency_scope
        if intent.status is repository.IntentStatus.FAILED:
            raise IntakeError(intent.failure_code or "intake_intent_failed")
        if intent.status is repository.IntentStatus.PENDING:
            await self._verify_media(scope, submission.media_ref)

        # The intent is committed before the ledger call. If cancellation
        # interrupts the await, recovery can still replay its historical pin.
        try:
            task = await to_thread(
                self._releases.accept_task, submission, principal=principal
            )
        except StoreBusy:
            raise IntakeError("intake_release_store_busy", retryable=True) from None
        except ReleaseOperationError as error:
            if error.code in {"task_principal_mismatch", "task_idempotency_conflict"}:
                await self._record_failure(intent, error.code)
            if error.code == "task_idempotency_conflict":
                raise IntakeConflict(error.code) from None
            raise IntakeError(
                error.code,
                retryable=error.code
                in {
                    "no_active_release",
                    "requested_release_not_selected",
                    "new_task_disallowed",
                    "task_time_precedes_activation",
                },
            ) from None
        _check_accepted(task, submission)

        original = await self._original_result(intent, task)
        if original is not None:
            return original

        capabilities = await self._host.capabilities(scope)
        ownership = (
            ()
            if self._historical_ownership is None
            else await self._historical_ownership(scope, capabilities)
        )
        async with self._engine.connect() as connection:
            validation_at = await _database_now(connection)
        resolved = await to_thread(
            resolve_execution_pins,
            self._releases,
            self._store,
            task,
            scope=scope,
            host=capabilities,
            validation_at=validation_at,
            limits=self._bundle_limits,
        )
        tool_ownership = (
            ()
            if self._historical_tool_ownership is None
            else await self._historical_tool_ownership(scope, resolved)
        )
        async with self._engine.begin() as connection:
            # The Release ledger supplies accepted_at. PostgreSQL establishes
            # the Execution clock and when its first job becomes claimable.
            created_at = await _database_now(connection)
            execution, root = initial_records(task, resolved, created_at)
            job = repository.NewJob(
                job_id=f"job.{root.node_run_id}",
                ready_at=created_at,
                budget=self._scheduling_limits.for_node(NodeId.N01),
            )
            result = await repository.accept_intent(
                connection,
                scope,
                intent,
                task,
                execution,
                resolved.policy_ref,
                self._scheduling_limits,
                root,
                job,
                historical_capabilities=HistoricalCapabilities(
                    validation_at=validation_at,
                    snapshot=capabilities,
                    ownership=ownership,
                    tool_ownership=tool_ownership,
                ),
            )
        return IntakeResult(task=result.task, execution=result.execution)
