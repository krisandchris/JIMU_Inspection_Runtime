"""Real PostgreSQL evidence for claims, fencing, cancellation and aggregation."""

import asyncio
import multiprocessing
import signal
from dataclasses import replace
from datetime import timedelta
from hashlib import sha256

import pytest
from sqlalchemy import text

from jimu_runtime.domain.enums import ExecutionStatus, NodeRunStatus, RuntimeRecordKind
from jimu_runtime.domain.fact_schemas.canonicalization import canonical_bytes
from jimu_runtime.domain.nodes.envelopes import SucceededNodeOutput
from jimu_runtime.domain.references import RuntimeRecordRef
from jimu_runtime.domain.runtime.executions import (
    LateResponseEvent,
    NodeId,
    PinnedRuntimeInput,
)
from jimu_runtime.workflow import jobs as jobs_module
from jimu_runtime.workflow import repository as repo
from jimu_runtime.workflow.jobs import CompletionDisposition as Disposition
from jimu_runtime.workflow.jobs import DurableJobs, JobConflict
from jimu_runtime.workflow.postgres import create_postgres_engine
from jimu_runtime.workflow.scheduling import NodeBudgetOverride, SchedulingLimits
from jimu_runtime.workflow.worker_poll import runnable_executions
from tests.fixtures.wp03.jobs.support import (
    FixtureRecord,
    apply_finalization,
    branch,
    budget,
    database,
    fetch_node,
    inspect,
    outcome,
    process_worker,
    response,
    seed,
    wait_for_db_time,
)


def _spawn(url, scope, execution_id, owner, action):
    context = multiprocessing.get_context("spawn")
    parent, child = context.Pipe(duplex=False)
    go = context.Event()
    process = context.Process(
        target=process_worker, args=(url, scope, execution_id, owner, action, child, go)
    )
    process.start()
    child.close()
    return process, parent, go


def _read(pipe):
    assert pipe.poll(15), "worker did not reach its barrier"
    return pipe.recv()


def _stop(process, pipe):
    if process.is_alive():
        process.kill()
    process.join(10)
    pipe.close()
    assert not process.is_alive()


def test_two_processes_contend_for_one_job_then_recover_killed_completion():
    async def run():
        async with database() as (engine, url):
            scope, execution_id = await seed(engine)
            first = _spawn(
                url, scope, execution_id, "contender-a", "crash-in-completion"
            )
            second = _spawn(
                url, scope, execution_id, "contender-b", "crash-in-completion"
            )
            try:
                assert _read(first[1]) == _read(second[1]) == "ready"
                first[2].set()
                second[2].set()
                first_result, second_result = _read(first[1]), _read(second[1])
                assert (first_result is None) != (second_result is None)
                loser, winner = (
                    (first, second) if first_result is None else (second, first)
                )
                loser[0].join(5)
                assert loser[0].exitcode == 0
                claim, started = first_result or second_result
                before = await inspect(engine, scope, execution_id)
                assert before["starts"] == [(started.attempt_id, 1)]
                assert before["successors"] == []
                assert (
                    before["attempts"]
                    == before["authority"]
                    == before["receipts"]
                    == []
                )
                winner[0].kill()
                winner[0].join(10)
                assert winner[0].exitcode == -signal.SIGKILL
            finally:
                _stop(first[0], first[1])
                _stop(second[0], second[1])

            await wait_for_db_time(engine, claim.lease_until)
            assert await DurableJobs(engine).reconcile(scope, execution_id)
            resumed = _spawn(url, scope, execution_id, "restarted-worker", "complete")
            try:
                assert _read(resumed[1]) == "ready"
                resumed[2].set()
                new_claim, second_attempt, completed = _read(resumed[1])
                resumed[0].join(5)
                assert resumed[0].exitcode == 0
            finally:
                _stop(resumed[0], resumed[1])

            assert new_claim.fence == claim.fence + 1
            assert second_attempt.attempt_number == 2
            assert completed is Disposition.APPLIED
            after = await inspect(engine, scope, execution_id)
            assert dict(after["attempts"]) == {
                started.attempt_id: "superseded",
                second_attempt.attempt_id: "succeeded",
            }
            assert after["authority"] == [
                (new_claim.node_run_id, second_attempt.attempt_id)
            ]
            assert after["receipts"] == [(second_attempt.attempt_id, new_claim.fence)]
            assert after["successors"] == [(new_claim.node_run_id, "node-next")]
            assert after["events"][: len(before["events"])] == before["events"]

    asyncio.run(run())


