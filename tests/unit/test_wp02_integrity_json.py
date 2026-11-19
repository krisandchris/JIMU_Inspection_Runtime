import json
import struct
from pathlib import Path

import pytest

from jimu_runtime.bundle.integrity import (
    artifact_digest,
    canonical_json,
    canonicalize_json,
    read_canonical_json,
    resolve_json_pointer,
    sha256_digest,
)

_BUDGET = {"max_bytes": 4096, "max_depth": 8}
_RFC_VECTORS = Path(__file__).parents[1] / "fixtures/wp01b/jcs_vectors.json"


def test_rfc8785_text_vectors() -> None:
    vectors = json.loads(_RFC_VECTORS.read_text(encoding="utf-8"))
    for key in ("utf16", "escaping"):
        value = vectors[key]["input"]
        expected = vectors[key]["expected"].encode("utf-8")
        assert canonical_json(value, **_BUDGET) == expected
        assert read_canonical_json(expected, **_BUDGET) == value


@pytest.mark.parametrize(
    ("wire", "expected"),
    [
        (b'  { "b" : -0.0, "a": [1e+00] }  ', b'{"a":[1],"b":0}'),
        (b"9007199254740991", b"9007199254740991"),
        (b"-9007199254740991", b"-9007199254740991"),
        (b"4.9406564584124654e-324", b"5e-324"),
        (b"1.0000000000000000e+21", b"1e+21"),
        (b'"[{}]\\"[]"', b'"[{}]\\"[]"'),
    ],
)
def test_normalization_has_canonical_readback(wire: bytes, expected: bytes) -> None:
    assert canonicalize_json(wire, **_BUDGET) == expected
    assert (
        canonical_json(read_canonical_json(expected, **_BUDGET), **_BUDGET) == expected
    )


def test_rfc8785_number_vectors_in_bundle_domain() -> None:
    vectors = json.loads(_RFC_VECTORS.read_text(encoding="utf-8"))
    excluded = {
        "9007199254740992",
        "-9007199254740992",
        "295147905179352830000",
        "999999999999999700000",
        "999999999999999900000",
    }
    for bytestream, expected in vectors["numbers"]:
        if expected not in excluded:
            (value,) = struct.unpack(">d", bytes.fromhex(bytestream))
            assert canonical_json(value, **_BUDGET) == expected.encode("ascii")


def test_pointer_escapes_root_objects_arrays_and_empty_keys() -> None:
    value = {"": [{"~1/a": {"01": 7}}]}
    assert resolve_json_pointer(value, "") is value
    assert resolve_json_pointer(value, "//0/~01~1a/01") == 7


def test_json_artifact_and_opaque_asset_hash_exact_intended_bytes() -> None:
    aggregate = b'{"items":[{"a":1},{"b":2}]}'
    assert artifact_digest(
        aggregate, path="example.json", json_pointer="/items/1", **_BUDGET
    ) == sha256_digest(b'{"b":2}')
    assert artifact_digest(
        aggregate, path="example.json", json_pointer="", **_BUDGET
    ) == sha256_digest(aggregate)
    opaque = b"\xff\x00\r\n"
    assert (
        artifact_digest(opaque, path="resources/assets/data/example.json", **_BUDGET)
        == "6375a1044d294c4efc761ce86b9c48d451d11bcf9ef4b586f56d833edb18f6da"
    )
