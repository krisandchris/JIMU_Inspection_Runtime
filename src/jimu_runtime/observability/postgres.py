"""Scoped, bounded read-only PostgreSQL observability.

The engine port is implemented by the runtime's PostgreSQL AsyncEngine.
The host authenticates its caller and supplies an already-authorized scope.
"""

from asyncio import Semaphore, timeout
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping, Sequence
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from hashlib import sha256
from itertools import groupby
from json import dumps

from pydantic import TypeAdapter, ValidationError
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from jimu_runtime.domain.base import ContractModel
from jimu_runtime.domain.primitives import RecordId
from jimu_runtime.domain.runtime.intake import IdempotencyScope
from jimu_runtime.observability import queries
from jimu_runtime.observability.contracts import (
    Availability,
    CallKind,
    CallPage,
    CallReceipt,
    CallSpan,
    EventCursor,
    EventPage,
    PageSize,
    Readiness,
    TimeCursor,
    ToolStepCursor,
    ToolStepPage,
    ToolStepSpan,
    TraceEvent,
    TraceExecution,
)
from jimu_runtime.observability.prometheus import CALL_ERROR_CLASSES, render_metrics

type WorkerClaimProbe = Callable[[IdempotencyScope], Awaitable[Availability]]

_PAGE_SIZE = TypeAdapter(PageSize)
_AVAILABILITY: TypeAdapter[Availability] = TypeAdapter(Availability)
_ID = TypeAdapter(RecordId)
_TIMEOUT_BUDGET = 5
_MAX_TRACE_BYTES = 256 * 1024
type _Row = Mapping[str, object]


class ObservabilityUnavailable(RuntimeError):
    """No operational payload is produced from a failed or inconsistent read."""


class ObservabilityCursorStale(RuntimeError):
    """The source changed between pages. Restart at the first page."""


def _params(scope: IdempotencyScope) -> tuple[str, str]:
    scope = IdempotencyScope.model_validate(scope)
    return scope.project_id, scope.client_id


def _utc(row: _Row) -> dict[str, object]:
    return {
        key: value.astimezone(UTC) if isinstance(value, datetime) else value
        for key, value in row.items()
    }


def _bounded[T: ContractModel](output: T) -> T:
    if len(output.model_dump_json().encode("utf-8")) > _MAX_TRACE_BYTES:
        raise ObservabilityUnavailable("observability_trace_limit")
    return output


def _kind(value: object) -> CallKind:
    if value == "tool":
        return "tool"
    if value in (
        "model",
        "model_unselected",
        "nested_model",
        "nested_model_unselected",
    ):
        return "model"
    return "unknown"


def _call_span(group: Sequence[_Row]) -> CallSpan:
    first = _utc(group[0])
    receipts = tuple(
        CallReceipt.model_validate(
            {
                "recorded_at": row["receipt_at"],
                "boundary": row["boundary"],
                "late": row["late"],
                "fence_qualified": row["fence_qualified"],
                "status": row["outcome"] or "unqualified",
                "producer_id": row["producer_id"],
                "error_class": (
                    row["error_class"]
                    if row["error_class"] is None
                    or row["error_class"] in CALL_ERROR_CLASSES
                    else "other"
                ),
                "started_at": row["started_at"],
                "completed_at": row["completed_at"],
                "response_evidence_failed": row["response_evidence_failed"],
                "invalid_slots": row["invalid_slots"],
                "missing_slots": row["missing_slots"],
            }
        )
        for raw in group
        if (row := _utc(raw))["receipt_at"] is not None
    )
    return CallSpan.model_validate(
        {
            **{
                key: first[key]
                for key in (
                    "dispatch_id",
                    "recorded_at",
                    "node_id",
                    "operation_node_run_id",
                    "node_run_id",
                    "attempt_id",
                    "parent_node_run_id",
                    "parent_attempt_id",
                    "send_admitted",
                )
            },
            "kind": _kind(first["target_kind"]),
            "receipts": receipts,
        }
    )


