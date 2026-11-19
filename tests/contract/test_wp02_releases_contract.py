from dataclasses import replace
from pathlib import Path

from jimu_runtime.bundle.compiler import compile_bundle
from jimu_runtime.bundle.store import LocalBundleStore
from tests.fixtures.wp02.admission.support import sign_bundle, submit
from tests.fixtures.wp02.compiler.scenario import (
    LIMITS,
    compile_environment,
    source_scenario,
)
from tests.fixtures.wp02.releases.support import (
    GLOBAL,
    FixtureHost,
    activate,
    approve,
    register,
    submission,
)


def test_compile_admit_approve_activate_pin_rollback_and_reopen(tmp_path: Path) -> None:
    store_path = tmp_path / "local-ledger"
    store = LocalBundleStore(store_path)
    host = FixtureHost()
    manager = host.manager(store)
    approved = []
    activations = []
    original_archives = []
    for number in (1, 2):
        workspace = tmp_path / f"compile-{number}"
        workspace.mkdir()
        fixture = source_scenario()
        candidate = replace(
            fixture, metadata=replace(fixture.metadata, bundle_version=f"1.0.{number}")
        )
        compile_result = compile_bundle(
            candidate, limits=LIMITS, environment=compile_environment(workspace)
        )
        assert compile_result.bundle is not None, compile_result.errors
        bundle = compile_result.bundle
        signed = sign_bundle(
            workspace, bundle.archive_bytes, bundle.bundle_ref.bundle_digest
        )
        admitted = submit(store, signed.archive, f"admit-{number}", signed.environment)
        assert admitted.receipt is not None and admitted.accepted, (
            admitted.verification.result
        )
        original_archives.append((admitted.receipt.receipt_id, signed.archive))

        registered = register(manager, host, number, admitted.receipt.receipt_id)
        assert registered.bundle_ref == bundle.bundle_ref
        approval = approve(manager, host, number, GLOBAL)
        approved.append(approval)
        expected = activations[-1].activation_id if activations else None
        activations.append(activate(manager, host, number, GLOBAL, expected=expected))
        if number == 1:
            first_task = manager.accept_task(
                submission("accepted-first"), principal=host.task_client
            )

    second_task = manager.accept_task(
        submission("accepted-second"), principal=host.task_client
    )
    assert first_task.release_id == "release-1"
    assert first_task.bundle_digest == approved[0].bundle_ref.bundle_digest
    assert second_task.release_id == "release-2"
    assert second_task.bundle_digest == approved[1].bundle_ref.bundle_digest
    assert first_task.bundle_digest != second_task.bundle_digest
    assert manager.get_task(first_task.task_id) == first_task

    reopened_store = LocalBundleStore(store_path)
    reopened = host.manager(reopened_store)
    rolled_back = reopened.rollback(
        operation_key="rollback-second",
        scope=GLOBAL,
        target_activation_id=activations[0].activation_id,
        activation_id="rollback-to-first",
        expected_previous_activation_id=activations[1].activation_id,
        principal=host.controller,
    )
    assert rolled_back.release_ref == activations[0].release_ref
    assert rolled_back.previous_activation_ref.record_id == activations[1].activation_id
    assert reopened.active_activation(GLOBAL) == rolled_back
    assert (
        reopened.accept_task(
            submission("accepted-third"), principal=host.task_client
        ).bundle_digest
        == first_task.bundle_digest
    )

    for activation, release, (receipt_id, archive), task in zip(
        activations, approved, original_archives, (first_task, second_task), strict=True
    ):
        assert reopened.get_activation(activation.activation_id) == activation
        assert reopened.get_release(release.release_id, scope=GLOBAL) == release
        assert reopened_store.read_archive(receipt_id) == archive
        assert reopened.get_task(task.task_id) == task
        assert (
            reopened.accept_task(
                submission(task.request_id), principal=host.task_client
            )
            == task
        )

    assert (
        reopened.rollback(
            operation_key="rollback-second",
            scope=GLOBAL,
            target_activation_id=activations[0].activation_id,
            activation_id="rollback-to-first",
            expected_previous_activation_id=activations[1].activation_id,
            principal=host.controller,
        )
        == rolled_back
    )
