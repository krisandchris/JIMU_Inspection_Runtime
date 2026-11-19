import asyncio
import ctypes
import fcntl
import os
import signal
import socket
import sqlite3
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing, contextmanager, suppress
from dataclasses import replace
from multiprocessing import get_context
from pathlib import Path
from threading import Event
from time import monotonic, sleep

import pytest
from fixtures.wp04_resources import commands
from fixtures.wp04_resources.scenario import cpu, worker_inventory

from jimu_runtime.workers.lifecycle import (
    IsolatedWorker,
    WorkerFailure,
    WorkerProcessError,
    WorkerStart,
)
from jimu_runtime.workers.resources import (
    LeaseState,
    ResourceAdmission,
    ResourceUnavailable,
)
from jimu_runtime.workers.sealed_memory import create_artifact_memfd, seal_artifact_fd


def command(*args):
    return (sys.executable, commands.__file__, *map(str, args))


async def wait_ready(marker):
    async with asyncio.timeout(5):
        while not marker.exists():
            await asyncio.sleep(0.015)
    return tuple(map(int, marker.read_text().split()))


def assert_no_tree(pids):
    leader, descendant, guardian = pids
    for pid in (leader, descendant, guardian):
        with pytest.raises(ProcessLookupError):
            os.kill(pid, 0)
    with pytest.raises(ProcessLookupError):
        os.killpg(guardian, 0)


@contextmanager
def marked_subreaper():
    # The test must collect only its own orphaned guardian after killing a parent.
    libc = ctypes.CDLL(None, use_errno=True)
    before = ctypes.c_int()
    assert libc.prctl(37, ctypes.byref(before), 0, 0, 0) == 0
    assert libc.prctl(36, 1, 0, 0, 0) == 0
    try:
        yield
    finally:
        assert libc.prctl(36, before.value, 0, 0, 0) == 0


def test_worker_round_trip_and_exact_normal_failure(tmp_path):
    admission = ResourceAdmission.provision(
        tmp_path / "resources.db", worker_inventory()
    )
    runner = IsolatedWorker(admission)

    async def exercise():
        lease = admission.reserve(cpu(), "test.cpu", owner_id="worker")
        payload = bytes(range(256)) * 640
        output = await runner.run(
            lease,
            command("echo"),
            payload,
            timeout_ms=3000,
            output_byte_limit=len(payload),
        )
        assert output == payload[::-1]
        assert admission.state(lease) is None

        lease = admission.reserve(cpu(), "test.cpu", owner_id="worker")
        with pytest.raises(WorkerProcessError) as failed:
            await runner.run(
                lease, command("failure"), b"", timeout_ms=3000, output_byte_limit=16
            )
        assert failed.value.reason == WorkerFailure.PROCESS_FAILED
        assert failed.value.start_state == WorkerStart.STARTED
        assert failed.value.output == b"{}"
        assert "PRIVATE_STDERR" not in str(failed.value)
        assert admission.state(lease) is None

    asyncio.run(exercise())


@pytest.mark.parametrize(
    ("work", "reason", "start"),
    [
        ("oversize", WorkerFailure.OUTPUT_LIMIT, WorkerStart.STARTED),
        ("missing", WorkerFailure.START_FAILED, WorkerStart.NOT_DISPATCHED),
        ("relative", WorkerFailure.INVALID_BUDGET, WorkerStart.NOT_DISPATCHED),
    ],
)
def test_worker_fails_closed_without_truncated_evidence(tmp_path, work, reason, start):
    admission = ResourceAdmission.provision(
        tmp_path / "resources.db", worker_inventory()
    )
    lease = admission.reserve(cpu(), "test.cpu", owner_id="worker")
    args = {
        "oversize": command("oversize"),
        "missing": (str(tmp_path / "missing-executable"),),
        "relative": ("relative-executable",),
    }[work]
    with pytest.raises(WorkerProcessError) as failed:
        asyncio.run(
            IsolatedWorker(admission).run(
                lease, args, b"", timeout_ms=3000, output_byte_limit=16
            )
        )
    assert failed.value.reason == reason
    assert failed.value.start_state == start
    assert failed.value.output is None
    assert admission.state(lease) is None


