import io
import json
import stat
import struct
import warnings
from dataclasses import replace
from pathlib import Path
from zipfile import ZIP_BZIP2, ZIP_DEFLATED, ZipFile, ZipInfo

import pytest

from jimu_runtime.bundle.archive import (
    BundleLimits,
    read_archive,
    read_bundle,
    write_archive,
)
from jimu_runtime.bundle.integrity import (
    BundleIntegrityError,
    build_checksums,
    canonical_json,
    canonicalize_json,
    compute_root_digest,
    read_canonical_json,
    resolve_json_pointer,
    validate_payload_path,
)

_BUDGET = {"max_bytes": 16384, "max_depth": 8}
_LIMITS = BundleLimits(
    max_archive_bytes=16384,
    max_expanded_bytes=16384,
    max_file_bytes=16384,
    max_files=12,
    max_json_depth=8,
)
_VECTOR = Path(__file__).parents[1] / "fixtures/wp02/integrity/root_vector.json"


@pytest.mark.parametrize(
    ("wire", "code", "location"),
    [
        (b'{"a":[{"x":1,"\\u0078":2}]}', "duplicate_json_key", ("a", 0, "x")),
        (b'{"a":"\\ud800"}', "invalid_unicode", ("a",)),
        (b'"\xff"', "invalid_unicode", ()),
        (b"\xef\xbb\xbfnull", "invalid_json", ()),
        (b'{"a":NaN}', "invalid_number", ("a",)),
        (b'{"a":Infinity}', "invalid_number", ("a",)),
        (b"1e9999", "invalid_number", ()),
        (b"1e-9999", "invalid_number", ()),
        (b"9007199254740992", "invalid_number", ()),
        (b"-9007199254740992", "invalid_number", ()),
        (b"9007199254740993.0", "invalid_number", ()),
        (b"1234567890" * 500, "invalid_number", ()),
        (b"[" * 3000 + b"]" * 3000, "json_depth_limit", ()),
        (b"] [", "invalid_json", ()),
    ],
)
def test_bad_json_is_rejected_with_member_and_location(wire, code, location) -> None:
    with pytest.raises(BundleIntegrityError) as error:
        canonicalize_json(wire, **_BUDGET, path="config.json")
    assert error.value.code == code
    assert error.value.path == "config.json"
    assert error.value.json_location == location


def test_source_and_output_limits_and_cyclic_tree() -> None:
    with pytest.raises(BundleIntegrityError) as error:
        canonicalize_json(b"[] ", max_bytes=2, max_depth=1)
    assert error.value.code == "json_size_limit"
    with pytest.raises(BundleIntegrityError) as error:
        canonicalize_json(b"1e-6", max_bytes=4, max_depth=0)
    assert error.value.code == "json_size_limit"
    value = []
    value.append(value)
    with pytest.raises(BundleIntegrityError) as error:
        canonical_json(value, **_BUDGET)
    assert error.value.code == "json_depth_limit"
    with pytest.raises(BundleIntegrityError) as error:
        canonical_json({"long": "x" * 5}, max_bytes=5, max_depth=2)
    assert error.value.code == "json_size_limit"


@pytest.mark.parametrize("wire", [b'{"b":1,"a":2}', b"null\n", b"0.00"])
def test_admission_strictly_requires_canonical_storage(wire) -> None:
    with pytest.raises(BundleIntegrityError) as error:
        read_canonical_json(wire, **_BUDGET)
    assert error.value.code == "noncanonical_json"


@pytest.mark.parametrize(
    "name",
    [
        "/bundle/one",
        "../one",
        "a/../b",
        "a/./b",
        "a//b",
        "a/.",
        "a/..",
        "C:/one",
        r"a\b",
        "a\x00b",
        "a\x85b",
        "a?b",
        "a#b",
        "a/",
        "",
        "a" * 513,
    ],
)
def test_unsafe_or_nonnormalized_paths(name) -> None:
    with pytest.raises(BundleIntegrityError) as error:
        validate_payload_path(name)
    assert error.value.path == name
    assert error.value.code == "invalid_path"


