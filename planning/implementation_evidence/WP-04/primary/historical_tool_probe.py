"""Cross-process readback of a signed pre-WP-04 Release with coupled Tool outputs."""

import hashlib
import json
import os
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

FROZEN = "b8e95556aa80fae936bc12eabdf8dff3eb50509bcbea55735028d89267fb6825"


def historical_release(directory, tree):
    from pydantic import TypeAdapter

    from jimu_runtime.bundle import compiler
    from jimu_runtime.bundle.capabilities import CapabilitySnapshot
    from tests.fixtures.wp03.pins.support import accepted_scenario

    assert Path(compiler.__file__).resolve().is_relative_to(tree)
    source = TypeAdapter(compiler.BundleCompileInput).validate_json(
        (directory / "source.json").read_bytes()
    )
    host = CapabilitySnapshot.model_validate_json(
        (directory / "host.json").read_bytes()
    )
    scenario = accepted_scenario(directory, source=source, host=host)
    (directory / "task.json").write_text(scenario.task.model_dump_json())
    print("Historical implementation", Path(compiler.__file__).relative_to(tree))
    print(
        "Historical activation", scenario.task.release_id, scenario.task.bundle_digest
    )


def compare(directory, repo):
    from pydantic import TypeAdapter

    from jimu_runtime.bundle.compiler import BundleCompileInput
    from jimu_runtime.bundle.store import LocalBundleStore
    from jimu_runtime.domain.enums import FactStatus
    from jimu_runtime.domain.runtime.executions import NodeId
    from jimu_runtime.domain.runtime.intake import AcceptedTask
    from tests.contract.test_wp04_tool_executor import (
        _invoke,
        _run_objects,
        _source,
        _worker,
    )
    from tests.fixtures.wp02.releases.support import FixtureHost
    from tests.fixtures.wp03.pins.support import AcceptedScenario

    freeze = repo / "planning/implementation_evidence/WP-03/primary_freeze_b8e95556aa80"
    manifest = (freeze / "product_sha256.json").read_bytes()
    assert hashlib.sha256(manifest).hexdigest() == FROZEN
    files = json.loads(manifest)
    tree = directory / "wp03"
    with zipfile.ZipFile(freeze / "product.zip") as archive:
        assert set(archive.namelist()) == files.keys()
        for name, digest in files.items():
            content = archive.read(name)
            assert hashlib.sha256(content).hexdigest() == digest
            path = tree / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content)
    source, host = _source("shared")
    (directory / "source.json").write_bytes(
        TypeAdapter(BundleCompileInput).dump_json(source)
    )
    (directory / "host.json").write_text(host.model_dump_json())
    subprocess.run(
        [
            sys.executable,
            str(Path(__file__).resolve()),
            "old",
            str(directory),
            str(tree),
        ],
        cwd=tree,
        env={
            **os.environ,
            "PYTHONPATH": os.pathsep.join(map(str, (tree / "src", tree))),
        },
        check=True,
    )
    store = LocalBundleStore(directory / "ledger")
    authority = FixtureHost()
    task = AcceptedTask.model_validate_json((directory / "task.json").read_bytes())
    reopened = AcceptedScenario(store, authority.manager(store), authority, task, host)
    args = _run_objects(reopened, NodeId.N05)
    executor, proof = _worker(directory, args[0])
    complete = _invoke(executor, proof, args, "shared-good")
    assert len(complete.facts) == 2 and all(
        f.status is FactStatus.KNOWN for f in complete.facts
    )
    partial = _invoke(executor, proof, args, "shared-bad")
    error = partial.execution.attempt_result.error.code
    assert error == "tool_partial_payload_unrepresentable"
    assert not partial.facts and partial.response_digest and partial.response_byte_count
    print("Current code reopened same Release", task.release_id, task.bundle_digest)
    print(
        "Valid response",
        complete.execution.status,
        [f.fact_key for f in complete.facts],
    )
    print(
        "Unrepresentable response",
        error,
        partial.response_byte_count,
        partial.response_digest,
    )


if __name__ == "__main__":
    if len(sys.argv) > 1:
        historical_release(Path(sys.argv[2]), Path(sys.argv[3]))
    else:
        with tempfile.TemporaryDirectory(prefix="jimu-pre-wp04-release-") as workspace:
            compare(Path(workspace), Path.cwd())