def test_timeout_holds_capacity_while_real_descendant_is_still_alive(tmp_path):
    admission = ResourceAdmission.provision(
        tmp_path / "resources.db", worker_inventory()
    )
    runner = IsolatedWorker(admission)
    marker = tmp_path / "timeout-ready"

    async def exercise():
        lease = admission.reserve(cpu(), "test.cpu", owner_id="initial")
        task = asyncio.create_task(
            runner.run(
                lease,
                command("tree", marker),
                b"",
                timeout_ms=700,
                output_byte_limit=16,
            )
        )
        pids = await wait_ready(marker)
        async with asyncio.timeout(5):
            while admission.state(lease) != LeaseState.DRAINING:
                await asyncio.sleep(0.004)
        os.kill(pids[1], 0)
        with pytest.raises(ResourceUnavailable, match="capacity_exhausted"):
            admission.reserve(cpu(), "test.cpu", owner_id="competitor")
        with pytest.raises(WorkerProcessError) as failed:
            await task
        assert failed.value.reason == WorkerFailure.TIMEOUT
        assert failed.value.dispatched
        assert_no_tree(pids)
        next_lease = admission.reserve(cpu(), "test.cpu", owner_id="competitor")
        assert not admission.release(lease)
        assert admission.release(next_lease)

    asyncio.run(exercise())


@pytest.mark.parametrize("mode", ["tree", "escaped-tree"])
def test_cancellation_reaps_real_worker_tree(tmp_path, mode):
    admission = ResourceAdmission.provision(
        tmp_path / "resources.db", worker_inventory()
    )
    runner = IsolatedWorker(admission)
    marker = tmp_path / "cancel-ready"

    async def exercise():
        lease = admission.reserve(cpu(), "test.cpu", owner_id="worker")
        task = asyncio.create_task(
            runner.run(
                lease,
                command(mode, marker),
                b"",
                timeout_ms=5000,
                output_byte_limit=16,
            )
        )
        pids = await wait_ready(marker)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert_no_tree(pids)
        assert admission.state(lease) is None

    asyncio.run(exercise())


def _supervisor(path: Path, marker: Path, output, mode, timeout_ms=8000):
    admission = ResourceAdmission(path, worker_inventory(ttl=1000))
    lease = admission.reserve(cpu(), "test.cpu", owner_id="before-crash")
    output.put(lease)
    asyncio.run(
        IsolatedWorker(admission).run(
            lease,
            command(mode, marker),
            b"",
            timeout_ms=timeout_ms,
            output_byte_limit=16,
        )
    )


def _wait_orphan(guardian: int):
    deadline = monotonic() + 5
    while monotonic() < deadline:
        pid, status = os.waitpid(guardian, os.WNOHANG)
        if pid:
            assert os.WIFEXITED(status)
            return
        sleep(0.025)
    raise AssertionError("guardian did not exit")


