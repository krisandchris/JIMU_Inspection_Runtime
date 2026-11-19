"""Fail closed on missing, misbound, invalid or revoked signature evidence."""

from dataclasses import replace
from datetime import timedelta
from hashlib import sha256
from pathlib import Path
from typing import Literal

import pytest
from fixtures.wp02.trust.support import (
    AT,
    ROOT,
    SIGNED_AT,
    make_signatures,
    manifest,
    pin,
    public_vector,
    trust_source,
)
from pydantic import ValidationError

from jimu_runtime.bundle.trust import (
    SignatureEvidence,
    SignatureProof,
    SigningKeyGrant,
    SourceEvidence,
    TrustError,
    verify_bundle_trust,
)

type Fault = Literal[
    "unsigned",
    "unavailable",
    "wrong_digest",
    "wrong_key_pin",
    "wrong_public_key",
    "wrong_root",
    "wrong_identity",
    "changed_time",
    "future",
    "subsecond",
    "expired",
    "unauthorized_source",
    "unauthorized_registry",
    "profile",
    "production",
]


@pytest.mark.parametrize(
    ("fault", "expected_code"),
    [
        ("unsigned", "trust.signature_missing"),
        ("unavailable", "trust.evidence_missing"),
        ("wrong_digest", "trust.evidence_digest"),
        ("wrong_key_pin", "trust.signer_unauthorized"),
        ("wrong_public_key", "trust.test_public_key"),
        ("wrong_root", "trust.test_signature_invalid"),
        ("wrong_identity", "trust.test_signature_invalid"),
        ("changed_time", "trust.test_signature_invalid"),
        ("future", "trust.signature_time"),
        ("subsecond", "trust.test_subsecond"),
        ("expired", "trust.signature_time"),
        ("unauthorized_source", "trust.signer_unauthorized"),
        ("unauthorized_registry", "trust.signer_unauthorized"),
        ("profile", "trust.signature_profile"),
        ("production", "trust.profile_mismatch"),
    ],
)
def test_detached_signature_rejection(
    fault: Fault, expected_code: str, tmp_path: Path
) -> None:
    vector_case = "different_root" if fault == "wrong_root" else "positive"
    key, public_key, signature, _ = public_vector(vector_case)
    if fault == "wrong_digest":
        signature = signature.model_copy(update={"payload": b"\x13" * 64})
    elif fault == "wrong_key_pin":
        signature = signature.model_copy(update={"key_pin": pin("some-other-key")})
    elif fault == "wrong_public_key":
        public_key = b"not the public key for this grant"
    elif fault == "wrong_identity":
        signature = signature.model_copy(
            update={"ref": signature.ref.model_copy(update={"signature_id": "changed"})}
        )
    elif fault == "changed_time":
        signature = signature.model_copy(
            update={"signed_at": SIGNED_AT + timedelta(seconds=1)}
        )
    elif fault == "future":
        signature = signature.model_copy(
            update={"signed_at": AT + timedelta(seconds=1)}
        )
    elif fault == "subsecond":
        signature = signature.model_copy(
            update={"signed_at": SIGNED_AT + timedelta(microseconds=1)}
        )
    elif fault == "expired":
        key = key.model_copy(update={"valid_until": AT})
    elif fault == "unauthorized_source":
        key = key.model_copy(update={"source_systems": ("unrelated.source",)})
    elif fault == "unauthorized_registry":
        key = key.model_copy(update={"signature_registry_ids": ("unrelated.registry",)})
    elif fault == "profile":
        signature = signature.model_copy(update={"profile_id": "some-other-profile"})
    bundle = manifest(() if fault == "unsigned" else (signature.ref,))
    host = trust_source(
        tmp_path, ((key, public_key),), () if fault == "unavailable" else (signature,)
    )
    if fault == "production":
        host.environment = "production"
    with pytest.raises(TrustError) as rejection:
        verify_bundle_trust(bundle, ROOT, host, validation_at=AT)
    assert rejection.value.issue.code == expected_code
    assert host.reads == 1


