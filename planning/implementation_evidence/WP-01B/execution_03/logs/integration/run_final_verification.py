"""Evidence-only runner. Execute only after Primary's explicit integration GO."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import stat
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

WORKTREE = Path(
    "/mnt/lc/LC/ares_xtws/4_agent/RSI_ares/inspect_pipeline/JIMU_Inspection_Runtime_WP01B"
)
EVIDENCE = (
    WORKTREE / "planning/implementation_evidence/WP-01B/execution_03/logs/integration"
)
ROOTS = ("src", "tests", "scripts", "schemas/generated")
DEPENDENCIES = ("pyproject.toml", "uv.lock", ".python-version")
CACHE_DIRECTORIES = {"__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache"}


def utc() -> str:
    return datetime.now(UTC).isoformat()


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def write_json(path: Path, value: object) -> None:
    with path.open("x", encoding="utf-8") as stream:
        stream.write(
            json.dumps(value, ensure_ascii=True, sort_keys=True, indent=2) + "\n"
        )


def snapshot() -> dict[str, object]:
    entries: dict[str, object] = {}

    def visit(path: Path) -> None:
        relative = path.relative_to(WORKTREE).as_posix()
        if path.name in CACHE_DIRECTORIES or path.suffix in {".pyc", ".pyo"}:
            return
        try:
            info = path.lstat()
        except FileNotFoundError:
            entries[relative] = {"type": "missing"}
            return
        mode = stat.S_IMODE(info.st_mode)
        if stat.S_ISLNK(info.st_mode):
            entries[relative] = {
                "type": "symlink",
                "mode": mode,
                "target": os.readlink(path),
                "mtime_ns": info.st_mtime_ns,
                "ctime_ns": info.st_ctime_ns,
            }
        elif stat.S_ISDIR(info.st_mode):
            # Directory timestamps respond to excluded cache creation; do not use them.
            entries[relative] = {"type": "directory", "mode": mode}
            for child in sorted(path.iterdir(), key=lambda item: item.name):
                visit(child)
        elif stat.S_ISREG(info.st_mode):
            content = path.read_bytes()
            after = path.lstat()
            if (info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns) != (
                after.st_ino,
                after.st_size,
                after.st_mtime_ns,
                after.st_ctime_ns,
            ):
                raise RuntimeError(f"Concurrent mutation while snapshotting {relative}")
            entries[relative] = {
                "type": "file",
                "mode": mode,
                "size": len(content),
                "sha256": sha(content),
                "mtime_ns": after.st_mtime_ns,
                "ctime_ns": after.st_ctime_ns,
            }
        else:
            raise RuntimeError(
                f"Unexpected filesystem entry in evidence scope: {relative}"
            )

    for root in (*ROOTS, *DEPENDENCIES):
        visit(WORKTREE / root)
    encoded = json.dumps(
        entries, ensure_ascii=True, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return {"captured_at": utc(), "inventory_sha256": sha(encoded), "entries": entries}


def changes(before: dict[str, object], after: dict[str, object]) -> list[str]:
    left, right = before["entries"], after["entries"]
    assert isinstance(left, dict) and isinstance(right, dict)
    return sorted(
        key for key in left.keys() | right.keys() if left.get(key) != right.get(key)
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--go",
        action="store_true",
        help="Required guard; only use after explicit Primary GO",
    )
    args = parser.parse_args()
    if not args.go:
        parser.error(
            "No verification executed. Await explicit Primary GO, then pass --go."
        )
    if Path.cwd() != WORKTREE or Path.cwd().resolve() != WORKTREE:
        raise RuntimeError(
            "Runner requires exact authorized WP01B cwd, with no root symlink"
        )
    if Path(__file__).resolve().parent != EVIDENCE or EVIDENCE.resolve() != EVIDENCE:
        raise RuntimeError(
            "Runner/evidence must remain in authorized integration directory"
        )
    run = EVIDENCE / (
        datetime.now(UTC).strftime("run_%Y%m%dT%H%M%S_%fZ_") + uuid4().hex[:8]
    )
    run.mkdir(exist_ok=False)
    prefix = ["uv", "run", "--locked", "--no-sync"]
    junit = run / "final_pytest.xml"
    commands = [
        (
            "versions",
            [
                *prefix,
                "python",
                "-c",
                "import sys,platform,importlib.metadata as m; print(sys.version); print(platform.platform()); print({p:m.version(p) for p in ['pydantic','pytest','ruff','mypy','rfc8785','jsonschema']})",
            ],
        ),
        (
            "pytest",
            [
                *prefix,
                "python",
                "-m",
                "pytest",
                "tests/unit",
                "tests/contract",
                "tests/negative",
                "-q",
                "--junitxml=" + str(junit),
            ],
        ),
        ("ruff", [*prefix, "ruff", "check", "."]),
        ("format", [*prefix, "ruff", "format", "--check", "src", "tests", "scripts"]),
        ("mypy", [*prefix, "mypy", "src/jimu_runtime"]),
        ("schema_check", [*prefix, "python", "scripts/generate_schemas.py", "--check"]),
    ]
    write_json(
        run / "metadata.json",
        {
            "cwd": str(WORKTREE),
            "started_at": utc(),
            "runner_sha256": sha(Path(__file__).read_bytes()),
            "runner_python": sys.version,
            "snapshot_roots": ROOTS,
            "dependency_files": DEPENDENCIES,
            "excluded_cache_directories": sorted(CACHE_DIRECTORIES),
            "excluded_suffixes": [".pyc", ".pyo"],
            "historical_disclosure": "Prior whole-tree format check failed on three sealed historical scripts. This runner neither modifies nor ignores those files; actual current format outcome is recorded.",
            "authorization": "Manual --go guard; external explicit Primary GO required. No test execution during preparation.",
        },
    )
    initial = snapshot()
    write_json(run / "initial_snapshot.json", initial)
    results = []
    for index, (name, argv) in enumerate(commands, 1):
        identifier = f"{index:02d}_{name}"
        before = snapshot()
        write_json(run / f"{identifier}_before.json", before)
        start = utc()
        result = subprocess.run(
            argv,
            cwd=WORKTREE,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            check=False,
        )
        end = utc()
        (run / f"{identifier}.stdout").write_bytes(result.stdout)
        (run / f"{identifier}.stderr").write_bytes(result.stderr)
        after = snapshot()
        write_json(run / f"{identifier}_after.json", after)
        delta = changes(before, after)
        record = {
            "cwd": str(WORKTREE),
            "argv": argv,
            "started_at": start,
            "ended_at": end,
            "exit": result.returncode,
            "stdout": result.stdout.decode("utf-8", errors="replace"),
            "stderr": result.stderr.decode("utf-8", errors="replace"),
            "stdout_sha256": sha(result.stdout),
            "stderr_sha256": sha(result.stderr),
            "before_inventory_sha256": before["inventory_sha256"],
            "after_inventory_sha256": after["inventory_sha256"],
            "changed_paths": delta,
            "snapshot_unchanged": not delta,
        }
        write_json(run / f"{identifier}.json", record)
        results.append(
            {
                "name": name,
                "exit": result.returncode,
                "snapshot_unchanged": not delta,
                "changed_paths": delta,
            }
        )
        print(
            f"{identifier}: exit={result.returncode}; snapshot_unchanged={not delta}",
            flush=True,
        )
    final = snapshot()
    write_json(run / "final_snapshot.json", final)
    overall_delta = changes(initial, final)
    success = (
        all(item["exit"] == 0 and item["snapshot_unchanged"] for item in results)
        and not overall_delta
    )
    write_json(
        run / "summary.json",
        {
            "ended_at": utc(),
            "commands": results,
            "all_commands_exit_zero": all(item["exit"] == 0 for item in results),
            "whole_run_snapshot_unchanged": not overall_delta,
            "changed_paths": overall_delta,
            "verification_success": success,
            "junit_present": junit.is_file(),
            "junit_sha256": sha(junit.read_bytes()) if junit.is_file() else None,
            "scope": "CPU integration verification only; no runtime/model/GPU or production readiness claim; Primary review pending",
        },
    )
    print(f"Evidence: {run}", flush=True)
    return 0 if success else 1


if __name__ == "__main__":
    raise SystemExit(main())