@pytest.mark.parametrize("mode", ["tree", "escaped-tree"])
def test_hard_supervisor_restart_preserves_lease_until_worker_tree_exits(
    tmp_path, mode
):
    inventory = worker_inventory(ttl=1000)
    path, marker = tmp_path / "resources.db", tmp_path / "restart-ready"
    ResourceAdmission.provision(path, inventory)
    ctx = get_context("spawn")
    results = ctx.Queue()
    supervisor = ctx.Process(target=_supervisor, args=(path, marker, results, mode))
    pids = None
    with marked_subreaper():
        supervisor.start()
        try:
            lease = results.get(timeout=5)
            pids = asyncio.run(wait_ready(marker))
            os.kill(pids[2], signal.SIGSTOP)
            supervisor.kill()
            supervisor.join(5)
            assert supervisor.exitcode == -signal.SIGKILL

            restarted = ResourceAdmission(path, inventory)
            # The owner changed. Even an explicit draining/release request must
            # keep capacity while the original guardian group actually exists.
            assert not restarted.release(lease)
            assert restarted.state(lease) == LeaseState.DRAINING
            with pytest.raises(ResourceUnavailable, match="capacity_exhausted"):
                restarted.reserve(cpu(), "test.cpu", owner_id="restarted")

            os.kill(pids[2], signal.SIGCONT)
            _wait_orphan(pids[2])
            assert_no_tree(pids)
            assert restarted.reconcile() == (lease.lease_id,)
            replacement = restarted.reserve(cpu(), "test.cpu", owner_id=lease.owner_id)
            assert not restarted.release(lease)
            assert restarted.release(replacement)
        finally:
            if supervisor.is_alive():
                supervisor.kill()
                supervisor.join(5)
            if pids is not None:
                with suppress(ProcessLookupError):
                    os.kill(pids[2], signal.SIGCONT)
                with suppress(ChildProcessError):
                    _wait_orphan(pids[2])


def test_expired_lease_still_charges_a_live_group(tmp_path):
    admission = ResourceAdmission.provision(
        tmp_path / "resources.db", worker_inventory(ttl=250)
    )
    lease = admission.reserve(cpu(), "test.cpu", owner_id="worker")
    # An independent worker is isolated in a new group. The supervisor deliberately
    # stops heartbeating to exercise the lease expiry path during physical work.
    process = subprocess.Popen(
        command("descendant", tmp_path / "manual-group"), start_new_session=True
    )
    try:
        admission.attach_process_group(lease, process.pid)
        sleep(0.28)
        assert admission.reconcile() == ()
        assert admission.state(lease) == LeaseState.DRAINING
        assert not admission.renew(lease)
        assert not admission.release(lease)
        os.kill(process.pid, 0)
        with pytest.raises(ResourceUnavailable, match="capacity_exhausted"):
            admission.reserve(cpu(), "test.cpu", owner_id="competitor")
    finally:
        process.kill()
        process.wait(timeout=5)
    # A process without the trusted guardian cannot prove it supervised every
    # descendant. Its exhaustion remains pending after ordinary process exit.
    assert admission.reconcile() == ()
    assert admission.state(lease) == LeaseState.DRAINING


def test_duplicate_launch_cannot_release_the_real_worker(tmp_path):
    admission = ResourceAdmission.provision(
        tmp_path / "resources.db", worker_inventory()
    )
    runner = IsolatedWorker(admission)
    marker = tmp_path / "original"

    async def exercise():
        lease = admission.reserve(cpu(), "test.cpu", owner_id="worker")
        task = asyncio.create_task(
            runner.run(
                lease,
                command("tree", marker),
                b"",
                timeout_ms=5000,
                output_byte_limit=16,
            )
        )
        try:
            pids = await wait_ready(marker)
            with pytest.raises(WorkerProcessError) as duplicate:
                await runner.run(
                    lease,
                    command("echo"),
                    b"second",
                    timeout_ms=1000,
                    output_byte_limit=16,
                )
            assert duplicate.value.reason == WorkerFailure.START_FAILED
            assert not duplicate.value.dispatched
            assert admission.state(lease) == LeaseState.ACTIVE
            os.kill(pids[1], 0)
        finally:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        assert_no_tree(pids)

    asyncio.run(exercise())


def test_deferred_reap_preserves_capacity_until_a_stopped_guardian_can_exit(tmp_path):
    admission = ResourceAdmission.provision(
        tmp_path / "resources.db", worker_inventory()
    )
    runner = IsolatedWorker(admission)
    marker = tmp_path / "deferred"

    async def exercise():
        lease = admission.reserve(cpu(), "test.cpu", owner_id="worker")
        task = asyncio.create_task(
            runner.run(
                lease,
                command("tree", marker),
                b"",
                timeout_ms=5000,
                output_byte_limit=16,
            )
        )
        pids = await wait_ready(marker)
        os.kill(pids[2], signal.SIGSTOP)
        try:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert admission.state(lease) == LeaseState.DRAINING
            os.kill(pids[1], 0)
            with pytest.raises(ResourceUnavailable, match="capacity_exhausted"):
                admission.reserve(cpu(), "test.cpu", owner_id="competitor")
        finally:
            os.kill(pids[2], signal.SIGCONT)
        async with asyncio.timeout(5):
            while admission.state(lease) is not None:
                await asyncio.sleep(0.015)
        assert_no_tree(pids)

    asyncio.run(exercise())


