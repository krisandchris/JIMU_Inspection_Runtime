"""The public admission entry verifies and durably retains real signed bundles."""

from hashlib import sha256
from io import BytesIO
from pathlib import Path
from zipfile import ZipFile

from fixtures.wp02.admission.support import (
    ACTOR,
    RUNTIME_ACTOR,
    build_scenario,
    sign_bundle,
    submit,
)
from fixtures.wp02.trust.support import AT, pin

from jimu_runtime.bundle.store import LocalBundleStore


def test_signed_compiler_output_is_independently_verified_and_reopenable(
    tmp_path: Path,
) -> None:
    compiled = build_scenario(tmp_path)
    signed = sign_bundle(
        tmp_path, compiled.archive_bytes, compiled.bundle_ref.bundle_digest
    )
    directory = tmp_path / "runtime-store"
    store = LocalBundleStore(directory)
    receipt_row = submit(store, signed.archive, "accept-1", signed.environment)
    assert receipt_row.receipt is not None
    receipt = receipt_row.receipt
    assert receipt.accepted, receipt.errors
    assert signed.trust.reads == 1
    assert receipt.bundle_digest == compiled.bundle_ref.bundle_digest
    assert receipt.runtime_identity == RUNTIME_ACTOR
    assert receipt.timestamp == AT
    assert receipt_row.archive_sha256 == sha256(signed.archive).hexdigest()
    assert receipt_row.archive_size == len(signed.archive)
    assert receipt_row.actor == ACTOR
    assert {item.status for item in receipt.capability_report.required} == {"supported"}
    assert receipt.capability_report.optional == ()
    assert "semantics.context_source_trust" in {
        warning.code for warning in receipt.warnings
    }
    evidence = receipt_row.verification.evidence
    assert evidence.capability_pin == signed.environment.capabilities.pin
    assert evidence.trust_policy_pin == pin("fixture.policy")
    assert evidence.trust_snapshot_pin == pin("fixture.snapshot")
    assert len(evidence.signatures) == 1
    assert evidence.signatures[0].ref == signed.signature.ref
    assert evidence.signatures[0].key_pin == signed.key.pin

    reopened = LocalBundleStore(directory)
    assert reopened.get_admission("accept-1") == receipt_row
    assert reopened.get_receipt(receipt.receipt_id) == receipt_row
    assert reopened.read_archive(receipt.receipt_id) == signed.archive
    assert signed.trust.reads == 1
    assert (
        submit(reopened, signed.archive, "accept-1", signed.environment) == receipt_row
    )
    assert signed.trust.reads == 2


def test_distinct_transport_bytes_with_same_root_receive_distinct_receipts(
    tmp_path: Path,
) -> None:
    compiled = build_scenario(tmp_path)
    signed = sign_bundle(
        tmp_path, compiled.archive_bytes, compiled.bundle_ref.bundle_digest
    )
    alternate = BytesIO(signed.archive)
    with ZipFile(alternate, "a") as zip_file:
        zip_file.comment = b"same logical payload and signatures"
    alternate_bytes = alternate.getvalue()
    assert alternate_bytes != signed.archive

    store = LocalBundleStore(tmp_path / "runtime-store")
    one = submit(store, signed.archive, "transport-1", signed.environment)
    two = submit(store, alternate_bytes, "transport-2", signed.environment)
    assert one.receipt is not None and two.receipt is not None
    assert one.accepted and two.accepted, (one.receipt.errors, two.receipt.errors)
    assert one.receipt.bundle_digest == two.receipt.bundle_digest == signed.digest
    assert one.receipt.receipt_id != two.receipt.receipt_id
    assert one.archive_sha256 == sha256(signed.archive).hexdigest()
    assert two.archive_sha256 == sha256(alternate_bytes).hexdigest()
    assert one.archive_sha256 != two.archive_sha256
    assert signed.trust.reads == 2
    assert store.read_archive(one.receipt.receipt_id) == signed.archive
    assert store.read_archive(two.receipt.receipt_id) == alternate_bytes
