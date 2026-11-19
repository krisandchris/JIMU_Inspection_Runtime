"""Linux liveness evidence for a same-host worker or HTTP transport owner."""

import os
from dataclasses import dataclass
from pathlib import Path
from uuid import UUID


@dataclass(frozen=True, slots=True)
class HostProcessIdentity:
    boot_id: str
    pid_namespace: int

    @classmethod
    def current(cls) -> "HostProcessIdentity":
        boot_id = str(
            UUID(
                Path("/proc/sys/kernel/random/boot_id")
                .read_text(encoding="ascii")
                .strip()
            )
        )
        proc_pid = int(
            Path("/proc/self/stat")
            .read_text(encoding="utf-8", errors="replace")
            .partition(" ")[0]
        )
        if proc_pid != os.getpid():
            raise ValueError("procfs does not match the supervisor PID namespace")
        return cls(boot_id, Path("/proc/self/ns/pid").stat().st_ino)

    def process_exited(
        self, boot_id: str, pid_namespace: int, pid: int, started: str
    ) -> bool:
        if boot_id != self.boot_id:
            return True
        if pid_namespace != self.pid_namespace:
            return False
        try:
            state, observed_start = process_start(pid)
        except FileNotFoundError:
            return True
        except (OSError, ValueError):
            return False
        return state == "Z" or observed_start != started


def process_start(pid: int) -> tuple[str, str]:
    # comm may contain spaces or a right parenthesis; stat fields follow the last one.
    stat = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8", errors="replace")
    ending = stat.rpartition(")")[2].split()
    if len(ending) < 20:
        raise ValueError("unverifiable process start")
    return ending[0], ending[19]
