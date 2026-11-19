"""Verify a recomputed Bundle root against deployment-owned trust.

The application resolves an explicit trust policy and snapshot on each
verification. Evidence storage, signature formats, cryptographic backends and
source attestation authorities are supplied by the trusted deployment.
"""

from dataclasses import dataclass, replace
from hashlib import sha256
from typing import Annotated, Literal, Protocol, Self

from pydantic import (
    BeforeValidator,
    Field,
    TypeAdapter,
    ValidationError,
    model_validator,
)

from jimu_runtime.domain.base import ContractModel
from jimu_runtime.domain.definition_support import (
    PositiveBudget,
    UTCDateTime,
    immutable_sequence,
)
from jimu_runtime.domain.primitives import ArtifactId, Digest, RecordId, Version
from jimu_runtime.domain.runtime.bundle_manifest import (
    BundleManifest,
    DetachedSignatureRef,
    SourceProvenance,
)
from jimu_runtime.domain.validation.issues import IssuePath, ValidationIssue

type TrustEnvironment = Literal["isolated_test", "production"]

_TEST_PROFILE_PREFIX = "jimu-wp02-test-"
_UTC = TypeAdapter[UTCDateTime](UTCDateTime)
_DIGEST = TypeAdapter[Digest](Digest)


class TrustError(ValueError):
    """A safe structured rejection without raw evidence or backend output."""

    def __init__(self, code: str, message: str, path: IssuePath = ()) -> None:
        super().__init__(message)
        self.issue = ValidationIssue(
            code=code, message=message, path=path, related_refs=()
        )


class TrustPin(ContractModel):
    """Host-managed identity of an immutable policy, snapshot or public key."""

    identity: ArtifactId
    version: Version
    sha256: Digest


class SourceAuthorization(ContractModel):
    source_system: ArtifactId
    evidence: Literal["signer_authorized", "independent_attestation"]


class TrustPolicy(ContractModel):
    pin: TrustPin
    environment: TrustEnvironment
    profile_id: ArtifactId
    minimum_distinct_signers: Annotated[int, Field(ge=1, le=64)]
    max_signature_evidence_bytes: PositiveBudget
    sources: Annotated[
        tuple[SourceAuthorization, ...],
        Field(min_length=1, max_length=1024),
        BeforeValidator(immutable_sequence),
    ]

    @model_validator(mode="after")
    def validate_trust_profile(self) -> Self:
        if self.profile_id.startswith(_TEST_PROFILE_PREFIX) != (
            self.environment == "isolated_test"
        ):
            raise ValueError("trust profile must match its deployment environment")
        if len({entry.source_system for entry in self.sources}) != len(self.sources):
            raise ValueError("source-system authorizations must be unique")
        return self


class SigningKeyGrant(ContractModel):
    pin: TrustPin
    signer_id: RecordId
    profile_id: ArtifactId
    signature_registry_ids: Annotated[
        tuple[ArtifactId, ...],
        Field(min_length=1, max_length=1024),
        BeforeValidator(immutable_sequence),
    ]
    source_systems: Annotated[
        tuple[ArtifactId, ...],
        Field(min_length=1, max_length=1024),
        BeforeValidator(immutable_sequence),
    ]
    valid_from: UTCDateTime
    valid_until: UTCDateTime
    # The host must explicitly supply the status from its revocation snapshot.
    revoked_at: UTCDateTime | None

    @model_validator(mode="after")
    def ordered_and_unique(self) -> Self:
        if self.valid_from >= self.valid_until:
            raise ValueError("signing key window must be ordered")
        if len(set(self.signature_registry_ids)) != len(self.signature_registry_ids):
            raise ValueError("allowed signature registries must be unique")
        if len(set(self.source_systems)) != len(self.source_systems):
            raise ValueError("allowed signing sources must be unique")
        return self


class SignatureEvidence(ContractModel):
    """Resolved detached bytes and claims that the profile must authenticate."""

    ref: DetachedSignatureRef
    profile_id: ArtifactId
    key_pin: TrustPin
    signed_at: UTCDateTime
    payload: Annotated[bytes, Field(repr=False, min_length=1)]


