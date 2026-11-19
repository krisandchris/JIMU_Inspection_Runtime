"""Read the primary Task and page the append-only Execution trace."""

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from jimu_runtime.api.boundary import ApiError
from jimu_runtime.api.contracts import TaskView, TracePage
from jimu_runtime.domain.runtime.executions import ExecutionRecord
from jimu_runtime.domain.runtime.intake import IdempotencyScope
from jimu_runtime.observability.contracts import EventCursor
from jimu_runtime.observability.postgres import (
    ObservabilityUnavailable,
    PostgresObservability,
)
from jimu_runtime.workflow import repository
from jimu_runtime.workflow.engine import ExecutionFinalization


class RuntimeRecords:
    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine
        self._observability = PostgresObservability(engine)

    async def primary(self, scope: IdempotencyScope, task_id: str) -> TaskView | None:
        async with self._engine.connect() as connection:
            execution_id = (
                await connection.execute(
                    text("""
                        SELECT execution_id FROM jimu_workflow.intake_intents
                        WHERE project_id = :project_id AND client_id = :client_id
                          AND task_id = :task_id AND status = 'accepted'
                        LIMIT 2
                    """),
                    {**scope.model_dump(), "task_id": task_id},
                )
            ).scalar_one_or_none()
            if execution_id is None:
                return None
            row = await repository.get_execution(connection, scope, execution_id)
        if row is None or row.task.task_id != task_id:
            raise ApiError(500, "primary_execution_mismatch")
        return TaskView(
            task=row.task,
            task_status=row.task_status,
            execution=row.execution,
            cancel_requested_at=row.cancel_requested_at,
        )

    async def execution(
        self, scope: IdempotencyScope, execution_id: str
    ) -> repository.LockedExecution | None:
        async with self._engine.connect() as connection:
            return await repository.get_execution(connection, scope, execution_id)

    async def finalization(
        self, scope: IdempotencyScope, selected: TaskView
    ) -> ExecutionFinalization | None:
        execution = selected.execution
        async with self._engine.connect() as connection:
            record = await repository.read_finalization(
                connection, scope, execution.execution_id, ExecutionFinalization
            )
        if record is not None and (
            record.execution_id,
            record.coverage.release_id,
            record.coverage.bundle_digest,
        ) != (
            execution.execution_id,
            execution.pins.release_id,
            execution.pins.bundle_digest,
        ):
            raise ApiError(500, "finalization_scope_mismatch")
        return record

    async def trace(
        self,
        scope: IdempotencyScope,
        execution: ExecutionRecord,
        *,
        cursor: EventCursor,
        limit: int,
        byte_limit: int,
    ) -> bytes:
        try:
            page = await self._observability.events(
                scope, execution.execution_id, cursor=cursor, limit=limit
            )
        except ObservabilityUnavailable as error:
            if str(error) == "observability_event_sequence_invalid":
                raise ApiError(500, "trace_event_scope_mismatch") from None
            if str(error) == "observability_trace_limit":
                raise ApiError(413, "trace_event_too_large") from None
            raise ApiError(503, "runtime_unavailable") from None
        except ValueError as error:
            if str(error) == "event cursor is outside retained execution history":
                raise ApiError(422, "invalid_trace_cursor") from None
            raise
        if page is None:
            raise ApiError(404, "resource_not_found")
        if (
            page.execution.execution_id != execution.execution_id
            or page.execution.task_id != execution.task_id
            or page.execution.trace_id != execution.trace.trace_id
        ):
            raise ApiError(500, "trace_event_scope_mismatch")
        return _encode_trace_page(
            TracePage(
                execution_id=page.execution.execution_id,
                trace_id=page.execution.trace_id,
                through_event_number=page.through,
                events=page.items,
                next_after_event_number=(
                    page.next_cursor.after if page.next_cursor is not None else None
                ),
            ),
            byte_limit,
        )


def _encode_trace_page(page: TracePage, byte_limit: int) -> bytes:
    """Encode each allowlisted event once, retaining the snapshot on byte limits."""
    header = (
        page.model_dump_json(exclude={"events", "next_after_event_number"}).encode(
            "utf-8"
        )[:-1]
        + b',"events":['
    )
    trailer = b'],"next_after_event_number":'
    cursor = b"null"
    events = bytearray()
    base_bytes = len(header) + len(trailer) + 1  # Closing object brace.
    for event in page.events:
        encoded = event.model_dump_json().encode("utf-8")
        next_cursor = (
            str(event.number).encode("ascii")
            if event.number < page.through_event_number
            else b"null"
        )
        size = base_bytes + len(events) + bool(events) + len(encoded) + len(next_cursor)
        if size > byte_limit:
            if not events:
                raise ApiError(413, "trace_event_too_large")
            break
        if events:
            events.extend(b",")
        events.extend(encoded)
        cursor = next_cursor
    return b"".join((header, events, trailer, cursor, b"}"))
