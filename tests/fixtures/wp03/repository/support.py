"""Synthetic, versioned repository records and an isolated workflow database."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime

from fixtures.wp01c.executions.support import execution, node
from sqlalchemy.ext.asyncio import AsyncEngine

from jimu_runtime.domain.enums import NodeRunStatus, TaskStatus
from jimu_runtime.domain.runtime.executions import ExecutionRecord, NodeId, NodeRun
from jimu_runtime.domain.runtime.intake import AcceptedTask, TaskSubmission
from jimu_runtime.workflow import repository
from jimu_runtime.workflow.postgres import apply_migrations, create_postgres_engine
from jimu_runtime.workflow.scheduling import JobBudget, SchedulingLimits
from tests.fixtures.wp03.postgres.support import isolated_postgres

NOW = datetime(2026, 11, 18, 3, tzinfo=UTC)
LIMITS = SchedulingLimits(
    schema_version="1.0.0",
    version="1.0.0",
    overrides=(),
    default=JobBudget(
        max_attempts=2,
        timeout_ms=100,
        retry_backoff_ms=(1,),
        retryable_error_classes=("timeout",),
        retry_on_lease_loss=True,
    ),
)


@asynccontextmanager
async def database() -> AsyncIterator[AsyncEngine]:
    with isolated_postgres() as postgres:
        owner = create_postgres_engine(postgres.migration_url)
        engine = create_postgres_engine(postgres.application_url)
        try:
            await apply_migrations(owner, postgres.target)
            yield engine
        finally:
            await engine.dispose()
            await owner.dispose()


def records(
    submission: TaskSubmission,
) -> tuple[AcceptedTask, ExecutionRecord, NodeRun, repository.NewJob]:
    suffix = submission.request_id
    draft: ExecutionRecord = execution()
    config: NodeRun = node()
    run = draft.model_copy(
        update={
            "execution_id": f"execution.{suffix}",
            "task_id": f"task.{suffix}",
            "trace": submission.trace,
            "created_at": NOW,
        }
    )
    task = AcceptedTask(
        schema_version="1.0.0",
        task_id=run.task_id,
        request_id=submission.request_id,
        idempotency_scope=submission.idempotency_scope,
        idempotency_key=submission.idempotency_key,
        context=submission.context,
        media_ref=submission.media_ref,
        release_id=run.pins.release_id,
        bundle_digest=run.pins.bundle_digest,
        trace=submission.trace,
        accepted_at=NOW,
        status=TaskStatus.ACCEPTED,
    )
    root = config.model_copy(
        update={
            "node_run_id": f"node.{suffix}",
            "execution_id": run.execution_id,
            "status": NodeRunStatus.PENDING,
            "node_id": NodeId.N01,
            "started_at": None,
            "attempt_history": (),
            "trace": submission.trace,
            "created_at": NOW,
            "upstream_inputs": (),
            "input_digest": run.pins.input_digest,
        }
    )
    return task, run, root, repository.NewJob(f"job.{suffix}", NOW, LIMITS.default)


async def accept(
    engine: AsyncEngine,
    submission: TaskSubmission,
) -> tuple[repository.LockedExecution, NodeRun, repository.NewJob]:
    scope = submission.idempotency_scope
    async with engine.begin() as connection:
        intent = await repository.reserve_intent(
            connection,
            scope,
            submission,
            issuer="fixture",
            subject="client",
        )
    task, run, root, job = records(submission)
    async with engine.begin() as connection:
        accepted = await repository.accept_intent(
            connection,
            scope,
            intent,
            task,
            run,
            root.config_ref,
            LIMITS,
            root,
            job,
        )
        persisted = await repository.get_node(
            connection, scope, run.execution_id, root.node_run_id
        )
    assert persisted is not None
    return accepted, persisted, job
