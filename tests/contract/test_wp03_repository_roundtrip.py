"""Application-role repository transactions against a disposable PostgreSQL."""

from asyncio import gather, run
from datetime import timedelta
from typing import Literal

import pytest
from sqlalchemy import text

from jimu_runtime.domain.base import ContractModel
from jimu_runtime.domain.enums import (
    AttemptStatus,
    ExecutionStatus,
    NodeRunStatus,
    RuntimeRecordKind,
    TaskStatus,
)
from jimu_runtime.domain.nodes.envelopes import ArtifactOutputRef, SucceededNodeOutput
from jimu_runtime.domain.references import RuntimeRecordRef
from jimu_runtime.domain.runtime.executions import (
    Attempt,
    AttemptLink,
    LifecycleReason,
    NodeId,
)
from jimu_runtime.domain.runtime.facts import AttemptRef, NodeRunRef
from jimu_runtime.domain.validation.authority_inputs import NodeAttemptOwner
from jimu_runtime.domain.validation.transitions import (
    AttemptTransitionGuard,
    NodeTransitionGuard,
)
from jimu_runtime.domain.values import RecordValue, StringValue
from jimu_runtime.workflow import repository as repo
from tests.fixtures.wp02.releases.support import submission
from tests.fixtures.wp03.repository.support import (
    LIMITS,
    NOW,
    accept,
    database,
    records,
)


class FixtureFinalization(ContractModel):
    schema_version: Literal["1.0.0"]
    execution_id: str
    summary: str


def test_original_wire_values_and_two_concurrent_finalizers_round_trip() -> None:
    async def scenario() -> None:
        async with database() as engine:
            request = submission("same")
            metadata = RecordValue(
                kind="record",
                value={
                    "note": StringValue(kind="string", value="A\x00B"),
                },
            )
            request = request.model_copy(
                update={
                    "idempotency_key": "键\x00same",
                    "context": request.context.model_copy(
                        update={"metadata": metadata}
                    ),
                }
            )
            first, second = await gather(
                accept(engine, request), accept(engine, request)
            )
            assert first == second
            accepted, node, _ = first
            scope = request.idempotency_scope
            identity = accepted.execution.execution_id
            async with engine.connect() as connection:
                durable = await repo.get_intent(connection, scope, request.request_id)
                assert durable is not None
                assert durable.submission == request
                assert durable.submission.stable_json() == request.stable_json()
                assert durable.status is repo.IntentStatus.ACCEPTED
                assert await repo.get_execution(connection, scope, identity) == accepted
                assert accepted.task.idempotency_key == request.idempotency_key
                assert accepted.scheduling_limits == LIMITS
                assert accepted.task_status is TaskStatus.QUEUED
                assert await repo.list_branches(connection, scope, identity) == (
                    ("global", node),
                )
                history = await repo.list_events(connection, scope, identity)
                assert [event.event_number for event in history] == [1, 2, 3]
                assert [event.payload.event_kind for event in history] == [
                    "task_transition",
                    "task_transition",
                    "node_transition",
                ]

    run(scenario())


