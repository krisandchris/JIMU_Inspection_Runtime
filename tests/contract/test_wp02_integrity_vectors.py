import io
import json
from dataclasses import replace
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZIP_STORED, ZipFile, ZipInfo

import pytest

from jimu_runtime.bundle.archive import (
    BundleLimits,
    read_archive,
    read_bundle,
    write_archive,
)
from jimu_runtime.bundle.integrity import (
    build_checksums,
    canonical_json,
    compute_root_digest,
    sha256_digest,
)

_VECTOR = Path(__file__).parents[1] / "fixtures/wp02/integrity/root_vector.json"
_LIMITS = BundleLimits(
    max_archive_bytes=262_144,
    max_expanded_bytes=393_216,
    max_file_bytes=200_000,
    max_files=10,
    max_json_depth=12,
)
_JSON = {"max_bytes": _LIMITS.max_file_bytes, "max_depth": _LIMITS.max_json_depth}


def _vector():
    vector = json.loads(_VECTOR.read_text())
    return vector, {name: wire.encode() for name, wire in vector["files"].items()}


def test_fixed_manifest_checksums_length_frames_and_root() -> None:
    vector, files = _vector()
    payloads = {
        key: val
        for key, val in files.items()
        if key not in ("manifest.json", "checksums.json")
    }
    checksums = build_checksums(payloads)
    assert (
        canonical_json(checksums.model_dump(mode="json"), **_JSON)
        == vector["c"].encode()
    )
    manifest = json.loads(files["manifest.json"])
    m = {
        **manifest,
        "integrity": {
            k: v
            for k, v in manifest["integrity"].items()
            if k not in ("root_digest", "signatures")
        },
    }
    assert canonical_json(m, **_JSON) == vector["m"].encode()
    assert len(vector["m"].encode()).to_bytes(8, "big").hex() == vector["m_frame_hex"]
    assert len(vector["c"].encode()).to_bytes(8, "big").hex() == vector["c_frame_hex"]
    assert compute_root_digest(manifest, checksums, **_JSON) == vector["root_sha256"]
    assert (
        read_bundle(
            write_archive(files, _LIMITS), _LIMITS
        ).manifest.integrity.root_digest
        == vector["root_sha256"]
    )


def test_different_zip_bytes_and_signature_list_keep_logical_root() -> None:
    vector, files = _vector()
    original = write_archive(files, _LIMITS)
    assert write_archive(dict(reversed(files.items())), _LIMITS) == original
    manifest = json.loads(files["manifest.json"])
    manifest["integrity"]["signatures"] = [
        {
            "signature_id": "local-test",
            "signature_registry_id": "local-test",
            "version": "1.0.0",
            "sha256": "c" * 64,
        }
    ]
    updated = {**files, "manifest.json": canonical_json(manifest, **_JSON)}
    with io.BytesIO() as stream:
        with ZipFile(stream, "w", compression=ZIP_STORED) as archive:
            archive.comment = b"transport comment contains PK\x05\x06 markers"
            for name, contents in reversed(updated.items()):
                info = ZipInfo(name, (2025, 1, 1, 1, 1, 0))
                info.comment = b"member comment"
                info.extra = b"\xfe\xca\x01\x00x"
                archive.writestr(info, contents)
        alternate = stream.getvalue()
    assert sha256_digest(alternate) != sha256_digest(original)
    assert read_archive(alternate, _LIMITS) == updated
    assert (
        read_bundle(alternate, _LIMITS).manifest.integrity.root_digest
        == vector["root_sha256"]
    )


class _NonSeekable(io.BytesIO):
    def seek(self, *args, **kwargs):
        raise io.UnsupportedOperation("non-seekable test transport")


@pytest.mark.parametrize("file_size", (65_537, 131_072))
def test_zip_standard_data_descriptor_and_expansion_boundaries(file_size: int) -> None:
    files = {"empty.bin": b"", "repeat.bin": b"A" * file_size, "unicode-é.bin": b"Z"}
    with _NonSeekable() as stream:
        with ZipFile(stream, "w", compression=ZIP_DEFLATED) as archive:
            for name, contents in files.items():
                archive.writestr(name, contents)
        wire = stream.getvalue()
    limits = replace(
        _LIMITS,
        max_file_bytes=file_size,
        max_expanded_bytes=file_size + 1,
        max_archive_bytes=len(wire),
        max_files=len(files),
    )
    assert read_archive(wire, limits) == files
