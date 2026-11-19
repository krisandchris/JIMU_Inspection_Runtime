"""Rejections through the public entry, with genuine transport and trust evidence."""

from dataclasses import replace
from datetime import timedelta
from hashlib import sha256
from pathlib import Path
from typing import cast

import pytest
from fixtures.wp02.admission.support import (
    JSON_BUDGET,
    LIMITS,
    build_scenario,
    rebuild_unsigned,
    sign_bundle,
    submit,
)
from fixtures.wp02.trust.support import AT, SIGNED_AT, pin

from jimu_runtime.bundle.archive import read_archive, write_archive
from jimu_runtime.bundle.integrity import (
    MANIFEST_PATH,
    canonical_json,
    read_canonical_json,
)
from jimu_runtime.bundle.store import (
    LocalBundleStore,
    StoreConflict,
    StoreError,
    UnidentifiedRejection,
)
from jimu_runtime.domain.fact_schemas.canonicalization import JSONValue


def test_cannot_reuse_signed_receipt_for_unsigned_bytes_of_same_root(
    tmp_path: Path,
) -> None:
    compiled = build_scenario(tmp_path)
    signed = sign_bundle(
        tmp_path, compiled.archive_bytes, compiled.bundle_ref.bundle_digest
    )
    store = LocalBundleStore(tmp_path / "store")
    accepted = submit(store, signed.archive, "first", signed.environment)
    assert accepted.accepted and accepted.receipt is not None
    with pytest.raises(StoreConflict):
        submit(store, signed.unsigned, "first", signed.environment)

    rejected = submit(store, signed.unsigned, "new-operation", signed.environment)
    assert not rejected.accepted and rejected.receipt is not None
    assert rejected.receipt.bundle_digest == accepted.receipt.bundle_digest
    assert rejected.archive_sha256 != accepted.archive_sha256
    assert tuple(issue.code for issue in rejected.receipt.errors) == (
        "trust.signature_missing",
    )
    assert rejected.verification.evidence.signatures == ()
    with pytest.raises(StoreError, match="no accepted archive"):
        store.read_archive(rejected.receipt.receipt_id)


def test_revoke_key_and_revalidate_even_identical_transport(tmp_path: Path) -> None:
    compiled = build_scenario(tmp_path)
    signed = sign_bundle(
        tmp_path, compiled.archive_bytes, compiled.bundle_ref.bundle_digest
    )
    store = LocalBundleStore(tmp_path / "store")
    accepted = submit(store, signed.archive, "before-revocation", signed.environment)
    assert accepted.accepted and accepted.receipt is not None
    old_snapshot = signed.trust.snapshot
    assert old_snapshot is not None
    revoked_key = signed.key.model_copy(update={"revoked_at": AT})
    new_pin = pin("fixture.snapshot", "b2" * 32)
    configuration = old_snapshot.configuration.model_copy(
        update={"pin": new_pin, "keys": (revoked_key,)}
    )
    signed.trust.snapshot = replace(old_snapshot, configuration=configuration)

    with pytest.raises(StoreConflict):
        submit(store, signed.archive, "before-revocation", signed.environment)
    rejected = submit(store, signed.archive, "at-revocation", signed.environment)
    assert rejected.receipt is not None and not rejected.accepted
    assert rejected.archive_sha256 == accepted.archive_sha256
    assert tuple(issue.code for issue in rejected.receipt.errors) == (
        "trust.signer_revoked",
    )
    assert rejected.verification.evidence.trust_snapshot_pin == new_pin
    assert rejected.verification.evidence.signatures == ()
    assert signed.trust.reads == 3
    assert store.get_receipt(accepted.receipt.receipt_id) == accepted
    assert store.read_archive(accepted.receipt.receipt_id) == signed.archive
    with pytest.raises(StoreError):
        store.read_archive(rejected.receipt.receipt_id)


def test_clock_and_external_detached_evidence_are_verified_on_each_submit(
    tmp_path: Path,
) -> None:
    compiled = build_scenario(tmp_path)
    signed = sign_bundle(
        tmp_path, compiled.archive_bytes, compiled.bundle_ref.bundle_digest
    )
    store = LocalBundleStore(tmp_path / "store")
    original = submit(store, signed.archive, "current", signed.environment)
    assert original.accepted
    before_signing = replace(
        signed.environment, validation_at=SIGNED_AT - timedelta(seconds=1)
    )
    with pytest.raises(StoreConflict):
        submit(store, signed.archive, "current", before_signing)
    early = submit(store, signed.archive, "early", before_signing)
    assert early.receipt is not None and not early.accepted
    assert "trust.signature_time" in {issue.code for issue in early.receipt.errors}

    # Simulate evidence registry loss with unchanged key/snapshot pins.
    snapshot = signed.trust.snapshot
    assert snapshot is not None
    signed.trust.snapshot = replace(
        snapshot, signatures=replace(snapshot.signatures, evidence=())
    )
    with pytest.raises(StoreConflict):
        submit(store, signed.archive, "current", signed.environment)
    missing_evidence = submit(
        store, signed.archive, "missing-evidence", signed.environment
    )
    assert missing_evidence.receipt is not None
    assert "trust.evidence_missing" in {
        issue.code for issue in missing_evidence.receipt.errors
    }


