"""Private Linux subreaper and durable witness of worker-tree completion."""

import ctypes
import errno
import os
import select
import signal
import socket
import sqlite3
import subprocess
import sys
from contextlib import closing, suppress
from math import ceil
from pathlib import Path
from time import monotonic, sleep

_TERM_GRACE_SECONDS = 0.15
_POLL_SECONDS = 0.025
_PR_SET_CHILD_SUBREAPER = 36


class PidfdUnavailable(OSError):
    """The host cannot provide a kernel-pinned process handle."""


def _pidfd_open(pid: int) -> int:
    native = getattr(os, "pidfd_open", None)
    if native is not None:
        return int(native(pid, 0))
    try:
        open_pidfd = ctypes.CDLL(None, use_errno=True).pidfd_open
    except AttributeError:
        raise PidfdUnavailable(errno.ENOSYS, "pidfd_open unavailable") from None
    open_pidfd.argtypes = (ctypes.c_int, ctypes.c_uint)
    open_pidfd.restype = ctypes.c_int
    fd = int(open_pidfd(pid, 0))
    if fd < 0:
        raise OSError(ctypes.get_errno(), "pidfd_open failed")
    return fd


def _pidfd_signal(fd: int, signum: int) -> None:
    native = getattr(signal, "pidfd_send_signal", None)
    if native is not None:
        native(fd, signum)
        return
    try:
        send = ctypes.CDLL(None, use_errno=True).pidfd_send_signal
    except AttributeError:
        raise PidfdUnavailable(errno.ENOSYS, "pidfd_send_signal unavailable") from None
    send.argtypes = (ctypes.c_int, ctypes.c_int, ctypes.c_void_p, ctypes.c_uint)
    send.restype = ctypes.c_int
    if send(fd, signum, None, 0) < 0:
        raise OSError(ctypes.get_errno(), "pidfd_send_signal failed")


def _probe_pidfd() -> None:
    fd = _pidfd_open(os.getpid())
    try:
        _pidfd_signal(fd, 0)
    finally:
        os.close(fd)


def _kill_adopted_children() -> None:
    # No concurrent waitpid can reap/reuse these PIDs before pidfd_open.
    guardian = os.getpid()
    children = Path(f"/proc/self/task/{guardian}/children").read_text(encoding="ascii")
    for child in children.split():
        with suppress(ProcessLookupError):
            pid_fd = _pidfd_open(int(child))
            try:
                _pidfd_signal(pid_fd, signal.SIGKILL)
            finally:
                os.close(pid_fd)


def _supervise(
    control: socket.socket, worker_pid: int, deadline: float, stopping: bool
) -> int:
    worker_code = 125
    stop_at: float | None = None
    poll = select.poll()
    poll.register(control, select.POLLIN)
    while True:
        now = monotonic()
        if stop_at is None and (stopping or now >= deadline):
            stop_at = now + _TERM_GRACE_SECONDS
            os.killpg(0, signal.SIGTERM)
        elif stop_at is not None and now >= stop_at:
            _kill_adopted_children()
        try:
            pid, status = os.waitpid(-1, os.WNOHANG)
        except ChildProcessError:
            return worker_code
        if pid > 0:
            if pid == worker_pid:
                code = os.waitstatus_to_exitcode(status)
                worker_code = code if code >= 0 else 128 - code
            continue
        if stop_at is None:
            remaining = max(0, deadline - monotonic())
            stopping = bool(poll.poll(round(min(remaining, _POLL_SECONDS) * 1000)))
        else:
            sleep(_POLL_SECONDS)


def _ignore_group_term(signum: int, frame: object) -> None:
    # A caught signal resets to the default for the exec'd worker.
    pass


def _wait_for_start(control: socket.socket, deadline: float) -> bool:
    wait = select.poll()
    wait.register(control, select.POLLIN)
    remaining = deadline - monotonic()
    while remaining > 0:
        # poll accepts a signed C int in milliseconds.
        if wait.poll(min(ceil(remaining * 1000), 2**31 - 1)):
            return control.recv(1) == b"S" and monotonic() < deadline
        remaining = deadline - monotonic()
    return False


def main(
    control_fd: int,
    ledger_uri: str,
    lease_id: str,
    generation: str,
    deadline: float,
    artifact_fd: int | None,
    argv: list[str],
) -> int:
    with socket.socket(fileno=control_fd) as control:
        if not _wait_for_start(control, deadline):
            _record_reaped(ledger_uri, lease_id, generation)
            return 125
        try:
            libc = ctypes.CDLL(None, use_errno=True)
            if libc.prctl(_PR_SET_CHILD_SUBREAPER, 1, 0, 0, 0) != 0:
                raise OSError(ctypes.get_errno(), "cannot supervise descendants")
            signal.signal(signal.SIGTERM, _ignore_group_term)
            _probe_pidfd()
            worker = subprocess.Popen(
                argv, pass_fds=() if artifact_fd is None else (artifact_fd,)
            )
        except OSError:
            # No successful worker launch. Verify that no child remains before
            # persisting the same witness used by the normal exit path.
            try:
                os.waitpid(-1, os.WNOHANG)
            except ChildProcessError:
                _record_reaped(ledger_uri, lease_id, generation)
            with suppress(OSError):
                control.sendall(b"N")
            return 125
        # Only the worker needs the captured artifact once spawned.
        if artifact_fd is not None:
            os.close(artifact_fd)
        # The guardian must not keep worker pipe endpoints open while waiting.
        os.close(0)
        os.close(1)
        try:
            control.sendall(b"Y")
        except OSError:
            stopping = True
        else:
            stopping = False
        worker.returncode = _supervise(control, worker.pid, deadline, stopping)
        _record_reaped(ledger_uri, lease_id, generation)
        return worker.returncode


def _record_reaped(ledger_uri: str, lease_id: str, generation: str) -> None:
    """Persist the receipt independently of the creator's control socket."""
    stat = Path("/proc/self/stat").read_text(encoding="utf-8", errors="replace")
    started = stat.rpartition(")")[2].split()[19]
    delay = _POLL_SECONDS
    while True:
        try:
            with closing(sqlite3.connect(ledger_uri, uri=True, timeout=0.25)) as db, db:
                db.execute("PRAGMA synchronous=FULL")
                db.execute(
                    """UPDATE leases SET tree_reaped = 1
                       WHERE lease_id = ? AND owner_generation = ?
                       AND activity = 'group' AND pid = ? AND process_start = ?""",
                    (lease_id, generation, os.getpid(), started),
                )
            return
        except sqlite3.OperationalError as error:
            # Retain the live witness while a temporary writer owns the ledger.
            code = error.sqlite_errorcode & 0xFF
            if code not in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED):
                raise
            sleep(delay)
            delay = min(delay * 2, 0.25)


if __name__ == "__main__":
    sys.exit(
        main(
            int(sys.argv[1]),
            sys.argv[2],
            sys.argv[3],
            sys.argv[4],
            float(sys.argv[5]),
            int(sys.argv[6]) if sys.argv[6] else None,
            sys.argv[7:],
        )
    )
