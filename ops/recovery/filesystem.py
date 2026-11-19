"""Private paths, frozen SQLite ledgers and media copy verification."""

import os
import re
import sqlite3
import stat
from collections.abc import Iterator
from contextlib import ExitStack, closing, contextmanager
from fcntl import LOCK_EX, LOCK_NB, flock
from hashlib import sha256
from pathlib import Path
from typing import BinaryIO

from pydantic import ValidationError

from jimu_runtime.bundle.store import ReleaseLedger, StoreError
from jimu_runtime.domain.runtime.releases import ReleaseRecord
from ops.recovery.contracts import (
    BUNDLE,
    DUMP,
    IDENTITY,
    MEDIA,
    RESOURCES,
    FileWitness,
    RecoveryError,
)

MAX_MANIFEST = 16 * 1024 * 1024
_MEDIA_NAME = re.compile(r"^[a-f0-9]{64}(?:\.stage)?$")


def private_path(
    path: Path, *, directory: bool = False, parent_protected: bool = False
) -> Path:
    path = path.absolute()
    try:
        metadata = path.lstat()
        real = path.resolve(strict=True)
    except OSError:
        raise RecoveryError("private_path_unavailable") from None
    kind = stat.S_ISDIR if directory else stat.S_ISREG
    if (
        real != path
        or not kind(metadata.st_mode)
        or metadata.st_uid != os.geteuid()
        or (not parent_protected and metadata.st_mode & 0o077)
        or not directory
        and metadata.st_nlink != 1
    ):
        raise RecoveryError("unsafe_private_path")
    return path


def trusted_ancestors(path: Path) -> None:
    path = path.absolute()
    if path.resolve(strict=True) != path:
        raise RecoveryError("unsafe_path_ancestor")
    owner = os.geteuid()
    for parent in (path, *path.parents):
        metadata = parent.lstat()
        if not stat.S_ISDIR(metadata.st_mode) or metadata.st_uid not in {owner, 0}:
            raise RecoveryError("unsafe_path_ancestor")
        if metadata.st_mode & 0o022 and not metadata.st_mode & stat.S_ISVTX:
            raise RecoveryError("unsafe_path_ancestor")


def trusted_executable(path: Path) -> Path:
    path = path.absolute()
    trusted_ancestors(path.parent)
    data = path.lstat()
    if (
        not stat.S_ISREG(data.st_mode)
        or data.st_uid not in {0, os.geteuid()}
        or data.st_mode & 0o022
    ):
        raise RecoveryError("untrusted_postgres_executable")
    return path


def create_directory(path: Path) -> Path:
    path = path.absolute()
    try:
        trusted_ancestors(path.parent)
        path.mkdir(mode=0o700)
    except OSError:
        raise RecoveryError("output_directory_must_be_new") from None
    return private_path(path, directory=True)


@contextmanager
def read_private(path: Path, *, parent_protected: bool = False) -> Iterator[BinaryIO]:
    if parent_protected:
        private_path(path.parent, directory=True)
    path = private_path(path, parent_protected=parent_protected)
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(descriptor, "rb") as content:
        state = os.fstat(content.fileno())
        if (
            not stat.S_ISREG(state.st_mode)
            or state.st_nlink != 1
            or state.st_uid != os.geteuid()
            or (not parent_protected and state.st_mode & 0o077)
        ):
            raise RecoveryError("unsafe_open_file")
        yield content


@contextmanager
def new_private(path: Path) -> Iterator[BinaryIO]:
    descriptor = os.open(
        path, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600
    )
    with os.fdopen(descriptor, "wb") as content:
        yield content
        content.flush()
        os.fsync(content.fileno())


def read_manifest(path: Path) -> bytes:
    with read_private(path) as content:
        raw = content.read(MAX_MANIFEST + 1)
    if not 0 < len(raw) <= MAX_MANIFEST:
        raise RecoveryError("manifest_size_invalid")
    return raw


def stream_witness(
    content: BinaryIO,
    destination: Path | None = None,
    *,
    readonly: bool = False,
    expected_size: int | None = None,
) -> FileWitness:
    if (
        expected_size is not None
        and os.fstat(content.fileno()).st_size != expected_size
    ):
        raise RecoveryError("backup_file_digest_mismatch")
    digest = sha256()
    count = 0
    with ExitStack() as context:
        output = (
            None
            if destination is None
            else context.enter_context(new_private(destination))
        )
        for chunk in iter(lambda: content.read(1024 * 1024), b""):
            count += len(chunk)
            if expected_size is not None and count > expected_size:
                raise RecoveryError("backup_file_digest_mismatch")
            digest.update(chunk)
            if output is not None:
                output.write(chunk)
        if output is not None and readonly:
            os.fchmod(output.fileno(), 0o400)
        if expected_size is not None and count != expected_size:
            raise RecoveryError("backup_file_digest_mismatch")
        if os.fstat(content.fileno()).st_size != count:
            raise RecoveryError("file_changed_during_copy")
    return FileWitness(sha256=digest.hexdigest(), size=count)


def file_witness(
    source: Path,
    destination: Path | None = None,
    *,
    parent_protected: bool = False,
    readonly: bool = False,
    expected_size: int | None = None,
) -> FileWitness:
    with read_private(source, parent_protected=parent_protected) as content:
        return stream_witness(
            content,
            destination,
            readonly=readonly,
            expected_size=expected_size,
        )


def publish_marker(path: Path, content: bytes) -> None:
    """Publish a complete marker without replacing a visible file."""
    staged = path.with_name(f".{path.name}.pending")
    with new_private(staged) as output:
        output.write(content)
    os.link(staged, path, follow_symlinks=False)
    staged.unlink()


