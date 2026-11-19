"""Run a host-allowlisted command with a durable start and stop boundary."""

import asyncio
import os
import selectors
import socket
import subprocess
import sys
from contextlib import suppress
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from threading import Event, Lock, Thread
from time import monotonic
from typing import IO

from jimu_runtime.workers.resources import (
    ResourceAdmission,
    ResourceLease,
    ResourceUnavailable,
)
from jimu_runtime.workers.sealed_memory import require_sealed_artifact_fd

_CHUNK_BYTES = 64 * 1024
_POLL_SECONDS = 0.025
_EXIT_WAIT_SECONDS = 1.0
_GUARDIAN = Path(__file__).with_name("process_guardian.py")


class WorkerFailure(StrEnum):
    START_FAILED = "start_failed"
    TIMEOUT = "timeout"
    OUTPUT_LIMIT = "output_limit"
    PROCESS_FAILED = "process_failed"
    INTERRUPTED = "interrupted"
    INVALID_BUDGET = "invalid_budget"


class WorkerStart(StrEnum):
    NOT_DISPATCHED = "not_dispatched"
    STARTED = "started"
    START_UNKNOWN = "start_unknown"


class WorkerProcessError(RuntimeError):
    """A bounded failure report independent of subprocess argv and stderr."""

    def __init__(
        self,
        reason: WorkerFailure,
        start_state: WorkerStart,
        *,
        output: bytes | None = None,
    ) -> None:
        self.reason = reason
        self.start_state = start_state
        self.output = output
        super().__init__(reason)

    @property
    def dispatched(self) -> bool:
        return self.start_state != WorkerStart.NOT_DISPATCHED


@dataclass(slots=True)
class _RunningCall:
    deadline: float
    lease: ResourceLease
    cancelled: Event
    output_byte_limit: int
    next_heartbeat: float = 0.0
    start: WorkerStart = WorkerStart.NOT_DISPATCHED
    _queue_lock: Lock = field(default_factory=Lock)
    _claimed: bool = False

    def claim(self) -> bool:
        with self._queue_lock:
            if self.cancelled.is_set():
                return False
            self._claimed = True
            return True

    def cancel(self, *, only_queued: bool = False) -> bool:
        """Prevent a queued run from ever obtaining dispatch authority."""
        with self._queue_lock:
            if only_queued and self._claimed:
                return False
            self.cancelled.set()
            return not self._claimed


