"""End-to-end WP-03 Worker using a new PostgreSQL database and offline providers."""

import asyncio
from pathlib import Path

import pytest
from sqlalchemy import text

from jimu_runtime.domain.enums import DecisionResult, ExecutionStatus, TaskStatus
from jimu_runtime.domain.runtime.coverage import CoverageStatus
from jimu_runtime.domain.runtime.executions import NodeId
from jimu_runtime.workflow import repository
from jimu_runtime.workflow.mock_perception import SyntheticGroundingCase
from jimu_runtime.workflow.worker import WorkerStep
from jimu_runtime.workflow.worker_records import (
    MESSAGE_KEY,
    NODE_MESSAGE,
    DiscoveryMessage,
)
from jimu_runtime.workflow.worker_source import SyntheticObjectCase, SyntheticScenario
from tests.fixtures.wp03.intake.support import migrated_application
from tests.fixtures.wp03.worker.support import accepted_worker


def test_worker_publishes_only_after_durable_staging(tmp_path: Path) -> None:
    async def run() -> None:
        async with migrated_application() as engine:
            worker, accepted, _ = await accepted_worker(engine, tmp_path)
            scope, eid = (
                accepted.task.idempotency_scope,
                accepted.execution.execution_id,
            )
            actions = []
            for _ in range(6):
                actions.append(
                    await worker.run_once(scope, eid, owner="fixture", lease_ms=1000)
                )
                assert await worker.read_finalization(scope, eid) is None
            assert actions == [WorkerStep.APPLIED] * 5 + [WorkerStep.STAGED]
            assert await worker.pending_finalizations(scope) == (eid,)
            assert (
                await worker.run_once(
                    scope, eid, owner="fixture.restart", lease_ms=1000
                )
                is WorkerStep.FINALIZED
            )
            final = await worker.read_finalization(scope, eid)
            assert final is not None
            assert final.coverage.object_count == 1
            assert final.coverage.summary_result is DecisionResult.VIOLATION
            assert len(final.decisions) == 1
            async with engine.connect() as conn:
                durable = await repository.get_execution(conn, scope, eid)
                runs = await repository.list_nodes(conn, scope, eid)
                assert durable is not None
                assert durable.execution.status is ExecutionStatus.COMPLETED
                assert durable.task_status is TaskStatus.COMPLETED
                assert {node.node_id for node in runs} == set(NodeId)
                assert len(durable.execution.model_invocation_refs) == 4
                assert all(len(node.attempt_history) == 1 for node in runs)
                assert (
                    await conn.execute(
                        text(
                            "SELECT count(*) FROM jimu_workflow.execution_finalizations"
                        )
                    )
                ).scalar_one() == 1
            assert (
                await worker.run_once(scope, eid, owner="fixture", lease_ms=1000)
                is WorkerStep.IDLE
            )

    asyncio.run(run())


