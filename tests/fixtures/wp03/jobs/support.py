"""Synthetic job scenarios run through the application role of an isolated PG."""

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, timedelta
from pathlib import Path

from sqlalchemy import text

from jimu_runtime.domain.base import ContractModel
from jimu_runtime.domain.enums import (
    ExecutionStatus,
    NodeRunStatus,
    RuntimeRecordKind,
    TaskStatus,
)
from jimu_runtime.domain.nodes.envelopes import (
    ArtifactOutputRef,
    FailedNodeOutput,
    NodeError,
    SucceededNodeOutput,
)
from jimu_runtime.domain.references import RuntimeRecordRef
from jimu_runtime.domain.runtime.executions import NodeId, PinnedRuntimeInput
from jimu_runtime.domain.runtime.intake import AcceptedTask, TaskSubmission
from jimu_runtime.domain.validation.transitions import NodeTransitionGuard
from jimu_runtime.workflow import repository as repo
from jimu_runtime.workflow.jobs import DurableJobs, ResponseEvidence
from jimu_runtime.workflow.postgres import apply_migrations, create_postgres_engine
from jimu_runtime.workflow.scheduling import JobBudget, SchedulingLimits
from tests.fixtures.wp01c.executions.support import execution, node
from tests.fixtures.wp03.postgres.support import isolated_postgres


class FixtureRecord(ContractModel):
    schema_version: str
    value: str


def budget(
    *, attempts=2, timeout=12_000, lease_retry=True, errors=("transport", "timeout")
):
    return JobBudget(
        max_attempts=attempts,
        timeout_ms=timeout,
        retry_backoff_ms=(20,) * (attempts - 1),
        retryable_error_classes=errors,
        retry_on_lease_loss=lease_retry,
    )


@asynccontextmanager
async def database() -> AsyncIterator:
    with isolated_postgres() as fixture:
        owner = create_postgres_engine(fixture.migration_url)
        engine = create_postgres_engine(fixture.application_url)
        try:
            assert fixture.server_version.startswith("16.")
            await apply_migrations(owner, fixture.target)
            yield engine, fixture.application_url
        finally:
            await owner.dispose()
            await engine.dispose()


async def seed(engine, *, limits=None, suffix=""):
    limits = limits or SchedulingLimits(
        schema_version="1.0.0", version="1.0.0", default=budget(), overrides=()
    )
    path = Path(__file__).parents[2] / "wp01c/intake/accepted-task-valid.json"
    task = AcceptedTask.model_validate_json(path.read_bytes())
    if suffix:
        task = task.model_copy(
            update={
                "task_id": f"{task.task_id}-{suffix}",
                "request_id": f"{task.request_id}-{suffix}",
                "idempotency_key": f"{task.idempotency_key}-{suffix}",
            }
        )
    submission = TaskSubmission(
        schema_version=task.schema_version,
        request_id=task.request_id,
        idempotency_scope=task.idempotency_scope,
        idempotency_key=task.idempotency_key,
        media_ref=task.media_ref,
        context=task.context,
        trace=task.trace,
    )
    scope = task.idempotency_scope
    async with engine.begin() as connection:
        now = (await connection.scalar(text("SELECT clock_timestamp()"))).astimezone(
            UTC
        )
        stamp = now.isoformat()
        execution_id = f"exec-1-{suffix}" if suffix else "exec-1"
        record = execution(
            created_at=stamp, task_id=task.task_id, execution_id=execution_id
        )
        pins = record.pins.model_copy(
            update={"release_id": task.release_id, "bundle_digest": task.bundle_digest}
        )
        record = record.model_copy(update={"pins": pins})
        root = node(
            execution_id=record.execution_id,
            node_id="N01",
            status="pending",
            upstream_inputs=[],
            attempt_history=[],
            output_refs=[],
            created_at=stamp,
            started_at=None,
        )
        intent = await repo.reserve_intent(
            connection, scope, submission, issuer="test", subject="worker"
        )
        await repo.accept_intent(
            connection,
            scope,
            intent,
            task,
            record,
            root.config_ref,
            limits,
            root,
            repo.NewJob("job-root", now, limits.for_node(NodeId.N01)),
        )
    return scope, record.execution_id