def test_heartbeat_checks_database_clock_after_waiting_for_root_lock():
    async def run():
        async with database() as (engine, url):
            scope, execution_id = await seed(engine)
            jobs = DurableJobs(engine)
            observer_name = "wp03-blocked-heartbeat"
            other = create_postgres_engine(
                url.update_query_dict({"application_name": observer_name})
            )
            try:
                claim = await jobs.claim(scope, "holder", 1200)
                assert claim is not None
                attempt = await jobs.start(claim)
                assert attempt is not None
                async with engine.begin() as blocker:
                    assert await repo.lock_execution(blocker, scope, execution_id)
                    heartbeat = asyncio.create_task(
                        DurableJobs(other).heartbeat(claim, 2000)
                    )

                    async def wait_for_lock():
                        async with engine.connect() as connection:
                            while True:
                                active = await connection.scalar(
                                    text("""
                                        SELECT EXISTS (SELECT 1 FROM pg_stat_activity
                                            WHERE application_name = :name AND wait_event_type = 'Lock')
                                        """),
                                    {"name": observer_name},
                                )
                                if active:
                                    return
                                # pg_stat_activity statistics snapshots are transaction-local.
                                await connection.rollback()
                                await asyncio.sleep(0.01)

                    await asyncio.wait_for(wait_for_lock(), 5)
                    assert (
                        await blocker.scalar(text("SELECT clock_timestamp()"))
                    ) < claim.lease_until
                    await wait_for_db_time(engine, claim.lease_until)
                assert await asyncio.wait_for(heartbeat, 5) is None
                state = await inspect(engine, scope, execution_id)
                assert state["attempts"] == [(attempt.attempt_id, "superseded")]
                assert state["jobs"] == [(claim.job_id, "ready", claim.fence)]
            finally:
                await other.dispose()

    asyncio.run(run())


def test_late_response_before_takeover_is_audited_without_a_new_attempt():
    async def run():
        async with database() as (engine, _):
            scope, execution_id = await seed(engine)
            jobs = DurableJobs(engine)
            claim = await jobs.claim(scope, "slow-worker", 250)
            assert claim is not None
            attempt = await jobs.start(claim)
            run = await fetch_node(engine, scope, execution_id, claim.node_run_id)
            completion = outcome(attempt, run)
            await wait_for_db_time(engine, claim.lease_until)
            received = FixtureRecord(
                schema_version="1.0.0", value="immutable late packet"
            )
            digest = sha256(
                canonical_bytes(received.model_dump(mode="json"))
            ).hexdigest()
            evidence = response(attempt, digest=digest)
            with pytest.raises(JobConflict, match="pinned digest"):
                await jobs.complete(
                    claim,
                    completion,
                    response=evidence,
                    response_record=received.model_copy(update={"value": "different"}),
                )
            assert (
                await jobs.complete(
                    claim, completion, response=evidence, response_record=received
                )
                is Disposition.LATE_RECORDED
            )
            assert (
                await jobs.complete(claim, completion, response=evidence)
                is Disposition.DUPLICATE
            )
            with pytest.raises(JobConflict, match="different persisted digest"):
                await jobs.complete(
                    claim, completion, response=response(attempt, digest="5" * 64)
                )
            async with engine.connect() as connection:
                retained = await repo.read_output(
                    connection,
                    scope,
                    execution_id,
                    claim.node_run_id,
                    attempt.attempt_id,
                    "late.artifact.reply-1",
                    FixtureRecord,
                )
                retained_outcome = await repo.read_output(
                    connection,
                    scope,
                    execution_id,
                    claim.node_run_id,
                    attempt.attempt_id,
                    "late.result.reply-1",
                    SucceededNodeOutput,
                )
                assert retained == received
                assert retained_outcome == completion

            state = await inspect(engine, scope, execution_id)
            assert state["authority"] == []
            assert state["starts"] == [(attempt.attempt_id, 1)]
            assert state["attempts"] == [(attempt.attempt_id, "superseded")]
            assert state["late"] == [
                (attempt.attempt_id, "reply-1", "fixture/request-01")
            ]
            assert isinstance(state["events"][-1].payload, LateResponseEvent)

            await asyncio.sleep(0.03)
            new_claim = await jobs.claim(scope, "replacement", 1500)
            assert new_claim is not None
            assert new_claim.fence == claim.fence + 1
            new_attempt = await jobs.start(new_claim)
            valid = outcome(new_attempt, run)
            assert await jobs.complete(new_claim, valid) is Disposition.APPLIED
            assert await jobs.complete(new_claim, valid) is Disposition.DUPLICATE
            with pytest.raises(JobConflict, match="already persisted"):
                await jobs.complete(new_claim, valid, response=response(new_attempt))
            assert (
                await jobs.complete(
                    claim, completion, response=response(attempt, "reply-2")
                )
                is Disposition.LATE_RECORDED
            )
            final = await inspect(engine, scope, execution_id)
            assert final["authority"] == [
                (new_claim.node_run_id, new_attempt.attempt_id)
            ]
            assert final["events"][: len(state["events"])] == state["events"]
            assert len(final["late"]) == 2

    asyncio.run(run())