@dataclass(frozen=True, slots=True)
class SignatureProof:
    """Claims authenticated by an externally supplied cryptographic backend."""

    ref: DetachedSignatureRef
    key_pin: TrustPin
    signed_at: UTCDateTime
    root_digest: Digest


class SignatureBackend(Protocol):
    environment: TrustEnvironment
    profile_id: str

    def resolve(
        self, ref: DetachedSignatureRef, *, max_bytes: int
    ) -> SignatureEvidence | None:
        """Resolve one exact ref in the external bounded evidence registry."""

    def verify(
        self, evidence: SignatureEvidence, key: SigningKeyGrant, root_digest: Digest
    ) -> SignatureProof:
        """Verify bytes, public-key pin and signed claims or raise TrustError.

        Verification authenticates the recomputed root and exact signature
        registry identity, key identity/version and signed timestamp. The
        backend must raise on any invalid, unverifiable or unsupported input.
        """


class SourceEvidence(ContractModel):
    """An externally verified attestation and the exact claims it covers."""

    pin: TrustPin
    provenance: SourceProvenance
    bundle_digest: Digest


class SourceAuthority(Protocol):
    def verify(
        self,
        provenance: SourceProvenance,
        bundle_digest: Digest,
        validation_at: UTCDateTime,
    ) -> SourceEvidence | None:
        """Resolve and authenticate immutable proof or fail with TrustError."""


class TrustConfiguration(ContractModel):
    pin: TrustPin
    policy: TrustPolicy
    valid_from: UTCDateTime
    valid_until: UTCDateTime
    keys: Annotated[
        tuple[SigningKeyGrant, ...],
        Field(max_length=1024),
        BeforeValidator(immutable_sequence),
    ]

    @model_validator(mode="after")
    def unique_keys(self) -> Self:
        if self.valid_from >= self.valid_until:
            raise ValueError("trust snapshot window must be ordered")
        identities = [(key.pin.identity, key.pin.version) for key in self.keys]
        fingerprints = [key.pin.sha256 for key in self.keys]
        if len(set(identities)) != len(identities) or len(set(fingerprints)) != len(
            fingerprints
        ):
            raise ValueError(
                "snapshot signing identities and key fingerprints must be unique"
            )
        if any(key.profile_id != self.policy.profile_id for key in self.keys):
            raise ValueError("snapshot signing keys must use its trust policy profile")
        return self


@dataclass(frozen=True, slots=True)
class TrustSnapshot:
    configuration: TrustConfiguration
    signatures: SignatureBackend
    source_authority: SourceAuthority | None = None
    resolved_at: UTCDateTime | None = None


class TrustSource(Protocol):
    environment: TrustEnvironment

    def snapshot_at(self, validation_at: UTCDateTime) -> TrustSnapshot | None:
        """Read current authoritative state for this new admission attempt."""


@dataclass(frozen=True, slots=True)
class VerifiedSignature:
    ref: DetachedSignatureRef
    signer_id: RecordId
    key_pin: TrustPin
    signed_at: UTCDateTime


@dataclass(frozen=True, slots=True)
class TrustVerification:
    policy_pin: TrustPin
    snapshot_pin: TrustPin
    profile_id: ArtifactId
    bundle_digest: Digest
    verified_at: UTCDateTime
    signatures: tuple[VerifiedSignature, ...]
    source_evidence: SourceEvidence | None