class PostgresObservability:
    """Read persisted workflow and dispatch state without acquiring worker locks."""

    def __init__(
        self,
        engine: AsyncEngine,
        *,
        worker_claim_probe: WorkerClaimProbe | None = None,
    ) -> None:
        self._engine = engine
        self._worker_claim_probe = worker_claim_probe
        self._slots = Semaphore(4)

    @asynccontextmanager
    async def _transaction(self) -> AsyncIterator[AsyncConnection]:
        try:
            async with (
                timeout(_TIMEOUT_BUDGET),
                self._slots,
                self._engine.connect() as connection,
            ):
                await connection.exec_driver_sql(
                    "SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY"
                )
                await connection.exec_driver_sql("SET LOCAL statement_timeout = '4s'")
                yield connection
        except (SQLAlchemyError, TimeoutError, ValidationError):
            raise ObservabilityUnavailable("observability_read_unavailable") from None

    @staticmethod
    async def _rows(
        connection: AsyncConnection, query: str, params: tuple[object, ...] = ()
    ) -> Sequence[_Row]:
        return tuple(
            dict(row)
            for row in (await connection.exec_driver_sql(query, params)).mappings()
        )

    async def _execution(
        self, connection: AsyncConnection, scope: IdempotencyScope, execution_id: str
    ) -> tuple[TraceExecution, int] | None:
        rows = await self._rows(
            connection, queries.EXECUTION, (*_params(scope), execution_id)
        )
        if not rows:
            return None
        (row,) = rows
        data = _utc(row)
        through = data.pop("through")
        if type(through) is not int or not 0 <= through < 2**63 - 1:
            raise ValueError("invalid event high-water mark")
        return TraceExecution.model_validate(data), through

    async def events(
        self,
        scope: IdempotencyScope,
        execution_id: RecordId,
        *,
        cursor: EventCursor | None = None,
        limit: PageSize = 50,
    ) -> EventPage | None:
        """List a stable audit page with an immutable event-number high-water mark."""
        execution_id = _ID.validate_python(execution_id, strict=True)
        cursor = EventCursor() if cursor is None else EventCursor.model_validate(cursor)
        limit = _PAGE_SIZE.validate_python(limit, strict=True)
        async with self._transaction() as connection:
            anchored = await self._execution(connection, scope, execution_id)
            if anchored is None:
                return None
            execution, high = anchored
            through = high if cursor.through is None else cursor.through
            if cursor.after > through or through > high:
                raise ValueError("event cursor is outside retained execution history")
            rows = await self._rows(
                connection,
                queries.EVENTS,
                (*_params(scope), execution_id, cursor.after, through, limit + 1),
            )
            items = tuple(TraceEvent.model_validate(_utc(row)) for row in rows[:limit])
            if len(rows) != min(limit + 1, through - cursor.after) or any(
                item.number != cursor.after + index
                for index, item in enumerate(items, 1)
            ):
                raise ObservabilityUnavailable("observability_event_sequence_invalid")
            next_cursor = (
                EventCursor(after=items[-1].number, through=through)
                if len(rows) > limit
                else None
            )
            return _bounded(
                EventPage(
                    execution=execution,
                    execution_state_through=high,
                    through=through,
                    items=items,
                    next_cursor=next_cursor,
                )
            )

    async def _revision(
        self,
        connection: AsyncConnection,
        query: str,
        params: tuple[object, ...],
        supplied: str | None,
    ) -> str:
        (row,) = await self._rows(connection, query, params)
        version = sha256(
            dumps(
                [
                    query,
                    params,
                    [(key, str(value)) for key, value in sorted(row.items())],
                ]
            ).encode("utf-8")
        ).hexdigest()
        if supplied is not None and supplied != version:
            raise ObservabilityCursorStale("observability_cursor_stale")
        return version

    async def calls(
        self,
        scope: IdempotencyScope,
        execution_id: RecordId,
        *,
        attempt_id: RecordId | None = None,
        cursor: TimeCursor | None = None,
        limit: PageSize = 25,
    ) -> CallPage | None:
        """Page physical dispatch intents with their independent normal/late receipts."""
        execution_id = _ID.validate_python(execution_id, strict=True)
        limit = _PAGE_SIZE.validate_python(limit, strict=True)
        if attempt_id is not None:
            attempt_id = _ID.validate_python(attempt_id, strict=True)
        if cursor is not None:
            cursor = TimeCursor.model_validate(cursor)
        since = None if cursor is None else cursor.recorded_at
        record_id = None if cursor is None else cursor.record_id
        async with self._transaction() as connection:
            anchored = await self._execution(connection, scope, execution_id)
            if anchored is None:
                return None
            execution, _ = anchored
            params = (*_params(scope), execution_id, attempt_id, attempt_id)
            revision = (
                None
                if cursor is None
                else await self._revision(
                    connection, queries.CALL_REVISION, params, cursor.revision
                )
            )
            rows = await self._rows(
                connection,
                queries.CALLS,
                (
                    *params,
                    since,
                    since,
                    record_id,
                    limit + 1,
                ),
            )
            spans = tuple(
                _call_span(tuple(group))
                for _, group in groupby(rows, key=lambda row: row["dispatch_id"])
            )
            items = spans[:limit]
            next_cursor = None
            if len(spans) > limit:
                revision = revision or await self._revision(
                    connection, queries.CALL_REVISION, params, None
                )
                next_cursor = TimeCursor(
                    recorded_at=items[-1].recorded_at,
                    record_id=items[-1].dispatch_id,
                    revision=revision,
                )
            return _bounded(
                CallPage(
                    task_id=execution.task_id,
                    execution_id=execution_id,
                    trace_id=execution.trace_id,
                    items=items,
                    next_cursor=next_cursor,
                )
            )

    async def tool_steps(
        self,
        scope: IdempotencyScope,
        execution_id: RecordId,
        *,
        cursor: ToolStepCursor | None = None,
        limit: PageSize = 25,
    ) -> ToolStepPage | None:
        """Page Tool Plan provenance only while the selected source is unchanged."""
        execution_id = _ID.validate_python(execution_id, strict=True)
        limit = _PAGE_SIZE.validate_python(limit, strict=True)
        if cursor is not None:
            cursor = ToolStepCursor.model_validate(cursor)
        after = None if cursor is None else cursor.child_attempt_id
        params = (*_params(scope), execution_id)
        async with self._transaction() as connection:
            anchored = await self._execution(connection, scope, execution_id)
            if anchored is None:
                return None
            execution, _ = anchored
            revision = (
                None
                if cursor is None
                else await self._revision(
                    connection, queries.TOOL_REVISION, params, cursor.revision
                )
            )
            rows = await self._rows(
                connection, queries.TOOL_STEPS, (*params, after, after, limit + 1)
            )
            items = tuple(
                ToolStepSpan.model_validate(_utc(row)) for row in rows[:limit]
            )
            next_cursor = None
            if len(rows) > limit:
                revision = revision or await self._revision(
                    connection, queries.TOOL_REVISION, params, None
                )
                next_cursor = ToolStepCursor(
                    child_attempt_id=items[-1].child_attempt_id, revision=revision
                )
            return _bounded(
                ToolStepPage(
                    task_id=execution.task_id,
                    execution_id=execution_id,
                    trace_id=execution.trace_id,
                    items=items,
                    next_cursor=next_cursor,
                )
            )

    async def metrics(self, scope: IdempotencyScope) -> bytes:
        """Snapshot measurements of this owner without raw labels or payloads."""
        params = _params(scope)
        async with self._transaction() as connection:
            counts = await self._rows(connection, queries.COUNTS, params)
            timings = await self._rows(connection, queries.HISTOGRAMS, params)
            return render_metrics(counts, timings)

    async def readiness(self, scope: IdempotencyScope) -> Readiness:
        """Distinguish connectivity, required read schema, and unobserved worker state."""
        database: Availability = "unavailable"
        try:
            async with self._transaction() as connection:
                await self._rows(connection, "SELECT 1")
                database = "ready"
                owner = _params(scope)
                selected = (*owner, "readiness-probe")
                assumed_call = (*selected, None, None)
                for query, params in (
                    (queries.EXECUTION, selected),
                    (queries.EVENTS, (*selected, 0, 0, 1)),
                    (queries.CALLS, (*assumed_call, None, None, None, 1)),
                    (queries.CALL_REVISION, assumed_call),
                    (queries.TOOL_STEPS, (*selected, None, None, 1)),
                    (queries.TOOL_REVISION, selected),
                    (queries.COUNTS, owner),
                    (queries.HISTOGRAMS, owner),
                ):
                    await self._rows(
                        connection,
                        f"SELECT * FROM ({query}) AS projection LIMIT 0",
                        params,
                    )
        except ObservabilityUnavailable:
            return Readiness(
                database=database, workflow_schema="unavailable", worker_claim="unknown"
            )
        worker: Availability = "unknown"
        if self._worker_claim_probe is not None:
            try:
                async with timeout(1):
                    worker = _AVAILABILITY.validate_python(
                        await self._worker_claim_probe(scope)
                    )
            except Exception:
                worker = "unavailable"
        return Readiness(
            database=database, workflow_schema="ready", worker_claim=worker
        )