def test_provider_receipt_boolean_cannot_substitute_for_verified_claims(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from fixtures.wp02.trust.support import OpenSSLTestBackend

    key, public_key, signature, _ = public_vector()
    host = trust_source(tmp_path, ((key, public_key),), (signature,))

    def boolean_result(*args: object, **kwargs: object) -> bool:
        return True

    monkeypatch.setattr(OpenSSLTestBackend, "verify", boolean_result)
    with pytest.raises(TrustError) as rejection:
        verify_bundle_trust(manifest((signature.ref,)), ROOT, host, validation_at=AT)
    assert rejection.value.issue.code == "trust.backend_result"


def test_manifest_root_must_match_independently_computed_root(tmp_path: Path) -> None:
    key, public_key, signature, _ = public_vector()
    host = trust_source(tmp_path, ((key, public_key),), (signature,))
    with pytest.raises(TrustError) as rejection:
        verify_bundle_trust(
            manifest((signature.ref,)), "22" * 32, host, validation_at=AT
        )
    assert rejection.value.issue.code == "trust.root_mismatch"
    assert host.reads == 0


def test_same_bundle_revalidated_after_key_revocation(tmp_path: Path) -> None:
    key, public_key, signature, _ = public_vector()
    bundle = manifest((signature.ref,))
    host = trust_source(tmp_path, ((key, public_key),), (signature,))
    snapshot = host.snapshot
    assert snapshot is not None
    revoked_at = AT + timedelta(seconds=1)
    revoked_key = key.model_copy(update={"revoked_at": revoked_at})
    accepted = verify_bundle_trust(bundle, ROOT, host, validation_at=AT)
    refreshed_configuration = snapshot.configuration.model_copy(
        update={"keys": (revoked_key,), "pin": pin("fixture.snapshot", "ee" * 32)}
    )
    host.snapshot = replace(snapshot, configuration=refreshed_configuration)

    with pytest.raises(TrustError) as rejection:
        verify_bundle_trust(bundle, ROOT, host, validation_at=revoked_at)
    assert rejection.value.issue.code == "trust.signer_revoked"
    assert accepted.signatures[0].key_pin == key.pin
    assert host.reads == 2


def test_multiple_keys_for_same_publisher_do_not_satisfy_quorum(tmp_path: Path) -> None:
    first, public_first, (sig_first,) = make_signatures(
        tmp_path, signature_ids=("sig-first",)
    )
    second, public_second, (sig_second,) = make_signatures(
        tmp_path, key_id="second", signature_ids=("sig-second",)
    )
    host = trust_source(
        tmp_path,
        ((first, public_first), (second, public_second)),
        (sig_first, sig_second),
        quorum=2,
    )
    with pytest.raises(TrustError) as rejection:
        verify_bundle_trust(
            manifest((sig_first.ref, sig_second.ref)), ROOT, host, validation_at=AT
        )
    assert rejection.value.issue.code == "trust.signature_quorum"


def test_invalid_extra_signature_rejects_even_when_quorum_already_met(
    tmp_path: Path,
) -> None:
    key, public_key, valid_signature, _ = public_vector()
    raw_invalid = bytes([valid_signature.payload[0] ^ 1]) + valid_signature.payload[1:]
    invalid = valid_signature.model_copy(
        update={
            "ref": valid_signature.ref.model_copy(
                update={
                    "signature_id": "extra",
                    "sha256": sha256(raw_invalid).hexdigest(),
                }
            ),
            "payload": raw_invalid,
        }
    )
    host = trust_source(tmp_path, ((key, public_key),), (valid_signature, invalid))
    bundle = manifest((valid_signature.ref, invalid.ref))
    with pytest.raises(TrustError) as rejection:
        verify_bundle_trust(bundle, ROOT, host, validation_at=AT)
    assert rejection.value.issue.code == "trust.test_signature_invalid"
    assert rejection.value.issue.path == ("integrity", "signatures", 1)


def test_no_trust_snapshot_or_source_proof_rejects(tmp_path: Path) -> None:
    key, public_key, signature, _ = public_vector()
    host = trust_source(
        tmp_path,
        ((key, public_key),),
        (signature,),
        source_policy="independent_attestation",
    )
    bundle = manifest((signature.ref,))
    with pytest.raises(TrustError) as missing:
        verify_bundle_trust(bundle, ROOT, host, validation_at=AT)
    assert missing.value.issue.code == "trust.source_authority_missing"

    class OtherSourceProof:
        def verify(self, *args: object) -> SourceEvidence:
            return SourceEvidence(
                pin=pin("other.source.pin"),
                provenance=bundle.source,
                bundle_digest="22" * 32,
            )

    snapshot = host.snapshot
    assert snapshot is not None
    host.snapshot = replace(snapshot, source_authority=OtherSourceProof())
    with pytest.raises(TrustError) as mismatch:
        verify_bundle_trust(bundle, ROOT, host, validation_at=AT)
    assert mismatch.value.issue.code == "trust.source_evidence_mismatch"

    host.snapshot = None
    with pytest.raises(TrustError) as unavailable:
        verify_bundle_trust(bundle, ROOT, host, validation_at=AT)
    assert unavailable.value.issue.code == "trust.snapshot_missing"


def test_backend_cannot_return_proof_of_another_root(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from fixtures.wp02.trust.support import OpenSSLTestBackend

    key, public_key, signature, _ = public_vector()
    host = trust_source(tmp_path, ((key, public_key),), (signature,))

    def wrong_proof(
        self: OpenSSLTestBackend,
        evidence: SignatureEvidence,
        key: SigningKeyGrant,
        root: str,
    ) -> SignatureProof:
        return SignatureProof(signature.ref, signature.key_pin, SIGNED_AT, "22" * 32)

    monkeypatch.setattr(OpenSSLTestBackend, "verify", wrong_proof)
    with pytest.raises(TrustError) as mismatch:
        verify_bundle_trust(manifest((signature.ref,)), ROOT, host, validation_at=AT)
    assert mismatch.value.issue.code == "trust.backend_result"


def test_missing_revocation_state_invalidates_signing_grant() -> None:
    key, _, _, _ = public_vector()
    incomplete = key.model_dump(exclude={"revoked_at"})
    with pytest.raises(ValidationError, match="revoked_at"):
        SigningKeyGrant.model_validate(incomplete)


def test_frozen_snapshot_cannot_cross_verification_instants(tmp_path: Path) -> None:
    from jimu_runtime.bundle.trust import (
        resolve_trust_snapshot,
        verify_bundle_trust_at_snapshot,
    )

    key, public_key, evidence, _ = public_vector()
    host = trust_source(tmp_path, ((key, public_key),), (evidence,))
    snapshot = resolve_trust_snapshot(host, validation_at=AT)
    with pytest.raises(TrustError) as rejection:
        verify_bundle_trust_at_snapshot(
            manifest((evidence.ref,)),
            ROOT,
            snapshot,
            validation_at=AT + timedelta(seconds=1),
        )
    assert rejection.value.issue.code == "trust.snapshot_instant"


def test_unreadable_detached_evidence_is_structured_rejection(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from fixtures.wp02.trust.support import OpenSSLTestBackend

    key, public_key, evidence, _ = public_vector()
    host = trust_source(tmp_path, ((key, public_key),), (evidence,))

    def unavailable(*args: object, **kwargs: object) -> None:
        raise OSError("host backend detail")

    monkeypatch.setattr(OpenSSLTestBackend, "resolve", unavailable)
    with pytest.raises(TrustError) as rejection:
        verify_bundle_trust(manifest((evidence.ref,)), ROOT, host, validation_at=AT)
    assert rejection.value.issue.code == "trust.evidence_unreadable"
    assert "host backend detail" not in str(rejection.value)
