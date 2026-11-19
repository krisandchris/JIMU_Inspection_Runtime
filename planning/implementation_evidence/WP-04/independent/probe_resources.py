"""Independent CPU release and process-tree recovery on a shared local ledger."""

import asyncio
import json
import os
import signal
import sys
from concurrent.futures import ThreadPoolExecutor
from contextlib import suppress
from dataclasses import asdict
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event

from jimu_runtime.domain.policies import CPUResourceRequirements, PolicyKind
from jimu_runtime.workers.lifecycle import (
    IsolatedWorker,
    WorkerFailure,
    WorkerProcessError,
)
from jimu_runtime.workers.process_identity import process_start
from jimu_runtime.workers.resource_inventory import HostInventory, ResourceScope
from jimu_runtime.workers.resources import (
    ResourceAdmission,
    ResourceLease,
    ResourceUnavailable,
)

INVENTORY = HostInventory(
    "independent-cpu", 500, 1, (ResourceScope("geometry", "cpu", 1),)
)
REQUIREMENTS = CPUResourceRequirements(
    policy_kind=PolicyKind.RESOURCE_REQUIREMENTS,
    resource_class="cpu",
    max_concurrency=1,
    timeout_ms=20_000,
    input_byte_limit=4096,
    output_byte_limit=4096,
)
WORKLOAD = """
import json, os, subprocess, sys, time

descendant = subprocess.Popen((sys.executable, "-I", "-c", "import time; time.sleep(60)"),
                              start_new_session=True, stdin=subprocess.DEVNULL,
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
with open(sys.argv[1], "w", encoding="utf-8") as markers:
    json.dump([os.getpid(), descendant.pid, os.getppid()], markers)
time.sleep(60)
"""


async def child(ledger: Path, marker: Path) -> None:
    admission = ResourceAdmission(ledger, INVENTORY)
    lease = admission.reserve(REQUIREMENTS, "geometry", owner_id="old-supervisor")
    print(json.dumps(asdict(lease)), flush=True)
    await IsolatedWorker(admission).run(
        lease,
        (sys.executable, "-I", "-c", WORKLOAD, str(marker)),
        b"",
        timeout_ms=15_000,
        output_byte_limit=32,
    )


async def wait_for_file(marker: Path) -> list[int]:
    async with asyncio.timeout(5):
        while True:
            try:
                return json.loads(marker.read_text())
            except (FileNotFoundError, json.JSONDecodeError):
                await asyncio.sleep(0.015)


def alive(pid: int, original_start: str) -> bool:
    try:
        state, start = process_start(pid)
    except (FileNotFoundError, ProcessLookupError):
        return False
    return state != "Z" and start == original_start


