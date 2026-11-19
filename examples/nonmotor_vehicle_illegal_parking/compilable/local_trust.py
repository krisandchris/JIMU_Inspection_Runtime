"""An isolated Ed25519 signing host for the example's private local store."""

import shutil
import subprocess
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import cast

from jimu_runtime.bundle.integrity import sha256_digest
from jimu_runtime.bundle.trust import (
    SignatureBackend,
    SignatureEvidence,
    SignatureProof,
    SigningKeyGrant,
    SourceAuthorization,
    TrustConfiguration,
    TrustEnvironment,
    TrustError,
    TrustPin,
    TrustPolicy,
    TrustSnapshot,
    TrustSource,
)
from jimu_runtime.domain.runtime.bundle_manifest import DetachedSignatureRef

from .scene import AT, END, SOURCE, START, VERSION, configuration_pin

PROFILE = "jimu-wp02-test-nonmotor-ed25519-v1"
REGISTRY = "synthetic.scene.signatures"
ED25519_SPKI_PREFIX = bytes.fromhex("302a300506032b6570032100")


def _pin(identity: str, content: bytes) -> TrustPin:
    return TrustPin(identity=identity, version=VERSION, sha256=sha256_digest(content))


def _openssl(*args: str) -> subprocess.CompletedProcess[bytes]:
    path = shutil.which("openssl")
    if path is None:
        raise RuntimeError("The isolated signing demo requires OpenSSL 3")
    return subprocess.run(
        (path, *args),
        stdin=subprocess.DEVNULL,
        capture_output=True,
        env={"OPENSSL_CONF": "/dev/null", "LC_ALL": "C"},
        check=False,
        timeout=10,
    )


def _message(signature: SignatureEvidence, root_digest: str) -> bytes:
    fields = (
        signature.profile_id,
        signature.ref.signature_registry_id,
        signature.ref.signature_id,
        signature.ref.version,
        signature.key_pin.identity,
        signature.key_pin.version,
        signature.key_pin.sha256,
        signature.signed_at.isoformat(),
        root_digest,
    )
    return b"JIMU\x00SYNTHETIC-PARKING-ED25519\x00V1\x00" + b"".join(
        len(part).to_bytes(4, "big") + part for part in map(str.encode, fields)
    )


# These declarations are intentionally read-only. The host protocols only read them.
@dataclass(frozen=True, slots=True)
class LocalEd25519:
    directory: Path
    grant: SigningKeyGrant | None = None
    public_der: bytes = b""
    signature: SignatureEvidence | None = None
    environment: TrustEnvironment = field(init=False, default="isolated_test")
    profile_id: str = field(init=False, default=PROFILE)

    def as_trust_source(self) -> TrustSource:
        return cast(TrustSource, self)

    def resolve(
        self, ref: DetachedSignatureRef, *, max_bytes: int
    ) -> SignatureEvidence | None:
        if (
            self.signature is not None
            and self.signature.ref == ref
            and len(self.signature.payload) <= max_bytes
        ):
            return self.signature
        return None

    def verify(
        self, evidence: SignatureEvidence, key: SigningKeyGrant, root_digest: str
    ) -> SignatureProof:
        if sha256_digest(evidence.payload) != evidence.ref.sha256:
            raise TrustError(
                "trust.scene_signature_digest", "Detached signature digest mismatch"
            )
        if (
            _pin(key.pin.identity, self.public_der) != key.pin
            or evidence.key_pin != key.pin
            or not self.public_der.startswith(ED25519_SPKI_PREFIX)
            or len(self.public_der) != len(ED25519_SPKI_PREFIX) + 32
            or len(evidence.payload) != 64
        ):
            raise TrustError("trust.scene_key_invalid", "Invalid isolated key or proof")
        with TemporaryDirectory(dir=self.directory) as staged:
            directory = Path(staged)
            public = directory / "public.der"
            public.write_bytes(self.public_der)
            message = directory / "message"
            message.write_bytes(_message(evidence, root_digest))
            signature = directory / "signature"
            signature.write_bytes(evidence.payload)
            verified = _openssl(
                "pkeyutl",
                "-verify",
                "-rawin",
                "-pubin",
                "-keyform",
                "DER",
                "-inkey",
                str(public),
                "-in",
                str(message),
                "-sigfile",
                str(signature),
            )
        if verified.returncode != 0:
            raise TrustError(
                "trust.scene_signature_invalid", "Invalid Ed25519 signature"
            )
        return SignatureProof(evidence.ref, key.pin, evidence.signed_at, root_digest)

    def snapshot_at(self, validation_at: datetime) -> TrustSnapshot | None:
        if not START <= validation_at < END:
            return None
        policy = TrustPolicy(
            pin=_pin("synthetic.scene.policy", b""),
            environment=self.environment,
            profile_id=self.profile_id,
            minimum_distinct_signers=1,
            max_signature_evidence_bytes=64,
            sources=(
                SourceAuthorization(source_system=SOURCE, evidence="signer_authorized"),
            ),
        )
        configuration = TrustConfiguration(
            pin=_pin("synthetic.scene.trust", b""),
            policy=policy.model_copy(update={"pin": configuration_pin(policy)}),
            valid_from=START,
            valid_until=END,
            keys=() if self.grant is None else (self.grant,),
        )
        return TrustSnapshot(
            configuration.model_copy(update={"pin": configuration_pin(configuration)}),
            cast(SignatureBackend, self),
        )


def sign_bundle(
    directory: Path, root_digest: str
) -> tuple[LocalEd25519, DetachedSignatureRef]:
    """Sign one logical root and return only its public verifier and detached ref."""
    with TemporaryDirectory(dir=directory) as staged:
        staging = Path(staged)
        key_file = staging / "private.pem"
        key = _openssl("genpkey", "-algorithm", "Ed25519")
        if key.returncode != 0:
            raise RuntimeError("Unable to generate isolated Ed25519 key")
        with open(key_file, "xb", opener=_private_file) as key_stream:
            key_stream.write(key.stdout)
        public = _openssl("pkey", "-in", str(key_file), "-outform", "DER", "-pubout")
        if public.returncode != 0:
            raise RuntimeError("Unable to export isolated Ed25519 public key")
        pin = _pin("synthetic.scene.ephemeral-key", public.stdout)
        grant = SigningKeyGrant(
            pin=pin,
            signer_id="synthetic.local.publisher",
            profile_id=PROFILE,
            signature_registry_ids=(REGISTRY,),
            source_systems=(SOURCE,),
            valid_from=START,
            valid_until=END,
            revoked_at=None,
        )
        ref = DetachedSignatureRef(
            signature_id="synthetic.scene.signature",
            version=VERSION,
            signature_registry_id=REGISTRY,
            sha256="0" * 64,
        )
        data = SignatureEvidence(
            ref=ref, profile_id=PROFILE, key_pin=pin, signed_at=AT, payload=b"pending"
        )
        message = staging / "message"
        message.write_bytes(_message(data, root_digest))
        signed = _openssl(
            "pkeyutl", "-sign", "-rawin", "-inkey", str(key_file), "-in", str(message)
        )
        if signed.returncode != 0 or len(signed.stdout) != 64:
            raise RuntimeError("Unable to sign the isolated Bundle root")
    ref = ref.model_copy(update={"sha256": sha256_digest(signed.stdout)})
    evidence = data.model_copy(update={"ref": ref, "payload": signed.stdout})
    return LocalEd25519(directory, grant, public.stdout, evidence), ref


def _private_file(path: str, flags: int) -> int:
    import os

    return os.open(path, flags, 0o600)
