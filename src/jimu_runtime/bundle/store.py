"""Reopenable WP-02 Bundle ledger for one private local Linux directory.

The deployment owns the directory, the Bundle verifier and the Release
controller. Private write interfaces are for that trusted application code.
SQLite atomically commits bytes and authority metadata in one database.
"""

from __future__ import annotations

import os
import sqlite3
import stat
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from hashlib import sha256
from pathlib import Path
from typing import Annotated, Literal, Self

from pydantic import (
    BeforeValidator,
    Field,
    TypeAdapter,
    ValidationError,
    model_validator,
)

from jimu_runtime.bundle.trust import TrustPin
from jimu_runtime.domain.base import ContractModel
from jimu_runtime.domain.definition_support import UTCDateTime, immutable_sequence
from jimu_runtime.domain.primitives import Digest, RecordId, Version
from jimu_runtime.domain.runtime.authority import ActorRef
from jimu_runtime.domain.runtime.bundle_manifest import DetachedSignatureRef
from jimu_runtime.domain.runtime.releases import AdmissionIssues, AdmissionReceipt
from jimu_runtime.domain.validation.issues import ValidationIssue

_SCHEMA_VERSION = 1
_RECORD_ID = TypeAdapter[RecordId](RecordId)
_DIGEST = TypeAdapter[Digest](Digest)

_SCHEMA = (
    """
    CREATE TABLE archives (
        sha256 TEXT PRIMARY KEY,
        raw BLOB NOT NULL
    ) WITHOUT ROWID
    """,
    """
    CREATE TABLE bundle_identities (
        bundle_id TEXT NOT NULL,
        bundle_version TEXT NOT NULL,
        root_digest TEXT NOT NULL,
        PRIMARY KEY (bundle_id, bundle_version)
    ) WITHOUT ROWID
    """,
    """
    CREATE TABLE admissions (
        operation_id TEXT PRIMARY KEY,
        receipt_id TEXT UNIQUE,
        stored_archive_sha256 TEXT REFERENCES archives (sha256),
        record BLOB NOT NULL,
        CHECK (stored_archive_sha256 IS NULL OR receipt_id IS NOT NULL)
    ) WITHOUT ROWID
    """,
    """
    CREATE TABLE release_ledger (
        namespace TEXT NOT NULL,
        entry_key TEXT NOT NULL,
        record BLOB NOT NULL,
        PRIMARY KEY (namespace, entry_key)
    ) WITHOUT ROWID
    """,
)


class StoreError(RuntimeError):
    """Local persistence could not complete."""


class StoreBusy(StoreError):
    """Retry the same operation after the other writer completes."""


class StoreConflict(StoreError):
    """An operation key or expected record refers to different parameters."""


class StoreCorruption(StoreError):
    """Persistent data failed internal consistency or readback validation."""


class SignatureAudit(ContractModel):
    ref: DetachedSignatureRef
    signer_id: RecordId
    key_pin: TrustPin
    signed_at: UTCDateTime


class AdmissionEvidence(ContractModel):
    capability_pin: TrustPin | None
    trust_policy_pin: TrustPin | None
    trust_snapshot_pin: TrustPin | None
    signatures: Annotated[
        tuple[SignatureAudit, ...],
        Field(max_length=64),
        BeforeValidator(immutable_sequence),
    ]
    source_evidence_pin: TrustPin | None

    @model_validator(mode="after")
    def coherent_evidence(self) -> Self:
        if self.signatures or self.source_evidence_pin is not None:
            if self.trust_policy_pin is None or self.trust_snapshot_pin is None:
                raise ValueError("signature and source evidence require the trust pins")
        signatures = {
            (item.ref.signature_registry_id, item.ref.signature_id, item.ref.version)
            for item in self.signatures
        }
        if len(signatures) != len(self.signatures):
            raise ValueError("signature evidence must have unique references")
        return self


class IdentifiedAdmission(ContractModel):
    kind: Literal["identified"]
    receipt: AdmissionReceipt


class UnidentifiedRejection(ContractModel):
    kind: Literal["unidentified"]
    runtime_version: Version
    runtime_identity: ActorRef
    errors: AdmissionIssues
    warnings: AdmissionIssues
    timestamp: UTCDateTime

    @model_validator(mode="after")
    def nonempty_errors(self) -> Self:
        if not self.errors:
            raise ValueError("unidentified rejection requires errors")
        return self


