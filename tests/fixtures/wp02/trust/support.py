"""OpenSSL-backed test domain and synthetic trust registry snapshots."""

import json
import os
import shutil
import subprocess
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Literal

from jimu_runtime.bundle.trust import (
    SignatureEvidence,
    SignatureProof,
    SigningKeyGrant,
    SourceAuthority,
    SourceAuthorization,
    TrustConfiguration,
    TrustEnvironment,
    TrustError,
    TrustPin,
    TrustPolicy,
    TrustSnapshot,
)
from jimu_runtime.domain.runtime.bundle_manifest import (
    BundleManifest,
    CompatibilityRequirement,
    DetachedSignatureRef,
    IntegrityEnvelope,
    SourceProvenance,
)

PROFILE = "jimu-wp02-test-ed25519-v1"
REGISTRY = "fixture.signature"
SOURCE = "fixture.source"
VERSION = "1.0.0"
SIGNED_AT = datetime(2026, 1, 1, tzinfo=UTC)
AT = SIGNED_AT + timedelta(seconds=10)
START = SIGNED_AT - timedelta(days=1)
END = SIGNED_AT + timedelta(days=1)
ROOT = "11" * 32
ED25519_SPKI_PREFIX = bytes.fromhex("302a300506032b6570032100")


def pin(identity: str, digest: str = "a1" * 32) -> TrustPin:
    return TrustPin(identity=identity, version=VERSION, sha256=digest)


def manifest(
    refs: tuple[DetachedSignatureRef, ...] = (),
    *,
    root: str = ROOT,
    source: str = SOURCE,
) -> BundleManifest:
    return BundleManifest(
        format="jimu.runtime-bundle",
        schema_version=VERSION,
        bundle_id="test.bundle",
        bundle_version=VERSION,
        compiler_version=VERSION,
        created_at=START,
        source=SourceProvenance(source_system=source, artifacts=()),
        artifacts=(),
        compatibility=CompatibilityRequirement(
            minimum_runtime_version_inclusive="1.2.0",
            maximum_runtime_version_exclusive="1.12.0",
        ),
        required_capabilities=(),
        integrity=IntegrityEnvelope(
            hash_algorithm="sha256",
            canonicalization_id="jcs-sha256-v1",
            root_digest=root,
            signatures=refs,
        ),
    )


def preimage(evidence: SignatureEvidence, root: str) -> bytes:
    signed_at = evidence.signed_at
    if signed_at.microsecond:
        raise TrustError(
            "trust.test_subsecond", "The test profile requires whole UTC seconds."
        )
    values = (
        evidence.ref.signature_registry_id,
        evidence.ref.signature_id,
        evidence.ref.version,
        evidence.key_pin.identity,
        evidence.key_pin.version,
    )
    frame = bytearray(b"JIMU\x00WP02\x00TEST-ED25519\x00V1\x00")
    frame.extend(bytes.fromhex(root))
    for value in values:
        encoded = value.encode("utf-8")
        frame.extend(len(encoded).to_bytes(4, "big"))
        frame.extend(encoded)
    seconds = (signed_at - datetime(1970, 1, 1, tzinfo=UTC)) // timedelta(seconds=1)
    frame.extend(seconds.to_bytes(8, "big", signed=True))
    return bytes(frame)


def _openssl(
    *argv: str, output_file: Path | None = None
) -> subprocess.CompletedProcess[bytes]:
    executable = shutil.which("openssl")
    if executable is None:
        raise RuntimeError("OpenSSL 3 is required for isolated Ed25519 tests")
    binary = str(Path(executable).resolve(strict=True))
    params = dict(
        env={"OPENSSL_CONF": "/dev/null", "LC_ALL": "C"},
        stdin=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        timeout=10,
        check=False,
    )
    if output_file is None:
        return subprocess.run((binary, *argv), stdout=subprocess.PIPE, **params)
    flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
    with os.fdopen(os.open(output_file, flags, 0o600), "wb") as output:
        return subprocess.run((binary, *argv), stdout=output, **params)