def test_timeout_precedes_a_longer_renewed_lease_and_consumes_retry_budget():
    async def run():
        async with database() as (engine, _):
            limits = SchedulingLimits(
                schema_version="1.0.0",
                version="1.0.0",
                default=budget(attempts=3),
                overrides=(
                    NodeBudgetOverride(
                        node_id=NodeId.N01, budget=budget(attempts=1, timeout=100)
                    ),
                ),
            )
            scope, execution_id = await seed(engine, limits=limits)
            invoked = []

            async def finish(connection, scope, locked, run, attempt, outcome, now):
                invoked.append((run.status, attempt.status.value, outcome))
                await branch(
                    connection, scope, locked, run, now, NodeId.N02, "fallback"
                )

            jobs = DurableJobs(engine, on_terminal=finish)
            lease = await jobs.claim(scope, "holder", 2000)
            attempt = await jobs.start(lease)
            renewed = await jobs.heartbeat(lease, 3000)
            assert renewed is not None and renewed.lease_until > lease.lease_until
            await asyncio.sleep(0.13)
            assert await jobs.reconcile(scope, execution_id)
            assert not await jobs.reconcile(scope, execution_id)
            state = await inspect(engine, scope, execution_id)
            assert state["attempts"] == [(attempt.attempt_id, "timed_out")]
            assert state["authority"] == state["receipts"] == []
            assert state["successors"] == [(lease.node_run_id, "node-fallback")]
            assert invoked == [(NodeRunStatus.FAILED, "timed_out", None)]
            assert state["execution"].scheduling_limits == limits
            assert state["jobs"] == [
                ("job-fallback", "ready", 0),
                (lease.job_id, "done", 1),
            ]

    asyncio.run(run())


def test_cancel_intent_recovers_without_fabricating_attempts():
    async def run():
        async with database() as (engine, url):
            scope, execution_id = await seed(engine)
            worker = _spawn(url, scope, execution_id, "canceller", "cancel-intent")
            try:
                assert _read(worker[1]) == "ready"
                worker[2].set()
                assert _read(worker[1]) is True
                state = await inspect(engine, scope, execution_id)
                assert state["execution"].cancel_requested_at is not None
                assert (
                    state["execution"].execution.status is ExecutionStatus.INITIALIZED
                )
                worker[0].kill()
                worker[0].join(5)
                assert worker[0].exitcode == -signal.SIGKILL
            finally:
                _stop(worker[0], worker[1])

            restarted = DurableJobs(engine)
            assert await restarted.claim(scope, "new-scheduler", 1500) is None
            assert not await restarted.reconcile(scope, execution_id)
            final = await inspect(engine, scope, execution_id)
            assert final["starts"] == final["attempts"] == final["authority"] == []
            assert final["execution"].execution.status is ExecutionStatus.CANCELLED
            node_id = "node-1"
            assert (
                await fetch_node(engine, scope, execution_id, node_id)
            ).status is NodeRunStatus.CANCELLED
            assert final["jobs"] == [("job-root", "done", 0)]

    asyncio.run(run())


