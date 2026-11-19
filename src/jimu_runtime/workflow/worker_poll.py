"""Discover scope-owned executable, reclaimable, cancelled or staged work."""

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from jimu_runtime.domain.runtime.intake import IdempotencyScope
from jimu_runtime.workflow.jobs import OBSERVATION_WAIT_SETTLED_SQL


async def runnable_executions(
    connection: AsyncConnection,
    scope: IdempotencyScope,
    after_execution_id: str | None,
    limit: int,
) -> tuple[str, ...]:
    result = await connection.execute(
        text(f"""
        WITH observed_clock AS (SELECT pg_catalog.clock_timestamp() AS observed)
        SELECT e.execution_id
        FROM jimu_workflow.executions e CROSS JOIN observed_clock t
        WHERE e.project_id = :project_id AND e.client_id = :client_id
          AND e.status IN ('initialized','running')
          AND (CAST(:after AS text) IS NULL OR e.execution_id > :after)
          AND (
            e.cancel_requested_at IS NOT NULL
            OR EXISTS (
                SELECT 1 FROM jimu_workflow.jobs j
                WHERE (j.project_id, j.client_id, j.execution_id) =
                      (e.project_id, e.client_id, e.execution_id)
                  AND NOT j.internal_tool_step
                  AND {OBSERVATION_WAIT_SETTLED_SQL}
                  AND (
                    (j.status = 'ready' AND j.ready_at <= t.observed)
                    OR (j.status = 'leased' AND
                        (j.lease_until <= t.observed OR j.attempt_deadline <= t.observed))
                  )
            )
            OR (
                e.status = 'running'
                AND NOT EXISTS (
                    SELECT 1 FROM jimu_workflow.jobs j
                    WHERE (j.project_id, j.client_id, j.execution_id) =
                          (e.project_id, e.client_id, e.execution_id)
                      AND NOT j.internal_tool_step
                      AND j.status IN ('ready', 'leased')
                )
            )
          )
        ORDER BY e.execution_id
        LIMIT :limit
        """),
        {
            "project_id": scope.project_id,
            "client_id": scope.client_id,
            "after": after_execution_id,
            "limit": limit,
        },
    )
    return tuple(result.scalars())