type AdmissionResult = Annotated[
    IdentifiedAdmission | UnidentifiedRejection, Field(discriminator="kind")
]


class AdmissionVerification(ContractModel):
    """Result of the deployment-owned verifier for one raw archive."""

    result: AdmissionResult
    evidence: AdmissionEvidence

    @model_validator(mode="after")
    def accepted_evidence(self) -> Self:
        receipt = self.receipt
        if receipt is not None and receipt.accepted:
            evidence = self.evidence
            if (
                evidence.capability_pin is None
                or evidence.trust_policy_pin is None
                or evidence.trust_snapshot_pin is None
                or not evidence.signatures
            ):
                raise ValueError(
                    "accepted admission requires capability and trust evidence"
                )
        return self

    @property
    def receipt(self) -> AdmissionReceipt | None:
        if isinstance(self.result, IdentifiedAdmission):
            return self.result.receipt
        return None


class StoredAdmission(ContractModel):
    """An immutable operation result tied to the exact original archive bytes."""

    schema_version: Literal["1.0.0"]
    operation_id: RecordId
    archive_sha256: Digest
    archive_size: Annotated[int, Field(ge=0)]
    actor: ActorRef
    parameters_digest: Digest
    verification: AdmissionVerification

    @property
    def receipt(self) -> AdmissionReceipt | None:
        return self.verification.receipt

    @property
    def accepted(self) -> bool:
        receipt = self.receipt
        return receipt is not None and receipt.accepted


