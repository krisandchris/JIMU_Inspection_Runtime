import os
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from dataclasses import replace
from multiprocessing import get_context
from threading import Barrier
from time import monotonic, sleep

import pytest
from fixtures.wp04_resources.scenario import cpu, gpu, remote, worker_inventory

from jimu_runtime.workers.resource_inventory import (
    GPUDevice,
    GPUWorkerPool,
    HostInventory,
    LeaseLifetime,
    RemoteCapacity,
    RemoteQuota,
    ResourceScope,
)
from jimu_runtime.workers.resources import (
    LeaseState,
    ResourceAdmission,
    ResourceUnavailable,
)


def _compete(path, inventory, start, release, output, owner):
    admission = ResourceAdmission(path, inventory)
    if not start.wait(5):
        output.put("barrier")
        return
    try:
        lease = admission.reserve(cpu(), "test.cpu", owner_id=owner)
    except ResourceUnavailable as error:
        output.put(error.reason)
    else:
        output.put("admitted")
        if release.wait(5):
            assert admission.release(lease)


def test_admission_is_atomic_between_two_independent_processes(tmp_path):
    inventory = worker_inventory(ttl=10_000)
    path = tmp_path / "resources.db"
    admission = ResourceAdmission.provision(path, inventory)
    ctx = get_context("spawn")
    start, release = ctx.Event(), ctx.Event()
    output = ctx.Queue()
    workers = [
        ctx.Process(
            target=_compete, args=(path, inventory, start, release, output, str(index))
        )
        for index in range(2)
    ]
    try:
        for worker in workers:
            worker.start()
        start.set()
        assert sorted(output.get(timeout=10) for _ in workers) == [
            "admitted",
            "capacity_exhausted",
        ]
    finally:
        release.set()
        for worker in workers:
            worker.join(5)
            if worker.is_alive():
                worker.terminate()
                worker.join(5)
    assert all(worker.exitcode == 0 for worker in workers)
    after = admission.reserve(cpu(), "test.cpu", owner_id="after")
    assert admission.release(after)


def test_reopened_inventory_requires_exact_config_and_never_creates_state(tmp_path):
    path = tmp_path / "resources.db"
    inventory = worker_inventory()
    with pytest.raises(ResourceUnavailable, match="ledger_unavailable"):
        ResourceAdmission(path, inventory)
    assert not path.exists()
    ResourceAdmission.provision(path, inventory)
    with pytest.raises(FileExistsError):
        ResourceAdmission.provision(path, inventory)
    with pytest.raises(ResourceUnavailable, match="inventory_mismatch"):
        ResourceAdmission(path, replace(inventory, cpu_slots=2))
    with pytest.raises(ResourceUnavailable, match="inventory_mismatch"):
        ResourceAdmission(path, replace(inventory, inventory_id="second.host"))


def test_expired_reservation_and_stale_generation_cannot_release_new_holder(tmp_path):
    admission = ResourceAdmission.provision(
        tmp_path / "resources.db", worker_inventory(ttl=50)
    )
    old = admission.reserve(cpu(), "test.cpu", owner_id="worker.restarted")
    sleep(0.075)
    assert not admission.renew(old)
    assert admission.reconcile() == (old.lease_id,)
    current = admission.reserve(cpu(), "test.cpu", owner_id=old.owner_id)
    assert current.lease_id != old.lease_id
    assert not admission.release(old)
    assert not admission.begin_draining(
        replace(current, owner_generation=old.owner_generation)
    )
    assert admission.state(current) == LeaseState.RESERVED
    assert not admission.release(replace(current, scope_id="different"))
    assert admission.release_reserved(current)
    assert not admission.release(current)


