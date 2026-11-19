"""Bounded in-memory ZIP transport. Member names are never filesystem paths."""

import io
import stat
import struct
import zipfile
import zlib
from collections.abc import Buffer, Iterator, Mapping
from dataclasses import dataclass

from jimu_runtime.bundle.integrity import (
    MAX_JSON_DEPTH,
    METADATA_PATHS,
    BundleIntegrityError,
    VerifiedBundleFiles,
    validate_payload_path,
    verify_bundle_files,
)

_END = struct.Struct("<4s4H2IH")
_DIRECTORY = struct.Struct("<4s6H3I5H2I")
_LOCAL = struct.Struct("<4s5H3I2H")
_DESCRIPTOR = struct.Struct("<3I")
_END_SIGNATURE = b"PK\x05\x06"
_DIRECTORY_SIGNATURE = b"PK\x01\x02"
_LOCAL_SIGNATURE = b"PK\x03\x04"
_DESCRIPTOR_SIGNATURE = b"PK\x07\x08"
_DATA_DESCRIPTOR = 0x0008
_UTF8 = 0x0800
_DEFLATE_OPTIONS = 0x0006
_CHUNK_SIZE = 64 * 1024


@dataclass(frozen=True, kw_only=True)
class BundleLimits:
    """Explicit deployment budgets for one ZIP and its JSON files."""

    max_archive_bytes: int
    max_expanded_bytes: int
    max_file_bytes: int
    max_files: int
    max_json_depth: int

    def __post_init__(self) -> None:
        byte_and_file_budgets = (
            self.max_archive_bytes,
            self.max_expanded_bytes,
            self.max_file_bytes,
            self.max_files,
        )
        if any(
            type(budget) is not int or budget <= 0 for budget in byte_and_file_budgets
        ):
            raise ValueError("archive byte and file budgets must be positive integers")
        if (
            type(self.max_json_depth) is not int
            or not 0 <= self.max_json_depth <= MAX_JSON_DEPTH
        ):
            raise ValueError(
                f"JSON depth budget must be between 0 and {MAX_JSON_DEPTH}"
            )


@dataclass(frozen=True)
class _Member:
    name: str
    name_bytes: bytes
    flags: int
    method: int
    crc: int
    compressed_size: int
    size: int
    offset: int


def _invalid_archive(message: str, path: str | None = None) -> BundleIntegrityError:
    return BundleIntegrityError("invalid_archive", message, path=path)


def _limit(code: str, path: str | None = None) -> BundleIntegrityError:
    return BundleIntegrityError(code, "ZIP exceeds the configured budget", path=path)


def _check_archive_bytes(size: int, limits: BundleLimits) -> None:
    if size > limits.max_archive_bytes:
        raise _limit("archive_size_limit")


def _check_name(name: str) -> str:
    return name if name in METADATA_PATHS else validate_payload_path(name)


def _directory_location(raw: bytes, limits: BundleLimits) -> tuple[int, int, int]:
    # A ZIP32 archive can have a trailing comment of at most 65,535 bytes.
    # Check candidates for exact framing so a signature inside a comment
    # cannot make the true end-of-directory record invisible.
    search_floor = max(0, len(raw) - _END.size - 0xFFFF)
    stop = max(0, len(raw) - _END.size + len(_END_SIGNATURE))
    while (position := raw.rfind(_END_SIGNATURE, search_floor, stop)) >= 0:
        _, disk, directory_disk, disk_count, count, size, offset, comment = (
            _END.unpack_from(raw, position)
        )
        if position + _END.size + comment == len(raw) and offset + size == position:
            if (
                disk != 0
                or directory_disk != 0
                or disk_count != count
                or count == 0xFFFF
            ):
                raise _invalid_archive("only single-disk ZIP32 archives are supported")
            if count > limits.max_files:
                raise _limit("archive_file_count_limit")
            return offset, position, count
        stop = position
    raise _invalid_archive("missing or inconsistent ZIP central directory")


