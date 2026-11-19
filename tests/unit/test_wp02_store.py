import hashlib
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier

from jimu_runtime.bundle.store import (
    AdmissionVerification,
    LocalBundleStore,
    StoredAdmission,
)
from tests.fixtures.wp02.store.support import (
    ACTOR,
    PARAMETERS,
    no_verification,
    raw_zip,
    verdict,
)


def test_reopen_and_exact_operation_query(tmp_path: Path) -> None:
    raw = raw_zip()
    store = LocalBundleStore(tmp_path)
    entry = store._admit(
        "operation-a",
        raw,
        actor=ACTOR,
        parameters_digest=PARAMETERS,
        verify=lambda payload: verdict("receipt-a")
        if payload == raw
        else no_verification(payload),
    )

    reopened = LocalBundleStore(tmp_path)
    assert entry.accepted
    assert entry.archive_sha256 == hashlib.sha256(raw).hexdigest()
    assert entry.archive_size == len(raw)
    assert reopened.read_archive("receipt-a") == raw
    assert reopened.get_receipt("receipt-a") == entry
    assert reopened.get_admission("operation-a") == entry
    assert reopened.get_receipt("fabricated") is None
    assert (
        reopened._admit(
            "operation-a",
            raw,
            actor=ACTOR,
            parameters_digest=PARAMETERS,
            verify=no_verification,
        )
        == entry
    )


def test_same_logical_root_separates_original_zip_and_signature_evidence(
    tmp_path: Path,
) -> None:
    store = LocalBundleStore(tmp_path)
    raw_a, raw_b = raw_zip(b"signature A"), raw_zip(b"signature B")
    admission_a = store._admit(
        "a",
        raw_a,
        actor=ACTOR,
        parameters_digest=PARAMETERS,
        verify=lambda _: verdict("receipt-a"),
    )
    admission_b = store._admit(
        "b",
        raw_b,
        actor=ACTOR,
        parameters_digest=PARAMETERS,
        verify=lambda _: verdict("receipt-b", signature_id="signature-b"),
    )

    assert admission_a.receipt is not None
    assert admission_b.receipt is not None
    assert admission_a.receipt.bundle_digest == admission_b.receipt.bundle_digest
    assert admission_a.archive_sha256 != admission_b.archive_sha256
    assert admission_a.verification.evidence.signatures != (
        admission_b.verification.evidence.signatures
    )
    assert store.read_archive("receipt-a") == raw_a
    assert store.read_archive("receipt-b") == raw_b


def test_concurrent_exact_retries_commit_one_result(tmp_path: Path) -> None:
    first = LocalBundleStore(tmp_path)
    second = LocalBundleStore(tmp_path)
    start = Barrier(2)
    raw = raw_zip()

    def write(store: LocalBundleStore, candidate_id: str) -> StoredAdmission:
        def verify(_: bytes) -> AdmissionVerification:
            start.wait(timeout=10)
            return verdict(candidate_id)

        return store._admit(
            "shared-key", raw, actor=ACTOR, parameters_digest=PARAMETERS, verify=verify
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        writes = (
            executor.submit(write, first, "receipt-a"),
            executor.submit(write, second, "receipt-b"),
        )
        first_result, second_result = (write.result(timeout=15) for write in writes)

    assert first_result == second_result
    with sqlite3.connect(tmp_path / "bundle-store.sqlite3") as connection:
        assert connection.execute("SELECT count(*) FROM archives").fetchone() == (1,)
        assert connection.execute("SELECT count(*) FROM admissions").fetchone() == (1,)


def test_release_ledger_reads_authoritative_receipts_and_persists_records(
    tmp_path: Path,
) -> None:
    store = LocalBundleStore(tmp_path)
    admission = store._admit(
        "a",
        raw_zip(),
        actor=ACTOR,
        parameters_digest=PARAMETERS,
        verify=lambda _: verdict("receipt-a"),
    )
    with store._release_transaction() as ledger:
        assert ledger.get_receipt("receipt-a") == admission
        assert ledger.get_receipt("unknown") is None
        ledger.compare_and_set("activation", "v1", expected=None, value=b"record")
        ledger.compare_and_set("active_scope", "global", expected=None, value=b"v1")

    with LocalBundleStore(tmp_path)._release_transaction() as ledger:
        assert ledger.get("activation", "v1") == b"record"
        assert ledger.get("active_scope", "global") == b"v1"
