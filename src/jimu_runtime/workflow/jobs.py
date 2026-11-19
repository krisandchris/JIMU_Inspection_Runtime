"""PostgreSQL job leasing and append-only attempt/result reconciliation."""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from enum import Enum
from hashlib import sha256
from typing import Annotated, Literal, cast
from uuid import uuid4

from pydantic import Field, TypeAdapter
from sqlalchemy import RowMapping, text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from jimu_runtime.domain.base import ContractModel
from jimu_runtime.domain.enums import (
    AttemptStatus,
    ExecutionStatus,
    NodeRunStatus,
    RuntimeRecordKind,
    TaskStatus,
)
from jimu_runtime.domain.fact_schemas.canonicalization import canonical_bytes
from jimu_runtime.domain.nodes.envelopes import FailedNodeOutput, SucceededNodeOutput
from jimu_runtime.domain.primitives import Digest, RecordId
from jimu_runtime.domain.runtime.executions import (
    OBSERVATION_DEPENDENCY_DEFERRED,
    Attempt,
    AttemptLink,
    LateResponseEvent,
    LifecycleReason,
    NodeId,
    NodeRun,
    PinnedRuntimeInput,
)
from jimu_runtime.domain.runtime.facts import AttemptRef, NodeRunRef
from jimu_runtime.domain.runtime.intake import IdempotencyScope
from jimu_runtime.domain.validation.authority_inputs import NodeAttemptOwner
from jimu_runtime.domain.validation.transitions import (
    AttemptTransitionGuard,
    NodeTransitionGuard,
)
from jimu_runtime.workflow import repository as repo
from jimu_runtime.workflow.repository import LockedExecution
from jimu_runtime.workflow.scheduling import validated_milliseconds

type Outcome = SucceededNodeOutput | FailedNodeOutput
type TerminalHandler = Callable[
    [
        AsyncConnection,
        IdempotencyScope,
        LockedExecution,
        NodeRun,
        Attempt,
        Outcome | None,
        datetime,
    ],
    Awaitable[None],
]

_OWNER: TypeAdapter[str] = TypeAdapter(RecordId)
_CLAIM_PAGE_SIZE = 64
NODE_OUTPUT_KEY = "node.output"
_STAGE_RECEIPT_KEY = "node.stage_receipt"
_RESPONSE_KEY = "node.response"
_ACTIVE_EXECUTIONS = frozenset({ExecutionStatus.INITIALIZED, ExecutionStatus.RUNNING})
_ACTIVE_NODES = frozenset(
    {NodeRunStatus.PENDING, NodeRunStatus.READY, NodeRunStatus.RUNNING}
)


OBSERVATION_WAIT_SETTLED_SQL = """
(j.observation_wait_node_id IS NULL OR EXISTS (
    SELECT 1 FROM jimu_workflow.node_runs AS observation
    WHERE (observation.project_id, observation.client_id, observation.execution_id, observation.node_run_id)
        = (j.project_id, j.client_id, j.execution_id, j.observation_wait_node_id)
        AND observation.status IN ('succeeded', 'failed', 'blocked', 'skipped', 'cancelled')
))
"""

# Permit a ready ToolPlan before a sibling Observation in backoff. If both jobs
# can run now, give N04 the first slot for their common Object.
_TOOL_OBSERVATION_ORDER_SQL = """
(n.node_id <> 'N05' OR NOT EXISTS (
    SELECT 1 FROM jimu_workflow.node_runs AS observation
    JOIN jimu_workflow.jobs AS sibling USING (project_id, client_id, execution_id, node_run_id)
    WHERE (observation.project_id, observation.client_id, observation.execution_id, observation.branch_key)
        = (n.project_id, n.client_id, n.execution_id, n.branch_key)
        AND n.branch_key <> 'global' AND observation.node_id = 'N04'
        AND NOT sibling.internal_tool_step
        AND (
            (sibling.status = 'ready' AND sibling.ready_at <= pg_catalog.clock_timestamp())
            OR (sibling.status = 'leased' AND (
                sibling.lease_until <= pg_catalog.clock_timestamp()
                OR sibling.attempt_deadline <= pg_catalog.clock_timestamp()
            ))
        )
))
"""


class JobConflict(ValueError):
    """A previously recorded result has a different immutable identity or content."""


class CompletionDisposition(Enum):
    APPLIED = "applied"
    STAGED = "staged"
    RETRY_SCHEDULED = "retry_scheduled"
    DUPLICATE = "duplicate"
    LATE_RECORDED = "late_recorded"
    LEASE_LOST = "lease_lost"


class ResponseEvidence(ContractModel):
    schema_version: Literal["1.0.0"]
    execution_id: RecordId
    attempt_id: RecordId
    response_key: RecordId
    response: PinnedRuntimeInput
    external_request_id: (
        Annotated[str, Field(min_length=1, max_length=256, pattern=r"^[ -~]+$")] | None
    )

    # The response digest is a pin to the response bytes, not the receipt digest.


@dataclass(frozen=True, slots=True)
class JobClaim:
    scope: IdempotencyScope
    execution_id: str
    node_run_id: str
    job_id: str
    fence: int
    owner: str
    lease_until: datetime


@dataclass(frozen=True, slots=True)
class StagedDecision:
    node: NodeRun
    attempt: Attempt
    output: SucceededNodeOutput
    draft_json: str


type FinalizationHandler = Callable[
    [
        AsyncConnection,
        IdempotencyScope,
        LockedExecution,
        tuple[StagedDecision, ...],
        datetime,
    ],
    Awaitable[bool],
]


class _JobStatus(Enum):
    READY = "ready"
    LEASED = "leased"
    STAGED = "staged"
    DONE = "done"


@dataclass(frozen=True, slots=True)
class _Job:
    execution_id: str
    node_run_id: str
    job_id: str
    status: _JobStatus
    fence: int
    lease_owner: str | None
    lease_until: datetime | None
    ready_at: datetime
    max_attempts: int
    timeout_ms: int
    retry_backoff_ms: tuple[int, ...]
    retryable_error_classes: tuple[str, ...]
    retry_on_lease_loss: bool
    attempt_count: int
    current_attempt_id: str | None
    attempt_deadline: datetime | None
    internal_tool_step: bool = False
    observation_wait_node_id: str | None = None

    @property
    def charged_attempts(self) -> int:
        return self.attempt_count - int(self.observation_wait_node_id is not None)


@dataclass(frozen=True, slots=True)
class _LockedJob:
    execution: LockedExecution
    node: NodeRun
    job: _Job
    now: datetime


@dataclass(frozen=True, slots=True)
class _Interruption:
    status: AttemptStatus
    guard: AttemptTransitionGuard
    reason: LifecycleReason
    retryable: bool


def _scope(scope: IdempotencyScope, execution_id: str) -> dict[str, str]:
    return {
        "project_id": scope.project_id,
        "client_id": scope.client_id,
        "execution_id": execution_id,
    }


