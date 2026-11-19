import json
from hashlib import sha256

import pytest
import rfc8785
from pydantic import ValidationError

from jimu_runtime.bundle.archive import read_archive
from jimu_runtime.bundle.compiler import attach_signature_refs, compile_bundle
from jimu_runtime.domain.runtime.bundle_manifest import DetachedSignatureRef
from tests.fixtures.wp02.compiler.scenario import (
    LIMITS,
    compile_environment,
    source_scenario,
)


def test_archive_inventory_root_and_detached_signature_contract(tmp_path):
    report = compile_bundle(
        source_scenario(), limits=LIMITS, environment=compile_environment(tmp_path)
    )
    assert report.bundle is not None, report.errors
    raw = report.bundle.archive_bytes
    files = read_archive(raw, LIMITS)
    manifest = json.loads(files["manifest.json"])
    signatures = manifest["integrity"].pop("signatures")
    logical_root = manifest["integrity"].pop("root_digest")
    assert signatures == []
    m = rfc8785.dumps(manifest)
    c = files["checksums.json"]
    checksum_rows = json.loads(c)["entries"]
    assert {row["path"] for row in checksum_rows} == set(files) - {
        "manifest.json",
        "checksums.json",
    }
    for row in checksum_rows:
        payload = files[row["path"]]
        assert (row["size"], row["sha256"]) == (
            len(payload),
            sha256(payload).hexdigest(),
        )
    assert (
        logical_root
        == sha256(
            b"JIMU-RUNTIME-BUNDLE-ROOT-v1\0"
            + len(m).to_bytes(8, "big")
            + m
            + len(c).to_bytes(8, "big")
            + c
        ).hexdigest()
    )
    model_entries = [
        row for row in manifest["artifacts"] if row["path"] == "models/bindings.json"
    ]
    assert [row["json_pointer"] for row in model_entries] == ["/0", "/1", "/2", "/3"]
    aggregate = json.loads(files["models/bindings.json"])
    for index, row in enumerate(model_entries):
        assert (
            row["content_ref"]["digest"]
            == sha256(rfc8785.dumps(aggregate[index])).hexdigest()
        )

    ref = DetachedSignatureRef(
        signature_id="fixture.signature",
        version="1.0.0",
        sha256="aa" * 32,
        signature_registry_id="fixture.external",
    )
    attached = attach_signature_refs(raw, (ref,), limits=LIMITS)
    new_files = read_archive(attached, LIMITS)
    new = json.loads(new_files["manifest.json"])
    assert new["integrity"].pop("signatures") == [ref.model_dump(mode="json")]
    assert new["integrity"].pop("root_digest") == logical_root
    assert new == manifest
    assert {
        key: value for key, value in new_files.items() if key != "manifest.json"
    } == {key: value for key, value in files.items() if key != "manifest.json"}
    with pytest.raises(ValidationError, match="must be unique"):
        attach_signature_refs(attached, (ref,), limits=LIMITS)