class LocalBundleStore:
    """Local content-addressed storage and immutable admission history."""

    def __init__(self, directory: Path) -> None:
        self._directory = directory.absolute()
        self._database = self._directory / "bundle-store.sqlite3"
        self._prepare_directory()
        with self._transaction(write=True) as connection:
            version = connection.execute("PRAGMA user_version").fetchone()[0]
            if version == 0:
                occupied = connection.execute("SELECT 1 FROM sqlite_schema LIMIT 1")
                if occupied.fetchone() is not None:
                    raise StoreError("Bundle store has an unrecognized schema")
                for statement in _SCHEMA:
                    connection.execute(statement)
                connection.execute(f"PRAGMA user_version = {_SCHEMA_VERSION}")
            elif version != _SCHEMA_VERSION:
                raise StoreError("Bundle store has an unsupported schema version")

    def _prepare_directory(self) -> None:
        try:
            self._directory.mkdir(mode=0o700, exist_ok=True)
            _require_private_path(self._directory, stat.S_ISDIR)
            try:
                descriptor = os.open(
                    self._database,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                    0o600,
                )
            except FileExistsError:
                _require_private_path(self._database, stat.S_ISREG)
            else:
                os.close(descriptor)
            # Also cover a concurrent constructor that just created the database.
            _sync_directory(self._directory)
            _sync_directory(self._directory.parent)
        except OSError as exc:
            raise StoreError("Bundle store path is unavailable") from exc

    @contextmanager
    def _transaction(self, *, write: bool = False) -> Iterator[sqlite3.Connection]:
        try:
            connection = sqlite3.connect(
                self._database.as_uri() + "?mode=rw",
                uri=True,
                timeout=2.0,
                isolation_level=None,
            )
            try:
                connection.execute("PRAGMA foreign_keys = ON")
                connection.execute("PRAGMA synchronous = EXTRA")
                connection.execute("BEGIN IMMEDIATE" if write else "BEGIN")
                if connection.execute("PRAGMA journal_mode").fetchone()[0] != "delete":
                    raise StoreError("Bundle store requires a delete rollback journal")
                yield connection
                connection.commit()
            finally:
                # Closing rolls back any transaction interrupted before commit.
                connection.close()
        except sqlite3.IntegrityError as exc:
            raise StoreConflict("Bundle store record identity already exists") from exc
        except sqlite3.DatabaseError as exc:
            code = getattr(exc, "sqlite_errorcode", 0)
            # Extended result codes keep their primary category in the low byte.
            if code & 0xFF in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED):
                raise StoreBusy("Bundle store is busy") from exc
            raise StoreError("Bundle store transaction failed") from exc

    def _admit(
        self,
        operation_id: str,
        archive: bytes,
        *,
        actor: ActorRef,
        parameters_digest: str,
        verify: Callable[[bytes], AdmissionVerification],
    ) -> StoredAdmission:
        """Run trusted validation for a new operation, outside the writer lock."""
        operation = _RECORD_ID.validate_python(operation_id)
        parameters = _DIGEST.validate_python(parameters_digest)
        principal = ActorRef.model_validate(actor)
        if type(archive) is not bytes:
            raise TypeError("admission requires immutable archive bytes")
        archive_digest = sha256(archive).hexdigest()
        with self._transaction() as connection:
            existing = _find_admission(connection, operation_id=operation)
            if existing is not None:
                _check_retry(
                    connection, existing, archive_digest, principal, parameters
                )
                return existing

        verification = AdmissionVerification.model_validate(verify(archive))
        with self._transaction(write=True) as connection:
            existing = _find_admission(connection, operation_id=operation)
            if existing is not None:
                _check_retry(
                    connection, existing, archive_digest, principal, parameters
                )
                return existing

            verification = _bind_verified_identity(connection, verification)
            entry = StoredAdmission(
                schema_version="1.0.0",
                operation_id=operation,
                archive_sha256=archive_digest,
                archive_size=len(archive),
                actor=principal,
                parameters_digest=parameters,
                verification=verification,
            )
            receipt = entry.receipt
            if entry.accepted:
                connection.execute(
                    "INSERT INTO archives VALUES (?, ?) ON CONFLICT (sha256) DO NOTHING",
                    (archive_digest, archive),
                )
                _read_archive(connection, entry)
            connection.execute(
                "INSERT INTO admissions VALUES (?, ?, ?, ?)",
                (
                    operation,
                    receipt.receipt_id if receipt is not None else None,
                    archive_digest if entry.accepted else None,
                    entry.model_dump_json().encode("utf-8"),
                ),
            )
            return entry

    def get_admission(self, operation_id: str) -> StoredAdmission | None:
        """Read the result of a specific completed operation, including rejections."""
        operation = _RECORD_ID.validate_python(operation_id)
        with self._transaction() as connection:
            entry = _find_admission(connection, operation_id=operation)
            if entry is not None and entry.accepted:
                _read_archive(connection, entry)
            return entry

    def get_receipt(self, receipt_id: str) -> StoredAdmission | None:
        """Look up a receipt issued by a completed local operation."""
        with self._transaction() as connection:
            return _verified_receipt(connection, receipt_id)

    def read_archive(self, receipt_id: str) -> bytes:
        """Read and hash-check the accepted original ZIP for this exact receipt."""
        receipt = _RECORD_ID.validate_python(receipt_id)
        with self._transaction() as connection:
            entry = _find_admission(connection, receipt_id=receipt)
            if entry is None or not entry.accepted:
                raise StoreError("no accepted archive for this receipt")
            return _read_archive(connection, entry)

    @contextmanager
    def _release_transaction(self) -> Iterator[ReleaseLedger]:
        """Lend the trusted Release controller a transactional ledger view."""
        with self._transaction(write=True) as connection:
            yield ReleaseLedger(connection)


type ReleaseNamespace = Literal[
    "release", "approval", "activation", "active_scope", "task_pin", "operation"
]


class ReleaseLedger:
    """Narrow callback scope for Release writes and authoritative receipt reads."""

    def __init__(self, connection: sqlite3.Connection) -> None:
        self._connection = connection

    def get_receipt(self, receipt_id: str) -> StoredAdmission | None:
        return _verified_receipt(self._connection, receipt_id)

    def get(self, namespace: ReleaseNamespace, key: str) -> bytes | None:
        cursor = self._connection.execute(
            "SELECT record FROM release_ledger WHERE namespace = ? AND entry_key = ?",
            (namespace, key),
        )
        row = cursor.fetchone()
        return bytes(row[0]) if row is not None else None

    def compare_and_set(
        self,
        namespace: ReleaseNamespace,
        key: str,
        *,
        expected: bytes | None,
        value: bytes,
    ) -> None:
        if self.get(namespace, key) != expected:
            raise StoreConflict("Release ledger predecessor differs")
        self._connection.execute(
            """
            INSERT INTO release_ledger VALUES (?, ?, ?)
            ON CONFLICT (namespace, entry_key) DO UPDATE SET record = excluded.record
            """,
            (namespace, key, value),
        )


