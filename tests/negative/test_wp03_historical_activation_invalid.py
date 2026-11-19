import json
from datetime import timedelta
from pathlib import Path

import pytest

from jimu_runtime.bundle.store import LocalBundleStore, ReleaseLedger
from tests.fixtures.wp02.releases.support import (
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


@pytest.fixture
def historical(tmp_path: Path):
    host = FixtureHost()
    store = LocalBundleStore(tmp_path / "local")
    manager = host.manager(store)
    register(manager, host, 1, store_receipt(store, 1))
    approve(manager, host, 1, SCENE)
    approve(manager, host, 1, REGION)
    activation = activate(manager, host, 1, SCENE)
    task = manager.accept_task(submission("first"), principal=host.task_client)
    return manager, task, activation


def test_fake_task_is_rejected_without_returning_history(historical) -> None:
    manager, task, activation = historical
    other_identity = task.idempotency_scope.model_copy(update={"project_id": "other"})
    wrong_scope = task.context.model_copy(update={"region_id": "region-other"})
    wrong_media = task.media_ref.model_copy(update={"media_id": "different"})
    for replacement in (
        {"task_id": "task.unknown"},
        {"request_id": "different"},
        {"idempotency_key": "different"},
        {"idempotency_scope": other_identity},
        {"context": wrong_scope},
        {"bundle_digest": "d" * 64},
        {"release_id": "release-other"},
        {"accepted_at": task.accepted_at + timedelta(seconds=1)},
        {"media_ref": wrong_media},
        {"status": "not-a-task-status"},
    ):
        assert manager.get_task_activation(task.model_copy(update=replacement)) is None
    assert manager.get_task_activation(task) == activation


@pytest.mark.parametrize("broken_index", ("missing", "disagree"))
def test_missing_or_disagreeing_request_indices_fail_closed(
    historical, broken_index: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager, task, _ = historical
    real_get = ReleaseLedger.get

    def inconsistent_index(self, namespace, key):
        original = real_get(self, namespace, key)
        if namespace == "task_pin" and key.startswith("idempotency."):
            if broken_index == "missing":
                return None
            fields = json.loads(original)
            fields["activation_id"] = "other"
            return json.dumps(fields).encode()
        return original

    monkeypatch.setattr(ReleaseLedger, "get", inconsistent_index)
    assert manager.get_task_activation(task) is None


@pytest.mark.parametrize("changed_field", ("scene", "activation_id", "bundle_version"))
def test_inconsistent_activation_history_fails_closed(
    historical, changed_field: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager, task, activation = historical
    real_get = ReleaseLedger.get

    def unexpected_receipt(self, namespace, key):
        if namespace == "activation" and key == activation.activation_id:
            if changed_field == "scene":
                altered = activation.model_copy(update={"scope": scene_scope("f" * 64)})
            elif changed_field == "activation_id":
                altered = activation.model_copy(update={"activation_id": "other"})
            else:
                wrong_bundle = activation.bundle_ref.model_copy(
                    update={"bundle_version": "2.0.0"}
                )
                altered = activation.model_copy(update={"bundle_ref": wrong_bundle})
            return altered.model_dump_json().encode()
        return real_get(self, namespace, key)

    monkeypatch.setattr(ReleaseLedger, "get", unexpected_receipt)
    assert manager.get_task_activation(task) is None