def test_staged_n06_survives_restart_and_aggregates_with_failed_sibling():
    async def run():
        async with database() as (engine, url):
            scope, execution_id = await seed(
                engine,
                limits=SchedulingLimits(
                    schema_version="1.0.0",
                    version="1.0.0",
                    default=budget(attempts=1),
                    overrides=(),
                ),
            )

            async def schedule(connection, scope, locked, run, attempt, outcome, now):
                if run.node_id is NodeId.N01:
                    await branch(connection, scope, locked, run, now, NodeId.N06, "a")
                    await branch(connection, scope, locked, run, now, NodeId.N06, "b")

            jobs = DurableJobs(engine, on_terminal=schedule)
            first = await jobs.claim(scope, "worker", 1200)
            root = await jobs.start(first)
            root_result = outcome(
                root, await fetch_node(engine, scope, execution_id, first.node_run_id)
            )
            assert await jobs.complete(first, root_result) is Disposition.APPLIED
            assert await jobs.complete(first, root_result) is Disposition.DUPLICATE
            leases = []
            for key in ("a", "b"):
                claim = await jobs.claim(scope, f"worker-{key}", 5000)
                attempt = await jobs.start(claim)
                run = await fetch_node(engine, scope, execution_id, claim.node_run_id)
                leases.append((claim, attempt, run))
            stage_claim, stage_attempt, stage_run = leases[0]
            stage_output = outcome(stage_attempt, stage_run)
            draft = FixtureRecord(schema_version="1.0.0", value="validated-draft")
            assert (
                await jobs.stage_decision(stage_claim, stage_output, draft)
                is Disposition.STAGED
            )
            assert (
                await jobs.stage_decision(stage_claim, stage_output, draft)
                is Disposition.DUPLICATE
            )
            failed_claim, failed_attempt, failed_run = leases[1]
            assert (
                await jobs.complete(
                    failed_claim,
                    outcome(failed_attempt, failed_run, result="validation"),
                )
                is Disposition.APPLIED
            )
            staged = await inspect(engine, scope, execution_id)
            assert staged["drafts"] == [
                (stage_claim.node_run_id, stage_attempt.attempt_id)
            ]
            assert staged["authority"] == [(first.node_run_id, root.attempt_id)]
            assert "staged" in {row.status for row in staged["jobs"]}

            recovered_engine = create_postgres_engine(url)
            try:
                recovered = DurableJobs(recovered_engine)
                assert await recovered.pending_finalizations(scope) == (execution_id,)
                assert (
                    await recovered.claim(
                        scope, "recovered", 1000, execution_id=execution_id
                    )
                    is None
                )

                interrupted = _spawn(
                    url, scope, execution_id, "aggregation-crash", "finalize-crash"
                )
                try:
                    assert _read(interrupted[1]) == "ready"
                    interrupted[2].set()
                    assert _read(interrupted[1]) == "aggregate-uncommitted"
                    assert await inspect(engine, scope, execution_id) == staged
                    interrupted[0].kill()
                    interrupted[0].join(5)
                    assert interrupted[0].exitcode == -signal.SIGKILL
                finally:
                    _stop(interrupted[0], interrupted[1])
                assert await inspect(engine, scope, execution_id) == staged

                async def reject(*args):
                    return False

                assert not await recovered.finalize_staged(scope, execution_id, reject)
                assert await inspect(engine, scope, execution_id) == staged
                committed = _spawn(
                    url, scope, execution_id, "aggregation-commit", "finalize-commit"
                )
                try:
                    assert _read(committed[1]) == "ready"
                    committed[2].set()
                    assert _read(committed[1]) is True
                    committed[0].kill()
                    committed[0].join(5)
                    assert committed[0].exitcode == -signal.SIGKILL
                finally:
                    _stop(committed[0], committed[1])
                assert not await recovered.finalize_staged(
                    scope, execution_id, apply_finalization
                )
                assert await recovered.pending_finalizations(scope) == ()
                assert (
                    await recovered.stage_decision(stage_claim, stage_output, draft)
                    is Disposition.DUPLICATE
                )
            finally:
                await recovered_engine.dispose()
            final = await inspect(engine, scope, execution_id)
            assert len(final["finalizations"]) == 1
            assert final["execution"].execution.status is ExecutionStatus.PARTIAL_FAILED
            assert final["authority"] == [
                (first.node_run_id, root.attempt_id),
                (stage_claim.node_run_id, stage_attempt.attempt_id),
            ]
            assert dict(final["attempts"])[failed_attempt.attempt_id] == "failed"
            assert dict(final["attempts"])[stage_attempt.attempt_id] == "succeeded"
            assert dict(final["receipts"]) == {
                root.attempt_id: first.fence,
                stage_attempt.attempt_id: stage_claim.fence,
                failed_attempt.attempt_id: failed_claim.fence,
            }
            assert len(final["successors"]) == 2

    asyncio.run(run())