def key_grant(key_pin: TrustPin, signer_id: str) -> SigningKeyGrant:
    return SigningKeyGrant(
        pin=key_pin,
        signer_id=signer_id,
        profile_id=PROFILE,
        signature_registry_ids=(REGISTRY,),
        source_systems=(SOURCE,),
        valid_from=START,
        valid_until=END,
        revoked_at=None,
    )


def _with_signature(evidence: SignatureEvidence, signature: bytes) -> SignatureEvidence:
    ref = evidence.ref.model_copy(update={"sha256": sha256(signature).hexdigest()})
    return evidence.model_copy(update={"ref": ref, "payload": signature})


def make_signatures(
    directory: Path,
    *,
    key_id: str = "ephemeral",
    signer_id: str = "Publisher-one",
    signature_ids: tuple[str, ...] = ("sig-ephemeral",),
    root: str = ROOT,
) -> tuple[SigningKeyGrant, bytes, tuple[SignatureEvidence, ...]]:
    """Generate and discard one isolated test private key before returning."""
    with TemporaryDirectory(dir=directory) as staging:
        root_path = Path(staging)
        private_key_path = root_path / "private.pem"
        try:
            generated = _openssl(
                "genpkey", "-algorithm", "Ed25519", output_file=private_key_path
            )
            if generated.returncode != 0:
                raise RuntimeError("Could not generate isolated test key")
            if private_key_path.stat().st_mode & 0o777 != 0o600:
                raise RuntimeError("Isolated test private key has unsafe permissions")
            exported = _openssl(
                "pkey", "-in", str(private_key_path), "-outform", "DER", "-pubout"
            )
            if exported.returncode != 0:
                raise RuntimeError("Could not export isolated test public key")
            public_der = exported.stdout
            key_pin = pin(key_id, sha256(public_der).hexdigest())
            evidence_entries = []
            for signature_id in signature_ids:
                placeholder = SignatureEvidence(
                    ref=DetachedSignatureRef(
                        signature_id=signature_id,
                        version=VERSION,
                        sha256="00" * 32,
                        signature_registry_id=REGISTRY,
                    ),
                    profile_id=PROFILE,
                    key_pin=key_pin,
                    signed_at=SIGNED_AT,
                    payload=b"placeholder until signing",
                )
                message = root_path / "preimage.bin"
                message.write_bytes(preimage(placeholder, root))
                signed = _openssl(
                    "pkeyutl",
                    "-sign",
                    "-rawin",
                    "-inkey",
                    str(private_key_path),
                    "-in",
                    str(message),
                )
                if signed.returncode != 0 or len(signed.stdout) != 64:
                    raise RuntimeError(
                        "Could not create 64-byte isolated Ed25519 signature"
                    )
                evidence_entries.append(_with_signature(placeholder, signed.stdout))
        finally:
            private_key_path.unlink(missing_ok=True)
    return key_grant(key_pin, signer_id), public_der, tuple(evidence_entries)