@pytest.mark.parametrize("member_bytes", [None, b"{", b"{}", b'{"x":1,"x":2}'])
def test_bad_or_missing_manifest_has_raw_provenance_without_bundle_identity(
    tmp_path: Path, member_bytes: bytes | None
) -> None:
    compiled = build_scenario(tmp_path)
    signed = sign_bundle(
        tmp_path, compiled.archive_bytes, compiled.bundle_ref.bundle_digest
    )
    files = {} if member_bytes is None else {MANIFEST_PATH: member_bytes}
    raw = write_archive(files, LIMITS)
    store = LocalBundleStore(tmp_path / "store")
    rejection = submit(store, raw, "bad-manifest", signed.environment)
    assert not rejection.accepted and rejection.receipt is None
    assert rejection.archive_sha256 == sha256(raw).hexdigest()
    assert rejection.archive_size == len(raw)
    result = rejection.verification.result
    assert isinstance(result, UnidentifiedRejection)
    assert result.errors[0].path[0] == MANIFEST_PATH
    assert "bundle_id" not in result.model_dump()
    assert "bundle_digest" not in result.model_dump()
    assert store.get_admission("bad-manifest") == rejection


def test_payload_tampering_keeps_bad_member_and_original_transport_hash(
    tmp_path: Path,
) -> None:
    compiled = build_scenario(tmp_path)
    signed = sign_bundle(
        tmp_path, compiled.archive_bytes, compiled.bundle_ref.bundle_digest
    )
    files = read_archive(signed.archive, LIMITS)
    changed_member = "ontology/entities.json"
    files[changed_member] = b"[]"
    raw = write_archive(files, LIMITS)
    rejection = submit(
        LocalBundleStore(tmp_path / "store"), raw, "tampered", signed.environment
    )
    assert rejection.receipt is None and not rejection.accepted
    assert rejection.archive_sha256 == sha256(raw).hexdigest()
    result = rejection.verification.result
    assert isinstance(result, UnidentifiedRejection)
    assert result.errors[0].code == "checksum_mismatch"
    assert result.errors[0].path == (changed_member,)


def test_host_archive_budget_controls_independent_admission(tmp_path: Path) -> None:
    compiled = build_scenario(tmp_path)
    signed = sign_bundle(
        tmp_path, compiled.archive_bytes, compiled.bundle_ref.bundle_digest
    )
    limits = replace(LIMITS, max_archive_bytes=len(signed.archive) - 1)
    rejection = submit(
        LocalBundleStore(tmp_path / "store"),
        signed.archive,
        "over-budget",
        signed.environment,
        limits=limits,
    )
    result = rejection.verification.result
    assert isinstance(result, UnidentifiedRejection)
    assert result.errors[0].code == "archive_size_limit"
    assert result.errors[0].path == ("archive",)
    assert rejection.archive_sha256 == sha256(signed.archive).hexdigest()


def test_signed_byte_consistent_missing_dependency_is_rejected(tmp_path: Path) -> None:
    compiled = build_scenario(tmp_path)
    missing = "ontology/entities.json"

    def remove_entity(
        manifest: dict[str, JSONValue], payloads: dict[str, bytes]
    ) -> None:
        payloads.pop(missing)
        rows = cast(list[dict[str, JSONValue]], manifest["artifacts"])
        manifest["artifacts"] = [row for row in rows if row["path"] != missing]

    raw, root = rebuild_unsigned(compiled.archive_bytes, remove_entity)
    signed = sign_bundle(tmp_path, raw, root)
    rejected = submit(
        LocalBundleStore(tmp_path / "store"),
        signed.archive,
        "bad-closure",
        signed.environment,
    )
    assert rejected.receipt is not None and not rejected.accepted
    assert rejected.receipt.bundle_digest == root
    assert tuple(issue.code for issue in rejected.receipt.errors) == (
        "reference_kind_mismatch",
    )
    assert rejected.verification.evidence.signatures[0].ref == signed.signature.ref