def test_forged_scope_owner_attempt_and_started_time_have_no_authority():
    async def run():
        async with database() as (engine, _):
            scope, execution_id = await seed(engine)
            jobs = DurableJobs(engine)
            claim = await jobs.claim(scope, "owner", 5000)
            started = await jobs.start(claim)
            assert await jobs.start(claim) is None
            run = await fetch_node(engine, scope, execution_id, claim.node_run_id)
            output = outcome(started, run)
            initial = await inspect(engine, scope, execution_id)
            for fabricated in (
                replace(claim, scope=scope.model_copy(update={"client_id": "other"})),
                replace(claim, execution_id="different"),
                replace(claim, owner="other"),
                replace(claim, fence=claim.fence + 1),
            ):
                assert await jobs.complete(fabricated, output) is Disposition.LEASE_LOST
                assert await jobs.heartbeat(fabricated, 1000) is None
            assert (
                await jobs.complete(
                    claim, output.model_copy(update={"attempt_id": "different"})
                )
                is Disposition.LEASE_LOST
            )
            with pytest.raises(JobConflict, match="does not match"):
                await jobs.complete(
                    claim, output.model_copy(update={"node_id": NodeId.N02})
                )
            with pytest.raises(JobConflict, match="different Execution or Attempt"):
                await jobs.complete(
                    claim,
                    output,
                    response=response(started).model_copy(
                        update={"execution_id": "different"}
                    ),
                )
            assert await inspect(engine, scope, execution_id) == initial

    asyncio.run(run())


def test_claim_pages_past_full_busy_prefix(monkeypatch):
    monkeypatch.setattr(jobs_module, "_CLAIM_PAGE_SIZE", 2)

    async def run():
        async with database() as (engine, _):
            executions = [
                await seed(engine, suffix=f"queue-{index}") for index in range(5)
            ]
            scope = executions[0][0]
            async with engine.begin() as holder:
                for _, execution_id in executions[:-1]:
                    assert await repo.lock_execution(holder, scope, execution_id)
                claim = await asyncio.wait_for(
                    DurableJobs(engine).claim(scope, "paged", 5000), 5
                )
                assert claim is not None
                assert claim.execution_id == executions[-1][1]
                state = await inspect(engine, scope, claim.execution_id)
                assert state["starts"] == []
                assert state["jobs"] == [("job-root", "leased", 1)]

    asyncio.run(run())