def test_only_registered_budget_and_pinned_scope_concurrency_are_accepted(tmp_path):
    admission = ResourceAdmission.provision(
        tmp_path / "resources.db", worker_inventory(slots=4)
    )
    with pytest.raises(ResourceUnavailable, match="no_host_budget"):
        admission.reserve(cpu(), "test.missing", owner_id="worker")
    with pytest.raises(ResourceUnavailable, match="no_host_budget"):
        admission.reserve(remote("quota.other"), "test.cpu", owner_id="worker")
    current = admission.reserve(cpu(concurrency=1), "test.cpu", owner_id="worker")
    with pytest.raises(ResourceUnavailable, match="capacity_exhausted"):
        admission.reserve(cpu(concurrency=3), "test.cpu", owner_id="other")
    assert admission.release(current)
    assert admission.release(admission.reserve(cpu(), "test.cpu", owner_id="other"))


def test_gpu_model_residency_and_tool_run_share_synthetic_physical_memory(tmp_path):
    inventory = HostInventory(
        "synthetic.gpu",
        5000,
        0,
        (
            ResourceScope("model", "gpu", 2, 1, "model.pool"),
            ResourceScope("tool", "gpu", 2, 0, "tool.pool"),
        ),
        gpu_devices=(GPUDevice("synthetic.0", 100, 3, 1),),
        gpu_worker_pools=(
            GPUWorkerPool("model.pool", "synthetic.0", 2, 1),
            GPUWorkerPool("tool.pool", "synthetic.0", 1, 0),
        ),
    )
    admission = ResourceAdmission.provision(tmp_path / "resources.db", inventory)
    resident = admission.reserve(
        gpu("model.pool", 70),
        "model",
        owner_id="model-server",
        lifetime=LeaseLifetime.RESIDENT,
    )
    with pytest.raises(ResourceUnavailable, match="capacity_exhausted"):
        admission.reserve(gpu("tool.pool", 31), "tool", owner_id="vision-worker")
    tool = admission.reserve(gpu("tool.pool", 30), "tool", owner_id="vision-worker")
    with pytest.raises(ResourceUnavailable, match="capacity_exhausted"):
        admission.reserve(gpu("tool.pool", 1), "tool", owner_id="second-tool")
    with pytest.raises(ResourceUnavailable, match="no_host_budget"):
        admission.reserve(gpu("model.pool", 30), "tool", owner_id="vision-worker")
    assert admission.release(resident)
    assert admission.release(tool)


def test_provider_inflight_needs_exact_terminal_proof_after_restart(tmp_path):
    inventory = HostInventory(
        "remote.host",
        50,
        0,
        (
            ResourceScope("provider", "remote", 2, pool_or_quota_id="provider.quota"),
            ResourceScope("transport", "remote", 2, pool_or_quota_id="local.quota"),
        ),
        (
            RemoteQuota("provider.quota", 1, RemoteCapacity.PROVIDER_INFLIGHT),
            RemoteQuota("local.quota", 1, RemoteCapacity.LOCAL_TRANSPORT),
        ),
    )
    path = tmp_path / "resources.db"
    before = ResourceAdmission.provision(path, inventory)
    remote_lease = before.reserve(
        remote("provider.quota"), "provider", owner_id="worker"
    )
    before.activate_remote(remote_lease, "dispatch.abc")
    local_lease = before.reserve(remote("local.quota"), "transport", owner_id="worker")
    before.activate_remote(local_lease, "dispatch.def")
    assert before.release(local_lease)
    assert not before.release(remote_lease)
    sleep(0.075)

    terminal = {("provider", "dispatch.abc", "trusted.completion")}
    reopened = ResourceAdmission(
        path, inventory, provider_terminal_verifier=lambda *proof: proof in terminal
    )
    assert reopened.reconcile() == ()
    assert reopened.state(remote_lease) == LeaseState.DRAINING
    assert not reopened.renew(remote_lease)
    with pytest.raises(ResourceUnavailable, match="capacity_exhausted"):
        reopened.reserve(remote("provider.quota"), "provider", owner_id="restarted")
    assert not reopened.settle_provider(
        remote_lease.lease_id, "dispatch.def", "trusted.completion"
    )
    assert not reopened.settle_provider(
        remote_lease.lease_id, "dispatch.abc", "timeout"
    )
    assert reopened.settle_provider(
        remote_lease.lease_id, "dispatch.abc", "trusted.completion"
    )
    assert not reopened.settle_provider(
        remote_lease.lease_id, "dispatch.abc", "trusted.completion"
    )
    successor = reopened.reserve(
        remote("provider.quota"), "provider", owner_id="restarted"
    )
    with pytest.raises(ResourceUnavailable, match="provider_dispatch_reused"):
        reopened.activate_remote(successor, "dispatch.abc")
    assert reopened.state(successor) == LeaseState.RESERVED
    reopened.activate_remote(successor, "dispatch.next")
    assert not reopened.settle_provider(
        successor.lease_id, "dispatch.abc", "trusted.completion"
    )
    assert not reopened.settle_provider(
        successor.lease_id, "dispatch.next", "trusted.completion"
    )
    assert not reopened.release(successor)


