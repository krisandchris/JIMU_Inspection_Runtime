import hashlib
import sqlite3
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier

import pytest
from pydantic import ValidationError

from jimu_runtime.bundle.store import (
    AdmissionVerification,
    LocalBundleStore,
    StoreBusy,
    StoreConflict,
    StoreCorruption,
    StoredAdmission,
    StoreError,
)
from tests.fixtures.wp02.store.support import (
    ACTOR,
    EMPTY_EVIDENCE,
    PARAMETERS,
    ROOT,
    RUNTIME,
    no_verification,
    raw_zip,
    rejection,
    verdict,
)


def test_conflicting_operation_parameters_do_not_call_verifier(tmp_path: Path) -> None:
    store = LocalBundleStore(tmp_path)
    raw = raw_zip()
    store._admit(
        "a",
        raw,
        actor=ACTOR,
        parameters_digest=PARAMETERS,
        verify=lambda _: verdict("receipt-a"),
    )
    for payload, actor, params in (
        (raw_zip(b"another ZIP"), ACTOR, PARAMETERS),
        (raw, RUNTIME, PARAMETERS),
        (raw, ACTOR, "c" * 64),
    ):
        with pytest.raises(StoreConflict, match="different parameters"):
            store._admit(
                "a",
                payload,
                actor=actor,
                parameters_digest=params,
                verify=no_verification,
            )


def test_new_submission_always_revalidates_original_zip(tmp_path: Path) -> None:
    store = LocalBundleStore(tmp_path)
    archive = raw_zip()
    accepted = store._admit(
        "accepted",
        archive,
        actor=ACTOR,
        parameters_digest=PARAMETERS,
        verify=lambda _: verdict("receipt-first"),
    )
    rejected = store._admit(
        "after-revocation",
        archive,
        actor=ACTOR,
        parameters_digest=PARAMETERS,
        verify=lambda _: verdict("receipt-after", accepted=False),
    )
    assert accepted.archive_sha256 == rejected.archive_sha256
    assert not rejected.accepted
    assert store.get_receipt("receipt-after") == rejected
    with pytest.raises(StoreError, match="no accepted archive"):
        store.read_archive("receipt-after")
    assert store.read_archive("receipt-first") == archive
    assert (
        store._admit(
            "accepted",
            archive,
            actor=ACTOR,
            parameters_digest=PARAMETERS,
            verify=no_verification,
        )
        == accepted
    )


def test_unsigned_same_root_never_inherits_previous_acceptance(tmp_path: Path) -> None:
    store = LocalBundleStore(tmp_path)
    store._admit(
        "signed",
        raw_zip(),
        actor=ACTOR,
        parameters_digest=PARAMETERS,
        verify=lambda _: verdict("signed-receipt"),
    )
    denied = store._admit(
        "unsigned",
        raw_zip(b""),
        actor=ACTOR,
        parameters_digest=PARAMETERS,
        verify=lambda _: verdict("unsigned-receipt", accepted=False),
    )
    assert not denied.accepted
    assert denied.receipt is not None
    assert denied.receipt.bundle_digest == ROOT
    assert not denied.verification.evidence.signatures
    with pytest.raises(StoreError, match="no accepted archive"):
        store.read_archive("unsigned-receipt")