def test_cancel_after_staging_closes_attempt_with_no_authoritative_decision():
    async def run():
        async with database() as (engine, _):
            scope, execution_id = await seed(engine)

            async def schedule(connection, scope, locked, run, attempt, outcome, now):
                if run.node_id is NodeId.N01:
                    await branch(
                        connection, scope, locked, run, now, NodeId.N06, "decision"
                    )

            jobs = DurableJobs(engine, on_terminal=schedule)
            root_claim = await jobs.claim(scope, "worker", 5000)
            root = await jobs.start(root_claim)
            root_output = outcome(
                root,
                await fetch_node(engine, scope, execution_id, root_claim.node_run_id),
            )
            assert await jobs.complete(root_claim, root_output) is Disposition.APPLIED

            claim = await jobs.claim(scope, "worker", 300)
            attempt = await jobs.start(claim)
            run = await fetch_node(engine, scope, execution_id, claim.node_run_id)
            output = outcome(attempt, run)
            draft = FixtureRecord(schema_version="1.0.0", value="audit-draft")
            assert await jobs.stage_decision(claim, output, draft) is Disposition.STAGED
            await wait_for_db_time(engine, claim.lease_until)
            assert not await jobs.reconcile(scope, execution_id)
            assert await jobs.claim(scope, "worker", 1000) is None
            assert await jobs.request_cancel(scope, execution_id)
            assert not await jobs.finalize_staged(
                scope, execution_id, apply_finalization
            )
            assert (
                await jobs.stage_decision(claim, output, draft) is Disposition.DUPLICATE
            )
            assert (
                await jobs.record_late(claim, response(attempt))
                is Disposition.LATE_RECORDED
            )
            final = await inspect(engine, scope, execution_id)
            assert final["execution"].execution.status is ExecutionStatus.CANCELLED
            assert dict(final["attempts"])[attempt.attempt_id] == "cancelled"
            assert final["drafts"] == [(claim.node_run_id, attempt.attempt_id)]
            assert final["authority"] == [(root_claim.node_run_id, root.attempt_id)]
            assert final["finalizations"] == []
            assert (
                await fetch_node(engine, scope, execution_id, claim.node_run_id)
            ).status is NodeRunStatus.CANCELLED

    asyncio.run(run())


def test_failed_completion_retries_once_and_replays_its_receipt():
    async def run():
        async with database() as (engine, _):
            scope, execution_id = await seed(engine)
            jobs = DurableJobs(engine)
            first = await jobs.claim(scope, "worker", 5000)
            attempt = await jobs.start(first)
            run = await fetch_node(engine, scope, execution_id, first.node_run_id)
            failed = outcome(attempt, run, result="transport")
            seen = response(attempt)
            assert (
                await jobs.complete(first, failed, response=seen)
                is Disposition.RETRY_SCHEDULED
            )
            assert (
                await jobs.complete(first, failed, response=seen)
                is Disposition.DUPLICATE
            )
            assert await jobs.record_late(first, seen) is Disposition.DUPLICATE
            assert (
                await jobs.record_late(first, response(attempt, "separate"))
                is Disposition.LATE_RECORDED
            )
            assert (
                await fetch_node(engine, scope, execution_id, first.node_run_id)
            ).status is NodeRunStatus.RUNNING
            await asyncio.sleep(0.03)
            second = await jobs.claim(scope, "replacement", 5000)
            again = await jobs.start(second)
            assert again.attempt_number == 2
            exhausted = outcome(again, run, result="transport")
            assert await jobs.complete(second, exhausted) is Disposition.APPLIED
            assert await jobs.complete(second, exhausted) is Disposition.DUPLICATE
            assert not await jobs.reconcile(scope, execution_id)
            state = await inspect(engine, scope, execution_id)
            assert dict(state["attempts"]) == {
                attempt.attempt_id: "failed",
                again.attempt_id: "failed",
            }
            assert dict(state["receipts"]) == {
                attempt.attempt_id: first.fence,
                again.attempt_id: second.fence,
            }
            assert state["authority"] == []
            assert state["late"] == [
                (attempt.attempt_id, "separate", "fixture/request-01")
            ]

    asyncio.run(run())


