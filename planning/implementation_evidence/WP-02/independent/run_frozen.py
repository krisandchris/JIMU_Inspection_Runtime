"""Record independent CPU checks against a coordinator-issued product freeze."""

import argparse
import json
import os
import subprocess
import sys
import tempfile
import time
import xml.etree.ElementTree as ET
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[3]
PREFIXES = ("src", "tests", "scripts", "schemas/generated")
FILES = ("pyproject.toml", "uv.lock")
BASELINE = (
    ROOT
    / "planning/implementation_evidence/WP-01C/primary_freeze_a1aec5dc27cc/product_sha256.json"
)
BASELINE_HASH = "a1aec5dc27cc99fa86c1efcfdfc9c026951e865b3e47a5f2a16eeaef4049a4d2"
UV = ["uv", "run", "--locked", "--no-sync"]


def utc():
    return datetime.now(UTC).isoformat()


def file_digest(path):
    return sha256(path.read_bytes()).hexdigest()


def write_json(path, obj):
    path.write_text(json.dumps(obj, sort_keys=True, indent=2) + "\n")


def snapshot():
    files = set(FILES)
    for prefix in PREFIXES:
        for path in (ROOT / prefix).rglob("*"):
            relative = path.relative_to(ROOT)
            if any(
                part.startswith(".") or part == "__pycache__" for part in relative.parts
            ):
                continue
            if path.is_symlink():
                raise ValueError(f"Unexpected symlink in product inventory: {relative}")
            if path.is_file() and path.suffix not in (".pyc", ".pyo"):
                files.add(relative.as_posix())
    return {path: file_digest(ROOT / path) for path in sorted(files)}


def diff(want, actual):
    return {
        "added": sorted(actual.keys() - want.keys()),
        "missing": sorted(want.keys() - actual.keys()),
        "changed": sorted(
            key for key in want.keys() & actual.keys() if want[key] != actual[key]
        ),
    }


def state(expected):
    actual = snapshot()
    differences = diff(expected, actual)
    raw = (json.dumps(actual, sort_keys=True, indent=2) + "\n").encode()
    return {
        "files": len(actual),
        "sha256": sha256(raw).hexdigest(),
        "match": not any(differences.values()),
        **differences,
    }, actual


def run(argv, label, evidence, env, expected):
    started = utc()
    monotonic = time.monotonic()
    before, _ = state(expected)
    stdout_path = evidence / f"{label}.stdout"
    stderr_path = evidence / f"{label}.stderr"
    error = None
    with stdout_path.open("wb") as stdout, stderr_path.open("wb") as stderr:
        try:
            status = subprocess.run(
                argv, cwd=ROOT, env=env, stdout=stdout, stderr=stderr, timeout=600
            ).returncode
        except subprocess.TimeoutExpired:
            status, error = 124, "600-second timeout"
        except OSError as exception:
            status, error = 126, str(exception)
    after, _ = state(expected)
    return {
        "label": label,
        "argv": argv,
        "start_utc": started,
        "end_utc": utc(),
        "duration_seconds": round(time.monotonic() - monotonic, 4),
        "exit_code": status,
        "invocation_error": error,
        "stdout": stdout_path.name,
        "stderr": stderr_path.name,
        "product_before": before,
        "product_after": after,
    }


def schema_digest(directory):
    return {
        path.name: file_digest(path)
        for path in sorted(directory.glob("*"))
        if path.is_file()
    }


def schema_compare(first, second, expected, evidence):
    baseline = json.loads(BASELINE.read_bytes())
    if file_digest(BASELINE) != BASELINE_HASH:
        raise ValueError("WP-01C baseline manifest digest differs")
    prefix = "schemas/generated/"
    older = {
        key.removeprefix(prefix): value
        for key, value in baseline.items()
        if key.startswith(prefix)
    }
    current = {
        key.removeprefix(prefix): value
        for key, value in expected.items()
        if key.startswith(prefix)
    }
    legacy_schema_count = sum(name.endswith(".schema.json") for name in older)
    frozen_schema_count = sum(name.endswith(".schema.json") for name in current)
    first_hashes, second_hashes = schema_digest(first), schema_digest(second)
    legacy_diff = diff(older, {key: current[key] for key in older if key in current})
    result = {
        "expected_c_manifest_sha256": BASELINE_HASH,
        "c_schema_count": legacy_schema_count,
        "c_generated_file_count": len(older),
        "frozen_schema_count": frozen_schema_count,
        "frozen_generated_file_count": len(current),
        "first_files": len(first_hashes),
        "second_files": len(second_hashes),
        "first_to_second": diff(first_hashes, second_hashes),
        "frozen_to_first": diff(current, first_hashes),
        "frozen_to_second": diff(current, second_hashes),
        "wp01c_to_frozen": legacy_diff,
        "wp02_additions": sorted(current.keys() - older.keys()),
        "wp01c_each": {
            key: {
                "legacy": value,
                "frozen": current.get(key),
                "first": first_hashes.get(key),
                "second": second_hashes.get(key),
            }
            for key, value in older.items()
        },
        "test_passed": legacy_schema_count == 347
        and all(
            first_hashes.get(key) == second_hashes.get(key) == current.get(key) == value
            for key, value in older.items()
        )
        and first_hashes == second_hashes == current,
    }
    write_json(evidence / "schemas_comparison.json", result)
    write_json(evidence / "schema_first_sha256.json", first_hashes)
    write_json(evidence / "schema_second_sha256.json", second_hashes)
    return result