def outcome(attempt, run, *, result="success"):
    elapsed = timedelta(milliseconds=1)
    fields = {
        "schema_version": "1.0.0",
        "node_id": run.node_id,
        "node_run_id": run.node_run_id,
        "attempt_id": attempt.attempt_id,
        "trace": run.trace,
        "started_at": attempt.started_at,
        "finished_at": attempt.started_at + elapsed,
        "duration_ms": 1,
    }
    if result == "success":
        return SucceededNodeOutput(
            **fields,
            status=NodeRunStatus.SUCCEEDED,
            output_artifacts=(
                ArtifactOutputRef(
                    ref=RuntimeRecordRef(
                        record_kind=RuntimeRecordKind.EVIDENCE,
                        record_id=f"out-{attempt.attempt_id}",
                    ),
                    digest="1" * 64,
                ),
            ),
        )
    return FailedNodeOutput(
        **fields,
        status=NodeRunStatus.FAILED,
        output_artifacts=(),
        error=NodeError(
            code=f"{result}_failure",
            error_class=result,
            retryable=True,
            message="Synthetic node failure",
            validation_issues=(),
        ),
    )


def response(attempt, key="reply-1", digest="2" * 64):
    return ResponseEvidence(
        schema_version="1.0.0",
        execution_id=attempt.execution_id,
        attempt_id=attempt.attempt_id,
        response_key=key,
        response=PinnedRuntimeInput(
            ref=RuntimeRecordRef(record_kind=RuntimeRecordKind.EVIDENCE, record_id=key),
            digest=digest,
        ),
        external_request_id="fixture/request-01",
    )


async def fetch_node(engine, scope, execution_id, node_run_id):
    async with engine.connect() as connection:
        run = await repo.get_node(connection, scope, execution_id, node_run_id)
        assert run is not None
        return run


async def inspect(engine, scope, execution_id):
    params = {"p": scope.project_id, "c": scope.client_id, "e": execution_id}
    statements = {
        "authority": "SELECT node_run_id, attempt_id FROM jimu_workflow.authority_outputs",
        "attempts": "SELECT attempt_id, status FROM jimu_workflow.attempt_results",
        "starts": "SELECT attempt_id, attempt_number FROM jimu_workflow.attempts",
        "jobs": "SELECT job_id, status, fence FROM jimu_workflow.jobs",
        "receipts": "SELECT attempt_id, fence FROM jimu_workflow.job_completions",
        "late": "SELECT attempt_id, response_key, external_request_id FROM jimu_workflow.late_responses",
        "drafts": "SELECT node_run_id, attempt_id FROM jimu_workflow.decision_drafts",
        "finalizations": "SELECT finalization_digest FROM jimu_workflow.execution_finalizations",
        "successors": "SELECT upstream_node_run_id, downstream_node_run_id FROM jimu_workflow.successors",
    }
    async with engine.connect() as connection:
        snapshots = {
            name: (
                await connection.execute(
                    text(
                        sql
                        + " WHERE project_id = :p AND client_id = :c AND execution_id = :e ORDER BY 1"
                    ),
                    params,
                )
            ).all()
            for name, sql in statements.items()
        }
        snapshots["events"] = await repo.list_events(connection, scope, execution_id)
        snapshots["execution"] = await repo.get_execution(
            connection, scope, execution_id
        )
        return snapshots


async def wait_for_db_time(engine, when):
    async with engine.connect() as connection:
        await connection.execute(
            text(
                "SELECT pg_sleep(GREATEST(0, EXTRACT(epoch FROM (:deadline - clock_timestamp()))))"
            ),
            {"deadline": when},
        )


