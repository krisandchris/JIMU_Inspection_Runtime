"""Byte-level JSON, locator and digest rules for Runtime Bundle v1."""

import json
import math
import re
from collections.abc import Mapping
from dataclasses import dataclass
from hashlib import sha256
from types import MappingProxyType
from typing import cast

import rfc8785
from pydantic import TypeAdapter, ValidationError

from jimu_runtime.domain.fact_schemas.canonicalization import JSONValue
from jimu_runtime.domain.runtime.bundle_manifest import (
    BundleManifest,
    BundlePayloadPath,
    ChecksumEntry,
    ChecksumManifest,
    JsonPointer,
)

MANIFEST_PATH = "manifest.json"
CHECKSUMS_PATH = "checksums.json"
METADATA_PATHS = frozenset((MANIFEST_PATH, CHECKSUMS_PATH))
MAX_JSON_DEPTH = 128
_ROOT_DOMAIN = b"JIMU-RUNTIME-BUNDLE-ROOT-v1\x00"
_MAX_INTEGER = 2**53 - 1
_JSON_SPECIAL = re.compile(r'["\\{}\[\]]')
_ARRAY_INDEX = re.compile(r"0|[1-9][0-9]*")
_PAYLOAD_PATH: TypeAdapter[str] = TypeAdapter(BundlePayloadPath)
_POINTER: TypeAdapter[str] = TypeAdapter(JsonPointer)

type JSONLocation = tuple[str | int, ...]