def junit_result(filename):
    if not filename.exists():
        return None
    suites = ET.parse(filename).getroot()
    if suites.tag == "testsuite":
        suites = (suites,)
    return {
        key: sum(int(suite.get(key, 0)) for suite in suites)
        for key in ("tests", "failures", "errors", "skipped")
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--manifest-sha256", required=True)
    args = parser.parse_args()
    if file_digest(args.manifest) != args.manifest_sha256:
        parser.error("coordinator-supplied manifest does not match its stated digest")
    expected = json.loads(args.manifest.read_bytes())
    evidence = Path(tempfile.mkdtemp(prefix="frozen_", dir=HERE))
    print(evidence, flush=True)
    (evidence / "driver.py").write_bytes(Path(__file__).read_bytes())
    (evidence / "probe.py").write_bytes((HERE / "runtime_probe.py").read_bytes())
    before, actual = state(expected)
    write_json(evidence / "product_before.json", actual)
    write_json(
        evidence / "freeze_received.json",
        {
            "file": str(args.manifest.resolve()),
            "provided_sha256": args.manifest_sha256,
            "start_utc": utc(),
            "working_directory": str(ROOT),
            "evidence_probe_sha256": file_digest(HERE / "runtime_probe.py"),
            "driver_sha256": file_digest(Path(__file__)),
            "configured_product_scope": [*PREFIXES, *FILES],
            "product_before": before,
        },
    )
    if not before["match"]:
        print(
            "Product is different from the supplied freeze. No test commands ran.",
            file=sys.stderr,
        )
        return 2
    results = []
    with tempfile.TemporaryDirectory(prefix="jimu-wp02-frozen-") as temporary:
        work = Path(temporary)
        first, second = work / "schema-one", work / "schema-two"
        environment = os.environ.copy()
        environment.update(
            UV_OFFLINE="1",
            PYTHONDONTWRITEBYTECODE="1",
            MYPY_CACHE_DIR=str(work / "mypy-cache"),
            RUFF_CACHE_DIR=str(work / "ruff-cache"),
        )
        environment.pop("PYTHONPATH", None)
        environment.pop("PYTEST_ADDOPTS", None)
        environment.pop("PYTEST_PLUGINS", None)
        runtime_environment = {**environment, "PYTHONPATH": str(ROOT / "tests")}
        write_json(
            evidence / "environment_overrides.json",
            {
                key: environment[key]
                for key in (
                    "UV_OFFLINE",
                    "PYTHONDONTWRITEBYTECODE",
                    "MYPY_CACHE_DIR",
                    "RUFF_CACHE_DIR",
                )
            }
            | {
                "PYTHONPATH for isolated runtime probe": runtime_environment[
                    "PYTHONPATH"
                ],
                "unset": [
                    "PYTHONPATH (other checks)",
                    "PYTEST_ADDOPTS",
                    "PYTEST_PLUGINS",
                ],
            },
        )
        environment_query = (
            "import importlib.metadata as m, json, platform, sqlite3, sys; "
            "print(json.dumps(dict(python=sys.version, executable=sys.executable, "
            "platform=platform.platform(), sqlite=sqlite3.sqlite_version, "
            "packages={p:m.version(p) for p in "
            "['jimu-runtime','pydantic','pydantic-core','jsonschema','referencing','rfc8785','pytest','mypy','ruff','uv-build']}),sort_keys=True,indent=2))"
        )
        junit = evidence / "pytest.xml"
        steps = (
            ("uv_version", ["uv", "--version"], environment),
            ("openssl_version", ["openssl", "version"], environment),
            (
                "dependency_versions",
                [*UV, "python", "-c", environment_query],
                environment,
            ),
            (
                "pytest",
                [
                    *UV,
                    "python",
                    "-m",
                    "pytest",
                    "tests/unit",
                    "tests/contract",
                    "tests/negative",
                    "-q",
                    "-p",
                    "no:cacheprovider",
                    f"--junitxml={junit}",
                ],
                environment,
            ),
            (
                "ruff_lint",
                [*UV, "ruff", "check", "src", "tests", "scripts"],
                environment,
            ),
            (
                "ruff_format",
                [*UV, "ruff", "format", "--check", "src", "tests", "scripts"],
                environment,
            ),
            ("mypy", [*UV, "mypy", "src"], environment),
            (
                "schema_first",
                [*UV, "python", "scripts/generate_schemas.py", "--output", str(first)],
                environment,
            ),
            (
                "schema_second",
                [*UV, "python", "scripts/generate_schemas.py", "--output", str(second)],
                environment,
            ),
            (
                "schema_check",
                [*UV, "python", "scripts/generate_schemas.py", "--check"],
                environment,
            ),
            (
                "isolated_runtime",
                [*UV, "python", str(HERE / "runtime_probe.py")],
                runtime_environment,
            ),
        )
        for label, argv, env in steps:
            result = run(argv, label, evidence, env, expected)
            results.append(result)
            write_json(evidence / "commands.json", results)
            print(f"{label} {result['exit_code']}", flush=True)
        schema_result = schema_compare(first, second, expected, evidence)
    after, actual = state(expected)
    write_json(evidence / "product_after.json", actual)
    integrity = all(
        row["product_before"]["match"] and row["product_after"]["match"]
        for row in results
    )
    passed = (
        before["match"]
        and after["match"]
        and integrity
        and schema_result["test_passed"]
        and all(row["exit_code"] == 0 for row in results)
    )
    write_json(
        evidence / "run_result.json",
        {
            "frozen_manifest_sha256": args.manifest_sha256,
            "product_before": before,
            "product_after": after,
            "product_every_step_matches": integrity,
            "cpu_pytest_junit": junit_result(junit),
            "schema_bytes_pass": schema_result["test_passed"],
            "step_exit_codes": {row["label"]: row["exit_code"] for row in results},
            "engineering_acceptance": "PASS" if passed else "FAIL",
            "production_quality": "UNVERIFIED",
            "end_utc": utc(),
        },
    )
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