def _verify_signature(
    ref: DetachedSignatureRef,
    snapshot: TrustSnapshot,
    keys: dict[TrustPin, SigningKeyGrant],
    source_system: ArtifactId,
    root_digest: Digest,
    at: UTCDateTime,
    index: int,
) -> VerifiedSignature:
    policy = snapshot.configuration.policy
    path = ("integrity", "signatures", index)
    try:
        evidence = snapshot.signatures.resolve(
            ref, max_bytes=policy.max_signature_evidence_bytes
        )
        if evidence is None:
            raise TrustError(
                "trust.evidence_missing", "Detached signature evidence is missing."
            )
        evidence = SignatureEvidence.model_validate(evidence)
        if (
            evidence.ref != ref
            or len(evidence.payload) > policy.max_signature_evidence_bytes
        ):
            raise TrustError(
                "trust.evidence_ref",
                "Detached signature evidence conflicts with the expected identity or bound.",
            )
        if sha256(evidence.payload).hexdigest() != ref.sha256:
            raise TrustError(
                "trust.evidence_digest",
                "Detached signature evidence has a different SHA-256.",
            )
        if evidence.profile_id != policy.profile_id:
            raise TrustError(
                "trust.signature_profile",
                "Detached evidence uses a different trust profile.",
            )
        key = keys.get(evidence.key_pin)
        if (
            key is None
            or source_system not in key.source_systems
            or ref.signature_registry_id not in key.signature_registry_ids
        ):
            raise TrustError(
                "trust.signer_unauthorized",
                "Signing key is unauthorized for the source or registry.",
            )
        if key.revoked_at is not None and key.revoked_at <= at:
            raise TrustError(
                "trust.signer_revoked", "Signing key is revoked at verification time."
            )
        if not key.valid_from <= evidence.signed_at <= at < key.valid_until:
            raise TrustError(
                "trust.signature_time",
                "The signature time or signing-key validity window is invalid.",
            )
        verification_result = snapshot.signatures.verify(evidence, key, root_digest)
        if type(
            verification_result
        ) is not SignatureProof or verification_result != SignatureProof(
            ref, key.pin, evidence.signed_at, root_digest
        ):
            raise TrustError(
                "trust.backend_result",
                "Signature backend did not authenticate the exact required claims.",
            )
    except TrustError as exc:
        raise TrustError(exc.issue.code, exc.issue.message, path) from None
    except (OSError, ValidationError):
        raise TrustError(
            "trust.evidence_unreadable",
            "Detached signature evidence could not be read or validated.",
            path,
        ) from None
    return VerifiedSignature(ref, key.signer_id, key.pin, evidence.signed_at)


def _verify_source(
    snapshot: TrustSnapshot,
    manifest: BundleManifest,
    root_digest: Digest,
    at: UTCDateTime,
) -> SourceEvidence | None:
    authorization = next(
        (
            entry
            for entry in snapshot.configuration.policy.sources
            if entry.source_system == manifest.source.source_system
        ),
        None,
    )
    if authorization is None:
        raise TrustError(
            "trust.source_unauthorized",
            "Trust policy does not authorize the Bundle's provenance source.",
            ("source",),
        )
    if authorization.evidence == "signer_authorized":
        return None
    if snapshot.source_authority is None:
        raise TrustError(
            "trust.source_authority_missing",
            "Independent provenance authority is required.",
            ("source",),
        )
    try:
        evidence = snapshot.source_authority.verify(manifest.source, root_digest, at)
        if evidence is None:
            raise TrustError(
                "trust.source_evidence_missing",
                "Independent provenance evidence is required.",
                ("source",),
            )
        evidence = SourceEvidence.model_validate(evidence)
    except (OSError, ValidationError):
        raise TrustError(
            "trust.source_evidence_unreadable",
            "Provenance evidence could not be read or validated.",
            ("source",),
        ) from None
    if evidence.provenance != manifest.source or evidence.bundle_digest != root_digest:
        raise TrustError(
            "trust.source_evidence_mismatch",
            "Independent evidence has different provenance or Bundle root.",
            ("source",),
        )
    return evidence


def _verified_root(manifest: BundleManifest, root_digest: Digest) -> Digest:
    root = _DIGEST.validate_python(root_digest)
    if root != manifest.integrity.root_digest:
        raise TrustError(
            "trust.root_mismatch",
            "Manifest root differs from the recomputed Bundle root.",
            ("integrity", "root_digest"),
        )
    return root