def test_expired_unstarted_claim_uses_no_attempt_budget():
    async def run():
        async with database() as (engine, _):
            one = SchedulingLimits(
                schema_version="1.0.0",
                version="1.0.0",
                default=budget(attempts=1, lease_retry=False),
                overrides=(),
            )
            scope, execution_id = await seed(engine, limits=one)
            jobs = DurableJobs(engine)
            abandoned = await jobs.claim(scope, "old", 100)
            assert abandoned is not None
            await wait_for_db_time(engine, abandoned.lease_until)
            taken = await jobs.claim(scope, "new", 3000)
            assert taken is not None and taken.fence == abandoned.fence + 1
            assert await jobs.start(abandoned) is None
            state = await inspect(engine, scope, execution_id)
            assert state["execution"].execution.status is ExecutionStatus.INITIALIZED
            assert state["starts"] == state["attempts"] == []
            first = await jobs.start(taken)
            assert first is not None and first.attempt_number == 1
            assert await jobs.request_cancel(scope, execution_id)
            run = await fetch_node(engine, scope, execution_id, taken.node_run_id)
            assert (
                await jobs.complete(
                    taken, outcome(first, run), response=response(first)
                )
                is Disposition.LATE_RECORDED
            )
            state = await inspect(engine, scope, execution_id)
            assert state["attempts"] == [(first.attempt_id, "cancelled")]
            assert state["authority"] == state["receipts"] == []
            assert state["execution"].execution.status is ExecutionStatus.CANCELLED

    asyncio.run(run())