class IsolatedWorker:
    def __init__(self, admission: ResourceAdmission) -> None:
        self._admission = admission
        self._running: set[asyncio.Task[bytes]] = set()

    async def run(
        self,
        lease: ResourceLease,
        argv: tuple[str, ...],
        payload: bytes,
        *,
        timeout_ms: int,
        output_byte_limit: int,
        pass_fds: tuple[int, ...] = (),
    ) -> bytes:
        """Keep the physical reservation until a local process group has exited."""
        if type(timeout_ms) is not int or not 0 < timeout_ms <= lease.timeout_ms:
            self._abandon_reservation(lease)
            raise WorkerProcessError(
                WorkerFailure.INVALID_BUDGET, WorkerStart.NOT_DISPATCHED
            )
        call = _RunningCall(
            monotonic() + timeout_ms / 1000, lease, Event(), output_byte_limit
        )
        artifact_fd: int | None = None
        try:
            argv, artifact_fd = self._capture_artifact(argv, pass_fds)
        except (OSError, ValueError):
            self._abandon_reservation(lease)
            raise WorkerProcessError(
                WorkerFailure.INVALID_BUDGET, WorkerStart.NOT_DISPATCHED
            ) from None
        loop = asyncio.get_running_loop()
        started = asyncio.Event()

        def use_thread() -> bytes:
            if not call.claim():
                raise WorkerProcessError(WorkerFailure.INTERRUPTED, call.start)
            try:
                loop.call_soon_threadsafe(started.set)
                return self._run(call, argv, payload, artifact_fd)
            finally:
                if artifact_fd is not None:
                    os.close(artifact_fd)

        task = asyncio.create_task(asyncio.to_thread(use_thread))
        self._running.add(task)
        task.add_done_callback(self._finished)
        try:
            try:
                async with asyncio.timeout(max(0.0, call.deadline - monotonic())):
                    await started.wait()
            except TimeoutError:
                if call.cancel(only_queued=True):
                    task.cancel()
                    if artifact_fd is not None:
                        os.close(artifact_fd)
                    self._abandon_reservation(lease)
                    raise WorkerProcessError(
                        WorkerFailure.TIMEOUT, WorkerStart.NOT_DISPATCHED
                    ) from None
                # A thread that claimed the call has its own deadline and cleanup.
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            if call.cancel():
                task.cancel()
                if artifact_fd is not None:
                    os.close(artifact_fd)
                self._abandon_reservation(lease)
            else:
                with suppress(WorkerProcessError, ResourceUnavailable):
                    await asyncio.shield(task)
            raise

    @staticmethod
    def _capture_artifact(
        argv: tuple[str, ...], pass_fds: tuple[int, ...]
    ) -> tuple[tuple[str, ...], int | None]:
        if not pass_fds:
            return argv, None
        if len(pass_fds) != 1 or type(pass_fds[0]) is not int or pass_fds[0] < 3:
            raise ValueError("exactly one sealed artifact descriptor is supported")
        source = pass_fds[0]
        supplied_path = f"/proc/self/fd/{source}"
        if supplied_path not in argv[1:]:
            raise ValueError("sealed artifact descriptor requires an exact argument")
        # Capture before crossing the cancellation boundary; the caller may close
        # its descriptor as soon as the awaitable is abandoned.
        owned = os.dup(source)
        try:
            require_sealed_artifact_fd(owned)
        except OSError:
            os.close(owned)
            raise
        return tuple(
            f"/proc/self/fd/{owned}" if arg == supplied_path else arg for arg in argv
        ), owned

    def _abandon_reservation(self, lease: ResourceLease) -> None:
        # Do not enqueue accounting behind the same saturated executor. If the
        # ledger is unavailable the unused reservation expires without dispatch.
        with suppress(ResourceUnavailable):
            self._admission.release_reserved(lease)

    def _finished(self, task: asyncio.Task[bytes]) -> None:
        self._running.discard(task)
        if not task.cancelled():
            task.exception()  # Observe cleanup failures if its caller cancelled twice.

    def _run(
        self,
        call: _RunningCall,
        argv: tuple[str, ...],
        payload: bytes,
        artifact_fd: int | None,
    ) -> bytes:
        lease = call.lease
        child: subprocess.Popen[bytes] | None = None
        attached = False
        completed = False
        try:
            if (
                not argv
                or not os.path.isabs(argv[0])
                or any("\x00" in argument for argument in argv)
                or lease.resource_class == "remote"
                or type(payload) is not bytes
                or len(payload) > lease.input_byte_limit
                or type(call.output_byte_limit) is not int
                or not 0 < call.output_byte_limit <= lease.output_byte_limit
            ):
                raise WorkerProcessError(WorkerFailure.INVALID_BUDGET, call.start)
            self._check_call(call)
            operator, guardian = socket.socketpair()
            with operator, guardian:
                child = subprocess.Popen(
                    [
                        sys.executable,
                        "-I",
                        str(_GUARDIAN),
                        str(guardian.fileno()),
                        self._admission.guardian_ledger_uri,
                        lease.lease_id,
                        lease.owner_generation,
                        repr(call.deadline),
                        "" if artifact_fd is None else str(artifact_fd),
                        *argv,
                    ],
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL,
                    pass_fds=(
                        (guardian.fileno(),)
                        if artifact_fd is None
                        else (guardian.fileno(), artifact_fd)
                    ),
                    start_new_session=True,
                    env={"LANG": "C.UTF-8", "PATH": os.defpath},
                )
                guardian.close()
                self._check_call(call)
                self._admission.attach_process_group(
                    lease, child.pid, deadline=call.deadline
                )
                attached = True
                self._check_call(call)
                call.start = WorkerStart.START_UNKNOWN
                operator.sendall(b"S")
                output = self._exchange(call, child, operator, payload)
                completed = True
                return output
        except (OSError, ResourceUnavailable):
            if monotonic() >= call.deadline:
                reason = WorkerFailure.TIMEOUT
            elif call.start == WorkerStart.NOT_DISPATCHED:
                reason = WorkerFailure.START_FAILED
            else:
                reason = WorkerFailure.INTERRUPTED
            raise WorkerProcessError(reason, call.start) from None
        finally:
            if attached and not completed:
                # The control endpoint closes first; the guardian owns tree shutdown.
                with suppress(ResourceUnavailable):
                    self._admission.begin_draining(lease)
            if child is not None:
                self._reap(child, lease if attached else None)
            released = False
            with suppress(ResourceUnavailable):
                # The ledger checks the durable tree receipt and guardian exit.
                released = (
                    self._admission.release(lease)
                    if attached
                    else self._admission.release_reserved(lease)
                )
            if completed and not released:
                raise WorkerProcessError(WorkerFailure.INTERRUPTED, call.start)

    def _check_call(self, call: _RunningCall) -> None:
        if call.cancelled.is_set():
            raise WorkerProcessError(WorkerFailure.INTERRUPTED, call.start)
        now = monotonic()
        if now >= call.deadline:
            raise WorkerProcessError(WorkerFailure.TIMEOUT, call.start)
        if now >= call.next_heartbeat:
            if not self._admission.renew(call.lease):
                raise WorkerProcessError(WorkerFailure.INTERRUPTED, call.start)
            call.next_heartbeat = now + self._admission.heartbeat_interval_seconds

    def _exchange(
        self,
        call: _RunningCall,
        child: subprocess.Popen[bytes],
        operator: socket.socket,
        payload: bytes,
    ) -> bytes:
        assert child.stdin is not None and child.stdout is not None
        output = bytearray()
        written = 0
        output_open = True
        operator.setblocking(False)
        with selectors.DefaultSelector() as ready:
            ready.register(operator, selectors.EVENT_READ, "ack")
            os.set_blocking(child.stdout.fileno(), False)
            ready.register(child.stdout, selectors.EVENT_READ, "output")
            if payload:
                os.set_blocking(child.stdin.fileno(), False)
                ready.register(child.stdin, selectors.EVENT_WRITE, "input")
            else:
                child.stdin.close()
            while True:
                self._check_call(call)
                returncode = child.poll()
                if returncode is not None and not output_open:
                    if call.start == WorkerStart.START_UNKNOWN:
                        self._read_ack(call, operator, ready)
                    if returncode < 0 or not self._admission.worker_completion_proven(
                        call.lease
                    ):
                        raise WorkerProcessError(WorkerFailure.INTERRUPTED, call.start)
                    if returncode != 0 or written != len(payload):
                        raise WorkerProcessError(
                            WorkerFailure.PROCESS_FAILED,
                            call.start,
                            output=bytes(output),
                        )
                    if call.start != WorkerStart.STARTED:
                        raise WorkerProcessError(WorkerFailure.INTERRUPTED, call.start)
                    return bytes(output)
                remaining = min(call.deadline, call.next_heartbeat) - monotonic()
                for key, _ in ready.select(max(0.0, min(_POLL_SECONDS, remaining))):
                    if key.data == "ack":
                        self._read_ack(call, operator, ready)
                    elif key.data == "input":
                        written = self._write_input(
                            child.stdin, payload, written, ready
                        )
                    elif key.data == "output":
                        chunk = os.read(child.stdout.fileno(), _CHUNK_BYTES)
                        if chunk:
                            # Only the actual worker can write to this pipe.
                            call.start = WorkerStart.STARTED
                        if len(chunk) > call.output_byte_limit - len(output):
                            raise WorkerProcessError(
                                WorkerFailure.OUTPUT_LIMIT, call.start
                            )
                        if chunk:
                            output.extend(chunk)
                        else:
                            output_open = False
                            ready.unregister(child.stdout)

    @staticmethod
    def _read_ack(
        call: _RunningCall, operator: socket.socket, ready: selectors.BaseSelector
    ) -> None:
        with suppress(KeyError):
            ready.unregister(operator)
        ack = operator.recv(1)
        if ack == b"Y":
            call.start = WorkerStart.STARTED
        elif ack == b"N":
            call.start = WorkerStart.NOT_DISPATCHED
            raise WorkerProcessError(WorkerFailure.START_FAILED, call.start)
        else:
            raise WorkerProcessError(WorkerFailure.INTERRUPTED, call.start)

    @staticmethod
    def _write_input(
        pipe: IO[bytes], payload: bytes, written: int, ready: selectors.BaseSelector
    ) -> int:
        try:
            written += os.write(
                pipe.fileno(), payload[written : written + _CHUNK_BYTES]
            )
        except BlockingIOError:
            return written
        except BrokenPipeError:
            ready.unregister(pipe)
            pipe.close()
            return written
        if written == len(payload):
            ready.unregister(pipe)
            pipe.close()
        return written

    def _reap(
        self, child: subprocess.Popen[bytes], lease: ResourceLease | None
    ) -> None:
        if child.stdin is not None:
            child.stdin.close()
        if child.stdout is not None:
            child.stdout.close()
        try:
            child.wait(timeout=_EXIT_WAIT_SECONDS)
        except subprocess.TimeoutExpired:
            # A guardian blocked in the kernel may exit long after the caller's
            # deadline. Reap it without depending on another admission or launch.
            Thread(target=self._reap_later, args=(child, lease), daemon=True).start()

    def _reap_later(
        self, child: subprocess.Popen[bytes], lease: ResourceLease | None
    ) -> None:
        child.wait()
        if lease is not None:
            with suppress(ResourceUnavailable):
                self._admission.release(lease)