def test_guardian_death_with_escaped_descendant_requires_proof_before_reuse(tmp_path):
    inventory = worker_inventory()
    path, marker = tmp_path / "resources.db", tmp_path / "escaped"
    admission = ResourceAdmission.provision(path, inventory)
    runner = IsolatedWorker(admission)

    async def exercise():
        lease = admission.reserve(cpu(), "test.cpu", owner_id="worker")
        task = asyncio.create_task(
            runner.run(
                lease,
                command("escaped-partial", marker),
                b"",
                timeout_ms=5000,
                output_byte_limit=16,
            )
        )
        leader, descendant, guardian = await wait_ready(marker)
        try:
            assert os.getpgid(descendant) != guardian
            os.kill(guardian, signal.SIGKILL)
            os.kill(leader, signal.SIGKILL)
            with pytest.raises(WorkerProcessError) as failed:
                await task
            assert failed.value.reason == WorkerFailure.INTERRUPTED
            assert failed.value.output is None
            os.waitpid(leader, 0)
            with pytest.raises(ProcessLookupError):
                os.killpg(guardian, 0)
            os.kill(descendant, 0)
            restarted = ResourceAdmission(path, inventory)
            assert restarted.reconcile() == ()
            assert restarted.state(lease) == LeaseState.DRAINING
            assert not restarted.release(lease)
            with pytest.raises(ResourceUnavailable, match="capacity_exhausted"):
                restarted.reserve(cpu(), "test.cpu", owner_id="replacement")
        finally:
            os.kill(descendant, signal.SIGKILL)
            os.waitpid(descendant, 0)
        assert restarted.reconcile() == ()
        # Only the boot-identity change releases a lost receipt in this
        # implementation. The process exits above are real; reboot is simulated.
        restarted._identity = replace(
            restarted._identity, boot_id="00000000-0000-0000-0000-000000000000"
        )
        assert restarted.reconcile() == (lease.lease_id,)
        next_lease = restarted.reserve(cpu(), "test.cpu", owner_id=lease.owner_id)
        assert not restarted.release(lease)
        assert restarted.state(next_lease) == LeaseState.RESERVED
        assert restarted.release(next_lease)

    with marked_subreaper():
        asyncio.run(exercise())


def test_guardian_enforces_deadline_while_supervisor_is_stopped(tmp_path):
    inventory = worker_inventory(ttl=1000)
    path, marker = tmp_path / "resources.db", tmp_path / "deadline"
    admission = ResourceAdmission.provision(path, inventory)
    ctx = get_context("spawn")
    results = ctx.Queue()
    supervisor = ctx.Process(
        target=_supervisor, args=(path, marker, results, "escaped-tree", 1200)
    )
    pids = None
    with marked_subreaper():
        supervisor.start()
        try:
            lease = results.get(timeout=5)
            pids = asyncio.run(wait_ready(marker))
            os.kill(supervisor.pid, signal.SIGSTOP)
            deadline = monotonic() + 4
            while not admission.worker_completion_proven(lease):
                assert monotonic() < deadline
                sleep(0.025)
            # The supervisor has not resumed, so only the guardian could stop work.
            for pid in pids[:2]:
                with pytest.raises(ProcessLookupError):
                    os.kill(pid, 0)
        finally:
            if supervisor.is_alive():
                supervisor.kill()
                supervisor.join(5)
            if pids is not None:
                _wait_orphan(pids[2])
    assert admission.release(lease)