async def crash_and_recover(ledger: Path, marker: Path) -> None:
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        __file__,
        "child",
        str(ledger),
        str(marker),
        stdout=asyncio.subprocess.PIPE,
    )
    assert process.stdout is not None
    try:
        async with asyncio.timeout(5):
            old = ResourceLease(**json.loads(await process.stdout.readline()))
        leader, descendant, guardian = await wait_for_file(marker)
        pids = (leader, descendant)
        starts = [process_start(pid)[1] for pid in pids]
        assert os.getpgid(leader) == guardian
        assert os.getpgid(pids[0]) != os.getpgid(pids[1])
        restarted = ResourceAdmission(ledger, INVENTORY)
        try:
            restarted.reserve(REQUIREMENTS, "geometry", owner_id="competitor")
        except ResourceUnavailable as error:
            assert str(error) == "capacity_exhausted"
        else:
            raise AssertionError("competing caller acquired an occupied CPU slot")
        os.kill(process.pid, signal.SIGKILL)
        assert await asyncio.wait_for(process.wait(), 5) == -signal.SIGKILL
        async with asyncio.timeout(8):
            while True:
                restarted.reconcile()
                try:
                    fresh = restarted.reserve(
                        REQUIREMENTS, "geometry", owner_id=old.owner_id
                    )
                    break
                except ResourceUnavailable as error:
                    assert str(error) == "capacity_exhausted"
                    await asyncio.sleep(0.03)
        assert [alive(pid, start) for pid, start in zip(pids, starts, strict=True)] == [
            False,
            False,
        ]
        assert (
            old.lease_id != fresh.lease_id
            and old.owner_generation != fresh.owner_generation
        )
        assert not restarted.release(old)
        value = await IsolatedWorker(restarted).run(
            fresh,
            (
                sys.executable,
                "-I",
                "-c",
                "import sys; sys.stdout.buffer.write(sys.stdin.buffer.read())",
            ),
            b"drained",
            timeout_ms=3_000,
            output_byte_limit=32,
        )
        assert value == b"drained" and restarted.state(fresh) is None
        print(
            json.dumps(
                {
                    "check": "cpu_supervisor_kill_reconcile_and_reopen",
                    "supervisor_signal": process.returncode,
                    "old_worker_pid": pids[0],
                    "old_detached_descendant_pid": pids[1],
                    "capacity_reused_after_both_exit": True,
                    "old_token_release": False,
                    "new_worker_response": value.decode(),
                }
            ),
            flush=True,
        )
        timeout_lease = restarted.reserve(
            REQUIREMENTS, "geometry", owner_id="timeout-run"
        )
        try:
            await IsolatedWorker(restarted).run(
                timeout_lease,
                (sys.executable, "-I", "-c", "import time; time.sleep(60)"),
                b"",
                timeout_ms=250,
                output_byte_limit=32,
            )
        except WorkerProcessError as error:
            assert error.reason == WorkerFailure.TIMEOUT and error.dispatched
        else:
            raise AssertionError("worker unexpectedly completed within its deadline")
        restarted.reconcile()
        assert restarted.state(timeout_lease) is None
        recovery = restarted.reserve(REQUIREMENTS, "geometry", owner_id="after-timeout")
        assert restarted.release(recovery)
        print(
            json.dumps({"check": "cpu_deadline_and_reclaim", "recovered": True}),
            flush=True,
        )
    finally:
        if process.returncode is None:
            process.kill()
            await process.wait()


def check_denied(admission: ResourceAdmission) -> None:
    try:
        unexpected = admission.reserve(REQUIREMENTS, "geometry", owner_id="competitor")
    except ResourceUnavailable as error:
        assert str(error) == "capacity_exhausted"
    else:
        admission.release(unexpected)
        raise AssertionError("unverified live descendants released a CPU slot")


def signal_exact(pid: int, start: str, sig: signal.Signals) -> None:
    if alive(pid, start):
        try:
            descriptor = os.pidfd_open(pid)
        except ProcessLookupError:
            return
        try:
            if alive(pid, start):
                signal.pidfd_send_signal(descriptor, sig)
        except ProcessLookupError:
            return
        finally:
            os.close(descriptor)