def resolve_trust_snapshot(
    trust_source: TrustSource, *, validation_at: UTCDateTime
) -> TrustSnapshot:
    """Read and pin one host trust snapshot for a new verification operation."""
    at = _UTC.validate_python(validation_at)
    try:
        snapshot = trust_source.snapshot_at(at)
        if snapshot is None:
            raise TrustError(
                "trust.snapshot_missing",
                "No authoritative trust snapshot is available.",
            )
        config = TrustConfiguration.model_validate(snapshot.configuration)
    except (OSError, ValidationError):
        raise TrustError(
            "trust.snapshot_invalid",
            "The trust snapshot could not be read or validated.",
        ) from None
    policy = config.policy
    if not config.valid_from <= at < config.valid_until:
        raise TrustError(
            "trust.snapshot_time", "The trust snapshot is outside its validity window."
        )
    if (
        policy.environment != trust_source.environment
        or policy.environment != snapshot.signatures.environment
        or policy.profile_id != snapshot.signatures.profile_id
    ):
        raise TrustError(
            "trust.profile_mismatch",
            "The signature backend and trust policy have different deployment profiles.",
        )
    return replace(snapshot, configuration=config, resolved_at=at)


def check_bundle_source(
    manifest: BundleManifest,
    independently_computed_root: Digest,
    trust_source: TrustSource,
    *,
    validation_at: UTCDateTime,
) -> SourceEvidence | None:
    """Check configured provenance policy only, including pre-signing calls."""
    manifest = BundleManifest.model_validate(manifest)
    root_digest = _verified_root(manifest, independently_computed_root)
    at = _UTC.validate_python(validation_at)
    return _verify_source(
        resolve_trust_snapshot(trust_source, validation_at=at),
        manifest,
        root_digest,
        at,
    )


def verify_bundle_trust(
    manifest: BundleManifest,
    independently_computed_root: Digest,
    trust_source: TrustSource,
    *,
    validation_at: UTCDateTime,
) -> TrustVerification:
    """Verify every attached signature and the distinct authorized-signer quorum."""
    manifest = BundleManifest.model_validate(manifest)
    root_digest = _verified_root(manifest, independently_computed_root)
    at = _UTC.validate_python(validation_at)
    snapshot = resolve_trust_snapshot(trust_source, validation_at=at)
    return verify_bundle_trust_at_snapshot(
        manifest, root_digest, snapshot, validation_at=at
    )


def verify_bundle_trust_at_snapshot(
    manifest: BundleManifest,
    independently_computed_root: Digest,
    snapshot: TrustSnapshot,
    *,
    validation_at: UTCDateTime,
) -> TrustVerification:
    """Verify one operation with the snapshot already resolved for its instant."""
    manifest = BundleManifest.model_validate(manifest)
    root_digest = _verified_root(manifest, independently_computed_root)
    at = _UTC.validate_python(validation_at)
    if snapshot.resolved_at != at:
        raise TrustError(
            "trust.snapshot_instant",
            "Trust snapshot must be resolved for this verification instant.",
        )
    config = snapshot.configuration
    policy = config.policy
    if not manifest.integrity.signatures:
        raise TrustError(
            "trust.signature_missing",
            "At least one detached signature is required.",
            ("integrity", "signatures"),
        )
    keys = {key.pin: key for key in config.keys}
    signatures = tuple(
        _verify_signature(
            ref, snapshot, keys, manifest.source.source_system, root_digest, at, index
        )
        for index, ref in enumerate(manifest.integrity.signatures)
    )
    if (
        len({signature.signer_id for signature in signatures})
        < policy.minimum_distinct_signers
    ):
        raise TrustError(
            "trust.signature_quorum",
            "The Bundle does not meet the required distinct authorized-signer count.",
            ("integrity", "signatures"),
        )
    return TrustVerification(
        policy.pin,
        config.pin,
        policy.profile_id,
        root_digest,
        at,
        signatures,
        _verify_source(snapshot, manifest, root_digest, at),
    )
