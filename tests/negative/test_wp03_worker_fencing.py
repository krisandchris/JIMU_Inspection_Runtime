"""Public Worker recovery, provider failures, heartbeat and stale result fencing."""

import asyncio
from pathlib import Path

from sqlalchemy import text

from jimu_runtime.domain.enums import (
    AttemptStatus,
    ExecutionStatus,
    NodeRunStatus,
    TaskStatus,
)
from jimu_runtime.domain.runtime.executions import Attempt, NodeId, NodeRun
from jimu_runtime.domain.runtime.intake import AcceptedTask, IdempotencyScope
from jimu_runtime.workflow import repository
from jimu_runtime.workflow.engine import BranchKey
from jimu_runtime.workflow.intake import IntakeHostUnavailable, IntakeService
from jimu_runtime.workflow.worker import (
    RetryableNodeFailure,
    WorkerStep,
    WorkflowWorker,
)
from jimu_runtime.workflow.worker_nodes import BranchInputs, NodeResult, invoke
from jimu_runtime.workflow.worker_source import OpenExecution, WorkerSource
from tests.fixtures.wp02.compiler.scenario import LIMITS
from tests.fixtures.wp03.intake.support import migrated_application
from tests.fixtures.wp03.worker.support import (
    SCHEDULING,
    FixtureWorkerHost,
    accepted_worker,
    open_worker_fixture,
    worker_submission,
)


def test_worker_logs_late_response_after_cancellation_without_authority(
    tmp_path: Path,
) -> None:
    async def run() -> None:
        async with migrated_application() as engine:
            worker, accepted, host = await accepted_worker(engine, tmp_path)
            scope, eid = (
                accepted.task.idempotency_scope,
                accepted.execution.execution_id,
            )
            assert (
                await worker.run_next(scope, owner="fixture", lease_ms=1000)
            ).step is WorkerStep.APPLIED
            arrived, release = asyncio.Event(), asyncio.Event()

            async def delayed(
                opened: OpenExecution,
                inputs: BranchInputs,
                key: BranchKey,
                node: NodeRun,
                attempt: Attempt,
            ) -> NodeResult:
                result = invoke(opened, inputs, key, node, attempt)
                assert key.node_id is NodeId.N02
                arrived.set()
                await release.wait()
                return result

            store, manager, _ = open_worker_fixture(tmp_path, host)
            delayed_worker = WorkflowWorker(
                engine,
                manager,
                store,
                host=host,
                bundle_limits=LIMITS,
                invoke_node=delayed,
            )
            old = asyncio.create_task(
                delayed_worker.run_once(
                    scope, eid, owner="fixture.original", lease_ms=300
                )
            )
            try:
                await asyncio.wait_for(arrived.wait(), timeout=15)
                # Several renewal periods elapse while the valid handler remains busy.
                await asyncio.sleep(0.48)
                assert (
                    await worker.run_next(
                        scope, owner="fixture.contender", lease_ms=1000
                    )
                ).step is WorkerStep.IDLE
                assert await worker.request_cancel(scope, eid)
                assert (
                    await worker.run_next(
                        scope, owner="fixture.contender", lease_ms=1000
                    )
                ).step is WorkerStep.CANCELLED
            finally:
                release.set()
            assert await old is WorkerStep.LATE_RECORDED
            assert await worker.read_finalization(scope, eid) is None
            async with engine.connect() as conn:
                state = await repository.get_execution(conn, scope, eid)
                assert (
                    state is not None
                    and state.execution.status is ExecutionStatus.CANCELLED
                )
                assert state.task_status is TaskStatus.CANCELLED
                assert len(state.execution.model_invocation_refs) == 0
                node = next(
                    item
                    for item in await repository.list_nodes(conn, scope, eid)
                    if item.node_id is NodeId.N02
                )
                assert (
                    node.status is NodeRunStatus.CANCELLED
                    and len(node.attempt_history) == 1
                )
                attempt = await repository.get_attempt(
                    conn,
                    scope,
                    eid,
                    node.node_run_id,
                    node.attempt_history[0].attempt_ref.record_id,
                )
                assert attempt is not None and attempt.status is AttemptStatus.CANCELLED
                late = (
                    (
                        await conn.execute(
                            text(
                                "SELECT external_request_id FROM jimu_workflow.late_responses"
                            )
                        )
                    )
                    .scalars()
                    .all()
                )
                assert len(late) == 1 and late[0].startswith("synthetic.request.")
                assert (
                    await conn.execute(
                        text("SELECT count(*) FROM jimu_workflow.authority_outputs")
                    )
                ).scalar_one() == 1
                assert (
                    await conn.execute(
                        text(
                            "SELECT count(*) FROM jimu_workflow.produced_outputs WHERE output_kind IN ('late_artifact','late_result')"
                        )
                    )
                ).scalar_one() == 2

    asyncio.run(run())