def _members(
    raw: bytes, start: int, end: int, count: int, limits: BundleLimits
) -> Iterator[_Member]:
    position = start
    seen: set[str] = set()
    while position < end:
        if len(seen) >= limits.max_files:
            raise _limit("archive_file_count_limit")
        if position + _DIRECTORY.size > end:
            raise _invalid_archive("truncated ZIP central-directory header")
        (
            signature,
            _,
            _,
            flags,
            method,
            _,
            _,
            crc,
            compressed_size,
            size,
            name_size,
            extra_size,
            comment_size,
            disk,
            _,
            attributes,
            offset,
        ) = _DIRECTORY.unpack_from(raw, position)
        name_start = position + _DIRECTORY.size
        name_end = name_start + name_size
        position = name_end + extra_size + comment_size
        if signature != _DIRECTORY_SIGNATURE or position > end:
            raise _invalid_archive("invalid ZIP central-directory entry")
        if disk or 0xFFFFFFFF in (compressed_size, size, offset):
            raise _invalid_archive("only single-disk ZIP32 archives are supported")
        name_bytes = raw[name_start:name_end]
        try:
            name = _check_name(name_bytes.decode("utf-8" if flags & _UTF8 else "cp437"))
        except UnicodeDecodeError as exc:
            raise _invalid_archive("invalid ZIP member name encoding") from exc
        if name in seen:
            raise BundleIntegrityError(
                "duplicate_zip_member", "duplicate ZIP member name", path=name
            )
        seen.add(name)
        if stat.S_IFMT(attributes >> 16) not in (0, stat.S_IFREG) or attributes & 0x10:
            raise _invalid_archive("ZIP member must be a regular file", name)
        if method not in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED):
            raise _invalid_archive("unsupported ZIP compression method", name)
        permitted = _UTF8 | _DATA_DESCRIPTOR
        if method == zipfile.ZIP_DEFLATED:
            permitted |= _DEFLATE_OPTIONS
        if flags & ~permitted:
            raise _invalid_archive("unsupported ZIP member flags", name)

        yield _Member(
            name, name_bytes, flags, method, crc, compressed_size, size, offset
        )
    if len(seen) != count:
        raise _invalid_archive("ZIP member count disagrees with the central directory")


def _compressed_span(raw: bytes, member: _Member, end: int) -> memoryview:
    if member.offset + _LOCAL.size > end:
        raise _invalid_archive("truncated or overlapping ZIP local header", member.name)
    (
        signature,
        _,
        flags,
        method,
        _,
        _,
        crc,
        compressed_size,
        size,
        name_size,
        extra_size,
    ) = _LOCAL.unpack_from(raw, member.offset)
    name_start = member.offset + _LOCAL.size
    name_end = name_start + name_size
    data_start = name_end + extra_size
    data_end = data_start + member.compressed_size
    if (
        signature != _LOCAL_SIGNATURE
        or flags != member.flags
        or method != member.method
        or data_end > end
        or raw[name_start:name_end] != member.name_bytes
    ):
        raise _invalid_archive(
            "ZIP local header disagrees with central directory", member.name
        )
    expected = member.crc, member.compressed_size, member.size
    local = crc, compressed_size, size
    if flags & _DATA_DESCRIPTOR:
        for local_value, expected_value in zip(local, expected, strict=True):
            if local_value not in (0, expected_value):
                raise _invalid_archive(
                    "inconsistent ZIP local size or CRC", member.name
                )
        descriptor_start = data_end
        if end - data_end == len(_DESCRIPTOR_SIGNATURE) + _DESCRIPTOR.size:
            if (
                raw[data_end : data_end + len(_DESCRIPTOR_SIGNATURE)]
                != _DESCRIPTOR_SIGNATURE
            ):
                raise _invalid_archive("invalid ZIP data descriptor", member.name)
            descriptor_start += len(_DESCRIPTOR_SIGNATURE)
        if (
            end - descriptor_start != _DESCRIPTOR.size
            or _DESCRIPTOR.unpack_from(raw, descriptor_start) != expected
        ):
            raise _invalid_archive(
                "ZIP data descriptor disagrees with directory", member.name
            )
    elif data_end != end or local != expected:
        raise _invalid_archive("inconsistent ZIP data extent, size or CRC", member.name)
    return memoryview(raw)[data_start:data_end]


def _check_expanded_size(
    size: int, expanded: int, limits: BundleLimits, path: str
) -> None:
    if size > limits.max_file_bytes:
        raise _limit("file_size_limit", path)
    if expanded + size > limits.max_expanded_bytes:
        raise _limit("expanded_size_limit", path)