def _job(row: RowMapping) -> _Job:
    return _Job(
        execution_id=row.execution_id,
        node_run_id=row.node_run_id,
        job_id=row.job_id,
        status=_JobStatus(row.status),
        fence=row.fence,
        lease_owner=row.lease_owner,
        lease_until=row.lease_until,
        ready_at=row.ready_at,
        max_attempts=row.max_attempts,
        timeout_ms=row.timeout_ms,
        retry_backoff_ms=tuple(row.retry_backoff_ms),
        retryable_error_classes=tuple(row.retryable_error_classes),
        retry_on_lease_loss=row.retry_on_lease_loss,
        attempt_count=row.attempt_count,
        current_attempt_id=row.current_attempt_id,
        attempt_deadline=row.attempt_deadline,
        internal_tool_step=row.internal_tool_step,
        observation_wait_node_id=row.observation_wait_node_id,
    )


async def _clock(connection: AsyncConnection) -> datetime:
    result = await connection.execute(text("SELECT pg_catalog.clock_timestamp()"))
    return cast(datetime, result.scalar_one()).astimezone(UTC)


def _matches(claim: JobClaim, job: _Job) -> bool:
    return (
        job.status is _JobStatus.LEASED
        and job.fence == claim.fence
        and job.lease_owner == claim.owner
    )


def _interruption(
    locked: _LockedJob, *, include_cancellation: bool = True
) -> _Interruption | None:
    job, now = locked.job, locked.now
    cancellation_at = (
        locked.execution.cancel_requested_at if include_cancellation else None
    )
    # A durably staged result already arrived in time and has no live lease.
    candidates = (
        (
            cancellation_at,
            _Interruption(
                AttemptStatus.CANCELLED,
                AttemptTransitionGuard.FENCED_CANCELLATION,
                LifecycleReason(code="execution_cancelled"),
                False,
            ),
        ),
        (
            job.attempt_deadline,
            _Interruption(
                AttemptStatus.TIMED_OUT,
                AttemptTransitionGuard.DEADLINE_ELAPSED,
                LifecycleReason(code="attempt_deadline_elapsed"),
                "timeout" in job.retryable_error_classes,
            ),
        ),
        (
            job.lease_until,
            _Interruption(
                AttemptStatus.SUPERSEDED,
                AttemptTransitionGuard.AUTHORITATIVE_FENCE_LOST,
                LifecycleReason(code="lease_expired"),
                job.retry_on_lease_loss,
            ),
        ),
    )
    overdue = [
        (deadline, item)
        for deadline, item in candidates
        if deadline is not None and deadline <= now
    ]
    first = min(overdue, key=lambda candidate: candidate[0], default=None)
    return first[1] if first is not None else None


async def _all_locked_jobs(
    connection: AsyncConnection, scope: IdempotencyScope, execution_id: str
) -> tuple[dict[str, NodeRun], tuple[_Job, ...], datetime]:
    nodes = await repo.list_nodes(connection, scope, execution_id, for_update=True)
    result = await connection.execute(
        text("""
            SELECT * FROM jimu_workflow.jobs
            WHERE project_id = :project_id AND client_id = :client_id
                AND execution_id = :execution_id
            ORDER BY job_id FOR UPDATE
        """),
        _scope(scope, execution_id),
    )
    return (
        {node.node_run_id: node for node in nodes},
        tuple(_job(row) for row in result.mappings()),
        await _clock(connection),
    )


async def _write_job(
    connection: AsyncConnection, scope: IdempotencyScope, job: _Job
) -> None:
    await connection.execute(
        text("""
            UPDATE jimu_workflow.jobs SET
                status = :status, fence = :fence, lease_owner = :lease_owner,
                lease_until = :lease_until, ready_at = :ready_at,
                attempt_count = :attempt_count, current_attempt_id = :current_attempt_id,
                attempt_deadline = :attempt_deadline,
                observation_wait_node_id = :observation_wait_node_id
            WHERE project_id = :project_id AND client_id = :client_id
                AND execution_id = :execution_id AND node_run_id = :node_run_id
                AND job_id = :job_id
        """),
        {
            **_scope(scope, job.execution_id),
            "node_run_id": job.node_run_id,
            "job_id": job.job_id,
            "status": job.status.value,
            "fence": job.fence,
            "lease_owner": job.lease_owner,
            "lease_until": job.lease_until,
            "ready_at": job.ready_at,
            "attempt_count": job.attempt_count,
            "current_attempt_id": job.current_attempt_id,
            "attempt_deadline": job.attempt_deadline,
            "observation_wait_node_id": job.observation_wait_node_id,
        },
    )


def _release_job(job: _Job, status: _JobStatus, now: datetime) -> _Job:
    return replace(
        job,
        status=status,
        lease_owner=None,
        lease_until=None,
        attempt_deadline=None,
        current_attempt_id=job.current_attempt_id
        if status is _JobStatus.STAGED
        else None,
        ready_at=now,
    )


async def _ensure_running(
    connection: AsyncConnection,
    scope: IdempotencyScope,
    locked: LockedExecution,
    now: datetime,
) -> LockedExecution:
    record = locked.execution
    if record.status is ExecutionStatus.INITIALIZED:
        if not locked.is_inference_replay:
            await repo.transition_task(
                connection,
                scope,
                record.execution_id,
                record.task_id,
                locked.task_status,
                TaskStatus.RUNNING,
                now=now,
                trace=record.trace,
            )
        running = record.model_copy(
            update={"status": ExecutionStatus.RUNNING, "started_at": now}
        )
        await repo.transition_execution(connection, scope, record, running, now=now)
        return replace(
            locked,
            task_status=locked.task_status
            if locked.is_inference_replay
            else TaskStatus.RUNNING,
            execution=running,
        )
    return locked


def _attempt_ref(attempt_id: str) -> AttemptRef:
    return AttemptRef(record_kind=RuntimeRecordKind.ATTEMPT, record_id=attempt_id)


def _node_ref(node_run_id: str) -> NodeRunRef:
    return NodeRunRef(record_kind=RuntimeRecordKind.NODE_RUN, record_id=node_run_id)


async def _live_attempt(
    connection: AsyncConnection, scope: IdempotencyScope, job: _Job
) -> Attempt:
    if job.current_attempt_id is None:
        raise RuntimeError("job has no started Attempt")
    attempt = await repo.get_attempt(
        connection, scope, job.execution_id, job.node_run_id, job.current_attempt_id
    )
    if attempt is None or attempt.status is not AttemptStatus.RUNNING:
        raise RuntimeError("job references a missing or terminal live Attempt")
    return attempt


async def _finish_attempt(
    connection: AsyncConnection,
    scope: IdempotencyScope,
    node: NodeRun,
    attempt: Attempt,
    status: AttemptStatus,
    guard: AttemptTransitionGuard,
    now: datetime,
    *,
    reason: LifecycleReason | None = None,
) -> Attempt:
    terminal = attempt.model_copy(
        update={"status": status, "ended_at": now, "reason": reason}
    )
    await repo.finish_attempt(
        connection,
        scope,
        node.execution_id,
        node.node_run_id,
        attempt,
        terminal,
        guard,
        now=now,
    )
    return terminal