async def branch(
    connection,
    scope,
    locked,
    prior,
    now,
    node_id,
    key,
    *,
    object_pin=None,
    branch_key=None,
):
    child_id = f"node-{key}"
    child = node(
        node_run_id=child_id,
        node_id=node_id.value,
        status="pending",
        upstream_inputs=[]
        if object_pin is None
        else [object_pin.model_dump(mode="json")],
        attempt_history=[],
        output_refs=[],
        created_at=now.isoformat(),
        started_at=None,
    )
    await repo.insert_node(
        connection,
        scope,
        locked.execution.execution_id,
        child,
        branch_key=branch_key or key,
    )
    ready = child.model_copy(update={"status": NodeRunStatus.READY})
    await repo.transition_node(
        connection,
        scope,
        locked.execution.execution_id,
        child,
        ready,
        now=now,
        guard=NodeTransitionGuard.PREREQUISITES_AVAILABLE,
    )
    await repo.insert_job(
        connection,
        scope,
        locked.execution.execution_id,
        ready.node_run_id,
        repo.NewJob(f"job-{key}", now, locked.scheduling_limits.for_node(node_id)),
    )
    assert await repo.link_successor(
        connection,
        scope,
        locked.execution.execution_id,
        prior.node_run_id,
        ready.node_run_id,
    )


async def apply_finalization(connection, scope, locked, staged, now):
    record = locked.execution
    nodes = await repo.list_nodes(connection, scope, record.execution_id)
    partial = any(item.status is NodeRunStatus.FAILED for item in nodes)
    await repo.store_finalization(
        connection,
        scope,
        record.execution_id,
        FixtureRecord(schema_version="1.0.0", value="synthetic-finalization"),
    )
    await repo.transition_execution(
        connection,
        scope,
        record,
        record.model_copy(
            update={
                "status": ExecutionStatus.PARTIAL_FAILED
                if partial
                else ExecutionStatus.COMPLETED,
                "ended_at": now,
            }
        ),
        now=now,
    )
    await repo.transition_task(
        connection,
        scope,
        record.execution_id,
        record.task_id,
        locked.task_status,
        TaskStatus.PARTIAL_FAILED if partial else TaskStatus.COMPLETED,
        now=now,
        trace=record.trace,
    )
    return True


def process_worker(url, scope, execution_id, owner, action, channel, go):
    """A killable worker, with explicit barriers and an independent PG connection."""

    async def work():
        engine = create_postgres_engine(url)
        try:

            async def finished(connection, scope, locked, run, attempt, outcome, now):
                if run.status is NodeRunStatus.SUCCEEDED:
                    await branch(
                        connection, scope, locked, run, now, NodeId.N02, "next"
                    )
                if action == "crash-in-completion":
                    channel.send((claim, attempt))
                    await asyncio.Event().wait()

            scheduler = DurableJobs(engine, on_terminal=finished)
            channel.send("ready")
            if not go.wait(15):
                raise AssertionError("worker barrier timed out")
            if action == "cancel-intent":
                channel.send(await scheduler.request_cancel(scope, execution_id))
                await asyncio.Event().wait()
                return
            if action == "finalize-crash":

                async def persist(*args):
                    assert await apply_finalization(*args)
                    channel.send("aggregate-uncommitted")
                    await asyncio.Event().wait()

                await scheduler.finalize_staged(scope, execution_id, persist)
                return
            if action == "finalize-commit":
                result = await scheduler.finalize_staged(
                    scope, execution_id, apply_finalization
                )
                channel.send(result)
                await asyncio.Event().wait()
                return
            claim = await scheduler.claim(scope, owner, 1000, execution_id=execution_id)
            if claim is None:
                channel.send(None)
                return
            attempt = await scheduler.start(claim)
            if action == "report":
                channel.send((claim, attempt))
                return
            run = await fetch_node(engine, scope, execution_id, claim.node_run_id)
            completed = await scheduler.complete(claim, outcome(attempt, run))
            channel.send((claim, attempt, completed))
        finally:
            await engine.dispose()

    asyncio.run(work())
