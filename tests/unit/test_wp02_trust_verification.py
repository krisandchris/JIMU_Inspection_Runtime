"""Independent and ephemeral Ed25519 verification in the isolated test domain."""

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from fixtures.wp02.trust.support import (
    AT,
    ROOT,
    SIGNED_AT,
    make_signatures,
    manifest,
    pin,
    preimage,
    public_vector,
    trust_source,
)

from jimu_runtime.bundle.trust import (
    SourceEvidence,
    check_bundle_source,
    verify_bundle_trust,
)
from jimu_runtime.domain.runtime.bundle_manifest import SourceProvenance


@dataclass(frozen=True)
class IndependentSourceRegistry:
    """Synthetic injection of an already-authenticated independent proof."""

    proof: SourceEvidence

    def verify(
        self, provenance: SourceProvenance, root_digest: str, validation_at: datetime
    ) -> SourceEvidence | None:
        return self.proof


def test_precomputed_public_signature_and_independent_preimage(tmp_path: Path) -> None:
    key, public_key, signature, expected_preimage = public_vector()
    assert preimage(signature, ROOT) == expected_preimage
    host = trust_source(tmp_path, ((key, public_key),), (signature,))
    bundle = manifest((signature.ref,))
    result = verify_bundle_trust(bundle, ROOT, host, validation_at=AT)
    assert result.bundle_digest == ROOT
    assert result.snapshot_pin == pin("fixture.snapshot")
    assert result.policy_pin == pin("fixture.policy")
    assert result.signatures[0].ref == signature.ref
    assert result.signatures[0].key_pin == key.pin
    assert result.signatures[0].signed_at == SIGNED_AT
    assert result.signatures[0].signer_id == key.signer_id
    assert result.source_evidence is None
    assert host.reads == 1


def test_generated_quorum_uses_two_distinct_publishers(tmp_path: Path) -> None:
    one, public_one, (sig_one,) = make_signatures(tmp_path, signature_ids=("sig-one",))
    two, public_two, (sig_two,) = make_signatures(
        tmp_path,
        key_id="key-two",
        signer_id="Publisher-two",
        signature_ids=("sig-two",),
    )
    host = trust_source(
        tmp_path, ((one, public_one), (two, public_two)), (sig_one, sig_two), quorum=2
    )
    bundle = manifest((sig_two.ref, sig_one.ref))

    result = verify_bundle_trust(bundle, ROOT, host, validation_at=AT)

    assert tuple(row.signer_id for row in result.signatures) == (
        two.signer_id,
        one.signer_id,
    )
    assert tuple(tmp_path.iterdir()) == ()


def test_independent_provenance_proof_binds_exact_root_and_source(
    tmp_path: Path,
) -> None:
    key, public_key, signature, _ = public_vector()
    bundle = manifest((signature.ref,))
    proof = SourceEvidence(
        pin=pin("fixture.proof"), provenance=bundle.source, bundle_digest=ROOT
    )
    host = trust_source(
        tmp_path,
        ((key, public_key),),
        (signature,),
        source_policy="independent_attestation",
        authority=IndependentSourceRegistry(proof),
    )

    unsigned = manifest()
    assert check_bundle_source(unsigned, ROOT, host, validation_at=AT) == proof
    assert (
        verify_bundle_trust(bundle, ROOT, host, validation_at=AT).source_evidence
        == proof
    )


def test_preflight_and_verification_share_one_frozen_snapshot(tmp_path: Path) -> None:
    from jimu_runtime.bundle.trust import (
        resolve_trust_snapshot,
        verify_bundle_trust_at_snapshot,
    )

    key, public_key, evidence, _ = public_vector()
    host = trust_source(tmp_path, ((key, public_key),), (evidence,))
    snapshot = resolve_trust_snapshot(host, validation_at=AT)
    host.snapshot = None

    result = verify_bundle_trust_at_snapshot(
        manifest((evidence.ref,)), ROOT, snapshot, validation_at=AT
    )

    assert result.snapshot_pin == snapshot.configuration.pin
    assert result.verified_at == AT
    assert host.reads == 1