def test_same_id_version_different_roots_compete_atomically(tmp_path: Path) -> None:
    first = LocalBundleStore(tmp_path)
    second = LocalBundleStore(tmp_path)
    start = Barrier(2)

    def write(store: LocalBundleStore, key: str, logical_root: str) -> StoredAdmission:
        def verify(_: bytes) -> AdmissionVerification:
            start.wait(timeout=10)
            return verdict(key, logical_root=logical_root)

        return store._admit(
            key,
            raw_zip(key.encode()),
            actor=ACTOR,
            parameters_digest=PARAMETERS,
            verify=verify,
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        writes = (
            executor.submit(write, first, "a", ROOT),
            executor.submit(write, second, "b", "c" * 64),
        )
        results = [write.result(timeout=15) for write in writes]

    denied, accepted = sorted(results, key=lambda entry: entry.accepted)
    assert accepted.accepted
    assert not denied.accepted
    assert denied.receipt is not None
    assert denied.receipt.errors[0].code == "bundle_identity_conflict"
    assert LocalBundleStore(tmp_path).get_receipt(denied.receipt.receipt_id) == denied
    with sqlite3.connect(tmp_path / "bundle-store.sqlite3") as connection:
        assert connection.execute(
            "SELECT count(*) FROM bundle_identities"
        ).fetchone() == (1,)
        assert connection.execute("SELECT count(*) FROM archives").fetchone() == (1,)


def test_unidentified_rejection_preserves_source_hash_without_raw_bytes(
    tmp_path: Path,
) -> None:
    raw = b"invalid archive"
    entry = LocalBundleStore(tmp_path)._admit(
        "invalid",
        raw,
        actor=ACTOR,
        parameters_digest=PARAMETERS,
        verify=lambda _: rejection(),
    )
    assert entry.archive_sha256 == hashlib.sha256(raw).hexdigest()
    assert entry.receipt is None
    assert entry.verification.result.kind == "unidentified"
    assert LocalBundleStore(tmp_path).get_admission("invalid") == entry
    with sqlite3.connect(tmp_path / "bundle-store.sqlite3") as connection:
        assert connection.execute("SELECT count(*) FROM archives").fetchone() == (0,)


def test_receipt_collision_rolls_back_staged_zip_and_identity(tmp_path: Path) -> None:
    store = LocalBundleStore(tmp_path)
    store._admit(
        "first",
        raw_zip(),
        actor=ACTOR,
        parameters_digest=PARAMETERS,
        verify=lambda _: verdict("receipt-a"),
    )
    with pytest.raises(StoreConflict, match="identity already exists"):
        store._admit(
            "second",
            raw_zip(b"a different ZIP"),
            actor=ACTOR,
            parameters_digest=PARAMETERS,
            verify=lambda _: verdict("receipt-a"),
        )
    assert store.get_admission("second") is None
    with sqlite3.connect(tmp_path / "bundle-store.sqlite3") as connection:
        assert connection.execute("SELECT count(*) FROM archives").fetchone() == (1,)


def test_valid_shape_without_host_evidence_cannot_commit(tmp_path: Path) -> None:
    store = LocalBundleStore(tmp_path)
    forged = verdict("forged").model_copy(update={"evidence": EMPTY_EVIDENCE})
    with pytest.raises(ValidationError, match="requires capability and trust evidence"):
        store._admit(
            "a",
            raw_zip(),
            actor=ACTOR,
            parameters_digest=PARAMETERS,
            verify=lambda _: forged,
        )
    assert store.get_receipt("forged") is None
    assert store.get_admission("a") is None


def test_readback_detects_changed_bytes(tmp_path: Path) -> None:
    store = LocalBundleStore(tmp_path)
    raw = raw_zip()
    store._admit(
        "a",
        raw,
        actor=ACTOR,
        parameters_digest=PARAMETERS,
        verify=lambda _: verdict("receipt-a"),
    )
    with sqlite3.connect(tmp_path / "bundle-store.sqlite3") as connection:
        connection.execute("UPDATE archives SET raw = ?", (bytes(len(raw)),))
    with pytest.raises(StoreCorruption, match="readback verification"):
        LocalBundleStore(tmp_path).get_receipt("receipt-a")


def test_ledger_failed_predecessor_rolls_back_all_changes(tmp_path: Path) -> None:
    store = LocalBundleStore(tmp_path)
    with store._release_transaction() as ledger:
        ledger.compare_and_set("active_scope", "global", expected=None, value=b"v1")

    with pytest.raises(StoreConflict, match="predecessor differs"):
        with store._release_transaction() as ledger:
            ledger.compare_and_set("activation", "v2", expected=None, value=b"record")
            ledger.compare_and_set("active_scope", "global", expected=None, value=b"v2")

    with LocalBundleStore(tmp_path)._release_transaction() as ledger:
        assert ledger.get("activation", "v2") is None
        assert ledger.get("active_scope", "global") == b"v1"


@pytest.mark.parametrize("stage", ["before-receipt", "before-commit"])
def test_interrupted_process_never_leaks_archive_or_root_pin(
    tmp_path: Path,
    stage: str,
) -> None:
    crash_command = r"""
import os
import sys
from contextlib import contextmanager
from io import BytesIO
from pathlib import Path
from zipfile import ZipFile, ZipInfo

from jimu_runtime.bundle import store as module
from tests.fixtures.wp02.store.support import ACTOR, PARAMETERS, verdict

directory = Path(sys.argv[1])
stage = sys.argv[2]
store = module.LocalBundleStore(directory)
transaction = store._transaction
read_archive = module._read_archive

def crash(connection, expected_admissions):
    assert connection.execute("SELECT count(*) FROM archives").fetchone() == (1,)
    assert connection.execute("SELECT count(*) FROM admissions").fetchone() == (expected_admissions,)
    assert (directory / "bundle-store.sqlite3-journal").stat().st_size > 512
    os._exit(73)

def readback(connection, entry):
    checked = read_archive(connection, entry)
    if stage == "before-receipt":
        crash(connection, 0)
    return checked

@contextmanager
def interrupted_transaction(*, write=False):
    with transaction(write=write) as connection:
        connection.execute("PRAGMA cache_size = 4")
        yield connection
        if write and stage == "before-commit":
            crash(connection, 1)

module._read_archive = readback
store._transaction = interrupted_transaction
raw = BytesIO()
with ZipFile(raw, "w") as archive:
    archive.writestr(ZipInfo("payload.bin"), bytes(512 * 1024))
store._admit(
    "interrupted", raw.getvalue(), actor=ACTOR, parameters_digest=PARAMETERS,
    verify=lambda _: verdict("interrupted-receipt"),
)
"""
    outcome = subprocess.run(
        [sys.executable, "-c", crash_command, str(tmp_path), stage],
        check=False,
        capture_output=True,
        timeout=10,
    )
    assert outcome.returncode == 73, outcome.stderr.decode()
    journal = tmp_path / "bundle-store.sqlite3-journal"
    assert journal.is_file()
    reopened = LocalBundleStore(tmp_path)
    assert not journal.exists()
    assert reopened.get_admission("interrupted") is None
    assert reopened.get_receipt("interrupted-receipt") is None
    with sqlite3.connect(tmp_path / "bundle-store.sqlite3") as connection:
        assert connection.execute("SELECT count(*) FROM archives").fetchone() == (0,)
        assert connection.execute(
            "SELECT count(*) FROM bundle_identities"
        ).fetchone() == (0,)
    retried = reopened._admit(
        "interrupted",
        raw_zip(),
        actor=ACTOR,
        parameters_digest=PARAMETERS,
        verify=lambda _: verdict("new-receipt", logical_root="c" * 64),
    )
    assert retried.accepted


def test_store_requires_owner_private_path(tmp_path: Path) -> None:
    unsafe = tmp_path / "unsafe"
    unsafe.mkdir(mode=0o755)
    with pytest.raises(StoreError, match="must be private"):
        LocalBundleStore(unsafe)
    private = tmp_path / "private"
    private.mkdir(mode=0o700)
    link = tmp_path / "link"
    link.symlink_to(private, target_is_directory=True)
    with pytest.raises(StoreError, match="must be private"):
        LocalBundleStore(link)


def test_busy_writer_can_retry_without_partial_record(tmp_path: Path) -> None:
    store = LocalBundleStore(tmp_path)
    attempted: list[bytes] = []
    raw = raw_zip()

    def verify(payload: bytes) -> AdmissionVerification:
        attempted.append(payload)
        return verdict("receipt-a")

    with sqlite3.connect(tmp_path / "bundle-store.sqlite3") as connection:
        connection.execute("BEGIN IMMEDIATE")
        with pytest.raises(StoreBusy, match="is busy"):
            store._admit(
                "a", raw, actor=ACTOR, parameters_digest=PARAMETERS, verify=verify
            )

    assert store.get_admission("a") is None
    retried = store._admit(
        "a", raw, actor=ACTOR, parameters_digest=PARAMETERS, verify=verify
    )
    assert retried.accepted
    assert attempted == [raw, raw]