@pytest.mark.parametrize(
    ("pointer", "reason"),
    [
        ("#/0", "invalid_pointer"),
        ("0", "invalid_pointer"),
        ("/~2", "invalid_pointer"),
        ("/~", "invalid_pointer"),
        ("/01", "unresolved_pointer"),
        ("/-", "unresolved_pointer"),
        ("/-1", "unresolved_pointer"),
        ("/１", "unresolved_pointer"),
        ("/" + "9" * 1000, "unresolved_pointer"),
        ("/0/child", "unresolved_pointer"),
    ],
)
def test_invalid_rfc6901_pointer(pointer, reason) -> None:
    with pytest.raises(BundleIntegrityError) as error:
        resolve_json_pointer([None], pointer, path="aggregate.json")
    assert error.value.code == reason
    assert error.value.path == "aggregate.json"


def _zip(entries, *, compression=ZIP_DEFLATED):
    stream = io.BytesIO()
    with (
        warnings.catch_warnings(),
        ZipFile(stream, "w", compression=compression) as archive,
    ):
        warnings.filterwarnings(
            "ignore", message="Duplicate name:", category=UserWarning
        )
        for name, data in entries:
            archive.writestr(name, data)
    return stream.getvalue()


@pytest.mark.parametrize(
    ("entries", "expected"),
    [
        ([("same.dat", b"a"), ("same.dat", b"b")], "duplicate_zip_member"),
        ([("../untrusted.dat", b"a")], "invalid_path"),
        ([("/untrusted.dat", b"a")], "invalid_path"),
        ([(r"nested\untrusted.dat", b"a")], "invalid_path"),
        ([("directory/", b"")], "invalid_path"),
    ],
)
def test_invalid_zip_member_names_are_rejected(entries, expected) -> None:
    with pytest.raises(BundleIntegrityError) as error:
        read_archive(_zip(entries), _LIMITS)
    assert error.value.code == expected


def test_link_unsupported_compression_and_nonzip() -> None:
    link = ZipInfo("local.bin")
    link.create_system = 3
    link.external_attr = (stat.S_IFLNK | 0o777) << 16
    for wire in (
        _zip([(link, b"a")]),
        _zip([("ordinary.bin", b"a")], compression=ZIP_BZIP2),
        b"not a zip",
    ):
        with pytest.raises(BundleIntegrityError) as error:
            read_archive(wire, _LIMITS)
        assert error.value.code == "invalid_archive"


def test_exact_archive_expansion_and_file_count_budgets() -> None:
    files = {"a": b"a" * 32, "b": b"b" * 32}
    wire = write_archive(files, _LIMITS)
    for key, limit, code in (
        ("max_archive_bytes", len(wire) - 1, "archive_size_limit"),
        ("max_file_bytes", 31, "file_size_limit"),
        ("max_expanded_bytes", 63, "expanded_size_limit"),
        ("max_files", 1, "archive_file_count_limit"),
    ):
        limits = replace(_LIMITS, **{key: limit})
        for operation, source in ((read_archive, wire), (write_archive, files)):
            with pytest.raises(BundleIntegrityError) as error:
                operation(source, limits)
            assert error.value.code == code


def test_actual_expansion_controls_small_archive_with_wrong_size_metadata() -> None:
    wire = bytearray(_zip([("sample.bin", b"x" * 128)]))
    directory = wire.index(b"PK\x01\x02")
    struct.pack_into("<I", wire, 22, 1)
    struct.pack_into("<I", wire, directory + 24, 1)

    for limits, code in (
        (replace(_LIMITS, max_file_bytes=16), "file_size_limit"),
        (replace(_LIMITS, max_expanded_bytes=16), "expanded_size_limit"),
        (_LIMITS, "invalid_archive"),
    ):
        with pytest.raises(BundleIntegrityError) as error:
            read_archive(bytes(wire), limits)
        assert error.value.code == code
        assert error.value.path == "sample.bin"