def test_provision_publishes_only_committed_database(tmp_path, monkeypatch):
    target = tmp_path / "resources.db"
    inventory = worker_inventory()
    original = os.link

    def interrupt_before_publish(staged, destination):
        assert destination == target
        assert not destination.exists()
        with closing(sqlite3.connect(staged)) as ledger:
            assert ledger.execute("SELECT inventory_id FROM inventory").fetchone() == (
                inventory.inventory_id,
            )
        raise OSError("interrupted before publishing")

    with monkeypatch.context() as patch:
        patch.setattr(os, "link", interrupt_before_publish)
        with pytest.raises(OSError, match="interrupted before publishing"):
            ResourceAdmission.provision(target, inventory)
    assert not target.exists()
    admission = ResourceAdmission.provision(target, inventory)
    assert os.link is original
    assert admission.release(admission.reserve(cpu(), "test.cpu", owner_id="worker"))


def test_other_pid_namespace_cannot_reap_local_transport(tmp_path):
    path = tmp_path / "resources.db"
    inventory = HostInventory(
        "transport.host",
        1000,
        0,
        (ResourceScope("transport", "remote", 1, pool_or_quota_id="quota"),),
        (RemoteQuota("quota", 1, RemoteCapacity.LOCAL_TRANSPORT),),
    )
    owner = ResourceAdmission.provision(path, inventory)
    lease = owner.reserve(remote("quota"), "transport", owner_id="first")
    owner.activate_remote(lease, "dispatch.1")

    observer = ResourceAdmission(path, inventory)
    observer._identity = replace(
        observer._identity, pid_namespace=observer._identity.pid_namespace + 1
    )
    assert not observer.release(lease)
    assert observer.reconcile() == ()
    with pytest.raises(ResourceUnavailable, match="capacity_exhausted"):
        observer.reserve(remote("quota"), "transport", owner_id="second")
    assert owner.release(lease)
    replacement = observer.reserve(remote("quota"), "transport", owner_id="second")
    assert not observer.release(lease)
    assert observer.release(replacement)