def _decompress(
    compressed: memoryview, member: _Member, expanded: int, limits: BundleLimits
) -> bytes:
    _check_expanded_size(member.size, expanded, limits, member.name)
    if member.method == zipfile.ZIP_STORED:
        _check_expanded_size(len(compressed), expanded, limits, member.name)
        contents = bytes(compressed)
    else:
        decoder = zlib.decompressobj(-zlib.MAX_WBITS)
        output = bytearray()
        try:
            for offset in range(0, len(compressed), _CHUNK_SIZE):
                stop = min(offset + _CHUNK_SIZE, len(compressed))
                pending: bytes | memoryview = compressed[offset:stop]
                while True:
                    remaining = min(
                        limits.max_file_bytes, limits.max_expanded_bytes - expanded
                    ) - len(output)
                    # Limit actual expansion, including the first byte over
                    # budget. Never use the member's claimed size as a cap.
                    chunk = decoder.decompress(pending, min(_CHUNK_SIZE, remaining + 1))
                    _check_expanded_size(
                        len(output) + len(chunk), expanded, limits, member.name
                    )
                    output.extend(chunk)
                    pending = decoder.unconsumed_tail
                    if decoder.eof:
                        if decoder.unused_data or stop < len(compressed):
                            raise _invalid_archive(
                                "trailing bytes in ZIP DEFLATE member", member.name
                            )
                        break
                    # After the final compressed input, zlib can have pending
                    # expansion even if it has consumed every input byte.
                    if not pending and (stop < len(compressed) or not chunk):
                        break
        except zlib.error as exc:
            raise _invalid_archive("invalid ZIP DEFLATE stream", member.name) from exc
        if not decoder.eof:
            raise _invalid_archive("truncated ZIP DEFLATE stream", member.name)
        contents = bytes(output)
    if len(contents) != member.size or zlib.crc32(contents) != member.crc:
        raise _invalid_archive(
            "ZIP member actual size or CRC disagrees with metadata", member.name
        )
    return contents


def read_archive(raw: bytes, limits: BundleLimits) -> dict[str, bytes]:
    """Read regular single-disk ZIP32 members under actual byte budgets."""
    _check_archive_bytes(len(raw), limits)
    start, end, count = _directory_location(raw, limits)
    members = sorted(
        _members(raw, start, end, count, limits), key=lambda member: member.offset
    )
    if (members[0].offset if members else start) != 0:
        raise _invalid_archive("unexpected bytes before the first ZIP local header")

    files: dict[str, bytes] = {}
    expanded = 0
    for index, member in enumerate(members):
        next_offset = members[index + 1].offset if index + 1 < len(members) else start
        if next_offset > start:
            raise _invalid_archive(
                "ZIP local header overlaps the central directory", member.name
            )
        compressed = _compressed_span(raw, member, next_offset)
        contents = _decompress(compressed, member, expanded, limits)
        files[member.name] = contents
        expanded += len(contents)
    return files


class _BoundedZipBuffer(io.BytesIO):
    def __init__(self, limits: BundleLimits) -> None:
        super().__init__()
        self.limits = limits

    def write(self, buffer: Buffer, /) -> int:
        _check_archive_bytes(self.tell() + memoryview(buffer).nbytes, self.limits)
        return super().write(buffer)


def write_archive(files: Mapping[str, bytes], limits: BundleLimits) -> bytes:
    """Write deterministic ZIP transport bytes from caller-provided member bytes."""
    if len(files) > limits.max_files:
        raise _limit("archive_file_count_limit")
    if len(files) >= 0xFFFF:
        raise _invalid_archive("only single-disk ZIP32 archives are supported")
    ordered = sorted(files.items())
    expanded = 0
    for name, contents in ordered:
        _check_name(name)
        _check_expanded_size(len(contents), expanded, limits, name)
        expanded += len(contents)
    with _BoundedZipBuffer(limits) as output:
        try:
            with zipfile.ZipFile(output, "w", allowZip64=False) as archive:
                for name, contents in ordered:
                    info = zipfile.ZipInfo(name)
                    info.create_system = 3
                    info.external_attr = (stat.S_IFREG | 0o644) << 16
                    info.compress_type = zipfile.ZIP_DEFLATED
                    info.file_size = len(contents)
                    with archive.open(info, "w") as member:
                        view = memoryview(contents)
                        for start in range(0, len(contents), _CHUNK_SIZE):
                            member.write(view[start : start + _CHUNK_SIZE])
        except zipfile.LargeZipFile as exc:
            raise _invalid_archive(
                "only single-disk ZIP32 archives are supported"
            ) from exc
        return output.getvalue()


def read_bundle(raw: bytes, limits: BundleLimits) -> VerifiedBundleFiles:
    """Independently recompute integrity from final ZIP transport bytes."""
    return verify_bundle_files(
        read_archive(raw, limits),
        max_file_bytes=limits.max_file_bytes,
        max_json_depth=limits.max_json_depth,
    )