def test_guardian_polls_high_numbered_control_socket(tmp_path, monkeypatch):
    admission = ResourceAdmission.provision(
        tmp_path / "resources.db", worker_inventory()
    )
    original_socketpair = socket.socketpair

    def high_socketpair():
        operator, guardian = original_socketpair()
        try:
            high_fd = fcntl.fcntl(guardian.fileno(), fcntl.F_DUPFD_CLOEXEC, 1050)
        finally:
            guardian.close()
        assert high_fd >= 1050
        return operator, socket.socket(fileno=high_fd)

    async def exercise():
        lease = admission.reserve(cpu(), "test.cpu", owner_id="worker")
        with monkeypatch.context() as patch:
            patch.setattr(socket, "socketpair", high_socketpair)
            assert (
                await IsolatedWorker(admission).run(
                    lease,
                    command("slow-echo"),
                    b"abc",
                    timeout_ms=2500,
                    output_byte_limit=16,
                )
                == b"cba"
            )
        assert admission.state(lease) is None

    asyncio.run(exercise())


def test_guardian_retries_receipt_until_sqlite_writer_unlocks(tmp_path):
    path, marker = tmp_path / "resources.db", tmp_path / "receipt"
    admission = ResourceAdmission.provision(path, worker_inventory())
    runner = IsolatedWorker(admission)

    async def exercise():
        lease = admission.reserve(cpu(), "test.cpu", owner_id="worker")
        task = asyncio.create_task(
            runner.run(
                lease,
                command("escaped-tree", marker),
                b"",
                timeout_ms=5000,
                output_byte_limit=16,
            )
        )
        pids = await wait_ready(marker)
        with closing(sqlite3.connect(path)) as writer:
            writer.execute("BEGIN IMMEDIATE")
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            # Keep the write lock longer than the former 2-second single attempt.
            await asyncio.sleep(1.35)
            for pid in pids[:2]:
                with pytest.raises(ProcessLookupError):
                    os.kill(pid, 0)
            os.kill(pids[2], 0)
        async with asyncio.timeout(5):
            while not admission.reconcile() and admission.state(lease) is not None:
                await asyncio.sleep(0.015)
        assert_no_tree(pids)

    asyncio.run(exercise())


@pytest.mark.parametrize("cancel", [False, True])
def test_queued_worker_timeout_and_cancellation_do_not_dispatch(tmp_path, cancel):
    admission = ResourceAdmission.provision(
        tmp_path / "resources.db", worker_inventory()
    )
    permit = Event()
    occupied = Event()
    marker = tmp_path / "must-not-start"

    def use_executor():
        occupied.set()
        permit.wait(5)

    async def exercise():
        loop = asyncio.get_running_loop()
        loop.set_default_executor(ThreadPoolExecutor(max_workers=1))
        occupied_future = loop.run_in_executor(None, use_executor)
        lease = admission.reserve(cpu(), "test.cpu", owner_id="queued")
        try:
            async with asyncio.timeout(1):
                while not occupied.is_set():
                    await asyncio.sleep(0.005)
                queued = asyncio.create_task(
                    IsolatedWorker(admission).run(
                        lease,
                        command("tree", marker),
                        b"",
                        timeout_ms=300,
                        output_byte_limit=16,
                    )
                )
                await asyncio.sleep(0.02)
                if cancel:
                    queued.cancel()
                    with pytest.raises(asyncio.CancelledError):
                        await queued
                else:
                    with pytest.raises(WorkerProcessError) as error:
                        await queued
                    assert error.value.reason == WorkerFailure.TIMEOUT
                    assert error.value.start_state == WorkerStart.NOT_DISPATCHED
            assert admission.state(lease) is None
            next_lease = admission.reserve(cpu(), "test.cpu", owner_id="replacement")
            assert admission.release(next_lease)
        finally:
            permit.set()
            await occupied_future
        assert not marker.exists()

    asyncio.run(exercise())


