import asyncio
from datetime import UTC, datetime
from multiprocessing import get_context
from pathlib import Path
from signal import SIGKILL

import pytest
from sqlalchemy import text

from jimu_runtime.bundle.integrity import sha256_digest
from jimu_runtime.bundle.store import LocalBundleStore
from jimu_runtime.domain.enums import ExecutionStatus, TaskStatus
from jimu_runtime.domain.runtime.executions import NodeId, NodeRunStatus
from jimu_runtime.domain.runtime.intake import IdempotencyScope, TaskSubmission
from jimu_runtime.workflow import repository
from jimu_runtime.workflow.intake import (
    IntakeAuthorizationError,
    IntakeError,
    IntakeHostUnavailable,
    IntakeIdentity,
    IntakeRecoveryFailure,
    IntakeResult,
)
from jimu_runtime.workflow.postgres import create_postgres_engine
from tests.fixtures.wp02.releases.support import FixtureHost
from tests.fixtures.wp03.intake.support import (
    IDENTITY,
    SCHEDULING,
    FixtureIntakeHost,
    make_service,
    manager_for,
    migrated_application,
    publish,
    submission,
    submit_until_release_pin,
)


def test_persisted_intent_recovers_immutable_release_after_process_death(
    tmp_path: Path,
) -> None:
    ledger = tmp_path / "ledger"
    store = LocalBundleStore(ledger)
    release_host = FixtureHost()
    manager = manager_for(store, release_host)
    publish(tmp_path / "first", store, manager, release_host, number=1)

    original = submission("interrupted")
    scope = original.idempotency_scope

    async def run() -> None:
        async with migrated_application() as app:
            context = get_context("spawn")
            pinned = context.Event()
            process = context.Process(
                target=submit_until_release_pin,
                args=(app.url, ledger, original.request_id, pinned),
            )
            process.start()
            try:
                assert await asyncio.to_thread(pinned.wait, 15), (
                    "child did not reach pin barrier"
                )
            finally:
                process.kill()
                await asyncio.to_thread(process.join, 10)
                exit_code = process.exitcode
                if exit_code is not None:
                    process.close()
            assert exit_code == -SIGKILL

            async with app.connect() as connection:
                intent = await repository.get_intent(
                    connection, scope, original.request_id
                )
                assert (
                    intent is not None
                    and intent.status is repository.IntentStatus.PENDING
                )
                assert intent.submission == original
                assert intent.submission_digest == sha256_digest(
                    original.stable_json().encode()
                )
                assert (intent.issuer, intent.subject) == (
                    IDENTITY.issuer,
                    IDENTITY.subject,
                )
                assert (
                    await connection.scalar(
                        text("SELECT count(*) FROM jimu_workflow.tasks")
                    )
                    == 0
                )
                assert (
                    await connection.scalar(
                        text("SELECT count(*) FROM jimu_workflow.execution_events")
                    )
                    == 0
                )

            publish(tmp_path / "second", store, manager, release_host, number=2)

            # Checking the ledger after the switch proves the killed process
            # persisted the first pin. A new acceptance would select release-2.
            pin = manager.accept_task(original, principal=release_host.task_client)
            assert pin.release_id == "release-1"

            # Recovery opens new PostgreSQL and local-ledger connections and
            # supplies a new opaque principal for the same verified issuer/subject.
            store_reopened = LocalBundleStore(ledger)
            authority = FixtureHost()
            manager_reopened = manager_for(store_reopened, authority)
            recovered_host = FixtureIntakeHost(authority)
            new_app = create_postgres_engine(app.url)
            try:
                recovery = make_service(
                    new_app, store_reopened, manager_reopened, recovered_host
                )
                discovered = await recovery.recover_pending(scope, limit=1)
                assert discovered.next_cursor is None
                (recovered,) = discovered.results
                assert isinstance(recovered, IntakeResult)
                assert recovered.task == pin
                assert (await recovery.list_pending(scope, limit=1)).request_ids == ()
                assert recovered.execution.pins.release_id == pin.release_id
                assert recovered.execution.pins.bundle_digest == pin.bundle_digest
                assert recovered.execution.status is ExecutionStatus.INITIALIZED

                fresh = await recovery.submit(
                    submission("new"), principal=authority.task_client
                )
                assert fresh.task.release_id == "release-2"
                assert fresh.task.bundle_digest != pin.bundle_digest

                replay = await recovery.submit(
                    original, principal=authority.task_client
                )
                assert replay == recovered
                assert (
                    recovered_host.media_calls == recovered_host.capabilities_calls == 2
                )

                future_clock = datetime(2120, 1, 1, tzinfo=UTC)
                skewed = manager_for(
                    store_reopened, authority, clock=lambda: future_clock
                )
                skewed_service = make_service(
                    new_app, store_reopened, skewed, recovered_host
                )
                result = await skewed_service.submit(
                    submission("skewed-release-clock"), principal=authority.task_client
                )
                assert result.task.accepted_at == future_clock
                assert result.execution.created_at < future_clock
                async with new_app.connect() as connection:
                    assert (
                        await connection.scalar(
                            text("""
                            SELECT ready_at <= pg_catalog.clock_timestamp()
                            FROM jimu_workflow.jobs WHERE execution_id = :execution_id
                        """),
                            {"execution_id": result.execution.execution_id},
                        )
                        is True
                    )
                recovered_host.denied = True
                with pytest.raises(IntakeAuthorizationError):
                    await recovery.submit(original, principal=authority.task_client)
                with pytest.raises(IntakeAuthorizationError):
                    await recovery.resume(scope, original.request_id)
            finally:
                await new_app.dispose()

            async with app.connect() as connection:
                intent = await repository.get_intent(
                    connection, scope, original.request_id
                )
                assert (
                    intent is not None
                    and intent.status is repository.IntentStatus.ACCEPTED
                )
                assert (intent.task_id, intent.execution_id) == (
                    recovered.task.task_id,
                    recovered.execution.execution_id,
                )
                assert intent.execution_id is not None
                scoped = {
                    "project_id": scope.project_id,
                    "client_id": scope.client_id,
                    "request_id": original.request_id,
                    "task_id": recovered.task.task_id,
                }
                counts = await connection.execute(
                    text("""
                    SELECT
                        (SELECT count(*) FROM jimu_workflow.tasks
                         WHERE project_id = :project_id AND client_id = :client_id
                           AND request_id = :request_id),
                        (SELECT count(*) FROM jimu_workflow.executions
                         WHERE project_id = :project_id AND client_id = :client_id
                           AND task_id = :task_id)
                """),
                    scoped,
                )
                assert counts.one() == (1, 1)
                acquired = await repository.get_execution(
                    connection, scope, intent.execution_id
                )
                assert (
                    acquired is not None and acquired.task_status is TaskStatus.QUEUED
                )
                assert acquired.scheduling_limits == SCHEDULING
                assert acquired.scheduling_limits_digest == sha256_digest(
                    SCHEDULING.stable_json().encode()
                )
                assert acquired.execution == recovered.execution
                events = await repository.list_events(
                    connection, scope, intent.execution_id
                )
                assert tuple(event.event_number for event in events) == (1, 2, 3)
                assert tuple(event.payload.next_status for event in events) == (
                    TaskStatus.ACCEPTED,
                    TaskStatus.QUEUED,
                    NodeRunStatus.READY,
                )
                row = (
                    await connection.execute(
                        text("""
                    SELECT node_id, node_runs.status, jobs.status, max_attempts, timeout_ms
                    FROM jimu_workflow.node_runs
                    JOIN jimu_workflow.jobs USING (project_id, client_id, execution_id, node_run_id)
                    WHERE execution_id = :execution_id
                """),
                        {"execution_id": intent.execution_id},
                    )
                ).one()
                assert row == (
                    NodeId.N01.value,
                    NodeRunStatus.READY.value,
                    "ready",
                    2,
                    500,
                )

    asyncio.run(run())