async def _close_node(
    connection: AsyncConnection,
    scope: IdempotencyScope,
    node: NodeRun,
    status: NodeRunStatus,
    guard: NodeTransitionGuard,
    now: datetime,
    *,
    outcome: Outcome | None = None,
    reason: LifecycleReason | None = None,
) -> NodeRun:
    terminal = node.model_copy(
        update={
            "status": status,
            "ended_at": now,
            "reason": reason,
            "output_refs": tuple(item.ref for item in outcome.output_artifacts)
            if outcome
            else (),
        }
    )
    await repo.transition_node(
        connection, scope, node.execution_id, node, terminal, now=now, guard=guard
    )
    return terminal


def _digest(*records: ContractModel) -> str:
    # Stable per-component JSON plus an unambiguous length prefix for the receipt.
    hash_state = sha256()
    for record in records:
        data = record.stable_json().encode("utf-8")
        hash_state.update(len(data).to_bytes(8, "big"))
        hash_state.update(data)
    return hash_state.hexdigest()


async def _attempt_owned(
    connection: AsyncConnection, claim: JobClaim, attempt_id: str
) -> bool:
    result = await connection.execute(
        text("""
            SELECT 1 FROM jimu_workflow.attempts
            WHERE project_id = :project_id AND client_id = :client_id
                AND execution_id = :execution_id AND node_run_id = :node_run_id
                AND job_id = :job_id AND fence = :fence AND lease_owner = :lease_owner
                AND attempt_id = :attempt_id
        """),
        {
            **_scope(claim.scope, claim.execution_id),
            "node_run_id": claim.node_run_id,
            "job_id": claim.job_id,
            "fence": claim.fence,
            "lease_owner": claim.owner,
            "attempt_id": attempt_id,
        },
    )
    return result.first() is not None


type ResultHandler = Callable[
    [
        AsyncConnection,
        IdempotencyScope,
        LockedExecution,
        NodeRun,
        Attempt,
        Outcome,
        datetime,
    ],
    Awaitable[None],
]