def _supervisor_paused_before_gate(path, marker, output):
    class PausingAdmission(ResourceAdmission):
        def attach_process_group(self, lease, pgid, *, deadline=None):
            super().attach_process_group(lease, pgid, deadline=deadline)
            output.send(pgid)
            os.kill(os.getpid(), signal.SIGSTOP)
            # Signal delivery is asynchronous across the supervisor's threads.
            # This launcher thread must never advance past the closed gate.
            Event().wait(8)
            raise AssertionError("test supervisor was expected to stay stopped")

    admission = PausingAdmission(path, worker_inventory(ttl=1000))
    lease = admission.reserve(cpu(), "test.cpu", owner_id="before-gate")
    output.send(lease)
    asyncio.run(
        IsolatedWorker(admission).run(
            lease,
            command("tree", marker),
            b"",
            timeout_ms=1200,
            output_byte_limit=16,
        )
    )


def test_guardian_closed_gate_also_expires_without_supervisor(tmp_path):
    inventory = worker_inventory(ttl=1000)
    path, marker = tmp_path / "resources.db", tmp_path / "gate"
    admission = ResourceAdmission.provision(path, inventory)
    ctx = get_context("spawn")
    results, sender = ctx.Pipe(duplex=False)
    supervisor = ctx.Process(
        target=_supervisor_paused_before_gate, args=(path, marker, sender)
    )
    guardian = None
    with marked_subreaper():
        supervisor.start()
        sender.close()
        try:
            assert results.poll(5)
            lease = results.recv()
            assert results.poll(5)
            guardian = results.recv()
            deadline = monotonic() + 4
            while not admission.worker_completion_proven(lease):
                assert monotonic() < deadline
                sleep(0.025)
            assert not marker.exists()
        finally:
            results.close()
            if supervisor.is_alive():
                supervisor.kill()
                supervisor.join(5)
            if guardian is not None:
                _wait_orphan(guardian)
    assert admission.release(lease)


def test_guardian_delivers_only_captured_sealed_artifact(tmp_path):
    admission = ResourceAdmission.provision(
        tmp_path / "resources.db", worker_inventory()
    )
    claimed = Event()
    proceed = Event()

    class PausedWorker(IsolatedWorker):
        def _run(self, call, argv, payload, artifact_fd):
            claimed.set()
            assert proceed.wait(5)
            return super()._run(call, argv, payload, artifact_fd)

    async def exercise():
        lease = admission.reserve(cpu(), "test.cpu", owner_id="worker")
        fd = create_artifact_memfd()
        os.write(fd, b"trusted-artifact")
        seal_artifact_fd(fd)
        task = asyncio.create_task(
            PausedWorker(admission).run(
                lease,
                command("artifact", f"/proc/self/fd/{fd}"),
                b"",
                timeout_ms=3000,
                output_byte_limit=32,
                pass_fds=(fd,),
            )
        )
        try:
            async with asyncio.timeout(2):
                while not claimed.is_set():
                    await asyncio.sleep(0.005)
        finally:
            os.close(fd)
            proceed.set()
        assert await task == b"trusted-artifact"
        assert admission.state(lease) is None

    asyncio.run(exercise())


def test_worker_rejects_unsealed_artifact_before_dispatch(tmp_path):
    admission = ResourceAdmission.provision(
        tmp_path / "resources.db", worker_inventory()
    )
    lease = admission.reserve(cpu(), "test.cpu", owner_id="worker")
    fd = create_artifact_memfd()
    try:
        with pytest.raises(WorkerProcessError) as rejected:
            asyncio.run(
                IsolatedWorker(admission).run(
                    lease,
                    command("artifact", f"/proc/self/fd/{fd}"),
                    b"",
                    timeout_ms=1500,
                    output_byte_limit=32,
                    pass_fds=(fd,),
                )
            )
        assert rejected.value.reason == WorkerFailure.INVALID_BUDGET
        assert rejected.value.start_state == WorkerStart.NOT_DISPATCHED
        assert admission.state(lease) is None
    finally:
        os.close(fd)