@pytest.mark.parametrize(
    "mode", [RemoteCapacity.LOCAL_TRANSPORT, RemoteCapacity.PROVIDER_INFLIGHT]
)
def test_abort_requires_exact_current_armed_dispatch(tmp_path, mode):
    path = tmp_path / "resources.db"
    inventory = HostInventory(
        "armed.host",
        1000,
        0,
        (ResourceScope("scope", "remote", 1, pool_or_quota_id="quota"),),
        (RemoteQuota("quota", 1, mode),),
    )
    owner = ResourceAdmission.provision(path, inventory)
    lease = owner.reserve(remote("quota"), "scope", owner_id="owner")
    assert not owner.abort_remote_before_send(lease, "dispatch.first")
    owner.prepare_remote_send(lease, "dispatch.first")
    assert owner.state(lease) == LeaseState.ARMED
    assert not owner.abort_remote_before_send(
        replace(lease, owner_generation="invalid"), "dispatch.first"
    )
    assert not owner.abort_remote_before_send(lease, "dispatch.other")
    with pytest.raises(ResourceUnavailable, match="lease_inactive"):
        owner.activate_remote(lease, "dispatch.other")
    with pytest.raises(ResourceUnavailable, match="lease_inactive"):
        owner.activate_remote(lease, "dispatch.first", deadline=monotonic() - 1)
    assert owner.abort_remote_before_send(lease, "dispatch.first")
    assert not owner.abort_remote_before_send(lease, "dispatch.first")
    with pytest.raises(ResourceUnavailable, match="lease_inactive"):
        owner.activate_remote(lease, "dispatch.first")

    reopened = ResourceAdmission(path, inventory)
    replacement = reopened.reserve(remote("quota"), "scope", owner_id=lease.owner_id)
    if mode == RemoteCapacity.PROVIDER_INFLIGHT:
        with pytest.raises(ResourceUnavailable, match="provider_dispatch_reused"):
            reopened.prepare_remote_send(replacement, "dispatch.first")
    with pytest.raises(ResourceUnavailable, match="lease_inactive"):
        reopened.prepare_remote_send(
            replacement, "dispatch.next", deadline=monotonic() - 1
        )
    reopened.prepare_remote_send(replacement, "dispatch.next")
    assert not reopened.abort_remote_before_send(lease, "dispatch.next")
    reopened.activate_remote(replacement, "dispatch.next")
    assert not reopened.abort_remote_before_send(replacement, "dispatch.next")
    if mode == RemoteCapacity.LOCAL_TRANSPORT:
        assert reopened.release(replacement)
    else:
        assert not reopened.release(replacement)
        assert not reopened.abort_remote_before_send(replacement, "dispatch.next")
        with pytest.raises(ResourceUnavailable, match="capacity_exhausted"):
            reopened.reserve(remote("quota"), "scope", owner_id="other")


def test_abort_and_authorize_cannot_both_win(tmp_path):
    path = tmp_path / "resources.db"
    inventory = HostInventory(
        "armed.race",
        5000,
        0,
        (ResourceScope("scope", "remote", 1, pool_or_quota_id="quota"),),
        (RemoteQuota("quota", 1, RemoteCapacity.PROVIDER_INFLIGHT),),
    )
    owner = ResourceAdmission.provision(path, inventory)
    lease = owner.reserve(remote("quota"), "scope", owner_id="owner")
    owner.prepare_remote_send(lease, "dispatch.race")
    with pytest.raises(ResourceUnavailable, match="capacity_exhausted"):
        owner.reserve(remote("quota"), "scope", owner_id="other")
    sending = ResourceAdmission(path, inventory)
    aborting = ResourceAdmission(path, inventory)
    barrier = Barrier(2)

    def authorize():
        barrier.wait(5)
        try:
            sending.activate_remote(lease, "dispatch.race")
        except ResourceUnavailable as exc:
            assert exc.reason == "lease_inactive"
            return False
        return True

    def abort():
        barrier.wait(5)
        return aborting.abort_remote_before_send(lease, "dispatch.race")

    with ThreadPoolExecutor(max_workers=2) as threads:
        send = threads.submit(authorize)
        cancelled = threads.submit(abort)
        assert send.result(5) != cancelled.result(5)
    if send.result():
        assert owner.state(lease) == LeaseState.ACTIVE
        assert not owner.release(lease)
    else:
        assert owner.state(lease) is None
        replacement = owner.reserve(remote("quota"), "scope", owner_id=lease.owner_id)
        assert not owner.abort_remote_before_send(lease, "dispatch.race")
        assert owner.release_reserved(replacement)