def _require_private_path(path: Path, is_type: Callable[[int], bool]) -> None:
    info = path.lstat()
    if (
        not is_type(info.st_mode)
        or info.st_uid != os.geteuid()
        or stat.S_IMODE(info.st_mode) & 0o077
    ):
        raise StoreError("Bundle store path must be private to the runtime owner")


def _sync_directory(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _check_retry(
    connection: sqlite3.Connection,
    entry: StoredAdmission,
    digest: str,
    actor: ActorRef,
    parameters: str,
) -> None:
    if (
        entry.archive_sha256 != digest
        or entry.actor != actor
        or entry.parameters_digest != parameters
    ):
        raise StoreConflict("admission operation key identifies different parameters")
    if entry.accepted:
        _read_archive(connection, entry)


def _find_admission(
    connection: sqlite3.Connection,
    *,
    operation_id: str | None = None,
    receipt_id: str | None = None,
) -> StoredAdmission | None:
    if operation_id is not None:
        column, key = "operation_id", operation_id
    elif receipt_id is not None:
        column, key = "receipt_id", receipt_id
    else:
        raise ValueError("admission lookup requires an operation or receipt identity")
    row = connection.execute(
        f"SELECT operation_id, receipt_id, stored_archive_sha256, record "
        f"FROM admissions WHERE {column} = ?",
        (key,),
    ).fetchone()
    if row is None:
        return None
    try:
        entry = StoredAdmission.model_validate_json(row[3])
    except ValidationError:
        raise StoreCorruption("invalid stored admission metadata") from None
    receipt = entry.receipt
    if (
        row[0] != entry.operation_id
        or row[1] != (receipt.receipt_id if receipt is not None else None)
        or row[2] != (entry.archive_sha256 if entry.accepted else None)
    ):
        raise StoreCorruption("inconsistent stored admission keys")
    return entry


def _read_archive(connection: sqlite3.Connection, entry: StoredAdmission) -> bytes:
    receipt = entry.receipt
    if receipt is None:
        raise StoreCorruption("accepted archive is missing its receipt")
    if _identity_root(connection, receipt) != receipt.bundle_digest:
        raise StoreCorruption("accepted archive has an inconsistent logical root pin")

    row = connection.execute(
        "SELECT raw FROM archives WHERE sha256 = ?", (entry.archive_sha256,)
    ).fetchone()
    if row is None or not isinstance(row[0], bytes):
        raise StoreCorruption("accepted archive bytes are missing")
    raw = row[0]
    if (
        len(raw) != entry.archive_size
        or sha256(raw).hexdigest() != entry.archive_sha256
    ):
        raise StoreCorruption("accepted archive bytes failed readback verification")
    return raw


def _verified_receipt(
    connection: sqlite3.Connection, receipt_id: str
) -> StoredAdmission | None:
    receipt = _RECORD_ID.validate_python(receipt_id)
    entry = _find_admission(connection, receipt_id=receipt)
    if entry is not None and entry.accepted:
        _read_archive(connection, entry)
    return entry


def _bind_verified_identity(
    connection: sqlite3.Connection, verification: AdmissionVerification
) -> AdmissionVerification:
    receipt = verification.receipt
    if receipt is None or not receipt.accepted:
        return verification
    identity = (receipt.bundle_id, receipt.bundle_version)
    connection.execute(
        """
        INSERT INTO bundle_identities VALUES (?, ?, ?)
        ON CONFLICT (bundle_id, bundle_version) DO NOTHING
        """,
        (*identity, receipt.bundle_digest),
    )
    if _identity_root(connection, receipt) == receipt.bundle_digest:
        return verification
    issue = ValidationIssue(
        code="bundle_identity_conflict",
        message="Bundle identity already pins a different logical root",
        path=("bundle_id", "bundle_version"),
        related_refs=(),
    )
    rejected = receipt.model_copy(update={"accepted": False, "errors": (issue,)})
    return AdmissionVerification(
        result=IdentifiedAdmission(kind="identified", receipt=rejected),
        evidence=verification.evidence,
    )


def _identity_root(
    connection: sqlite3.Connection, receipt: AdmissionReceipt
) -> str | None:
    row = connection.execute(
        "SELECT root_digest FROM bundle_identities WHERE bundle_id = ? AND bundle_version = ?",
        (receipt.bundle_id, receipt.bundle_version),
    ).fetchone()
    if row is None:
        return None
    if not isinstance(row[0], str):
        raise StoreCorruption("invalid stored logical root pin")
    return row[0]