@dataclass(frozen=True)
class OpenSSLTestBackend:
    temp_dir: Path
    evidence: tuple[SignatureEvidence, ...]
    public_keys: tuple[tuple[TrustPin, bytes], ...]
    environment: TrustEnvironment = "isolated_test"
    profile_id: str = PROFILE

    def resolve(
        self, ref: DetachedSignatureRef, *, max_bytes: int
    ) -> SignatureEvidence | None:
        return next(
            (
                item
                for item in self.evidence
                if item.ref == ref and len(item.payload) <= max_bytes
            ),
            None,
        )

    def verify(
        self, evidence: SignatureEvidence, key: SigningKeyGrant, root_digest: str
    ) -> SignatureProof:
        public_der = dict(self.public_keys).get(key.pin)
        if public_der is None or sha256(public_der).hexdigest() != key.pin.sha256:
            raise TrustError(
                "trust.test_public_key",
                "Isolated Ed25519 public key does not match the granted pin.",
            )
        if (
            not public_der.startswith(ED25519_SPKI_PREFIX)
            or len(public_der) != len(ED25519_SPKI_PREFIX) + 32
        ):
            raise TrustError(
                "trust.test_public_key_algorithm",
                "The test profile requires an Ed25519 SPKI DER public key.",
            )
        if len(evidence.payload) != 64:
            raise TrustError(
                "trust.test_signature_length",
                "Isolated Ed25519 signature requires 64 raw bytes.",
            )
        with TemporaryDirectory(dir=self.temp_dir) as staging:
            artifacts = Path(staging)
            key_file = artifacts / "public.der"
            key_file.write_bytes(public_der)
            signed_message = artifacts / "preimage.bin"
            signed_message.write_bytes(preimage(evidence, root_digest))
            signature = artifacts / "signature.bin"
            signature.write_bytes(evidence.payload)
            verified = _openssl(
                "pkeyutl",
                "-verify",
                "-rawin",
                "-pubin",
                "-keyform",
                "DER",
                "-inkey",
                str(key_file),
                "-in",
                str(signed_message),
                "-sigfile",
                str(signature),
            )
        if verified.returncode != 0:
            raise TrustError(
                "trust.test_signature_invalid",
                "Isolated Ed25519 signature verification failed.",
            )
        return SignatureProof(evidence.ref, key.pin, evidence.signed_at, root_digest)


@dataclass
class FixtureTrustSource:
    snapshot: TrustSnapshot | None
    environment: TrustEnvironment = "isolated_test"
    reads: int = 0

    def snapshot_at(self, validation_at: datetime) -> TrustSnapshot | None:
        self.reads += 1
        return self.snapshot


def trust_source(
    tmp_path: Path,
    keys: tuple[tuple[SigningKeyGrant, bytes], ...],
    entries: tuple[SignatureEvidence, ...],
    *,
    quorum: int = 1,
    authority: SourceAuthority | None = None,
    source_policy: Literal[
        "signer_authorized", "independent_attestation"
    ] = "signer_authorized",
) -> FixtureTrustSource:
    config = TrustConfiguration(
        pin=pin("fixture.snapshot"),
        policy=TrustPolicy(
            pin=pin("fixture.policy"),
            profile_id=PROFILE,
            environment="isolated_test",
            minimum_distinct_signers=quorum,
            max_signature_evidence_bytes=64,
            sources=(
                SourceAuthorization(source_system=SOURCE, evidence=source_policy),
            ),
        ),
        keys=tuple(key for key, _ in keys),
        valid_from=START,
        valid_until=END,
    )
    backend = OpenSSLTestBackend(
        tmp_path, entries, tuple((key.pin, public_key) for key, public_key in keys)
    )
    return FixtureTrustSource(TrustSnapshot(config, backend, authority))


def public_vector(
    case: Literal["positive", "different_root"] = "positive",
) -> tuple[SigningKeyGrant, bytes, SignatureEvidence, bytes]:
    vectors = json.loads(Path(__file__).with_name("public_vectors.json").read_text())
    registry, signature_id, sig_ver, key_id, key_ver = vectors["fields"]
    key_pin = TrustPin(
        identity=key_id, version=key_ver, sha256=vectors["public_key_sha256"]
    )
    vector = vectors[case]
    signature = SignatureEvidence(
        ref=DetachedSignatureRef(
            signature_registry_id=registry,
            signature_id=signature_id,
            version=sig_ver,
            sha256=vector["signature_sha256"],
        ),
        profile_id=vectors["profile_id"],
        key_pin=key_pin,
        signed_at=datetime.fromtimestamp(vectors["signed_at_unix_seconds"], UTC),
        payload=bytes.fromhex(vector["signature_hex"]),
    )
    return (
        key_grant(key_pin, "Publisher-fixed"),
        bytes.fromhex(vectors["public_key_spki_der_hex"]),
        signature,
        bytes.fromhex(vector["preimage_hex"]),
    )
