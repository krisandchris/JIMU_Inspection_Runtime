"""Verify and extract a WP-07 product archive against its independently provided seal."""

import argparse
import json
import re
import stat
from contextlib import nullcontext
from hashlib import file_digest, sha256
from pathlib import Path, PurePosixPath
from typing import IO
from zipfile import ZipFile

_PREFIXES = ("src/", "tests/", "scripts/", "schemas/generated/", "examples/", "ops/")
_ROOTS = {"pyproject.toml", "uv.lock"}
_EXECUTABLES = {"ops/compose/build.sh", "ops/compose/local-docker.sh"}
_SHA256 = re.compile(r"[0-9a-f]{64}")


def _hash_file(path: Path) -> str:
    with path.open("rb") as content:
        return file_digest(content, "sha256").hexdigest()


def _copy_and_hash(content: IO[bytes], destination: IO[bytes] | None) -> str:
    digest = sha256()
    while block := content.read(1024 * 1024):
        digest.update(block)
        if destination is not None:
            destination.write(block)
    return digest.hexdigest()


def _name(name: str) -> None:
    path = PurePosixPath(name)
    if (
        not name
        or name.startswith("/")
        or path.as_posix() != name
        or "\\" in name
        or ".." in path.parts
        or any(not part or part.startswith(".") for part in path.parts)
        or (name not in _ROOTS and not name.startswith(_PREFIXES))
    ):
        raise ValueError(f"out-of-scope archive path: {name!r}")


def _manifest_pairs(pairs: list[tuple[str, object]]) -> dict[str, str]:
    result = {}
    for name, digest in pairs:
        _name(name)
        if name in result:
            raise ValueError(f"duplicate manifest path: {name}")
        if not isinstance(digest, str) or _SHA256.fullmatch(digest) is None:
            raise ValueError(f"invalid manifest digest: {name}")
        result[name] = digest
    return result


def verify(
    manifest: Path, pin: str, *, extract: Path | None, compare_tree: Path | None
) -> None:
    raw = manifest.read_bytes()
    if sha256(raw).hexdigest() != pin:
        raise ValueError("WP-07 manifest SHA-256 differs from the supplied seal")
    recorded = json.loads(raw, object_pairs_hook=_manifest_pairs)
    if not isinstance(recorded, dict) or not _ROOTS | _EXECUTABLES <= recorded.keys():
        raise ValueError("manifest omits a required product root")
    if any(
        not any(name.startswith(prefix) for name in recorded) for prefix in _PREFIXES
    ):
        raise ValueError("manifest omits a required product directory")
    archive = manifest.with_name("product.zip")
    archive_digest = _hash_file(archive)
    archive_pin = manifest.with_name("product.zip.sha256")
    if archive_pin.is_file() and archive_digest != archive_pin.read_text().split()[0]:
        raise ValueError("WP-07 ZIP SHA-256 differs from its sidecar seal")
    if extract is not None:
        extract.mkdir(mode=0o700)
    with ZipFile(archive) as zipped:
        names = zipped.namelist()
        if len(names) != len(recorded) or set(names) != set(recorded):
            raise ValueError("ZIP members differ from the sealed product inventory")
        for member in zipped.infolist():
            name = member.filename
            mode = stat.S_IFMT(member.external_attr >> 16)
            if member.is_dir() or mode not in (0, stat.S_IFREG):
                raise ValueError(f"ZIP member is not a regular file: {name}")
            if compare_tree is not None:
                candidate = compare_tree / name
                if (
                    not candidate.resolve().is_relative_to(compare_tree)
                    or candidate.is_symlink()
                    or not candidate.is_file()
                    or _hash_file(candidate) != recorded[name]
                ):
                    raise ValueError(
                        f"isolated tree differs from the sealed file: {name}"
                    )
            target = None if extract is None else extract / name
            if target is not None and extract is not None:
                for parent in reversed(target.parents):
                    if parent == extract or not parent.is_relative_to(extract):
                        continue
                    parent.mkdir(exist_ok=True)
                    parent.chmod(0o755)
            with (
                zipped.open(member) as item,
                nullcontext() if target is None else target.open("xb") as sink,
            ):
                if _copy_and_hash(item, sink) != recorded[name]:
                    raise ValueError(f"archive digest mismatch: {name}")
            if target is not None:
                # Compose bind mounts need non-root read access beneath this
                # private extraction root. Never copy permissions from the ZIP.
                target.chmod(0o755 if name in _EXECUTABLES else 0o644)
    print(
        json.dumps(
            {
                "manifest_sha256": pin,
                "archive_sha256": archive_digest,
                "files": len(recorded),
                "archive_sidecar_verified": archive_pin.is_file(),
                "tree_verified": compare_tree is not None,
                "executables": sorted(_EXECUTABLES),
            },
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("manifest", type=Path)
    parser.add_argument("sha256")
    parser.add_argument("--extract", type=Path)
    parser.add_argument("--compare-tree", type=Path)
    args = parser.parse_args()
    verify(
        args.manifest.resolve(),
        args.sha256,
        extract=args.extract.resolve() if args.extract else None,
        compare_tree=args.compare_tree.resolve() if args.compare_tree else None,
    )