class DurableJobs:
    """Own worker commit authority within short database transactions.

    Host callbacks must do only transactional persistence through their supplied
    connection. The worker performs inference and I/O outside these methods.
    """

    def __init__(
        self, engine: AsyncEngine, *, on_terminal: TerminalHandler | None = None
    ) -> None:
        self._engine = engine
        self._on_terminal = on_terminal

    async def _lock_for_work(
        self, connection: AsyncConnection, claim: JobClaim
    ) -> _LockedJob | None:
        execution = await repo.lock_execution(
            connection, claim.scope, claim.execution_id
        )
        if execution is None:
            return None
        nodes, jobs, now = await _all_locked_jobs(
            connection, claim.scope, claim.execution_id
        )
        if (
            execution.cancel_requested_at is not None
            and execution.execution.status in _ACTIVE_EXECUTIONS
        ):
            await self._cancel_locked(
                connection, claim.scope, execution, nodes, jobs, now
            )
            return None
        job = next(
            (
                job
                for job in jobs
                if job.job_id == claim.job_id
                and job.node_run_id == claim.node_run_id
                and not job.internal_tool_step
            ),
            None,
        )
        if job is None:
            return None
        return _LockedJob(execution, nodes[claim.node_run_id], job, now)

    async def _end_running_job(
        self,
        connection: AsyncConnection,
        scope: IdempotencyScope,
        locked: _LockedJob,
        attempt: Attempt,
        *,
        retryable: bool,
        outcome: Outcome | None = None,
    ) -> CompletionDisposition:
        job, now = locked.job, locked.now
        if not job.internal_tool_step and locked.node.node_id is NodeId.N05:
            await self._end_tool_step_children(connection, scope, locked, attempt)
        if attempt.status is AttemptStatus.SUCCEEDED:
            status = NodeRunStatus.SUCCEEDED
            guard = NodeTransitionGuard.AUTHORITATIVE_OUTPUT_VALIDATED
        elif retryable and job.charged_attempts < job.max_attempts:
            backoff = job.retry_backoff_ms[job.charged_attempts - 1]
            await _write_job(
                connection,
                scope,
                _release_job(
                    job, _JobStatus.READY, now + timedelta(milliseconds=backoff)
                ),
            )
            return CompletionDisposition.RETRY_SCHEDULED
        else:
            status = NodeRunStatus.FAILED
            guard = (
                NodeTransitionGuard.RETRY_BUDGET_EXHAUSTED
                if retryable
                else NodeTransitionGuard.NONRETRYABLE_FAILURE
            )
        node = await _close_node(
            connection,
            scope,
            locked.node,
            status,
            guard,
            now,
            outcome=outcome,
            reason=attempt.reason,
        )
        await _write_job(connection, scope, _release_job(job, _JobStatus.DONE, now))
        if self._on_terminal is not None and not job.internal_tool_step:
            await self._on_terminal(
                connection, scope, locked.execution, node, attempt, outcome, now
            )
        return CompletionDisposition.APPLIED

    async def _end_tool_step_children(
        self,
        connection: AsyncConnection,
        scope: IdempotencyScope,
        locked: _LockedJob,
        parent_attempt: Attempt,
    ) -> None:
        rows = await connection.execute(
            text("""
                SELECT j.* FROM jimu_workflow.tool_step_attempts AS s
                JOIN jimu_workflow.jobs AS j
                    ON (j.project_id, j.client_id, j.execution_id, j.node_run_id, j.job_id)
                    = (s.project_id, s.client_id, s.execution_id, s.child_node_run_id, s.child_job_id)
                WHERE s.project_id = :project_id AND s.client_id = :client_id
                    AND s.execution_id = :execution_id AND s.parent_node_run_id = :parent_node_run_id
                    AND s.parent_attempt_id = :parent_attempt_id AND j.status = 'leased'
                ORDER BY j.job_id FOR UPDATE OF j
            """),
            {
                **_scope(scope, parent_attempt.execution_id),
                "parent_node_run_id": locked.node.node_run_id,
                "parent_attempt_id": parent_attempt.attempt_id,
            },
        )
        for row in rows.mappings():
            child_job = _job(row)
            child_node = await repo.get_node(
                connection, scope, child_job.execution_id, child_job.node_run_id
            )
            if child_node is None:
                raise RuntimeError("tool step has no durable child NodeRun")
            child = _LockedJob(locked.execution, child_node, child_job, locked.now)
            if await self._expire(connection, scope, child) is not child_job:
                continue
            child_attempt = await _live_attempt(connection, scope, child_job)
            terminal = await _finish_attempt(
                connection,
                scope,
                child_node,
                child_attempt,
                AttemptStatus.SUPERSEDED,
                AttemptTransitionGuard.AUTHORITATIVE_FENCE_LOST,
                locked.now,
                reason=LifecycleReason(code="parent_tool_attempt_ended"),
            )
            await self._end_running_job(
                connection, scope, child, terminal, retryable=False
            )

    async def _expire(
        self,
        connection: AsyncConnection,
        scope: IdempotencyScope,
        locked: _LockedJob,
    ) -> _Job:
        job, now = locked.job, locked.now
        interruption = _interruption(locked, include_cancellation=False)
        if interruption is None:
            return job
        if job.current_attempt_id is None:
            ready = _release_job(job, _JobStatus.READY, now)
            await _write_job(connection, scope, ready)
            return ready
        attempt = await _live_attempt(connection, scope, job)
        terminal = await _finish_attempt(
            connection,
            scope,
            locked.node,
            attempt,
            interruption.status,
            interruption.guard,
            now,
            reason=interruption.reason,
        )
        await self._end_running_job(
            connection, scope, locked, terminal, retryable=interruption.retryable
        )
        return replace(job, status=_JobStatus.DONE)

    async def _claim_page(
        self,
        scope: IdempotencyScope,
        execution_filter: str | None,
        after: tuple[datetime, str] | None,
    ) -> tuple[tuple[str, str, str, datetime], ...]:
        cursor_time, cursor_execution = after if after is not None else (None, None)
        async with self._engine.connect() as connection:
            candidates = await connection.execute(
                text(f"""
                    SELECT execution_id, node_run_id, job_id, ready_at
                    FROM (
                        SELECT DISTINCT ON (j.execution_id) j.execution_id,
                            j.node_run_id, j.job_id, j.ready_at
                        FROM jimu_workflow.jobs AS j
                        JOIN jimu_workflow.executions AS e
                            USING (project_id, client_id, execution_id)
                        JOIN jimu_workflow.node_runs AS n
                            USING (project_id, client_id, execution_id, node_run_id)
                        WHERE j.project_id = :project_id AND j.client_id = :client_id
                            AND NOT j.internal_tool_step
                            AND e.status IN ('initialized', 'running')
                            AND (CAST(:execution_filter AS text) IS NULL OR j.execution_id = :execution_filter)
                            AND (e.cancel_requested_at IS NOT NULL
                                OR (j.status = 'ready' AND j.ready_at <= pg_catalog.clock_timestamp())
                                OR (j.status = 'leased' AND
                                    (j.lease_until <= pg_catalog.clock_timestamp()
                                     OR j.attempt_deadline <= pg_catalog.clock_timestamp())))
                            AND (e.cancel_requested_at IS NOT NULL OR (
                                {OBSERVATION_WAIT_SETTLED_SQL} AND {_TOOL_OBSERVATION_ORDER_SQL}
                            ))
                        ORDER BY j.execution_id, (n.node_id = 'N05') DESC,
                            (j.status = 'ready') DESC, j.ready_at, j.job_id
                    ) AS candidates
                    WHERE CAST(:cursor_time AS timestamptz) IS NULL
                        OR (ready_at, execution_id) > (CAST(:cursor_time AS timestamptz), CAST(:cursor_execution AS text))
                    ORDER BY ready_at, execution_id
                    LIMIT :page_size
                """),
                {
                    "project_id": scope.project_id,
                    "client_id": scope.client_id,
                    "execution_filter": execution_filter,
                    "cursor_time": cursor_time,
                    "cursor_execution": cursor_execution,
                    "page_size": _CLAIM_PAGE_SIZE,
                },
            )
            return tuple(
                (row.execution_id, row.node_run_id, row.job_id, row.ready_at)
                for row in candidates
            )

    async def claim(
        self,
        scope: IdempotencyScope,
        owner: str,
        lease_ms: int,
        *,
        execution_id: str | None = None,
    ) -> JobClaim | None:
        """Lease one available job, leaving its invocation history untouched."""
        owner = _OWNER.validate_python(owner, strict=True)
        duration = timedelta(milliseconds=validated_milliseconds(lease_ms))
        cursor = None
        while page := await self._claim_page(scope, execution_id, cursor):
            for candidate_execution, node_run_id, job_id, ready_at in page:
                claim = await self._claim_candidate(
                    scope, owner, duration, candidate_execution, node_run_id, job_id
                )
                if claim is not None:
                    return claim
                cursor = (ready_at, candidate_execution)
            if len(page) < _CLAIM_PAGE_SIZE:
                break
        return None

    async def _claim_candidate(
        self,
        scope: IdempotencyScope,
        owner: str,
        duration: timedelta,
        execution_id: str,
        node_run_id: str,
        job_id: str,
    ) -> JobClaim | None:
        async with self._engine.begin() as connection:
            execution = await repo.lock_execution(
                connection, scope, execution_id, skip_locked=True
            )
            if (
                execution is None
                or execution.execution.status not in _ACTIVE_EXECUTIONS
            ):
                return None
            nodes, jobs, now = await _all_locked_jobs(connection, scope, execution_id)
            if execution.cancel_requested_at is not None:
                await self._cancel_locked(
                    connection, scope, execution, nodes, jobs, now
                )
                return None
            job = next(
                (
                    item
                    for item in jobs
                    if item.job_id == job_id and not item.internal_tool_step
                ),
                None,
            )
            if job is None:
                return None
            node = nodes[node_run_id]
            if (
                job.observation_wait_node_id is not None
                and nodes[job.observation_wait_node_id].status in _ACTIVE_NODES
            ):
                return None
            if job.status is _JobStatus.LEASED:
                job = await self._expire(
                    connection, scope, _LockedJob(execution, node, job, now)
                )
            if job.status is not _JobStatus.READY or job.ready_at > now:
                return None
            if node.status not in (NodeRunStatus.READY, NodeRunStatus.RUNNING):
                raise RuntimeError("ready job references an unavailable NodeRun")
            lease_until = now + duration
            leased = replace(
                job,
                status=_JobStatus.LEASED,
                fence=job.fence + 1,
                lease_owner=owner,
                lease_until=lease_until,
            )
            await _write_job(connection, scope, leased)
            return JobClaim(
                scope,
                execution_id,
                node_run_id,
                job_id,
                leased.fence,
                owner,
                lease_until,
            )

    async def start(self, claim: JobClaim) -> Attempt | None:
        """Start a single invocation under a fresh claim."""
        async with self._engine.begin() as connection:
            locked = await self._lock_for_work(connection, claim)
            if locked is None or not _matches(claim, locked.job):
                return None
            job, node, now = locked.job, locked.node, locked.now
            if _interruption(locked) is not None:
                await self._expire(connection, claim.scope, locked)
                return None
            if job.current_attempt_id is not None:
                return None
            if job.charged_attempts >= job.max_attempts:
                raise RuntimeError("job has no available Attempt budget")
            scope = claim.scope
            execution = await _ensure_running(connection, scope, locked.execution, now)
            attempt = Attempt(
                schema_version="1.0.0",
                attempt_id=uuid4().hex,
                execution_id=claim.execution_id,
                owner=NodeAttemptOwner(
                    owner_kind="node", node_run_ref=_node_ref(claim.node_run_id)
                ),
                attempt_number=job.attempt_count + 1,
                status=AttemptStatus.RUNNING,
                created_at=now,
                started_at=now,
            )
            previous = None
            guard = None
            if node.attempt_history:
                previous = await repo.get_attempt(
                    connection,
                    scope,
                    claim.execution_id,
                    node.node_run_id,
                    node.attempt_history[-1].attempt_ref.record_id,
                )
            else:
                guard = NodeTransitionGuard.FIRST_ATTEMPT_STARTED
            await repo.insert_attempt(
                connection,
                scope,
                execution.execution.execution_id,
                node.node_run_id,
                attempt,
                job_id=job.job_id,
                fence=job.fence,
                lease_owner=claim.owner,
            )
            following = node.model_copy(
                update={
                    "status": NodeRunStatus.RUNNING,
                    "started_at": node.started_at or now,
                    "attempt_history": (
                        *node.attempt_history,
                        AttemptLink(
                            attempt_ref=_attempt_ref(attempt.attempt_id),
                            attempt_number=attempt.attempt_number,
                        ),
                    ),
                },
            )
            await repo.transition_node(
                connection,
                scope,
                claim.execution_id,
                node,
                following,
                now=now,
                guard=guard,
                appended_attempt=attempt,
                prior_attempt=previous,
                retry_authorized=bool(node.attempt_history),
            )
            await _write_job(
                connection,
                scope,
                replace(
                    job,
                    attempt_count=attempt.attempt_number,
                    current_attempt_id=attempt.attempt_id,
                    attempt_deadline=now + timedelta(milliseconds=job.timeout_ms),
                ),
            )
            return attempt

    async def defer_tool_observation(
        self,
        claim: JobClaim,
        observation_node_run_id: str,
        object_pin: PinnedRuntimeInput,
    ) -> bool | None:
        """Park a live N05 once, or report that N04 already settled or the fence was lost."""
        scope = claim.scope
        async with self._engine.begin() as connection:
            locked = await self._lock_for_work(connection, claim)
            if locked is None or not _matches(claim, locked.job):
                return None
            node, job, now = locked.node, locked.job, locked.now
            interruption = _interruption(locked)
            if interruption is not None:
                if interruption.status is not AttemptStatus.CANCELLED:
                    await self._expire(connection, scope, locked)
                return None
            observation = await repo.get_node(
                connection, scope, claim.execution_id, observation_node_run_id
            )
            if (
                node.node_id is not NodeId.N05
                or node.status is not NodeRunStatus.RUNNING
                or job.current_attempt_id is None
                or observation is None
                or observation.node_id is not NodeId.N04
                or object_pin.ref.record_kind is not RuntimeRecordKind.OBJECT
                or object_pin not in node.upstream_inputs
                or object_pin not in observation.upstream_inputs
            ):
                raise JobConflict(
                    "observation deferral requires the exact Object prerequisite"
                )
            if observation.status not in _ACTIVE_NODES:
                return False
            if job.observation_wait_node_id is not None:
                raise JobConflict(
                    "ToolPlan already deferred its Observation dependency"
                )
            attempt = await _live_attempt(connection, scope, job)
            terminal = await _finish_attempt(
                connection,
                scope,
                node,
                attempt,
                AttemptStatus.SUPERSEDED,
                AttemptTransitionGuard.AUTHORITATIVE_FENCE_LOST,
                now,
                reason=LifecycleReason(code=OBSERVATION_DEPENDENCY_DEFERRED),
            )
            await self._end_tool_step_children(connection, scope, locked, terminal)
            waiting = _release_job(job, _JobStatus.READY, now)
            # The scoped foreign key proves that this observation is a configured edge.
            await _write_job(
                connection,
                scope,
                replace(waiting, observation_wait_node_id=observation_node_run_id),
            )
            return True

    async def heartbeat(self, claim: JobClaim, lease_ms: int) -> JobClaim | None:
        duration = timedelta(milliseconds=validated_milliseconds(lease_ms))
        async with self._engine.begin() as connection:
            locked = await self._lock_for_work(connection, claim)
            if locked is None or not _matches(claim, locked.job):
                return None
            if _interruption(locked) is not None:
                await self._expire(connection, claim.scope, locked)
                return None
            assert locked.job.lease_until is not None
            lease_until = max(locked.job.lease_until, locked.now + duration)
            await _write_job(
                connection, claim.scope, replace(locked.job, lease_until=lease_until)
            )
            return replace(claim, lease_until=lease_until)

    async def request_cancel(self, scope: IdempotencyScope, execution_id: str) -> bool:
        """Persist cancellation intent; reconciliation applies it after a crash."""
        async with self._engine.begin() as connection:
            locked = await repo.lock_execution(connection, scope, execution_id)
            if locked is None or locked.execution.status not in _ACTIVE_EXECUTIONS:
                return False
            if locked.cancel_requested_at is not None:
                return True
            now = await _clock(connection)
            await connection.execute(
                text("""
                    UPDATE jimu_workflow.executions SET cancel_requested_at = :now
                    WHERE project_id = :project_id AND client_id = :client_id
                        AND execution_id = :execution_id
                """),
                {**_scope(scope, execution_id), "now": now},
            )
            return True

    async def _cancel_locked(
        self,
        connection: AsyncConnection,
        scope: IdempotencyScope,
        execution: LockedExecution,
        nodes: dict[str, NodeRun],
        jobs: tuple[_Job, ...],
        now: datetime,
    ) -> None:
        running = await _ensure_running(connection, scope, execution, now)
        reason = LifecycleReason(code="execution_cancelled")
        for job in jobs:
            if job.current_attempt_id is not None:
                node = nodes[job.node_run_id]
                interruption = _interruption(_LockedJob(running, node, job, now))
                if interruption is None:
                    raise RuntimeError(
                        "cancelled Execution requires a durable cancellation time"
                    )
                attempt = await _live_attempt(connection, scope, job)
                await _finish_attempt(
                    connection,
                    scope,
                    node,
                    attempt,
                    interruption.status,
                    interruption.guard,
                    now,
                    reason=interruption.reason,
                )
            if job.status is not _JobStatus.DONE:
                await _write_job(
                    connection, scope, _release_job(job, _JobStatus.DONE, now)
                )
        for node in nodes.values():
            if node.status in _ACTIVE_NODES:
                guard = (
                    NodeTransitionGuard.FENCED_CANCELLATION
                    if node.status is NodeRunStatus.RUNNING
                    else NodeTransitionGuard.PRE_START_CANCELLATION
                )
                await _close_node(
                    connection,
                    scope,
                    node,
                    NodeRunStatus.CANCELLED,
                    guard,
                    now,
                    reason=reason,
                )
        record = running.execution
        following = record.model_copy(
            update={"status": ExecutionStatus.CANCELLED, "ended_at": now}
        )
        await repo.transition_execution(connection, scope, record, following, now=now)
        if not running.is_inference_replay:
            await repo.transition_task(
                connection,
                scope,
                record.execution_id,
                record.task_id,
                running.task_status,
                TaskStatus.CANCELLED,
                now=now,
                trace=record.trace,
            )

    async def reconcile(self, scope: IdempotencyScope, execution_id: str) -> bool:
        """Recover expired claims or apply a persisted cancellation intent."""
        async with self._engine.begin() as connection:
            execution = await repo.lock_execution(connection, scope, execution_id)
            if (
                execution is None
                or execution.execution.status not in _ACTIVE_EXECUTIONS
            ):
                return False
            nodes, jobs, now = await _all_locked_jobs(connection, scope, execution_id)
            if execution.cancel_requested_at is not None:
                await self._cancel_locked(
                    connection, scope, execution, nodes, jobs, now
                )
                return True
            changed = False
            for job in jobs:
                if job.status is _JobStatus.LEASED and not job.internal_tool_step:
                    following = await self._expire(
                        connection,
                        scope,
                        _LockedJob(execution, nodes[job.node_run_id], job, now),
                    )
                    changed = changed or following is not job
            return changed

    async def complete(
        self,
        claim: JobClaim,
        outcome: Outcome,
        *,
        response: ResponseEvidence | None = None,
        response_record: ContractModel | None = None,
        persist: ResultHandler | None = None,
    ) -> CompletionDisposition:
        """Apply a validated result, or keep its late response as audit evidence."""
        return await self._submit(
            claim, outcome, response, response_record, persist, draft=None
        )

    async def stage_decision(
        self,
        claim: JobClaim,
        outcome: SucceededNodeOutput,
        draft: ContractModel,
        *,
        response: ResponseEvidence | None = None,
        response_record: ContractModel | None = None,
        persist: ResultHandler | None = None,
    ) -> CompletionDisposition:
        """Accept an N06 draft durably, leaving publication to its aggregation."""
        if not isinstance(outcome, SucceededNodeOutput):
            raise ValueError("decision staging requires a successful result")
        return await self._submit(
            claim, outcome, response, response_record, persist, draft=draft
        )

    async def _submit(
        self,
        claim: JobClaim,
        outcome: Outcome,
        response: ResponseEvidence | None,
        response_record: ContractModel | None,
        persist: ResultHandler | None,
        *,
        draft: ContractModel | None,
    ) -> CompletionDisposition:
        if not isinstance(outcome, (SucceededNodeOutput, FailedNodeOutput)):
            raise ValueError(
                "only validated success and failure envelopes may be submitted"
            )
        outcome = type(outcome).model_validate(outcome)
        if response is not None:
            response = _checked_response(
                claim, outcome.attempt_id, response, response_record
            )
        elif response_record is not None:
            raise ValueError(
                "a retained response record requires its exact evidence pin"
            )
        if draft is not None:
            draft = type(draft).model_validate(draft)
        records: tuple[ContractModel, ...] = (outcome,)
        if response is not None:
            records += (response,)
        if draft is not None:
            records += (draft,)
        digest = _digest(*records)
        async with self._engine.begin() as connection:
            locked = await self._lock_for_work(connection, claim)
            if not await _attempt_owned(connection, claim, outcome.attempt_id):
                return CompletionDisposition.LEASE_LOST
            attempt = await repo.get_attempt(
                connection,
                claim.scope,
                claim.execution_id,
                claim.node_run_id,
                outcome.attempt_id,
            )
            node = (
                locked.node
                if locked
                else await repo.get_node(
                    connection, claim.scope, claim.execution_id, claim.node_run_id
                )
            )
            if attempt is None or node is None:
                raise RuntimeError("Attempt ownership requires its persisted NodeRun")
            if (
                outcome.node_run_id != node.node_run_id
                or outcome.node_id is not node.node_id
                or outcome.trace != node.trace
                or outcome.started_at != attempt.started_at
            ):
                raise JobConflict(
                    "outcome does not match its persisted NodeRun and Attempt"
                )
            if isinstance(outcome, SucceededNodeOutput):
                if not outcome.output_artifacts:
                    raise ValueError(
                        "a successful result requires at least one pinned artifact"
                    )
                if (node.node_id is NodeId.N06) != (draft is not None):
                    raise ValueError("successful N06 results require a staged decision")
            if await _prior_submission(
                connection, claim, attempt.attempt_id, digest, staged=draft is not None
            ):
                return CompletionDisposition.DUPLICATE
            if locked is not None and _matches(claim, locked.job):
                if locked.job.current_attempt_id != attempt.attempt_id:
                    raise JobConflict("the claim has no authority for this Attempt")
                if _interruption(locked) is None:
                    return await self._apply_submission(
                        connection,
                        claim,
                        locked,
                        attempt,
                        outcome,
                        response,
                        persist,
                        draft,
                        digest,
                    )
                await self._expire(connection, claim.scope, locked)
                terminal = await repo.get_attempt(
                    connection,
                    claim.scope,
                    claim.execution_id,
                    claim.node_run_id,
                    attempt.attempt_id,
                )
                if terminal is None:
                    raise RuntimeError("reconciled Attempt is missing")
                attempt = terminal
            if response is None or attempt.status is AttemptStatus.RUNNING:
                return CompletionDisposition.LEASE_LOST
            return await _record_late(
                connection,
                claim,
                node,
                attempt,
                response,
                response_record=response_record,
                outcome=outcome,
            )

    async def record_late(
        self,
        claim: JobClaim,
        response: ResponseEvidence,
        *,
        response_record: ContractModel | None = None,
    ) -> CompletionDisposition:
        """Append a separately arriving response for a started terminal Attempt."""
        response = _checked_response(
            claim, response.attempt_id, response, response_record
        )
        async with self._engine.begin() as connection:
            locked = await self._lock_for_work(connection, claim)
            if not await _attempt_owned(connection, claim, response.attempt_id):
                return CompletionDisposition.LEASE_LOST
            if locked is not None and _matches(claim, locked.job):
                await self._expire(connection, claim.scope, locked)
            attempt = await repo.get_attempt(
                connection,
                claim.scope,
                claim.execution_id,
                claim.node_run_id,
                response.attempt_id,
            )
            node = (
                locked.node
                if locked
                else await repo.get_node(
                    connection, claim.scope, claim.execution_id, claim.node_run_id
                )
            )
            if (
                attempt is None
                or node is None
                or attempt.status is AttemptStatus.RUNNING
            ):
                return CompletionDisposition.LEASE_LOST
            return await _record_late(
                connection,
                claim,
                node,
                attempt,
                response,
                response_record=response_record,
            )

    async def _apply_submission(
        self,
        connection: AsyncConnection,
        claim: JobClaim,
        locked: _LockedJob,
        attempt: Attempt,
        outcome: Outcome,
        response: ResponseEvidence | None,
        persist: ResultHandler | None,
        draft: ContractModel | None,
        digest: str,
    ) -> CompletionDisposition:
        scope, job, now = claim.scope, locked.job, locked.now
        terminal = attempt
        if draft is None:
            if isinstance(outcome, SucceededNodeOutput):
                terminal = await _finish_attempt(
                    connection,
                    scope,
                    locked.node,
                    attempt,
                    AttemptStatus.SUCCEEDED,
                    AttemptTransitionGuard.RESPONSE_VALIDATED_WITH_COMMIT_RIGHTS,
                    now,
                )
            else:
                terminal = await _finish_attempt(
                    connection,
                    scope,
                    locked.node,
                    attempt,
                    AttemptStatus.FAILED,
                    AttemptTransitionGuard.NORMALIZED_FAILURE,
                    now,
                    reason=LifecycleReason(code=outcome.error.code),
                )
        output_digest = await repo.store_output(
            connection,
            scope,
            claim.execution_id,
            claim.node_run_id,
            attempt.attempt_id,
            output_key=NODE_OUTPUT_KEY,
            output_kind="node_output",
            record=outcome,
        )
        if response is not None:
            await repo.store_output(
                connection,
                scope,
                claim.execution_id,
                claim.node_run_id,
                attempt.attempt_id,
                output_key=_RESPONSE_KEY,
                output_kind="response",
                record=response,
            )
        if persist is not None:
            await persist(
                connection, scope, locked.execution, locked.node, terminal, outcome, now
            )
        if draft is not None:
            await repo.store_decision_draft(
                connection,
                scope,
                claim.execution_id,
                claim.node_run_id,
                attempt.attempt_id,
                job_id=claim.job_id,
                fence=claim.fence,
                draft_key=attempt.attempt_id,
                record=draft,
            )
            await _write_job(
                connection, scope, _release_job(job, _JobStatus.STAGED, now)
            )
            await _write_stage_receipt(connection, claim, attempt.attempt_id, digest)
            return CompletionDisposition.STAGED
        if isinstance(outcome, SucceededNodeOutput):
            await repo.select_authority(
                connection,
                scope,
                claim.execution_id,
                claim.node_run_id,
                attempt.attempt_id,
                output_key=NODE_OUTPUT_KEY,
                output_digest=output_digest,
            )
        retryable = (
            isinstance(outcome, FailedNodeOutput)
            and outcome.error.retryable
            and outcome.error.error_class in job.retryable_error_classes
        )
        disposition = await self._end_running_job(
            connection, scope, locked, terminal, retryable=retryable, outcome=outcome
        )
        await _write_completion_receipt(
            connection, scope, job, attempt.attempt_id, digest
        )
        return disposition

    async def pending_finalizations(
        self, scope: IdempotencyScope, *, limit: int = 32
    ) -> tuple[str, ...]:
        """Discover scoped executions whose recorded jobs are settled or staged."""
        if type(limit) is not int or not 1 <= limit <= 1024:
            raise ValueError("finalization discovery limit must be between 1 and 1024")
        async with self._engine.connect() as connection:
            result = await connection.execute(
                text("""
                    SELECT e.execution_id
                    FROM jimu_workflow.executions AS e
                    WHERE e.project_id = :project_id AND e.client_id = :client_id
                        AND e.status = 'running'
                        AND EXISTS (
                            SELECT 1 FROM jimu_workflow.jobs j
                            WHERE (j.project_id, j.client_id, j.execution_id) =
                                (e.project_id, e.client_id, e.execution_id)
                        )
                        AND NOT EXISTS (
                            SELECT 1 FROM jimu_workflow.jobs j
                            WHERE (j.project_id, j.client_id, j.execution_id) =
                                (e.project_id, e.client_id, e.execution_id)
                            AND j.status IN ('ready', 'leased')
                        )
                    ORDER BY e.execution_id
                    LIMIT :limit
                """),
                {
                    "project_id": scope.project_id,
                    "client_id": scope.client_id,
                    "limit": limit,
                },
            )
            return tuple(result.scalars())

    async def finalize_staged(
        self, scope: IdempotencyScope, execution_id: str, apply: FinalizationHandler
    ) -> bool:
        """Promote the accepted drafts and write the aggregate in one transaction."""
        async with self._engine.begin() as connection:
            execution = await repo.lock_execution(connection, scope, execution_id)
            if (
                execution is None
                or execution.execution.status not in _ACTIVE_EXECUTIONS
            ):
                return False
            nodes, jobs, now = await _all_locked_jobs(connection, scope, execution_id)
            if execution.cancel_requested_at is not None:
                await self._cancel_locked(
                    connection, scope, execution, nodes, jobs, now
                )
                return False
            if (
                execution.execution.status is not ExecutionStatus.RUNNING
                or not jobs
                or any(
                    job.status in (_JobStatus.READY, _JobStatus.LEASED) for job in jobs
                )
                or any(
                    node.status in (NodeRunStatus.PENDING, NodeRunStatus.READY)
                    for node in nodes.values()
                )
            ):
                return False
            staged: list[StagedDecision] = []
            savepoint = await connection.begin_nested()
            async with savepoint:
                for job in jobs:
                    if job.status is _JobStatus.STAGED:
                        staged.append(
                            await self._promote(
                                connection,
                                scope,
                                _LockedJob(execution, nodes[job.node_run_id], job, now),
                            )
                        )
                if not await apply(connection, scope, execution, tuple(staged), now):
                    await savepoint.rollback()
                    return False
                latest = await repo.get_execution(connection, scope, execution_id)
                finalizations = await connection.execute(
                    text("""
                        SELECT 1 FROM jimu_workflow.execution_finalizations
                        WHERE project_id = :project_id AND client_id = :client_id
                            AND execution_id = :execution_id
                    """),
                    _scope(scope, execution_id),
                )
                if finalizations.scalar_one_or_none() is None:
                    raise JobConflict(
                        "successful aggregation requires a persisted finalization"
                    )
                if latest is None or latest.execution.status not in (
                    ExecutionStatus.COMPLETED,
                    ExecutionStatus.PARTIAL_FAILED,
                    ExecutionStatus.FAILED,
                ):
                    raise JobConflict(
                        "successful aggregation requires a terminal Execution"
                    )
            return True

    async def _promote(
        self, connection: AsyncConnection, scope: IdempotencyScope, locked: _LockedJob
    ) -> StagedDecision:
        job, now = locked.job, locked.now
        attempt = await _live_attempt(connection, scope, job)
        staged = await _read_staged(connection, scope, job, locked.node, attempt)
        receipt = await repo.read_output(
            connection,
            scope,
            job.execution_id,
            job.node_run_id,
            attempt.attempt_id,
            _STAGE_RECEIPT_KEY,
            _StageReceipt,
        )
        if receipt is None:
            raise JobConflict(
                "staged decision is missing its fenced submission receipt"
            )
        terminal = await _finish_attempt(
            connection,
            scope,
            locked.node,
            attempt,
            AttemptStatus.SUCCEEDED,
            AttemptTransitionGuard.RESPONSE_VALIDATED_WITH_COMMIT_RIGHTS,
            now,
        )
        output_digest = await repo.store_output(
            connection,
            scope,
            job.execution_id,
            job.node_run_id,
            terminal.attempt_id,
            output_key=NODE_OUTPUT_KEY,
            output_kind="node_output",
            record=staged.output,
        )
        await repo.select_authority(
            connection,
            scope,
            job.execution_id,
            job.node_run_id,
            terminal.attempt_id,
            output_key=NODE_OUTPUT_KEY,
            output_digest=output_digest,
        )
        # N06 promotion is part of the aggregate; its own terminal hook must not
        # independently dispatch work or publish a decision.
        await _close_node(
            connection,
            scope,
            locked.node,
            NodeRunStatus.SUCCEEDED,
            NodeTransitionGuard.AUTHORITATIVE_OUTPUT_VALIDATED,
            now,
            outcome=staged.output,
        )
        await _write_job(connection, scope, _release_job(job, _JobStatus.DONE, now))
        await _write_completion_receipt(
            connection,
            scope,
            job,
            attempt.attempt_id,
            receipt.result_digest,
        )
        return staged