def test_pending_recovery_advances_past_failed_items_and_reopens_from_start(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = LocalBundleStore(tmp_path / "ledger")
    authority = FixtureHost()
    manager = manager_for(store, authority)
    scope = submission().idempotency_scope
    other_scope = IdempotencyScope(project_id=scope.project_id, client_id="edge-other")

    async def run() -> None:
        async with migrated_application() as app:
            host = FixtureIntakeHost(authority)
            service = make_service(app, store, manager, host)
            for request_id in ("c-finish", "a-unavailable", "b-revoked"):
                with pytest.raises(IntakeError, match="no_active_release"):
                    await service.submit(
                        submission(request_id), principal=authority.task_client
                    )
            host.scope = other_scope
            cross_scope = submission("a-other-client").model_copy(
                update={"idempotency_scope": other_scope}
            )
            with pytest.raises(IntakeError, match="no_active_release"):
                await service.submit(cross_scope, principal=authority.task_client)
            host.scope = scope

            publish(tmp_path / "publish", store, manager, authority, number=1)
            original_restore = host.restore_principal

            async def restore(
                identity: IntakeIdentity, original: TaskSubmission
            ) -> object:
                if original.request_id == "a-unavailable":
                    raise IntakeHostUnavailable()
                if original.request_id == "b-revoked":
                    raise IntakeAuthorizationError()
                return await original_restore(identity, original)

            for invalid_limit in (0, True, 65):
                with pytest.raises(ValueError, match="limit must be between"):
                    await service.list_pending(scope, limit=invalid_limit)

            with monkeypatch.context() as patch:
                patch.setattr(host, "restore_principal", restore)
                first = await service.recover_pending(scope, limit=2)
                assert first.results == (
                    IntakeRecoveryFailure("a-unavailable", "intake_host_unavailable"),
                    IntakeRecoveryFailure("b-revoked", "intake_authorization_denied"),
                )
                cursor = first.next_cursor
                assert cursor is not None and cursor.after_request_id == "b-revoked"
                with pytest.raises(IntakeError, match="intake_cursor_scope_mismatch"):
                    await service.list_pending(other_scope, limit=2, cursor=cursor)

                pages = await asyncio.gather(
                    service.recover_pending(scope, limit=2, cursor=cursor),
                    service.recover_pending(scope, limit=2, cursor=cursor),
                )
                assert all(page.next_cursor is None for page in pages)
                completed = tuple(result for page in pages for result in page.results)
                assert completed and all(result == completed[0] for result in completed)
                result = completed[0]
                assert isinstance(result, IntakeResult)
                assert result.task.request_id == "c-finish"
                assert result.task.release_id == "release-1"

            reopened = await service.recover_pending(scope, limit=2)
            assert reopened.next_cursor is None
            (retried,) = reopened.results
            assert isinstance(retried, IntakeResult)
            assert retried.task.request_id == "a-unavailable"
            assert (await service.list_pending(scope, limit=2)).request_ids == ()
            assert (await service.list_pending(other_scope, limit=2)).request_ids == (
                "a-other-client",
            )
            async with app.connect() as connection:
                terminal = await repository.get_intent(connection, scope, "b-revoked")
                assert (
                    terminal is not None
                    and terminal.status is repository.IntentStatus.FAILED
                )
                assert (
                    await connection.scalar(
                        text("SELECT count(*) FROM jimu_workflow.executions")
                    )
                    == 2
                )

    asyncio.run(run())
