"""Allowlisted CPU-only subprocess bodies for worker lifecycle tests."""

import os
import signal
import subprocess
import sys
from pathlib import Path
from time import sleep


def _write_ready(path: Path, text: str) -> None:
    temporary = path.with_suffix(".pending")
    temporary.write_text(text, encoding="ascii")
    temporary.rename(path)


def main() -> None:
    mode = sys.argv[1]
    if mode in ("echo", "slow-echo"):
        if mode == "slow-echo":
            sleep(0.12)
        sys.stdout.buffer.write(sys.stdin.buffer.read()[::-1])
    elif mode == "artifact":
        sys.stdout.buffer.write(Path(sys.argv[2]).read_bytes())
    elif mode == "oversize":
        sys.stdout.buffer.write(b"x" * 131072)
    elif mode == "failure":
        sys.stdout.buffer.write(b"{}")
        sys.stderr.write("PRIVATE_STDERR")
        sys.exit(4)
    elif mode in ("tree", "escaped-tree", "escaped-partial"):
        ready = Path(sys.argv[2])
        descendant_ready = ready.with_suffix(".child")
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        child = subprocess.Popen(
            [sys.executable, __file__, "descendant", str(descendant_ready)],
            start_new_session=mode != "tree",
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        while not descendant_ready.exists():
            sleep(0.01)
        if mode == "escaped-partial":
            sys.stdout.buffer.write(b'{"partial":')
            sys.stdout.buffer.flush()
            os.close(1)
        _write_ready(ready, f"{os.getpid()} {child.pid} {os.getpgrp()}")
        child.wait(timeout=30)
    elif mode == "descendant":
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        _write_ready(Path(sys.argv[2]), str(os.getpid()))
        sleep(30)


if __name__ == "__main__":
    main()