class _StageReceipt(ContractModel):
    schema_version: Literal["1.0.0"]
    result_digest: Digest


async def _prior_submission(
    connection: AsyncConnection,
    claim: JobClaim,
    attempt_id: str,
    digest: str,
    *,
    staged: bool,
) -> bool:
    if staged:
        stage = await repo.read_output(
            connection,
            claim.scope,
            claim.execution_id,
            claim.node_run_id,
            attempt_id,
            _STAGE_RECEIPT_KEY,
            _StageReceipt,
        )
        stored = stage.result_digest if stage is not None else None
    else:
        result = await connection.execute(
            text("""
                SELECT result_digest FROM jimu_workflow.job_completions
                WHERE project_id = :project_id AND client_id = :client_id
                    AND execution_id = :execution_id AND node_run_id = :node_run_id
                    AND job_id = :job_id AND fence = :fence AND attempt_id = :attempt_id
            """),
            _receipt_parameters(claim, attempt_id, digest),
        )
        stored = result.scalar_one_or_none()
    if stored is None:
        return False
    if stored != digest:
        raise JobConflict("this submission conflicts with the already persisted result")
    return True


def _receipt_parameters(
    claim: JobClaim, attempt_id: str, digest: str
) -> dict[str, str | int]:
    return {
        **_scope(claim.scope, claim.execution_id),
        "node_run_id": claim.node_run_id,
        "attempt_id": attempt_id,
        "job_id": claim.job_id,
        "fence": claim.fence,
        "result_digest": digest,
    }