def sync_directory(directory: Path) -> None:
    descriptor = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def media_names(root: Path, *, source: bool = False) -> set[str]:
    media = private_path(root / MEDIA, directory=True)
    entries: set[str] = set()
    for child in media.iterdir():
        if child.name == ".locks":
            locks = private_path(child, directory=True)
            for path in locks.iterdir():
                if not re.fullmatch(r"[a-f0-9]{2}", path.name):
                    raise RecoveryError("unexpected_media_lock_file")
                private_path(path)
        elif child.name == ".database" or _MEDIA_NAME.fullmatch(child.name):
            private_path(child, parent_protected=source and child.name == ".database")
            entries.add(f"{MEDIA}/{child.name}")
        else:
            raise RecoveryError("unexpected_media_file")
    if IDENTITY not in entries:
        raise RecoveryError("media_identity_missing")
    return entries


def validate_package_names(paths: set[str]) -> None:
    fixed = {BUNDLE, DUMP, RESOURCES, IDENTITY}
    if not fixed <= paths or any(
        not item.startswith(f"{MEDIA}/")
        or not _MEDIA_NAME.fullmatch(item.removeprefix(f"{MEDIA}/"))
        for item in paths - fixed
    ):
        raise RecoveryError("manifest_contains_unsupported_paths")


def prepare_media_locks(root: Path) -> None:
    """Provision the workflow store's fixed lock names as a separate host action."""
    directory = private_path(root / MEDIA / ".locks", directory=True)
    trusted_ancestors(directory)
    for index in range(256):
        path = directory / f"{index:02x}"
        try:
            with new_private(path):
                pass
        except FileExistsError:
            private_path(path)
    sync_directory(directory)


@contextmanager
def freeze_media(root: Path) -> Iterator[None]:
    directory = private_path(root / MEDIA / ".locks", directory=True)
    with ExitStack() as locks:
        for index in range(256):
            path = directory / f"{index:02x}"
            if not os.path.lexists(path):
                raise RecoveryError("media_lock_shards_not_provisioned")
            try:
                content = locks.enter_context(read_private(path))
                flock(content.fileno(), LOCK_EX | LOCK_NB)
            except OSError:
                raise RecoveryError("media_writer_not_quiesced") from None
        yield


def require_clean_sqlite(path: Path) -> None:
    if any(
        os.path.lexists(str(path) + suffix) for suffix in ("-journal", "-wal", "-shm")
    ):
        raise RecoveryError("sqlite_recovery_required")


@contextmanager
def ledger(path: Path, *, freeze: bool) -> Iterator[sqlite3.Connection]:
    path = private_path(path)
    require_clean_sqlite(path)
    try:
        with closing(
            sqlite3.connect(
                path.as_uri() + "?mode=ro",
                uri=True,
                timeout=0,
                isolation_level=None,
            )
        ) as connection:
            if connection.execute("PRAGMA journal_mode").fetchone()[0] != "delete":
                raise RecoveryError("unsupported_sqlite_journal")
            connection.execute("BEGIN")
            if (
                connection.execute("PRAGMA integrity_check").fetchall() != [("ok",)]
                or connection.execute("PRAGMA foreign_key_check").fetchone() is not None
            ):
                raise RecoveryError("sqlite_integrity_failed")
            require_clean_sqlite(path)
            yield connection
            if freeze:
                require_clean_sqlite(path)
    except sqlite3.Error:
        raise RecoveryError("sqlite_ledger_unavailable_or_busy") from None


def ledger_versions(
    bundle: sqlite3.Connection,
    resources: sqlite3.Connection,
    *,
    quiesced: bool,
) -> tuple[int, int, str, str]:
    bundle_version = bundle.execute("PRAGMA user_version").fetchone()[0]
    tables = {
        row[0]
        for row in bundle.execute("SELECT name FROM sqlite_schema WHERE type='table'")
    }
    if (
        bundle_version != 1
        or not {"archives", "bundle_identities", "release_ledger"} <= tables
    ):
        raise RecoveryError("invalid_bundle_ledger")
    rows = resources.execute(
        "SELECT schema_version, inventory_id, fingerprint FROM inventory WHERE singleton=1"
    ).fetchall()
    if (
        len(rows) != 1
        or rows[0][0] != 4
        or not rows[0][1]
        or not re.fullmatch(r"[a-f0-9]{64}", rows[0][2])
    ):
        raise RecoveryError("invalid_resource_ledger")
    if quiesced:
        if (
            resources.execute(
                "SELECT 1 FROM leases WHERE pid IS NOT NULL AND tree_reaped = 0 LIMIT 1"
            ).fetchone()
            is not None
        ):
            raise RecoveryError("worker_process_tree_not_reaped")
    version, inventory, fingerprint = rows[0]
    return bundle_version, version, inventory, fingerprint


def verify_bundle_pins(
    connection: sqlite3.Connection, pins: tuple[tuple[str, str], ...]
) -> None:
    releases = ReleaseLedger(connection)
    for release_id, bundle_digest in pins:
        try:
            raw = releases.get("release", release_id)
            release = None if raw is None else ReleaseRecord.model_validate_json(raw)
            if (
                release is None
                or release.release_id != release_id
                or release.bundle_ref.bundle_digest != bundle_digest
                or release.admission_receipt is None
            ):
                raise RecoveryError("postgres_release_or_bundle_missing_from_ledger")
            entry = releases.get_receipt(release.admission_receipt.receipt_id)
            if (
                entry is None
                or not entry.accepted
                or entry.receipt != release.admission_receipt
            ):
                raise RecoveryError("postgres_release_bundle_archive_not_verified")
        except (StoreError, ValidationError):
            raise RecoveryError(
                "postgres_release_bundle_archive_not_verified"
            ) from None
