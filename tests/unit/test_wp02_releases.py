from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier

import pytest

from jimu_runtime.bundle.releases import ReleaseOperationError
from jimu_runtime.bundle.store import LocalBundleStore, ReleaseLedger
from jimu_runtime.domain.runtime.releases import ReleaseStatus
from tests.fixtures.wp02.releases.support import (
    DEVICE,
    GLOBAL,
    REGION,
    SCENE,
    FixtureHost,
    activate,
    approve,
    register,
    scene_scope,
    store_receipt,
    submission,
)


def test_exact_scope_precedence_uses_trusted_targets_and_is_configurable(
    tmp_path: Path,
) -> None:
    store = LocalBundleStore(tmp_path / "local")
    host = FixtureHost()
    manager = host.manager(store)
    for number, scope in enumerate(
        (GLOBAL, scene_scope("f" * 64), SCENE, REGION, DEVICE), 1
    ):
        registered = register(manager, host, number, store_receipt(store, number))
        approved = approve(manager, host, number, scope)
        assert registered.status is ReleaseStatus.ACCEPTED
        assert approved.status is ReleaseStatus.APPROVED
        assert manager.get_release(registered.release_id) == registered
        assert manager.get_release(registered.release_id, scope=scope) == approved
        activate(manager, host, number, scope)
        selected = manager.accept_task(
            submission(f"request-{number}"), principal=host.task_client
        )
        assert selected.release_id == f"release-{1 if number == 2 else number}"

    alternate = host.manager(store, ("scene", "region", "global", "device"))
    assert (
        alternate.accept_task(
            submission("different-order"), principal=host.task_client
        ).release_id
        == "release-3"
    )


def test_concurrent_replays_and_competing_cas_are_atomic(tmp_path: Path) -> None:
    path = tmp_path / "local"
    store = LocalBundleStore(path)
    host = FixtureHost()
    manager = host.manager(store)
    second_manager = host.manager(LocalBundleStore(path))
    for number in (1, 2, 3):
        register(manager, host, number, store_receipt(store, number))
        approve(manager, host, number, GLOBAL)

    same_operation = Barrier(2)

    def initial(manager):
        same_operation.wait()
        return activate(manager, host, 1, GLOBAL)

    with ThreadPoolExecutor(max_workers=2) as workers:
        first = workers.submit(initial, manager)
        duplicate = workers.submit(initial, second_manager)
        assert first.result(timeout=5) == duplicate.result(timeout=5)
        initial_head = first.result().activation_id

    competing = Barrier(2)

    def next_activation(manager, release):
        competing.wait()
        try:
            return activate(manager, host, release, GLOBAL, expected=initial_head)
        except ReleaseOperationError as error:
            return error

    with ThreadPoolExecutor(max_workers=2) as workers:
        futures = (
            workers.submit(next_activation, manager, 2),
            workers.submit(next_activation, second_manager, 3),
        )
        outcomes = [future.result(timeout=5) for future in futures]

    failures = [
        result for result in outcomes if isinstance(result, ReleaseOperationError)
    ]
    assert [failure.code for failure in failures] == ["stale_activation_predecessor"]
    (winner,) = [
        result for result in outcomes if not isinstance(result, ReleaseOperationError)
    ]
    assert second_manager.active_activation(GLOBAL) == winner
    assert winner.previous_activation_ref.record_id == initial_head
    loser = {2, 3} - {int(winner.release_ref.record_id[-1])}
    assert manager.get_activation(f"activation-{loser.pop()}-global") is None


def test_activation_failure_rolls_back_history_pointer_and_operation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    host = FixtureHost()
    store = LocalBundleStore(tmp_path / "local")
    manager = host.manager(store)
    register(manager, host, 1, store_receipt(store, 1))
    approve(manager, host, 1, GLOBAL)

    real_cas = ReleaseLedger.compare_and_set

    def fail_pointer(self, namespace, key, *, expected, value):
        if namespace == "active_scope":
            raise OSError("isolated test fault between writes")
        real_cas(self, namespace, key, expected=expected, value=value)

    with monkeypatch.context() as patch:
        patch.setattr(ReleaseLedger, "compare_and_set", fail_pointer)
        with pytest.raises(OSError, match="isolated test fault"):
            activate(manager, host, 1, GLOBAL)

    assert manager.get_activation("activation-1-global") is None
    assert manager.active_activation(GLOBAL) is None
    assert activate(manager, host, 1, GLOBAL) == manager.active_activation(GLOBAL)


def test_task_pin_and_exact_idempotent_retry_survive_policy_revocation(
    tmp_path: Path,
) -> None:
    path = tmp_path / "local"
    host = FixtureHost()
    store = LocalBundleStore(path)
    manager = host.manager(store)
    registered = register(manager, host, 1, store_receipt(store, 1))
    approve(manager, host, 1, GLOBAL)
    activate(manager, host, 1, GLOBAL)
    accepted = manager.accept_task(submission(), principal=host.task_client)
    host.revoked.add(registered.bundle_ref.bundle_digest)

    reopened = host.manager(LocalBundleStore(path))
    with pytest.raises(ReleaseOperationError, match="new_task_disallowed"):
        reopened.accept_task(submission("after-revocation"), principal=host.task_client)
    host.trusted_scope = None
    assert reopened.get_task(accepted.task_id) == accepted
    assert reopened.accept_task(submission(), principal=host.task_client) == accepted


def test_one_release_has_independent_immutable_approval_for_each_scope(
    tmp_path: Path,
) -> None:
    store = LocalBundleStore(tmp_path / "local")
    host = FixtureHost()
    manager = host.manager(store)
    original = register(manager, host, 1, store_receipt(store, 1))
    global_approval = approve(manager, host, 1, GLOBAL)
    region_approval = approve(manager, host, 1, REGION)
    assert global_approval.approval_receipt != region_approval.approval_receipt
    assert (
        global_approval.admission_receipt
        == region_approval.admission_receipt
        == original.admission_receipt
    )
    assert manager.get_release("release-1", scope=GLOBAL) == global_approval
    assert manager.get_release("release-1", scope=REGION) == region_approval
    assert manager.get_release("release-1") == original
    assert manager.get_release("release-1", scope=DEVICE) is None
    assert approve(manager, host, 1, REGION) == region_approval
    with pytest.raises(ReleaseOperationError, match="release_scope_already_approved"):
        manager.approve_release(
            operation_key="rewrite-approval",
            release_id="release-1",
            scope=REGION,
            principal=host.approver,
        )