async def _write_completion_receipt(
    connection: AsyncConnection,
    scope: IdempotencyScope,
    job: _Job,
    attempt_id: str,
    digest: str,
) -> None:
    await connection.execute(
        text("""
            INSERT INTO jimu_workflow.job_completions
                (project_id, client_id, execution_id, node_run_id, job_id,
                 attempt_id, fence, result_digest)
            VALUES (:project_id, :client_id, :execution_id, :node_run_id, :job_id,
                    :attempt_id, :fence, :result_digest)
        """),
        {
            **_scope(scope, job.execution_id),
            "node_run_id": job.node_run_id,
            "job_id": job.job_id,
            "fence": job.fence,
            "attempt_id": attempt_id,
            "result_digest": digest,
        },
    )


async def _write_stage_receipt(
    connection: AsyncConnection, claim: JobClaim, attempt_id: str, digest: str
) -> None:
    await repo.store_output(
        connection,
        claim.scope,
        claim.execution_id,
        claim.node_run_id,
        attempt_id,
        output_key=_STAGE_RECEIPT_KEY,
        output_kind="stage_receipt",
        record=_StageReceipt(schema_version="1.0.0", result_digest=digest),
    )


def _checked_response(
    claim: JobClaim,
    attempt_id: str,
    response: ResponseEvidence,
    response_record: ContractModel | None,
) -> ResponseEvidence:
    response = ResponseEvidence.model_validate(response)
    if (response.execution_id, response.attempt_id) != (claim.execution_id, attempt_id):
        raise JobConflict("response belongs to a different Execution or Attempt")
    if response_record is not None:
        record = type(response_record).model_validate(response_record)
        if (
            sha256(canonical_bytes(record.model_dump(mode="json"))).hexdigest()
            != response.response.digest
        ):
            raise JobConflict("response record does not match its pinned digest")
    return response