def test_worker_retry_budget_and_fenced_invalid_result(tmp_path: Path) -> None:
    async def run() -> None:
        async with migrated_application() as engine:
            worker, accepted, host = await accepted_worker(engine, tmp_path)
            scope, eid = (
                accepted.task.idempotency_scope,
                accepted.execution.execution_id,
            )
            foreign = IdempotencyScope(
                project_id=scope.project_id, client_id="another-client"
            )
            assert (
                await worker.run_next(foreign, owner="fixture", lease_ms=1000)
            ).step is WorkerStep.IDLE
            assert (
                await worker.run_once(foreign, eid, owner="fixture", lease_ms=1000)
                is WorkerStep.IDLE
            )
            assert (
                await worker.run_next(scope, owner="fixture", lease_ms=1000)
            ).step is WorkerStep.APPLIED

            async def unreliable(
                opened: OpenExecution,
                inputs: BranchInputs,
                key: BranchKey,
                node: NodeRun,
                attempt: Attempt,
            ) -> NodeResult:
                assert key.node_id is NodeId.N02
                if attempt.attempt_number == 1:
                    raise RetryableNodeFailure("transport")
                correct = invoke(opened, inputs, key, node, attempt)
                return NodeResult(correct.message, (), ())  # invalid missing artifacts

            store, manager, _ = open_worker_fixture(tmp_path, host)
            ad_hoc = WorkflowWorker(
                engine,
                manager,
                store,
                host=host,
                bundle_limits=LIMITS,
                invoke_node=unreliable,
            )
            assert (
                await ad_hoc.run_next(scope, owner="fixture.retry", lease_ms=1000)
            ).step is WorkerStep.RETRY_SCHEDULED
            await asyncio.sleep(0.10)
            assert (
                await ad_hoc.run_next(scope, owner="fixture.retry", lease_ms=1000)
            ).step is WorkerStep.APPLIED
            assert (
                await worker.run_next(scope, owner="fixture", lease_ms=1000)
            ).step is WorkerStep.FINALIZED
            final = await worker.read_finalization(scope, eid)
            assert final is not None and final.decisions == ()
            async with engine.connect() as conn:
                state = await repository.get_execution(conn, scope, eid)
                assert (
                    state is not None
                    and state.execution.status is ExecutionStatus.FAILED
                )
                node = next(
                    item
                    for item in await repository.list_nodes(conn, scope, eid)
                    if item.node_id is NodeId.N02
                )
                assert (
                    node.status is NodeRunStatus.FAILED
                    and len(node.attempt_history) == 2
                )
                codes = []
                for link in node.attempt_history:
                    attempt = await repository.get_attempt(
                        conn, scope, eid, node.node_run_id, link.attempt_ref.record_id
                    )
                    assert (
                        attempt is not None
                        and attempt.reason is not None
                        and attempt.status is AttemptStatus.FAILED
                    )
                    codes.append(attempt.reason.code)
                assert codes == [
                    "worker_provider_transport",
                    "worker_provider_invalid_output",
                ]
                assert (
                    await conn.execute(
                        text("SELECT count(*) FROM jimu_workflow.authority_outputs")
                    )
                ).scalar_one() == 1

    asyncio.run(run())


def test_scope_poll_reports_blocked_source_and_continues_by_cursor(
    tmp_path: Path,
) -> None:
    async def run() -> None:
        async with migrated_application() as engine:
            _, first, host = await accepted_worker(engine, tmp_path)
            scope = first.task.idempotency_scope
            store, manager, _ = open_worker_fixture(tmp_path, host)
            intake = IntakeService(
                engine,
                manager,
                store,
                host=host,
                bundle_limits=LIMITS,
                scheduling_limits=SCHEDULING,
                max_submission_bytes=64 * 1024,
            )
            second = await intake.submit(
                worker_submission("worker-2"), principal=host.release_host.task_client
            )
            blocked, healthy = sorted(
                (first, second), key=lambda entry: entry.execution.execution_id
            )

            class SourceHost(FixtureWorkerHost):
                async def worker_source(
                    self, scope: IdempotencyScope, task: AcceptedTask
                ) -> WorkerSource:
                    if task.task_id == blocked.task.task_id:
                        raise IntakeHostUnavailable()
                    return await super().worker_source(scope, task)

            worker = WorkflowWorker(
                engine,
                manager,
                store,
                host=SourceHost(host.release_host),
                bundle_limits=LIMITS,
            )
            poll = await worker.run_next(scope, owner="fixture", lease_ms=1000, limit=1)
            assert poll.step is WorkerStep.IDLE
            assert poll.after_execution_id == blocked.execution.execution_id
            (issue,) = poll.issues
            assert issue.execution_id == blocked.execution.execution_id
            assert issue.retryable and issue.code == "worker_source_unavailable"
            continuation = await worker.run_next(
                scope,
                owner="fixture",
                lease_ms=1000,
                after_execution_id=poll.after_execution_id,
            )
            assert continuation.step is WorkerStep.APPLIED
            assert continuation.execution_id == healthy.execution.execution_id
            assert not continuation.issues
            async with engine.connect() as conn:
                (waiting,) = await repository.list_nodes(
                    conn, scope, blocked.execution.execution_id
                )
                assert (
                    not waiting.attempt_history
                    and waiting.status is NodeRunStatus.READY
                )

    asyncio.run(run())
