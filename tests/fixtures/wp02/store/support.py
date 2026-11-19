"""Deterministic stand-in for the trusted validator at the storage port."""

from datetime import UTC, datetime
from io import BytesIO
from zipfile import ZipFile, ZipInfo

from jimu_runtime.bundle.store import (
    AdmissionEvidence,
    AdmissionVerification,
    IdentifiedAdmission,
    SignatureAudit,
    UnidentifiedRejection,
)
from jimu_runtime.bundle.trust import TrustPin
from jimu_runtime.domain.runtime.authority import ActorRef
from jimu_runtime.domain.runtime.bundle_manifest import DetachedSignatureRef
from jimu_runtime.domain.runtime.releases import AdmissionReceipt, CapabilityReport
from jimu_runtime.domain.validation.issues import ValidationIssue

ACTOR = ActorRef(actor_id="submitter", actor_kind="human", identity_registry_id="local")
RUNTIME = ActorRef(
    actor_id="runtime", actor_kind="service", identity_registry_id="local"
)
NOW = datetime(2026, 1, 1, tzinfo=UTC)
ROOT = "a" * 64
PARAMETERS = "b" * 64
CAPABILITIES = TrustPin(identity="capabilities", version="1.0.0", sha256="2" * 64)
POLICY = TrustPin(identity="signing-policy", version="1.0.0", sha256="3" * 64)
SNAPSHOT = TrustPin(identity="trust-snapshot", version="1.0.0", sha256="4" * 64)
KEY = TrustPin(identity="test-key", version="1.0.0", sha256="5" * 64)
EMPTY_EVIDENCE = AdmissionEvidence(
    capability_pin=None,
    trust_policy_pin=None,
    trust_snapshot_pin=None,
    signatures=(),
    source_evidence_pin=None,
)
REJECTED = ValidationIssue(
    code="untrusted_signature",
    path=("integrity", "signatures"),
    message="Synthetic verifier rejected the archive",
    related_refs=(),
)


def raw_zip(envelope: bytes = b"signature A") -> bytes:
    raw = BytesIO()
    with ZipFile(raw, "w") as archive:
        archive.writestr(ZipInfo("fixture.json"), b'{"geometry":[]}')
        archive.comment = envelope
    return raw.getvalue()


def verdict(
    receipt_id: str,
    *,
    logical_root: str = ROOT,
    accepted: bool = True,
    signature_id: str = "signature-a",
) -> AdmissionVerification:
    signature = SignatureAudit(
        ref=DetachedSignatureRef(
            signature_id=signature_id,
            version="1.0.0",
            sha256="6" * 64,
            signature_registry_id="test-evidence",
        ),
        signer_id="synthetic",
        key_pin=KEY,
        signed_at=NOW,
    )
    return AdmissionVerification(
        result=IdentifiedAdmission(
            kind="identified",
            receipt=AdmissionReceipt(
                schema_version="1.0.0",
                bundle_id="fixture",
                bundle_version="1.0.0",
                bundle_digest=logical_root,
                receipt_id=receipt_id,
                runtime_version="1.0.0",
                runtime_identity=RUNTIME,
                accepted=accepted,
                capability_report=CapabilityReport(required=(), optional=()),
                errors=() if accepted else (REJECTED,),
                warnings=(),
                timestamp=NOW,
            ),
        ),
        evidence=AdmissionEvidence(
            capability_pin=CAPABILITIES,
            trust_policy_pin=POLICY,
            trust_snapshot_pin=SNAPSHOT,
            signatures=(signature,) if accepted else (),
            source_evidence_pin=None,
        ),
    )


def rejection() -> AdmissionVerification:
    return AdmissionVerification(
        result=UnidentifiedRejection(
            kind="unidentified",
            runtime_version="1.0.0",
            runtime_identity=RUNTIME,
            errors=(REJECTED,),
            warnings=(),
            timestamp=NOW,
        ),
        evidence=EMPTY_EVIDENCE,
    )


def no_verification(raw: bytes) -> AdmissionVerification:
    raise AssertionError("an existing exact operation must not call the verifier")