async def guardian_fault(ledger: Path, marker: Path, *, stop: bool) -> None:
    operator = ResourceAdmission.provision(ledger, INVENTORY)
    lease = operator.reserve(REQUIREMENTS, "geometry", owner_id="guardian-fault")
    task = asyncio.create_task(
        IsolatedWorker(operator).run(
            lease,
            (sys.executable, "-I", "-c", WORKLOAD, str(marker)),
            b"",
            timeout_ms=700,
            output_byte_limit=64,
        )
    )
    pids = await wait_for_file(marker)
    leader, descendant, guardian = pids
    starts = [process_start(pid)[1] for pid in pids]
    assert guardian == os.getpgid(leader) != os.getpgid(descendant)
    reopened = ResourceAdmission(ledger, INVENTORY)
    try:
        signal_exact(guardian, starts[2], signal.SIGSTOP if stop else signal.SIGKILL)
        try:
            await asyncio.wait_for(task, timeout=5)
        except WorkerProcessError as exc:
            assert exc.dispatched
        else:
            raise AssertionError("faulted guardian unexpectedly returned success")
        assert alive(descendant, starts[1])
        if stop:
            assert alive(guardian, starts[2])
        else:
            assert not alive(guardian, starts[2])
        reopened.reconcile()
        check_denied(reopened)
        if stop:
            signal_exact(guardian, starts[2], signal.SIGCONT)
            async with asyncio.timeout(6):
                while reopened.state(lease) is not None:
                    reopened.reconcile()
                    await asyncio.sleep(0.03)
            assert all(
                not alive(pid, started)
                for pid, started in zip(pids, starts, strict=True)
            )
            new = reopened.reserve(REQUIREMENTS, "geometry", owner_id="post-reap")
            assert not reopened.release(lease) and reopened.release(new)
        else:
            # With the independent reaper killed, another process cannot infer
            # complete tree termination from two manually observed processes.
            for pid, started in zip(pids[:2], starts[:2], strict=True):
                signal_exact(pid, started, signal.SIGKILL)
            async with asyncio.timeout(5):
                while any(
                    alive(pid, started)
                    for pid, started in zip(pids, starts, strict=True)
                ):
                    await asyncio.sleep(0.03)
            reopened.reconcile()
            check_denied(reopened)
        print(
            json.dumps(
                {
                    "check": "guardian_stop_then_verified_reap"
                    if stop
                    else "guardian_kill_fails_closed",
                    "guardian_pid": guardian,
                    "detached_worker_pid": descendant,
                    "admission_during_unverified_tree": "denied",
                    "admission_after_observed_exit": "restored_with_receipt"
                    if stop
                    else "denied_without_receipt",
                }
            ),
            flush=True,
        )
    finally:
        signal_exact(guardian, starts[2], signal.SIGCONT)
        for pid, started in zip(pids, starts, strict=True):
            signal_exact(pid, started, signal.SIGKILL)
        if not task.done():
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task


async def expire_before_executor_start(ledger: Path, marker: Path) -> None:
    # This final probe owns the event loop's thread pool. Occupy its only thread
    # while the run call expires, then allow it to drain the queued work.
    loop = asyncio.get_running_loop()
    pool = ThreadPoolExecutor(max_workers=1)
    loop.set_default_executor(pool)
    occupied = asyncio.Event()
    resume = Event()

    def block_thread() -> None:
        loop.call_soon_threadsafe(occupied.set)
        resume.wait(timeout=5)

    pending = loop.run_in_executor(None, block_thread)
    try:
        await asyncio.wait_for(occupied.wait(), timeout=2)
        admission = ResourceAdmission.provision(ledger, INVENTORY)
        lease = admission.reserve(REQUIREMENTS, "geometry", owner_id="queue-expired")
        try:
            await asyncio.wait_for(
                IsolatedWorker(admission).run(
                    lease,
                    (sys.executable, "-I", "-c", WORKLOAD, str(marker)),
                    b"",
                    timeout_ms=200,
                    output_byte_limit=64,
                ),
                timeout=2,
            )
        except WorkerProcessError as error:
            assert error.reason == WorkerFailure.TIMEOUT and not error.dispatched
        else:
            raise AssertionError("expired queued call was not rejected")
        assert admission.state(lease) is None
    finally:
        resume.set()
        await pending
    await asyncio.to_thread(lambda: None)
    assert not marker.exists()
    print(
        json.dumps(
            {
                "check": "expired_before_executor_start",
                "worker_started": False,
                "reservation_removed_before_executor_resumed": True,
            }
        ),
        flush=True,
    )


async def main() -> None:
    with TemporaryDirectory(prefix="jimu-independent-wp04-cpu-") as root:
        ledger = Path(root) / "capacity.db"
        ResourceAdmission.provision(ledger, INVENTORY)
        await crash_and_recover(ledger, Path(root) / "pids.json")
        await guardian_fault(
            Path(root) / "stopped.db", Path(root) / "stopped.json", stop=True
        )
        await guardian_fault(
            Path(root) / "abnormal.db", Path(root) / "abnormal.json", stop=False
        )
        await expire_before_executor_start(
            Path(root) / "queued.db", Path(root) / "queued.json"
        )


if __name__ == "__main__":
    asyncio.run(
        child(Path(sys.argv[2]), Path(sys.argv[3]))
        if sys.argv[1:2] == ["child"]
        else main()
    )
