import asyncio
from pathlib import Path

import pytest
from sqlalchemy import text

from jimu_runtime.bundle.store import LocalBundleStore
from jimu_runtime.domain.runtime.intake import IdempotencyScope
from jimu_runtime.domain.runtime.media import (
    FinalizedAsset,
    MediaAsset,
    MediaRef,
    StagedAsset,
)
from jimu_runtime.workflow import repository
from jimu_runtime.workflow.intake import (
    IntakeAuthorizationError,
    IntakeConflict,
    IntakeError,
    IntakeHostUnavailable,
)
from tests.fixtures.wp02.releases.support import FixtureHost
from tests.fixtures.wp03.intake.support import (
    IDENTITY,
    FixtureIntakeHost,
    make_service,
    manager_for,
    migrated_application,
    publish,
    submission,
)


def test_scoped_keys_and_subject_are_independent(tmp_path: Path) -> None:
    store = LocalBundleStore(tmp_path / "ledger")
    release_host = FixtureHost()
    manager = manager_for(store, release_host)
    publish(tmp_path / "publish", store, manager, release_host, number=1)
    one, two = submission("one"), submission("two")
    scope = one.idempotency_scope

    async def run() -> None:
        async with migrated_application() as app:
            host = FixtureIntakeHost(release_host)
            service = make_service(app, store, manager, host)
            principal = release_host.task_client
            left, right = await asyncio.gather(
                service.submit(one, principal=principal),
                service.submit(one, principal=principal),
            )
            assert left == right
            await service.submit(two, principal=principal)
            for conflict in (
                one.model_copy(update={"idempotency_key": "fresh-key"}),
                one.model_copy(update={"request_id": "fresh-request"}),
                one.model_copy(update={"idempotency_key": two.idempotency_key}),
                one.model_copy(
                    update={
                        "trace": one.trace.model_copy(update={"trace_id": "different"})
                    }
                ),
            ):
                with pytest.raises(IntakeConflict):
                    await service.submit(conflict, principal=principal)

            host.restored_identity = IDENTITY.model_copy(update={"subject": "another"})
            with pytest.raises(IntakeConflict):
                await service.submit(one, principal=principal)
            host.restored_identity = IDENTITY.model_copy(
                update={"issuer": "other-registry"}
            )
            with pytest.raises(IntakeConflict):
                await service.submit(one, principal=principal)

            different_client = IdempotencyScope(
                project_id=scope.project_id, client_id="edge-other"
            )
            other = one.model_copy(update={"idempotency_scope": different_client})
            with pytest.raises(IntakeAuthorizationError):
                await service.submit(other, principal=principal)
            with pytest.raises(IntakeError, match="intake_intent_not_found"):
                await service.resume(different_client, one.request_id)

            async with app.connect() as connection:
                assert (
                    await repository.get_execution(
                        connection, different_client, left.execution.execution_id
                    )
                    is None
                )
                assert (
                    await connection.scalar(
                        text("SELECT count(*) FROM jimu_workflow.executions")
                    )
                    == 2
                )
            host.scope = different_client
            host.restored_identity = IDENTITY
            accepted_other = await service.submit(other, principal=principal)
            assert accepted_other.task.task_id != left.task.task_id

            host.scope = scope
            nul = submission("nul-request").model_copy(
                update={"idempotency_key": "media\x00key"}
            )
            plain = submission("plain-request").model_copy(
                update={"idempotency_key": "mediakey"}
            )
            exact_nul = await service.submit(nul, principal=principal)
            assert exact_nul.task.idempotency_key == "media\x00key"
            assert await service.submit(nul, principal=principal) == exact_nul
            assert (
                await service.submit(plain, principal=principal)
            ).task != exact_nul.task

    asyncio.run(run())


def test_resume_distinguishes_unavailable_host_and_changed_identity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = LocalBundleStore(tmp_path / "ledger")
    release_host = FixtureHost()
    manager = manager_for(store, release_host)
    initial = submission("recover-host")
    scope = initial.idempotency_scope

    async def run() -> None:
        async with migrated_application() as app:
            host = FixtureIntakeHost(release_host)
            service = make_service(app, store, manager, host)
            verify = host.verified_original

            async def staged(scoped: IdempotencyScope, ref: MediaRef) -> MediaAsset:
                original = await verify(scoped, ref)
                assert isinstance(original.stage, FinalizedAsset)
                return original.model_copy(
                    update={
                        "stage": StagedAsset(
                            stage="staged", staged_at=original.stage.staged_at
                        ),
                    }
                )

            with monkeypatch.context() as patch:
                patch.setattr(host, "verified_original", staged)
                with pytest.raises(IntakeError, match="intake_original_mismatch"):
                    await service.submit(initial, principal=release_host.task_client)

            with pytest.raises(
                IntakeError, match="no_active_release"
            ) as unavailable_release:
                await service.submit(initial, principal=release_host.task_client)
            assert unavailable_release.value.retryable
            host.restored_unavailable = True
            with pytest.raises(IntakeHostUnavailable) as unavailable:
                await service.resume(scope, initial.request_id)
            assert unavailable.value.retryable
            async with app.connect() as connection:
                original = await repository.get_intent(
                    connection, scope, initial.request_id
                )
                assert (
                    original is not None
                    and original.status is repository.IntentStatus.PENDING
                )
            host.restored_unavailable = False
            host.restored_identity = IDENTITY.model_copy(update={"issuer": "changed"})
            with pytest.raises(IntakeError, match="intake_principal_changed"):
                await service.resume(scope, initial.request_id)
            async with app.connect() as connection:
                failed = await repository.get_intent(
                    connection, scope, initial.request_id
                )
                assert (
                    failed is not None
                    and failed.status is repository.IntentStatus.FAILED
                )
                assert failed.failure_code == "intake_principal_changed"
                assert failed.task_id is None and failed.execution_id is None

    asyncio.run(run())