def test_signed_compiler_bypass_cannot_omit_catalogue_producers(tmp_path: Path) -> None:
    compiled = build_scenario(tmp_path)
    producer_catalogue = "facts/producers.json"

    def remove_producers(
        manifest: dict[str, JSONValue], payloads: dict[str, bytes]
    ) -> None:
        catalogue = cast(
            dict[str, JSONValue],
            read_canonical_json(payloads[producer_catalogue], **JSON_BUDGET),
        )
        catalogue["declarations"] = []
        raw = canonical_json(catalogue, **JSON_BUDGET)
        payloads[producer_catalogue] = raw
        rows = cast(list[dict[str, JSONValue]], manifest["artifacts"])
        (row,) = (item for item in rows if item["path"] == producer_catalogue)
        ref = cast(dict[str, JSONValue], row["content_ref"])
        ref["digest"] = sha256(raw).hexdigest()

    raw, root = rebuild_unsigned(compiled.archive_bytes, remove_producers)
    signed = sign_bundle(tmp_path, raw, root)
    rejected = submit(
        LocalBundleStore(tmp_path / "store"),
        signed.archive,
        "changed-producers",
        signed.environment,
    )
    assert rejected.receipt is not None and not rejected.accepted
    assert rejected.receipt.bundle_digest == root
    assert tuple(issue.code for issue in rejected.receipt.errors) == (
        "producer.catalogue_mismatch",
    )
    assert rejected.receipt.errors[0].path[0] == producer_catalogue
    assert rejected.verification.evidence.signatures[0].ref == signed.signature.ref


def test_valid_signature_does_not_cover_different_root(tmp_path: Path) -> None:
    compiled = build_scenario(tmp_path)
    root = compiled.bundle_ref.bundle_digest
    signed = sign_bundle(tmp_path, compiled.archive_bytes, root, signing_root="aa" * 32)
    rejected = submit(
        LocalBundleStore(tmp_path / "store"),
        signed.archive,
        "wrong-root",
        signed.environment,
    )
    assert rejected.receipt is not None and not rejected.accepted
    assert rejected.receipt.bundle_digest == root
    assert "trust.test_signature_invalid" in {
        issue.code for issue in rejected.receipt.errors
    }


def test_current_capability_revocation_overrides_successful_compilation(
    tmp_path: Path,
) -> None:
    compiled = build_scenario(tmp_path)
    signed = sign_bundle(
        tmp_path, compiled.archive_bytes, compiled.bundle_ref.bundle_digest
    )
    environment = signed.environment
    snapshot = environment.capabilities
    revoked, *other = snapshot.capabilities
    snapshot = snapshot.model_copy(
        update={"capabilities": (revoked.model_copy(update={"revoked_at": AT}), *other)}
    )
    rejected = submit(
        LocalBundleStore(tmp_path / "store"),
        signed.archive,
        "capability-revocation",
        replace(environment, capabilities=snapshot),
    )
    assert rejected.receipt is not None and not rejected.accepted
    assert "capability.unsupported" in {issue.code for issue in rejected.receipt.errors}
    assert "unsupported" in {
        item.status for item in rejected.receipt.capability_report.required
    }
    assert rejected.verification.evidence.capability_pin == snapshot.pin


def test_same_bundle_identity_cannot_be_rebound_to_new_signed_root(
    tmp_path: Path,
) -> None:
    compiled = build_scenario(tmp_path)
    first = sign_bundle(
        tmp_path, compiled.archive_bytes, compiled.bundle_ref.bundle_digest
    )

    def change_covered_metadata(
        manifest: dict[str, JSONValue], _payloads: dict[str, bytes]
    ) -> None:
        manifest["created_at"] = "2025-12-31T10:00:00Z"

    raw, root = rebuild_unsigned(compiled.archive_bytes, change_covered_metadata)
    assert root != first.digest
    second = sign_bundle(tmp_path, raw, root)
    store = LocalBundleStore(tmp_path / "store")
    original = submit(store, first.archive, "bind-first", first.environment)
    assert original.receipt is not None and original.accepted
    rejected = submit(store, second.archive, "try-new-root", second.environment)
    assert rejected.receipt is not None and not rejected.accepted
    assert rejected.receipt.bundle_digest == root
    assert tuple(issue.code for issue in rejected.receipt.errors) == (
        "bundle_identity_conflict",
    )
    assert store.read_archive(original.receipt.receipt_id) == first.archive
    with pytest.raises(StoreError):
        store.read_archive(rejected.receipt.receipt_id)
