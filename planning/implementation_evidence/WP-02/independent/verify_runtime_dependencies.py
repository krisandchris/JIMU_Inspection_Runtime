"""Repeat the WP-02 isolated runtime probe from the locked runtime-only environment."""

import importlib.util
import json
import os
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
DRIVER = HERE / "run_frozen.py"
SPEC = importlib.util.spec_from_file_location("wp02_frozen_driver", DRIVER)
assert SPEC is not None and SPEC.loader is not None
driver = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(driver)
ROOT = HERE.parents[3]

FROZEN = (
    ROOT
    / "planning/implementation_evidence/WP-02/primary_freeze_844ed4ac1275/product_sha256.json"
)
EXPECTED = "844ed4ac127528dad9eead9fb927459fa964ce784d405cb90bd4cb6d464e2556"


def main():
    if driver.file_digest(FROZEN) != EXPECTED:
        raise ValueError("Unexpected product freeze manifest")
    expected = json.loads(FROZEN.read_bytes())
    start, files = driver.state(expected)
    if not start["match"]:
        raise ValueError("Product files differ from the frozen manifest")
    directory = Path(tempfile.mkdtemp(prefix="runtime_deps_", dir=HERE))
    print(directory, flush=True)
    driver.write_json(directory / "product_before.json", files)
    with tempfile.TemporaryDirectory(prefix="jimu-wp02-cpu-nodev-") as temp:
        virtual = Path(temp) / "venv"
        environment = os.environ.copy()
        environment.update(
            UV_OFFLINE="1",
            UV_PROJECT_ENVIRONMENT=str(virtual),
            PYTHONDONTWRITEBYTECODE="1",
        )
        environment.pop("PYTHONPATH", None)
        steps = (
            (
                "sync_without_dev",
                [
                    "uv",
                    "sync",
                    "--offline",
                    "--locked",
                    "--no-dev",
                    "--python",
                    ".venv/bin/python",
                ],
                environment,
            ),
            (
                "runtime_package_versions",
                [
                    str(virtual / "bin/python"),
                    "-c",
                    "from importlib.metadata import version; "
                    "from importlib.util import find_spec; "
                    "assert all(find_spec(name) is None for name in ('pytest','ruff','mypy')); "
                    "print({name:version(name) for name in ('jimu-runtime','jsonschema','pydantic','rfc8785')})",
                ],
                environment,
            ),
            (
                "runtime_without_dev",
                [str(virtual / "bin/python"), str(HERE / "runtime_probe.py")],
                {**environment, "PYTHONPATH": str(ROOT / "tests")},
            ),
        )
        driver.write_json(
            directory / "environment_overrides.json",
            {
                "UV_OFFLINE": environment["UV_OFFLINE"],
                "UV_PROJECT_ENVIRONMENT": environment["UV_PROJECT_ENVIRONMENT"],
                "PYTHONDONTWRITEBYTECODE": environment["PYTHONDONTWRITEBYTECODE"],
                "PYTHONPATH for isolated runtime probe": str(ROOT / "tests"),
            },
        )
        results = []
        for name, args, env in steps:
            result = driver.run(args, name, directory, env, expected)
            results.append(result)
            print(name, result["exit_code"], flush=True)
            if result["exit_code"]:
                break
    finish, files = driver.state(expected)
    driver.write_json(directory / "commands.json", results)
    driver.write_json(directory / "product_after.json", files)
    accepted = (
        start["match"]
        and finish["match"]
        and len(results) == len(steps)
        and all(
            step["exit_code"] == 0
            and step["product_before"]["match"]
            and step["product_after"]["match"]
            for step in results
        )
    )
    driver.write_json(
        directory / "run_result.json",
        {
            "frozen_manifest_sha256": EXPECTED,
            "start": start,
            "finish": finish,
            "result": "PASS" if accepted else "FAIL",
        },
    )
    return 0 if accepted else 1


if __name__ == "__main__":
    raise SystemExit(main())