def test_authoritative_publication_and_successors_share_commit_boundary() -> None:
    async def scenario() -> None:
        async with database() as engine:
            request = submission()
            scope = request.idempotency_scope
            accepted, node, job = await accept(engine, request)
            execution = accepted.execution
            identity = execution.execution_id
            now = NOW + timedelta(seconds=1)
            attempt = Attempt(
                schema_version="1.0.0",
                attempt_id="attempt-1",
                execution_id=identity,
                owner=NodeAttemptOwner(
                    owner_kind="node",
                    node_run_ref=NodeRunRef(
                        record_kind=RuntimeRecordKind.NODE_RUN,
                        record_id=node.node_run_id,
                    ),
                ),
                attempt_number=1,
                status=AttemptStatus.RUNNING,
                created_at=now,
                started_at=now,
            )
            running = node.model_copy(
                update={
                    "status": NodeRunStatus.RUNNING,
                    "started_at": now,
                    "attempt_history": (
                        AttemptLink(
                            attempt_ref=AttemptRef(
                                record_kind=RuntimeRecordKind.ATTEMPT,
                                record_id=attempt.attempt_id,
                            ),
                            attempt_number=1,
                        ),
                    ),
                }
            )
            async with engine.begin() as connection:
                assert (
                    await repo.lock_execution(connection, scope, identity) == accepted
                )
                await repo.transition_task(
                    connection,
                    scope,
                    identity,
                    execution.task_id,
                    TaskStatus.QUEUED,
                    TaskStatus.RUNNING,
                    now=now,
                    trace=execution.trace,
                )
                await repo.transition_execution(
                    connection,
                    scope,
                    execution,
                    execution.model_copy(
                        update={
                            "status": ExecutionStatus.RUNNING,
                            "started_at": now,
                        }
                    ),
                    now=now,
                )
                await connection.execute(
                    text("""
                    UPDATE jimu_workflow.jobs
                    SET status = 'leased', lease_owner = 'worker', lease_until = :expiry, fence = 1
                    WHERE project_id = :project AND client_id = :client AND execution_id = :execution
                """),
                    {
                        "project": scope.project_id,
                        "client": scope.client_id,
                        "execution": identity,
                        "expiry": now + timedelta(seconds=1),
                    },
                )
                await repo.insert_attempt(
                    connection,
                    scope,
                    identity,
                    node.node_run_id,
                    attempt,
                    job_id=job.job_id,
                    fence=1,
                    lease_owner="worker",
                )
                await repo.transition_node(
                    connection,
                    scope,
                    identity,
                    node,
                    running,
                    now=now,
                    guard=NodeTransitionGuard.FIRST_ATTEMPT_STARTED,
                    appended_attempt=attempt,
                )
            terminal = attempt.model_copy(
                update={"status": AttemptStatus.SUCCEEDED, "ended_at": now}
            )
            output = SucceededNodeOutput(
                schema_version="1.0.0",
                node_id=node.node_id,
                node_run_id=node.node_run_id,
                attempt_id=attempt.attempt_id,
                trace=node.trace,
                started_at=now,
                finished_at=now,
                status=NodeRunStatus.SUCCEEDED,
                duration_ms=0,
                output_artifacts=(
                    ArtifactOutputRef(
                        ref=RuntimeRecordRef(
                            record_kind=RuntimeRecordKind.CONTEXT_SNAPSHOT,
                            record_id="ctx-1",
                        ),
                        digest="a" * 64,
                    ),
                ),
            )
            finalization = FixtureFinalization(
                schema_version="1.0.0",
                execution_id=identity,
                summary="typed\x00snapshot",
            )
            _, _, draft_node, _ = records(request)
            successor = draft_node.model_copy(
                update={"node_run_id": "successor-1", "node_id": NodeId.N02}
            )
            for should_rollback in (True, False):
                async with engine.connect() as connection:
                    transaction = await connection.begin()
                    await repo.lock_execution(connection, scope, identity)
                    await repo.get_node(
                        connection, scope, identity, node.node_run_id, for_update=True
                    )
                    await repo.finish_attempt(
                        connection,
                        scope,
                        identity,
                        node.node_run_id,
                        attempt,
                        terminal,
                        AttemptTransitionGuard.RESPONSE_VALIDATED_WITH_COMMIT_RIGHTS,
                        now=now,
                    )
                    digest = await repo.store_output(
                        connection,
                        scope,
                        identity,
                        node.node_run_id,
                        attempt.attempt_id,
                        output_key="node.output",
                        output_kind="node_output",
                        record=output,
                    )
                    await repo.select_authority(
                        connection,
                        scope,
                        identity,
                        node.node_run_id,
                        attempt.attempt_id,
                        output_key="node.output",
                        output_digest=digest,
                    )
                    await repo.transition_node(
                        connection,
                        scope,
                        identity,
                        running,
                        running.model_copy(
                            update={
                                "status": NodeRunStatus.SUCCEEDED,
                                "ended_at": now,
                                "output_refs": tuple(
                                    item.ref for item in output.output_artifacts
                                ),
                            }
                        ),
                        now=now,
                        guard=NodeTransitionGuard.AUTHORITATIVE_OUTPUT_VALIDATED,
                    )
                    await repo.insert_node(
                        connection, scope, identity, successor, branch_key="replay"
                    )
                    assert await repo.link_successor(
                        connection,
                        scope,
                        identity,
                        node.node_run_id,
                        successor.node_run_id,
                    )
                    assert not await repo.link_successor(
                        connection,
                        scope,
                        identity,
                        node.node_run_id,
                        successor.node_run_id,
                    )
                    await repo.transition_node(
                        connection,
                        scope,
                        identity,
                        successor,
                        successor.model_copy(
                            update={
                                "status": NodeRunStatus.SKIPPED,
                                "ended_at": now,
                                "reason": LifecycleReason(code="fixture_skip"),
                            }
                        ),
                        now=now,
                        guard=NodeTransitionGuard.DETERMINISTIC_SKIP,
                    )
                    await repo.store_finalization(
                        connection, scope, identity, finalization
                    )
                    locked = await repo.get_execution(connection, scope, identity)
                    assert locked is not None
                    await repo.transition_execution(
                        connection,
                        scope,
                        locked.execution,
                        locked.execution.model_copy(
                            update={
                                "status": ExecutionStatus.COMPLETED,
                                "ended_at": now,
                            }
                        ),
                        now=now,
                    )
                    await repo.transition_task(
                        connection,
                        scope,
                        identity,
                        execution.task_id,
                        TaskStatus.RUNNING,
                        TaskStatus.COMPLETED,
                        now=now,
                        trace=execution.trace,
                    )
                    if should_rollback:
                        await transaction.rollback()
                    else:
                        await transaction.commit()
                async with engine.connect() as connection:
                    chosen = await repo.read_authority(
                        connection,
                        scope,
                        identity,
                        node.node_run_id,
                        SucceededNodeOutput,
                    )
                    durable = await repo.get_attempt(
                        connection,
                        scope,
                        identity,
                        node.node_run_id,
                        attempt.attempt_id,
                    )
                    result = await repo.read_finalization(
                        connection, scope, identity, FixtureFinalization
                    )
                    events = await repo.list_events(connection, scope, identity)
                    stored = await repo.get_execution(connection, scope, identity)
                    assert stored is not None
                    if should_rollback:
                        assert stored.execution.status is ExecutionStatus.RUNNING
                        assert stored.task_status is TaskStatus.RUNNING
                        assert (chosen, durable, result, len(events)) == (
                            None,
                            attempt,
                            None,
                            7,
                        )
                    else:
                        assert stored.execution.status is ExecutionStatus.COMPLETED
                        assert stored.task_status is TaskStatus.COMPLETED
                        assert (chosen, durable, result, len(events)) == (
                            output,
                            terminal,
                            finalization,
                            12,
                        )
                        with pytest.raises(
                            repo.RepositoryError, match="finalization_identity_conflict"
                        ):
                            await repo.store_finalization(
                                connection,
                                scope,
                                identity,
                                finalization.model_copy(update={"summary": "changed"}),
                            )

    run(scenario())