async def _record_late(
    connection: AsyncConnection,
    claim: JobClaim,
    node: NodeRun,
    attempt: Attempt,
    response: ResponseEvidence,
    *,
    response_record: ContractModel | None,
    outcome: Outcome | None = None,
) -> CompletionDisposition:
    previous = await repo.read_output(
        connection,
        claim.scope,
        claim.execution_id,
        claim.node_run_id,
        attempt.attempt_id,
        _RESPONSE_KEY,
        ResponseEvidence,
    )
    if previous is not None and previous.response_key == response.response_key:
        if previous != response:
            raise JobConflict("response identity has a different persisted digest")
        return CompletionDisposition.DUPLICATE
    digest = _digest(response)
    parameters = {
        **_receipt_parameters(claim, attempt.attempt_id, digest),
        "response_key": response.response_key,
    }
    result = await connection.execute(
        text("""
            SELECT result_digest FROM jimu_workflow.late_responses
            WHERE project_id = :project_id AND client_id = :client_id
                AND execution_id = :execution_id AND node_run_id = :node_run_id
                AND attempt_id = :attempt_id AND response_key = :response_key
        """),
        parameters,
    )
    stored = result.scalar_one_or_none()
    if stored is not None and stored != digest:
        raise JobConflict("response identity has a different persisted digest")
    supplemental = (("artifact", response_record), ("result", outcome))
    for label, record in supplemental:
        if record is not None:
            await repo.store_output(
                connection,
                claim.scope,
                claim.execution_id,
                claim.node_run_id,
                attempt.attempt_id,
                output_key=f"late.{label}.{response.response_key}",
                output_kind=f"late_{label}",
                record=record,
            )
    if stored is not None:
        return CompletionDisposition.DUPLICATE
    await repo.store_output(
        connection,
        claim.scope,
        claim.execution_id,
        claim.node_run_id,
        attempt.attempt_id,
        output_key=f"late.{response.response_key}",
        output_kind="late_response",
        record=response,
    )
    event = await repo.append_event(
        connection,
        claim.scope,
        claim.execution_id,
        node.trace,
        LateResponseEvent(
            event_kind="late_response",
            attempt_ref=_attempt_ref(attempt.attempt_id),
            response_ref=response.response.ref,
            reason=LifecycleReason(code="non_authoritative_response"),
        ),
        now=await _clock(connection),
        attempt=attempt,
    )
    await connection.execute(
        text("""
            INSERT INTO jimu_workflow.late_responses
                (project_id, client_id, execution_id, node_run_id, attempt_id,
                 response_key, result_digest, external_request_id, response_ref, event_number)
            VALUES (:project_id, :client_id, :execution_id, :node_run_id, :attempt_id,
                    :response_key, :result_digest, :external_request_id,
                    CAST(:response_ref AS json), :event_number)
        """),
        {
            **parameters,
            "external_request_id": response.external_request_id,
            "response_ref": response.response.ref.stable_json(),
            "event_number": event.event_number,
        },
    )
    return CompletionDisposition.LATE_RECORDED


async def _read_staged(
    connection: AsyncConnection,
    scope: IdempotencyScope,
    job: _Job,
    node: NodeRun,
    attempt: Attempt,
) -> StagedDecision:
    result = await connection.execute(
        text("""
            SELECT record::text FROM jimu_workflow.decision_drafts
            WHERE project_id = :project_id AND client_id = :client_id
                AND execution_id = :execution_id AND node_run_id = :node_run_id
                AND attempt_id = :attempt_id AND job_id = :job_id AND fence = :fence
        """),
        {
            **_scope(scope, job.execution_id),
            "node_run_id": node.node_run_id,
            "attempt_id": attempt.attempt_id,
            "job_id": job.job_id,
            "fence": job.fence,
        },
    )
    draft = result.scalar_one_or_none()
    output = await repo.read_output(
        connection,
        scope,
        job.execution_id,
        job.node_run_id,
        attempt.attempt_id,
        NODE_OUTPUT_KEY,
        SucceededNodeOutput,
    )
    if draft is None or output is None:
        raise RuntimeError(
            "staged job requires its immutable decision draft and result"
        )
    return StagedDecision(node, attempt, output, draft)