@pytest.mark.parametrize("backoff", (False, True))
def test_tool_dependency_wait_releases_worker_preserves_budget_and_resumes_after_restart(
    backoff,
):
    async def run():
        async with database() as (engine, _):
            observation_budget = budget().model_copy(
                update={"retry_backoff_ms": (1_500,)}
            )
            tool_budget = budget(attempts=1 if backoff else 2, timeout=1_000)
            limits = SchedulingLimits(
                schema_version="1.0.0",
                version="1.0.0",
                default=budget(),
                overrides=(
                    NodeBudgetOverride(node_id=NodeId.N04, budget=observation_budget),
                    NodeBudgetOverride(node_id=NodeId.N05, budget=tool_budget),
                ),
            )
            scope, execution_id = await seed(engine, limits=limits)
            first_pin = PinnedRuntimeInput(
                ref=RuntimeRecordRef(
                    record_kind=RuntimeRecordKind.OBJECT, record_id="a"
                ),
                digest="1" * 64,
            )

            async def schedule(connection, scope, locked, run, attempt, output, now):
                if run.node_id is not NodeId.N01:
                    return
                for pin in (
                    (first_pin,)
                    if backoff
                    else (
                        first_pin,
                        PinnedRuntimeInput(
                            ref=RuntimeRecordRef(
                                record_kind=RuntimeRecordKind.OBJECT, record_id="b"
                            ),
                            digest="2" * 64,
                        ),
                    )
                ):
                    key = str(pin.ref.record_id)
                    observe, tool = f"observe-{key}", f"tool-{key}"
                    await branch(
                        connection,
                        scope,
                        locked,
                        run,
                        now,
                        NodeId.N04,
                        observe,
                        branch_key=f"object:{key}",
                        object_pin=pin,
                    )
                    await branch(
                        connection,
                        scope,
                        locked,
                        run,
                        now,
                        NodeId.N05,
                        tool,
                        branch_key=f"object:{key}",
                        object_pin=pin,
                    )
                    assert await repo.link_successor(
                        connection,
                        scope,
                        execution_id,
                        f"node-{observe}",
                        f"node-{tool}",
                    )

            async def complete(jobs, claim, attempt, *, result="success"):
                node = await fetch_node(engine, scope, execution_id, claim.node_run_id)
                return await jobs.complete(claim, outcome(attempt, node, result=result))

            jobs = DurableJobs(engine, on_terminal=schedule)
            root_claim = await jobs.claim(scope, "worker", 10_000)
            assert root_claim is not None
            root_attempt = await jobs.start(root_claim)
            assert root_attempt is not None
            assert await complete(jobs, root_claim, root_attempt) is Disposition.APPLIED

            observation = await jobs.claim(scope, "worker", 10_000)
            assert (
                observation is not None and observation.node_run_id == "node-observe-a"
            )
            first_observation = await jobs.start(observation)
            assert first_observation is not None
            if backoff:
                assert (
                    await complete(
                        jobs,
                        observation,
                        first_observation,
                        result="transport",
                    )
                    is Disposition.RETRY_SCHEDULED
                )

            tools = await jobs.claim(scope, "worker" if backoff else "tools", 10_000)
            # A.N05 takes the second worker ahead of unrelated B.N04, and it
            # also runs when a lone worker has put A.N04 into retry backoff.
            assert tools is not None and tools.node_run_id == "node-tool-a"
            first_tool = await jobs.start(tools)
            assert first_tool is not None and first_tool.started_at is not None
            wrong_pin = first_pin.model_copy(update={"digest": "3" * 64})
            with pytest.raises(JobConflict, match="exact Object"):
                await jobs.defer_tool_observation(
                    tools, observation.node_run_id, wrong_pin
                )
            if not backoff:
                with pytest.raises(JobConflict, match="exact Object"):
                    await jobs.defer_tool_observation(
                        tools, "node-observe-b", first_pin
                    )
            assert await jobs.defer_tool_observation(
                tools, observation.node_run_id, first_pin
            )
            assert (
                await jobs.defer_tool_observation(
                    tools, observation.node_run_id, first_pin
                )
                is None
            )
            assert await complete(jobs, tools, first_tool) is not Disposition.APPLIED

            restarted = DurableJobs(engine)
            if backoff:
                assert await restarted.claim(scope, "worker", 10_000) is None
                async with engine.connect() as connection:
                    assert not await runnable_executions(connection, scope, None, 10)
                    ready_at = (
                        await connection.execute(
                            text("""
                                SELECT ready_at FROM jimu_workflow.jobs
                                WHERE project_id = :project AND client_id = :client
                                    AND execution_id = :execution AND node_run_id = :node
                            """),
                            {
                                "project": scope.project_id,
                                "client": scope.client_id,
                                "execution": execution_id,
                                "node": observation.node_run_id,
                            },
                        )
                    ).scalar_one()
                await wait_for_db_time(engine, ready_at)
                retry = await restarted.claim(scope, "worker", 10_000)
                assert (
                    retry is not None and retry.node_run_id == observation.node_run_id
                )
                retry_attempt = await restarted.start(retry)
                assert retry_attempt is not None and retry_attempt.attempt_number == 2
                observation, first_observation = retry, retry_attempt
            else:
                other = await restarted.claim(scope, "tools", 10_000)
                assert other is not None and other.node_run_id == "node-observe-b"
                other_tools = await restarted.claim(scope, "other-tools", 10_000)
                assert (
                    other_tools is not None and other_tools.node_run_id == "node-tool-b"
                )

            # The first N05 deadline has now elapsed without consuming a retry.
            await wait_for_db_time(
                engine, first_tool.started_at + timedelta(milliseconds=1_050)
            )
            assert (
                await complete(restarted, observation, first_observation)
                is Disposition.APPLIED
            )
            resume = await restarted.claim(scope, "restarted", 10_000)
            assert resume is not None and resume.node_run_id == tools.node_run_id
            assert resume.fence > tools.fence
            attempt = await restarted.start(resume)
            assert attempt is not None and attempt.attempt_number == 2
            assert (
                await restarted.defer_tool_observation(
                    resume, observation.node_run_id, first_pin
                )
                is False
            )
            if not backoff:
                assert (
                    await complete(restarted, resume, attempt, result="transport")
                    is Disposition.RETRY_SCHEDULED
                )
                # A's regular retry still uses its configured backoff.
                await asyncio.sleep(0.03)
                resume = await restarted.claim(scope, "restarted", 10_000)
                assert resume is not None and resume.node_run_id == tools.node_run_id
                attempt = await restarted.start(resume)
                assert attempt is not None and attempt.attempt_number == 3
            assert await complete(restarted, resume, attempt) is Disposition.APPLIED
            durable = await inspect(engine, scope, execution_id)
            assert dict(durable["attempts"])[first_tool.attempt_id] == "superseded"
            assert dict(durable["authority"])[tools.node_run_id] == attempt.attempt_id
            async with engine.connect() as connection:
                parked = await repo.get_attempt(
                    connection,
                    scope,
                    execution_id,
                    tools.node_run_id,
                    first_tool.attempt_id,
                )
            assert parked is not None and parked.observation_dependency_deferred

    asyncio.run(run())
