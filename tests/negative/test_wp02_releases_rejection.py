from pathlib import Path

import pytest

from jimu_runtime.bundle.releases import ReleaseOperationError
from jimu_runtime.bundle.store import LocalBundleStore
from tests.fixtures.wp02.releases.support import (
    DEVICE,
    GLOBAL,
    REGION,
    FixtureHost,
    activate,
    approve,
    register,
    store_receipt,
    submission,
)


@pytest.fixture
def services(tmp_path: Path):
    host = FixtureHost()
    store = LocalBundleStore(tmp_path / "local")
    return host, store, host.manager(store)


@pytest.mark.parametrize(
    "precedence",
    [("device", "region", "scene"), ("device", "scene", "scene", "global")],
)
def test_incomplete_or_ambiguous_precedence_fails_configuration(
    services, precedence
) -> None:
    host, store, _ = services
    with pytest.raises(ValueError, match="every scope exactly once"):
        host.manager(store, precedence)


def test_approval_and_registration_are_required_and_actor_ref_cannot_authenticate(
    services,
) -> None:
    host, store, manager = services
    receipt_id = store_receipt(store, 1)
    with pytest.raises(ReleaseOperationError, match="accepted_admission_required"):
        register(manager, host, 1, "shape-valid-but-absent")
    with pytest.raises(ReleaseOperationError, match="accepted_admission_required"):
        register(manager, host, 2, store_receipt(store, 2, accepted=False))

    register(manager, host, 1, receipt_id)
    with pytest.raises(ReleaseOperationError, match="release_scope_not_approved"):
        activate(manager, host, 1, GLOBAL)
    assert manager.active_activation(GLOBAL) is None

    with pytest.raises(ReleaseOperationError, match="unauthorized_release_action"):
        manager.approve_release(
            operation_key="invalid-scope",
            release_id="release-1",
            scope=DEVICE,
            principal=host.region_approver,
        )
    approval = approve(manager, host, 1, GLOBAL)
    with pytest.raises(ReleaseOperationError, match="release_scope_not_approved"):
        activate(manager, host, 1, REGION)
    with pytest.raises(ReleaseOperationError, match="unauthorized_release_action"):
        manager.activate(
            operation_key="invalid-principal",
            release_id="release-1",
            scope=GLOBAL,
            activation_id="forged-principal",
            expected_previous_activation_id=None,
            principal=approval.approval_receipt.approved_by,
        )


def test_wrong_scope_rollback_and_operation_key_conflict(services) -> None:
    host, store, manager = services
    for number, scope in ((1, REGION), (2, GLOBAL)):
        register(manager, host, number, store_receipt(store, number))
        approve(manager, host, number, scope)
        activate(manager, host, number, scope)

    with pytest.raises(ReleaseOperationError, match="rollback_target_not_in_scope"):
        manager.rollback(
            operation_key="rollback-cross-scope",
            scope=GLOBAL,
            target_activation_id="activation-1-region",
            activation_id="rolled-back",
            expected_previous_activation_id="activation-2-global",
            principal=host.controller,
        )
    with pytest.raises(ReleaseOperationError, match="operation_key_conflict"):
        manager.activate(
            operation_key="activate-2-global",
            release_id="release-2",
            scope=GLOBAL,
            activation_id="different-activation",
            expected_previous_activation_id=None,
            principal=host.controller,
        )
    with pytest.raises(ReleaseOperationError, match="operation_key_conflict"):
        manager.approve_release(
            operation_key="register-2",
            release_id="release-2",
            scope=GLOBAL,
            principal=host.approver,
        )
    with pytest.raises(ReleaseOperationError, match="release_id_conflict"):
        manager.register_release(
            operation_key="new-registration",
            release_id="release-2",
            admission_receipt_id="receipt-1",
            principal=host.registrar,
        )
    assert manager.active_activation(GLOBAL).activation_id == "activation-2-global"


def test_requested_release_does_not_bypass_winning_scope_or_permissions(
    services,
) -> None:
    host, store, manager = services
    for release, scope in ((1, GLOBAL), (2, DEVICE)):
        register(manager, host, release, store_receipt(store, release))
        approve(manager, host, release, scope)
        activate(manager, host, release, scope)

    with pytest.raises(ReleaseOperationError, match="requested_release_not_selected"):
        manager.accept_task(
            submission("override-global", release="release-1"),
            principal=host.override_client,
        )
    with pytest.raises(ReleaseOperationError, match="unauthorized_release_action"):
        manager.accept_task(
            submission("unauthorized", release="release-2"), principal=host.task_client
        )
    permitted = manager.accept_task(
        submission("permitted", release="release-2"), principal=host.override_client
    )
    assert permitted.release_id == "release-2"


def test_trusted_context_and_idempotency_on_both_request_keys(services) -> None:
    host, store, manager = services
    register(manager, host, 1, store_receipt(store, 1))
    approve(manager, host, 1, GLOBAL)
    activate(manager, host, 1, GLOBAL)

    original = submission()
    forged = original.model_copy(
        update={
            "context": original.context.model_copy(update={"camera_id": "other-camera"})
        }
    )
    with pytest.raises(ReleaseOperationError, match="task_scope_mismatch"):
        manager.accept_task(forged, principal=host.task_client)
    accepted = manager.accept_task(original, principal=host.task_client)
    with pytest.raises(ReleaseOperationError, match="task_principal_mismatch"):
        manager.accept_task(original, principal=host.override_client)
    for field in ("request_id", "idempotency_key"):
        modified = original.model_copy(update={field: "changed"})
        with pytest.raises(ReleaseOperationError, match="task_idempotency_conflict"):
            manager.accept_task(modified, principal=host.task_client)
    assert manager.get_task(accepted.task_id) == accepted


def test_new_task_policy_does_not_silently_fall_back_to_a_lower_scope(services) -> None:
    host, store, manager = services
    with pytest.raises(ReleaseOperationError, match="no_active_release"):
        manager.accept_task(submission(), principal=host.task_client)
    for number, scope in ((1, GLOBAL), (2, DEVICE)):
        record = register(manager, host, number, store_receipt(store, number))
        approve(manager, host, number, scope)
        activate(manager, host, number, scope)
    host.revoked.add(record.bundle_ref.bundle_digest)
    with pytest.raises(ReleaseOperationError, match="new_task_disallowed"):
        manager.accept_task(submission(), principal=host.task_client)
