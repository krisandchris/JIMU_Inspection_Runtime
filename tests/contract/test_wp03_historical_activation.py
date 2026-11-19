from pathlib import Path

import pytest

from jimu_runtime.bundle.store import LocalBundleStore, ReleaseLedger
from tests.fixtures.wp02.releases.support import (
    GLOBAL,
    SCENE,
    FixtureHost,
    activate,
    approve,
    register,
    store_receipt,
    submission,
)


def test_historical_activation_survives_switch_rollback_and_reopen(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "local"
    host = FixtureHost()
    store = LocalBundleStore(path)
    manager = host.manager(store)
    for number in (1, 2):
        register(manager, host, number, store_receipt(store, number))
        approve(manager, host, number, SCENE)
    approve(manager, host, 1, GLOBAL)
    activate(manager, host, 1, GLOBAL)

    first = activate(manager, host, 1, SCENE)
    first_task = manager.accept_task(submission("first"), principal=host.task_client)
    second = activate(manager, host, 2, SCENE, expected=first.activation_id)
    second_task = manager.accept_task(submission("second"), principal=host.task_client)
    restored = manager.rollback(
        operation_key="restore-scene",
        scope=SCENE,
        target_activation_id=first.activation_id,
        activation_id="restored",
        expected_previous_activation_id=second.activation_id,
        principal=host.controller,
    )

    assert restored.release_ref == first.release_ref
    assert restored.activation_id != first.activation_id
    assert manager.active_activation(SCENE) == restored
    host.trusted_scope = None
    host.revoked.update((first_task.bundle_digest, second_task.bundle_digest))
    reopened = host.manager(LocalBundleStore(path))
    real_get = ReleaseLedger.get

    def immutable_history_only(self, namespace, key):
        assert namespace != "active_scope"
        return real_get(self, namespace, key)

    monkeypatch.setattr(ReleaseLedger, "get", immutable_history_only)
    assert reopened.get_task_activation(first_task) == first
    assert reopened.get_task_activation(second_task) == second
    assert reopened.get_task_activation(first_task).scope == SCENE