def test_concurrent_scope_workers_share_coverage_for_multiple_objects(
    tmp_path: Path,
) -> None:
    async def run() -> None:
        async with migrated_application() as engine:
            worker, accepted, host = await accepted_worker(
                engine, tmp_path, second_profile=True, second_question=True
            )
            scope, eid = (
                accepted.task.idempotency_scope,
                accepted.execution.execution_id,
            )
            for _ in range(2):
                assert (
                    await worker.run_next(scope, owner="fixture.setup", lease_ms=1000)
                ).step is WorkerStep.APPLIED
            async with engine.connect() as conn:
                node = next(
                    item
                    for item in await repository.list_nodes(conn, scope, eid)
                    if item.node_id is NodeId.N02
                )
                discovery = await repository.read_output(
                    conn,
                    scope,
                    eid,
                    node.node_run_id,
                    node.attempt_history[-1].attempt_ref.record_id,
                    MESSAGE_KEY,
                    NODE_MESSAGE,
                )
                assert isinstance(discovery, DiscoveryMessage)
                left, right = (
                    item.profile_ref for item in discovery.output.profile_hits
                )
            host.scenario = SyntheticScenario(
                grounding=(
                    SyntheticGroundingCase(profile_ref=left, object_count=2),
                    SyntheticGroundingCase(profile_ref=right, object_count=1),
                ),
                objects=(SyntheticObjectCase(right, 0, outside=None),),
            )
            for _ in range(20):
                results = await asyncio.gather(
                    worker.run_next(scope, owner="fixture.left", lease_ms=1000),
                    worker.run_next(scope, owner="fixture.right", lease_ms=1000),
                )
                assert all(not result.issues for result in results)
                if any(result.step is WorkerStep.FINALIZED for result in results):
                    break
            final = await worker.read_finalization(scope, eid)
            assert final is not None and final.coverage.object_count == 3
            assert len(final.decisions) == 3
            assert {item.execution_coverage_ref for item in final.decisions} == {
                final.coverage.coverage_ref
            }
            assert final.coverage.coverage_status is CoverageStatus.PARTIAL
            async with engine.connect() as conn:
                durable = await repository.get_execution(conn, scope, eid)
                assert (
                    durable is not None
                    and durable.execution.status is ExecutionStatus.PARTIAL_FAILED
                )
                runs = await repository.list_nodes(conn, scope, eid)
                assert len(runs) == 12 and all(
                    len(node.attempt_history) == 1 for node in runs
                )
                assert len([node for node in runs if node.node_id is NodeId.N04]) == 3
                assert len(durable.execution.model_invocation_refs) == 10

    asyncio.run(run())


@pytest.mark.parametrize(
    ("case", "attempted", "expected"),
    [
        ("excluded", {NodeId.N01}, CoverageStatus.NOT_APPLICABLE),
        ("no_hits", {NodeId.N01, NodeId.N02}, CoverageStatus.NONE),
        ("no_target", {NodeId.N01, NodeId.N02, NodeId.N03}, CoverageStatus.NONE),
        ("blocked_context", {NodeId.N01}, CoverageStatus.NONE),
    ],
)
def test_worker_persists_attempt_free_terminal_branches(
    tmp_path: Path,
    case: str,
    attempted: set[NodeId],
    expected: CoverageStatus,
) -> None:
    async def run() -> None:
        async with migrated_application() as engine:
            worker, accepted, host = await accepted_worker(
                engine,
                tmp_path,
                excluded=case == "excluded",
                empty_context=case == "blocked_context",
            )
            scope, eid = (
                accepted.task.idempotency_scope,
                accepted.execution.execution_id,
            )
            if case == "no_hits":
                host.scenario = SyntheticScenario(discovery_hits=())
            if case == "no_target":
                for _ in range(2):
                    assert (
                        await worker.run_next(scope, owner="fixture", lease_ms=1000)
                    ).step is WorkerStep.APPLIED
                async with engine.connect() as conn:
                    node = next(
                        item
                        for item in await repository.list_nodes(conn, scope, eid)
                        if item.node_id is NodeId.N02
                    )
                    message = await repository.read_output(
                        conn,
                        scope,
                        eid,
                        node.node_run_id,
                        node.attempt_history[-1].attempt_ref.record_id,
                        MESSAGE_KEY,
                        NODE_MESSAGE,
                    )
                    assert isinstance(message, DiscoveryMessage)
                    (hit,) = message.output.profile_hits
                host.scenario = SyntheticScenario(
                    grounding=(
                        SyntheticGroundingCase(
                            profile_ref=hit.profile_ref, object_count=0
                        ),
                    )
                )
            for _ in range(5):
                poll = await worker.run_next(scope, owner="fixture", lease_ms=1000)
                assert not poll.issues
                if poll.step is WorkerStep.FINALIZED:
                    break
            final = await worker.read_finalization(scope, eid)
            assert final is not None
            assert final.coverage.coverage_status is expected and final.decisions == ()
            async with engine.connect() as conn:
                runs = await repository.list_nodes(conn, scope, eid)
                assert {node.node_id for node in runs} == set(NodeId)
                assert {
                    node.node_id for node in runs if node.attempt_history
                } == attempted
                assert all(
                    node.reason is not None for node in runs if not node.attempt_history
                )

    asyncio.run(run())