def test_mismatched_zip_local_name_and_truncated_zip() -> None:
    wire = bytearray(_zip([("sample.bin", b"sample")]))
    wire[30] = ord("t")
    for raw in (bytes(wire), bytes(wire[:-1])):
        with pytest.raises(BundleIntegrityError) as error:
            read_archive(raw, _LIMITS)
        assert error.value.code == "invalid_archive"


def _files():
    vector = json.loads(_VECTOR.read_text())
    return {name: wire.encode() for name, wire in vector["files"].items()}


@pytest.mark.parametrize(
    ("path", "raw", "expected"),
    [
        ("resources/assets/data/fixture.json", b"opaque\n", "checksum_mismatch"),
        (
            "ontology/entities.json",
            b'[{"label":"bicycle"}, {"label":"scooter"}]',
            "noncanonical_json",
        ),
        (
            "ontology/entities.json",
            b'[{"label":"bicycle"},{"label":"scooter2"}]',
            "checksum_mismatch",
        ),
        ("unknown.bin", b"x", "checksum_path_mismatch"),
        (
            "checksums.json",
            b'{"entries":[],"schema_version":"1.0.0"}',
            "checksum_path_mismatch",
        ),
    ],
)
def test_byte_tampering_fails_read_from_final_zip(path, raw, expected) -> None:
    files = _files()
    files[path] = raw
    with pytest.raises(BundleIntegrityError) as error:
        read_bundle(write_archive(files, _LIMITS), _LIMITS)
    assert error.value.code == expected


def test_manifest_covered_metadata_and_member_digest_are_independently_checked() -> (
    None
):
    files = _files()
    manifest = json.loads(files["manifest.json"])
    manifest["created_at"] = "2026-01-01T00:00:00Z"
    files["manifest.json"] = canonical_json(manifest, **_BUDGET)
    with pytest.raises(BundleIntegrityError) as error:
        read_bundle(write_archive(files, _LIMITS), _LIMITS)
    assert error.value.code == "root_digest_mismatch"

    manifest["artifacts"][0]["content_ref"]["digest"] = "a" * 64
    checksums = build_checksums(
        {
            path: data
            for path, data in files.items()
            if path not in ("manifest.json", "checksums.json")
        }
    )
    manifest["integrity"]["root_digest"] = compute_root_digest(
        manifest, checksums, **_BUDGET
    )
    files["manifest.json"] = canonical_json(manifest, **_BUDGET)
    with pytest.raises(BundleIntegrityError) as error:
        read_bundle(write_archive(files, _LIMITS), _LIMITS)
    assert error.value.code == "artifact_digest_mismatch"
    assert error.value.path == "ontology/entities.json"


def test_overlapping_member_locators_are_rejected_before_artifact_hashing() -> None:
    files = _files()
    manifest = json.loads(files["manifest.json"])
    manifest["artifacts"][1]["json_pointer"] = "/0/label"
    checksums = build_checksums(
        {
            path: raw
            for path, raw in files.items()
            if path not in ("manifest.json", "checksums.json")
        }
    )
    manifest["integrity"]["root_digest"] = compute_root_digest(
        manifest, checksums, **_BUDGET
    )
    files["manifest.json"] = canonical_json(manifest, **_BUDGET)
    with pytest.raises(BundleIntegrityError) as error:
        read_bundle(write_archive(files, _LIMITS), _LIMITS)
    assert error.value.code == "overlapping_artifact_pointers"
    assert error.value.path == "ontology/entities.json"


def test_recomputed_file_checksums_cannot_keep_the_original_root() -> None:
    files = _files()
    files["resources/assets/data/fixture.json"] = b"opaque!!"
    checksums = build_checksums(
        {
            path: raw
            for path, raw in files.items()
            if path not in ("manifest.json", "checksums.json")
        }
    )
    manifest = json.loads(files["manifest.json"])
    assert manifest["integrity"]["root_digest"] != compute_root_digest(
        manifest, checksums, **_BUDGET
    )
    files["checksums.json"] = canonical_json(
        checksums.model_dump(mode="json"), **_BUDGET
    )
    with pytest.raises(BundleIntegrityError) as error:
        read_bundle(write_archive(files, _LIMITS), _LIMITS)
    assert error.value.code == "root_digest_mismatch"
