"""Recover a worker killed after committing its only Attempt for a node."""

import asyncio
import multiprocessing
import signal
from multiprocessing.connection import Connection
from pathlib import Path

import pytest
from sqlalchemy import URL, text

from jimu_runtime.domain.enums import AttemptStatus, DecisionResult, NodeRunStatus
from jimu_runtime.domain.runtime.coverage import CoverageReason, CoverageStatus
from jimu_runtime.domain.runtime.executions import Attempt, NodeId
from jimu_runtime.domain.runtime.intake import IdempotencyScope
from jimu_runtime.workflow import repository
from jimu_runtime.workflow.jobs import JobClaim
from jimu_runtime.workflow.postgres import create_postgres_engine
from jimu_runtime.workflow.scheduling import JobBudget, NodeBudgetOverride
from jimu_runtime.workflow.worker import WorkerStep, WorkflowWorker
from tests.fixtures.wp02.compiler.scenario import LIMITS
from tests.fixtures.wp03.intake.support import migrated_application
from tests.fixtures.wp03.jobs.support import inspect, wait_for_db_time
from tests.fixtures.wp03.worker.support import (
    SCHEDULING,
    accepted_worker,
    open_worker_fixture,
)


def _stop_at_committed_attempt(
    url: URL,
    ledger: Path,
    scope: IdempotencyScope,
    execution_id: str,
    ready: Connection,
) -> None:
    async def run() -> None:
        engine = create_postgres_engine(url)
        try:
            store, manager, host = open_worker_fixture(ledger)
            worker = WorkflowWorker(
                engine, manager, store, host=host, bundle_limits=LIMITS
            )

            async def pause(claim: JobClaim, attempt: Attempt) -> None:
                ready.send((claim, attempt))
                await asyncio.Event().wait()

            await worker.run_once(
                scope,
                execution_id,
                owner="fixture.crash",
                lease_ms=1000,
                after_start=pause,
            )
        finally:
            ready.close()
            await engine.dispose()

    asyncio.run(run())


@pytest.mark.parametrize(
    "interrupted,setup_steps,coverage_status,coverage_reason",
    [
        (NodeId.N06, 5, CoverageStatus.PARTIAL, CoverageReason.DECISION_NOT_EVALUATED),
        (NodeId.N02, 1, CoverageStatus.NONE, CoverageReason.DISCOVERY_NOT_EVALUATED),
    ],
)
def test_killed_worker_with_exhausted_budget_preserves_actual_superseded_attempt(
    tmp_path: Path,
    interrupted: NodeId,
    setup_steps: int,
    coverage_status: CoverageStatus,
    coverage_reason: CoverageReason,
) -> None:
    async def run() -> None:
        one_attempt = JobBudget(
            max_attempts=1,
            timeout_ms=10_000,
            retry_backoff_ms=(),
            retryable_error_classes=(),
            retry_on_lease_loss=True,
        )
        scheduling = SCHEDULING.model_copy(
            update={
                "overrides": (
                    NodeBudgetOverride(node_id=interrupted, budget=one_attempt),
                ),
            }
        )
        async with migrated_application() as engine:
            worker, accepted, _ = await accepted_worker(
                engine, tmp_path, scheduling=scheduling
            )
            scope, eid = (
                accepted.task.idempotency_scope,
                accepted.execution.execution_id,
            )
            for _ in range(setup_steps):
                assert (
                    await worker.run_next(scope, owner="fixture.setup", lease_ms=1000)
                ).step is WorkerStep.APPLIED
            async with engine.connect() as conn:
                (node,) = tuple(
                    item
                    for item in await repository.list_nodes(conn, scope, eid)
                    if item.node_id is interrupted
                )
                assert node.status is NodeRunStatus.READY
            context = multiprocessing.get_context("spawn")
            receive, send = context.Pipe(duplex=False)
            process = context.Process(
                target=_stop_at_committed_attempt,
                args=(engine.url, tmp_path, scope, eid, send),
            )
            process.start()
            send.close()
            try:
                assert await asyncio.to_thread(receive.poll, 15), (
                    "crash worker never started the selected node"
                )
                claim, started = receive.recv()
                assert claim.node_run_id == node.node_run_id
                assert started.status is AttemptStatus.RUNNING
                process.kill()
                await asyncio.to_thread(process.join, 10)
                assert process.exitcode == -signal.SIGKILL
            finally:
                if process.is_alive():
                    process.kill()
                    await asyncio.to_thread(process.join, 10)
                receive.close()

            async with engine.connect() as conn:
                lease_until = await conn.scalar(
                    text("""
                        SELECT lease_until FROM jimu_workflow.jobs
                        WHERE project_id = :p AND client_id = :c AND execution_id = :e
                          AND job_id = :job AND fence = :f
                    """),
                    {
                        "p": scope.project_id,
                        "c": scope.client_id,
                        "e": eid,
                        "job": claim.job_id,
                        "f": claim.fence,
                    },
                )
                assert lease_until is not None
            await wait_for_db_time(engine, lease_until)
            restarted = await worker.run_next(
                scope, owner="fixture.restart", lease_ms=1000
            )
            assert not restarted.issues
            assert restarted.step is WorkerStep.FINALIZED

            final = await worker.read_finalization(scope, eid)
            assert final is not None and not final.decisions
            assert final.coverage.summary_result is DecisionResult.UNCERTAIN
            assert final.coverage.coverage_status is coverage_status
            assert coverage_reason in final.coverage.reasons
            async with engine.connect() as conn:
                failed_node = await repository.get_node(
                    conn, scope, eid, node.node_run_id
                )
                terminal = await repository.get_attempt(
                    conn, scope, eid, node.node_run_id, started.attempt_id
                )
                assert failed_node is not None and terminal is not None
                assert failed_node.status is NodeRunStatus.FAILED
                assert terminal.status is AttemptStatus.SUPERSEDED
                assert failed_node.reason == terminal.reason
                assert (
                    terminal.reason is not None
                    and terminal.reason.code == "lease_expired"
                )
                assert len(failed_node.attempt_history) == 1
                assert failed_node.ended_at == terminal.ended_at
            audit = await inspect(engine, scope, eid)
            assert (started.attempt_id, "superseded") in audit["attempts"]
            assert all(item[0] != node.node_run_id for item in audit["authority"])
            assert not audit["drafts"]
            assert len(audit["finalizations"]) == 1

    asyncio.run(run())