class BundleIntegrityError(ValueError):
    """Failure with a stable reason and optional member/JSON location."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        path: str | None = None,
        json_location: JSONLocation = (),
    ) -> None:
        self.code = code
        self.path = path
        self.json_location = json_location
        super().__init__(message)


@dataclass(frozen=True)
class VerifiedBundleFiles:
    """Files which passed logical byte checks, prior to semantic admission."""

    manifest: BundleManifest
    checksums: ChecksumManifest
    payloads: Mapping[str, bytes]


@dataclass(frozen=True)
class _ObjectPairs:
    pairs: list[tuple[str, object]]


@dataclass(frozen=True)
class _Number:
    token: str


def validate_payload_path(path: str) -> str:
    """Validate exact wire spelling with the frozen payload-path contract."""
    try:
        _PAYLOAD_PATH.validate_python(path, strict=True)
        path.encode("utf-8")
    except (ValidationError, UnicodeEncodeError) as exc:
        raise BundleIntegrityError(
            "invalid_path", "invalid normalized Bundle payload path", path=path
        ) from exc
    return path


def _validate_limits(max_bytes: int, max_depth: int) -> None:
    if type(max_bytes) is not int or max_bytes <= 0:
        raise ValueError("JSON byte budget must be a positive integer")
    if type(max_depth) is not int or not 0 <= max_depth <= MAX_JSON_DEPTH:
        raise ValueError(f"JSON depth budget must be between 0 and {MAX_JSON_DEPTH}")


def _check_bytes(size: int, max_bytes: int, path: str | None) -> None:
    if size > max_bytes:
        raise BundleIntegrityError(
            "json_size_limit", "JSON exceeds the byte budget", path=path
        )


def _check_lexical_depth(source: str, max_depth: int, path: str | None) -> None:
    # Scan only syntax with nesting significance before entering the recursive
    # decoder. The JSON decoder validates syntax, including mismatched brackets.
    in_string = False
    escaped_position = -1
    depth = 0
    for match in _JSON_SPECIAL.finditer(source):
        if match.start() == escaped_position:
            continue
        token = match.group()
        if token == '"':
            in_string = not in_string
        elif in_string:
            if token == "\\":
                escaped_position = match.start() + 1
        elif token in "[{":
            depth += 1
            if depth > max_depth:
                raise BundleIntegrityError(
                    "json_depth_limit", "JSON exceeds the nesting budget", path=path
                )
        else:
            depth -= 1
            if depth < 0:
                raise BundleIntegrityError(
                    "invalid_json", "unexpected JSON closing delimiter", path=path
                )


def _check_text(text: str, path: str | None, location: JSONLocation) -> None:
    try:
        text.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise BundleIntegrityError(
            "invalid_unicode",
            "invalid Unicode scalar",
            path=path,
            json_location=location,
        ) from exc


def _invalid_number(path: str | None, location: JSONLocation) -> BundleIntegrityError:
    return BundleIntegrityError(
        "invalid_number",
        "number is outside the Bundle JCS number domain",
        path=path,
        json_location=location,
    )


def _canonical_float(value: float, path: str | None, location: JSONLocation) -> bytes:
    if not math.isfinite(value):
        raise _invalid_number(path, location)
    canonical = rfc8785.dumps(value)
    # Fixed-point floats rendered as whole numbers must survive the same
    # integer check on the independently re-read canonical JSON.
    if (
        not any(marker in canonical for marker in (b".", b"e"))
        and abs(value) > _MAX_INTEGER
    ):
        raise _invalid_number(path, location)
    return canonical


def _decode_number(token: str, path: str | None, location: JSONLocation) -> int | float:
    if not any(marker in token for marker in ".eE") and token not in (
        "NaN",
        "Infinity",
        "-Infinity",
    ):
        digits = token.removeprefix("-")
        if len(digits) > len(str(_MAX_INTEGER)):
            raise _invalid_number(path, location)
        integer = int(token)
        if abs(integer) > _MAX_INTEGER:
            raise _invalid_number(path, location)
        return integer
    number = float(token)
    if number == 0 and any(
        char in "123456789" for char in token.lower().partition("e")[0]
    ):
        raise _invalid_number(path, location)
    _canonical_float(number, path, location)
    return number


def _decode_values(
    value: object, path: str | None, location: JSONLocation
) -> JSONValue:
    if isinstance(value, _Number):
        return _decode_number(value.token, path, location)
    if isinstance(value, _ObjectPairs):
        result: dict[str, JSONValue] = {}
        for key, member in value.pairs:
            child = (*location, key)
            _check_text(key, path, child)
            if key in result:
                raise BundleIntegrityError(
                    "duplicate_json_key",
                    "duplicate JSON object member",
                    path=path,
                    json_location=child,
                )
            result[key] = _decode_values(member, path, child)
        return result
    if isinstance(value, list):
        return [
            _decode_values(item, path, (*location, index))
            for index, item in enumerate(value)
        ]
    if isinstance(value, str):
        _check_text(value, path, location)
    return cast("None | bool | str", value)


def _parse_json(
    raw: bytes, max_bytes: int, max_depth: int, path: str | None
) -> JSONValue:
    _validate_limits(max_bytes, max_depth)
    _check_bytes(len(raw), max_bytes, path)
    try:
        source = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise BundleIntegrityError(
            "invalid_unicode", "JSON must be UTF-8", path=path
        ) from exc
    _check_lexical_depth(source, max_depth, path)
    try:
        parsed = json.loads(
            source,
            object_pairs_hook=_ObjectPairs,
            parse_int=_Number,
            parse_float=_Number,
            parse_constant=_Number,
        )
    except json.JSONDecodeError as exc:
        raise BundleIntegrityError(
            "invalid_json", "invalid JSON syntax", path=path
        ) from exc
    return _decode_values(parsed, path, ())


def _render(value: JSONValue, max_bytes: int, path: str | None) -> bytes:
    try:
        result = rfc8785.dumps(value)
    except (rfc8785.CanonicalizationError, UnicodeEncodeError) as exc:
        raise BundleIntegrityError(
            "canonicalization_failed", "JCS rejected JSON", path=path
        ) from exc
    _check_bytes(len(result), max_bytes, path)
    return result


def canonicalize_json(
    raw: bytes, *, max_bytes: int, max_depth: int, path: str | None = None
) -> bytes:
    """Decode bounded JSON source bytes and emit JCS for compilation."""
    return _render(_parse_json(raw, max_bytes, max_depth, path), max_bytes, path)


def read_canonical_json(
    raw: bytes, *, max_bytes: int, max_depth: int, path: str | None = None
) -> JSONValue:
    """Require canonical stored bytes and return their exact parsed JSON value."""
    value = _parse_json(raw, max_bytes, max_depth, path)
    if _render(value, max_bytes, path) != raw:
        raise BundleIntegrityError(
            "noncanonical_json", "stored JSON must use JCS", path=path
        )
    return value


@dataclass
class _ValueBudget:
    remaining: int
    max_depth: int
    path: str | None

    def consume(self, minimum_size: int) -> None:
        self.remaining -= minimum_size
        if self.remaining < 0:
            raise BundleIntegrityError(
                "json_size_limit", "JSON exceeds the byte budget", path=self.path
            )

    def visit(self, value: object, depth: int, location: JSONLocation) -> None:
        if isinstance(value, (dict, list)):
            if depth >= self.max_depth:
                raise BundleIntegrityError(
                    "json_depth_limit",
                    "JSON exceeds the nesting budget",
                    path=self.path,
                    json_location=location,
                )
            self.consume(2 + max(0, len(value) - 1))
            if isinstance(value, dict):
                for key, member in value.items():
                    if type(key) is not str:
                        raise BundleIntegrityError(
                            "invalid_json_type",
                            "JSON object keys must be strings",
                            path=self.path,
                            json_location=location,
                        )
                    child = (*location, key)
                    self.consume(1)
                    self.visit(key, depth + 1, child)
                    self.visit(member, depth + 1, child)
            else:
                for index, item in enumerate(value):
                    self.visit(item, depth + 1, (*location, index))
        elif type(value) is str:
            self.consume(len(value) + 2)
            _check_text(value, self.path, location)
        elif type(value) is int:
            if abs(value) > _MAX_INTEGER:
                raise _invalid_number(self.path, location)
            self.consume(len(str(value)))
        elif type(value) is float:
            self.consume(len(_canonical_float(value, self.path, location)))
        elif value is None or type(value) is bool:
            self.consume(5 if value is False else 4)
        else:
            raise BundleIntegrityError(
                "invalid_json_type",
                "unsupported JSON value type",
                path=self.path,
                json_location=location,
            )


def canonical_json(
    value: JSONValue, *, max_bytes: int, max_depth: int, path: str | None = None
) -> bytes:
    """Emit JCS from a bounded JSON value; root containers have depth one."""
    _validate_limits(max_bytes, max_depth)
    _ValueBudget(max_bytes, max_depth, path).visit(value, 0, ())
    return _render(value, max_bytes, path)


def resolve_json_pointer(
    value: JSONValue, pointer: str, *, path: str | None = None
) -> JSONValue:
    """Resolve an RFC 6901 string pointer, with strict array-index syntax."""
    try:
        _POINTER.validate_python(pointer, strict=True)
    except ValidationError as exc:
        raise BundleIntegrityError(
            "invalid_pointer", "invalid JSON Pointer syntax", path=path
        ) from exc
    _check_text(pointer, path, ())
    if pointer == "":
        return value

    location: JSONLocation = ()
    for part in pointer[1:].split("/"):
        token = part.replace("~1", "/").replace("~0", "~")
        if isinstance(value, list) and _ARRAY_INDEX.fullmatch(token):
            # Compare length before conversion, independent of Python's limit
            # on decimal-to-int conversion.
            if len(token) <= len(str(len(value))):
                index = int(token)
                if index < len(value):
                    value = value[index]
                    location = (*location, index)
                    continue
        elif isinstance(value, dict) and token in value:
            value = value[token]
            location = (*location, token)
            continue
        raise BundleIntegrityError(
            "unresolved_pointer",
            "JSON Pointer does not identify a value",
            path=path,
            json_location=(*location, token),
        )
    return value


def sha256_digest(raw: bytes) -> str:
    return sha256(raw).hexdigest()


def _is_json(path: str) -> bool:
    # Static asset descriptors pin data bytes exactly even if their name uses a
    # JSON extension. The descriptors themselves remain canonical JSON.
    return path.endswith(".json") and not path.startswith("resources/assets/data/")


def artifact_digest(
    raw: bytes,
    *,
    path: str,
    json_pointer: str | None = None,
    max_bytes: int,
    max_depth: int,
) -> str:
    """Hash the selected canonical JSON value or exact binary resource bytes."""
    validate_payload_path(path)
    _validate_limits(max_bytes, max_depth)
    _check_bytes(len(raw), max_bytes, path)
    if _is_json(path):
        parsed = read_canonical_json(
            raw, max_bytes=max_bytes, max_depth=max_depth, path=path
        )
        if json_pointer:
            value = resolve_json_pointer(parsed, json_pointer, path=path)
            raw = canonical_json(
                value, max_bytes=max_bytes, max_depth=max_depth, path=path
            )
    elif json_pointer is not None:
        raise BundleIntegrityError(
            "invalid_pointer", "binary payload cannot have a JSON Pointer", path=path
        )
    return sha256_digest(raw)


def build_checksums(payloads: Mapping[str, bytes]) -> ChecksumManifest:
    """Build the sorted checksum inventory from the exact stored payload bytes."""
    entries = []
    try:
        for path, contents in sorted(payloads.items()):
            validate_payload_path(path)
            entries.append(
                ChecksumEntry(
                    path=path, size=len(contents), sha256=sha256_digest(contents)
                )
            )
        return ChecksumManifest(schema_version="1.0.0", entries=tuple(entries))
    except ValidationError as exc:
        raise BundleIntegrityError(
            "invalid_checksums",
            "payload inventory exceeds the checksum contract",
            path=CHECKSUMS_PATH,
        ) from exc


def compute_root_digest(
    manifest: dict[str, JSONValue],
    checksums: ChecksumManifest,
    *,
    max_bytes: int,
    max_depth: int,
) -> str:
    """Hash the actual manifest value using only the two §4 exclusions."""
    integrity = manifest.get("integrity")
    if not isinstance(integrity, dict):
        raise BundleIntegrityError(
            "invalid_manifest",
            "manifest requires an integrity object",
            path=MANIFEST_PATH,
        )
    covered_integrity = {
        key: value
        for key, value in integrity.items()
        if key not in ("root_digest", "signatures")
    }
    covered: dict[str, JSONValue] = {**manifest, "integrity": covered_integrity}
    manifest_bytes = canonical_json(
        covered, max_bytes=max_bytes, max_depth=max_depth, path=MANIFEST_PATH
    )
    checksum_bytes = canonical_json(
        checksums.model_dump(mode="json"),
        max_bytes=max_bytes,
        max_depth=max_depth,
        path=CHECKSUMS_PATH,
    )
    digest = sha256(_ROOT_DOMAIN)
    for raw in (manifest_bytes, checksum_bytes):
        digest.update(len(raw).to_bytes(8, "big"))
        digest.update(raw)
    return digest.hexdigest()


def verify_bundle_files(
    files: Mapping[str, bytes], *, max_file_bytes: int, max_json_depth: int
) -> VerifiedBundleFiles:
    """Verify package bytes and artifact digests before closure and trust checks."""
    _validate_limits(max_file_bytes, max_json_depth)
    parsed: dict[str, JSONValue] = {}
    payloads: dict[str, bytes] = {}
    for path, raw in files.items():
        if path not in METADATA_PATHS:
            validate_payload_path(path)
            payloads[path] = raw
        if len(raw) > max_file_bytes:
            raise BundleIntegrityError(
                "file_size_limit", "member exceeds the file byte budget", path=path
            )
        if _is_json(path):
            parsed[path] = read_canonical_json(
                raw, max_bytes=max_file_bytes, max_depth=max_json_depth, path=path
            )

    manifest_json = parsed.get(MANIFEST_PATH)
    if not isinstance(manifest_json, dict):
        raise BundleIntegrityError(
            "invalid_manifest", "manifest object is required", path=MANIFEST_PATH
        )
    try:
        manifest = BundleManifest.model_validate_json(files[MANIFEST_PATH])
    except ValidationError as exc:
        raise BundleIntegrityError(
            "invalid_manifest",
            "manifest failed the Bundle contract",
            path=MANIFEST_PATH,
            json_location=exc.errors(include_input=False)[0]["loc"],
        ) from exc
    try:
        checksums = ChecksumManifest.model_validate_json(
            files.get(CHECKSUMS_PATH, b"null")
        )
    except ValidationError as exc:
        raise BundleIntegrityError(
            "invalid_checksums",
            "checksums failed the Bundle contract",
            path=CHECKSUMS_PATH,
            json_location=exc.errors(include_input=False)[0]["loc"],
        ) from exc
    actual_checksums = build_checksums(payloads)
    actual_paths = {item.path for item in actual_checksums.entries}
    declared_paths = {item.path for item in checksums.entries}
    if actual_paths != declared_paths:
        path = min(actual_paths ^ declared_paths)
        raise BundleIntegrityError(
            "checksum_path_mismatch",
            "checksum inventory differs from stored payloads",
            path=path,
        )
    for actual, expected in zip(
        actual_checksums.entries, checksums.entries, strict=True
    ):
        if actual != expected:
            raise BundleIntegrityError(
                "checksum_mismatch",
                "stored payload size or hash differs from checksum",
                path=actual.path,
            )

    root = compute_root_digest(
        manifest_json,
        actual_checksums,
        max_bytes=max_file_bytes,
        max_depth=max_json_depth,
    )
    if manifest.integrity.root_digest != root:
        raise BundleIntegrityError(
            "root_digest_mismatch",
            "manifest root digest differs from computed root",
            path=MANIFEST_PATH,
        )
    full_digests = {item.path: item.sha256 for item in actual_checksums.entries}
    pointers_by_path: dict[str, set[str]] = {}
    for item in manifest.artifacts:
        if item.json_pointer:
            pointers_by_path.setdefault(item.path, set()).add(item.json_pointer)
    for path, pointers in pointers_by_path.items():
        for pointer in sorted(pointers):
            if any(
                pointer[: match.start()] in pointers
                for match in re.finditer("/", pointer)
            ):
                raise BundleIntegrityError(
                    "overlapping_artifact_pointers",
                    "artifact locators must designate distinct JSON members",
                    path=path,
                )
    for index, item in enumerate(manifest.artifacts):
        path = item.path
        if path not in full_digests:
            raise BundleIntegrityError(
                "missing_artifact_payload",
                "artifact locator has no stored payload",
                path=path,
                json_location=("artifacts", index),
            )
        expected_digest = full_digests[path]
        if item.json_pointer is not None and not _is_json(path):
            raise BundleIntegrityError(
                "invalid_pointer",
                "binary payload cannot have a JSON Pointer",
                path=path,
            )
        if item.json_pointer:
            value = resolve_json_pointer(parsed[path], item.json_pointer, path=path)
            expected_digest = sha256_digest(
                canonical_json(
                    value, max_bytes=max_file_bytes, max_depth=max_json_depth, path=path
                )
            )
        if item.content_ref.digest != expected_digest:
            raise BundleIntegrityError(
                "artifact_digest_mismatch",
                "artifact hash differs from its inventory ContentRef",
                path=path,
                json_location=("artifacts", index, "content_ref", "digest"),
            )
    return VerifiedBundleFiles(manifest, actual_checksums, MappingProxyType(payloads))
