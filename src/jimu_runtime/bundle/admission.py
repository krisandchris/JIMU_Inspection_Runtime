"""Independent admission of an immutable ZIP to the local Runtime store."""

import json
from dataclasses import asdict, dataclass
from hashlib import sha256
from uuid import uuid4

from pydantic import TypeAdapter

from jimu_runtime.bundle.archive import BundleLimits, read_bundle
from jimu_runtime.bundle.artifacts import BundleArtifactError
from jimu_runtime.bundle.capabilities import CapabilitySnapshot, assess_capabilities
from jimu_runtime.bundle.catalogue import build_catalogue
from jimu_runtime.bundle.closure import validate_closure
from jimu_runtime.bundle.integrity import BundleIntegrityError, JSONLocation
from jimu_runtime.bundle.producer_rules import SystemProducerGrant
from jimu_runtime.bundle.semantics import validate_bundle_semantics
from jimu_runtime.bundle.store import (
    AdmissionEvidence,
    AdmissionVerification,
    IdentifiedAdmission,
    LocalBundleStore,
    SignatureAudit,
    StoredAdmission,
    UnidentifiedRejection,
)
from jimu_runtime.bundle.trust import (
    TrustError,
    TrustSnapshot,
    TrustSource,
    resolve_trust_snapshot,
    verify_bundle_trust_at_snapshot,
)
from jimu_runtime.domain.definition_support import UTCDateTime
from jimu_runtime.domain.fact_schemas.models import ProjectionValidationError
from jimu_runtime.domain.primitives import RecordId
from jimu_runtime.domain.runtime.authority import ActorRef
from jimu_runtime.domain.runtime.bundle_manifest import BundleManifest
from jimu_runtime.domain.runtime.releases import AdmissionReceipt, CapabilityReport
from jimu_runtime.domain.validation.issues import ValidationIssue

_UTC = TypeAdapter[UTCDateTime](UTCDateTime)
_RECORD_ID = TypeAdapter[RecordId](RecordId)
_RECEIPT_ISSUE_LIMIT = 128


@dataclass(frozen=True, slots=True, kw_only=True)
class AdmissionEnvironment:
    """Trusted host inputs for one request, independent of the submitted bytes."""

    runtime_identity: ActorRef
    capabilities: CapabilitySnapshot
    trust: TrustSource
    validation_at: UTCDateTime
    system_grants: tuple[SystemProducerGrant, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "runtime_identity", ActorRef.model_validate(self.runtime_identity)
        )
        object.__setattr__(
            self, "capabilities", CapabilitySnapshot.model_validate(self.capabilities)
        )
        object.__setattr__(
            self, "validation_at", _UTC.validate_python(self.validation_at)
        )
        object.__setattr__(
            self,
            "system_grants",
            tuple(
                SystemProducerGrant.model_validate(grant)
                for grant in self.system_grants
            ),
        )


def _issue(
    code: str,
    message: str,
    *,
    member: str | None = None,
    location: JSONLocation = (),
) -> ValidationIssue:
    # The receipt path has a smaller bound than a ZIP member or JSON key.
    # Include the escaped member spelling in the bounded message.
    if member is not None:
        message = f"ZIP member {json.dumps(member, ensure_ascii=True)}. {message}"
        location = (member, *location)
    path = tuple(
        component[:128] if isinstance(component, str) else component
        for component in location[:32]
        if component != ""
    )
    return ValidationIssue(
        code=code, path=path, message=message[:4096], related_refs=()
    )


def _bounded(issues: list[ValidationIssue]) -> tuple[ValidationIssue, ...]:
    if len(issues) <= _RECEIPT_ISSUE_LIMIT:
        return tuple(issues)
    return (
        *issues[: _RECEIPT_ISSUE_LIMIT - 1],
        _issue(
            "admission.issue_limit", "Additional validation diagnostics were omitted."
        ),
    )


def _verify_submission(
    raw: bytes,
    limits: BundleLimits,
    environment: AdmissionEnvironment,
    trust_state: TrustSnapshot | TrustError,
) -> AdmissionVerification:
    snapshot = environment.capabilities
    at = environment.validation_at
    trust_config = (
        trust_state.configuration if isinstance(trust_state, TrustSnapshot) else None
    )
    evidence = AdmissionEvidence(
        capability_pin=snapshot.pin,
        trust_policy_pin=trust_config.policy.pin if trust_config is not None else None,
        trust_snapshot_pin=trust_config.pin if trust_config is not None else None,
        signatures=(),
        source_evidence_pin=None,
    )
    try:
        package = read_bundle(raw, limits)
    except BundleIntegrityError as error:
        return AdmissionVerification(
            result=UnidentifiedRejection(
                kind="unidentified",
                runtime_version=snapshot.runtime_version,
                runtime_identity=environment.runtime_identity,
                errors=(
                    _issue(
                        error.code,
                        str(error),
                        member=error.path,
                        location=(
                            error.json_location
                            if error.path is not None
                            else ("archive",)
                        ),
                    ),
                ),
                warnings=(),
                timestamp=at,
            ),
            evidence=evidence,
        )

    manifest = package.manifest
    # read_bundle has compared the manifest root to the root of the actual bytes.
    root = manifest.integrity.root_digest
    errors: list[ValidationIssue] = []
    warnings: list[ValidationIssue] = []

    try:
        if isinstance(trust_state, TrustError):
            raise trust_state
        trust = verify_bundle_trust_at_snapshot(
            manifest, root, trust_state, validation_at=at
        )
    except TrustError as error:
        errors.append(error.issue)
    else:
        evidence = AdmissionEvidence(
            capability_pin=snapshot.pin,
            trust_policy_pin=trust.policy_pin,
            trust_snapshot_pin=trust.snapshot_pin,
            signatures=tuple(
                SignatureAudit(
                    ref=signature.ref,
                    signer_id=signature.signer_id,
                    key_pin=signature.key_pin,
                    signed_at=signature.signed_at,
                )
                for signature in trust.signatures
            ),
            source_evidence_pin=(
                trust.source_evidence.pin if trust.source_evidence is not None else None
            ),
        )

    try:
        catalogue = build_catalogue(
            manifest,
            package.payloads,
            max_bytes=limits.max_file_bytes,
            max_depth=limits.max_json_depth,
        )
        closure = validate_closure(catalogue)
        semantics = validate_bundle_semantics(
            catalogue,
            closure,
            manifest,
            snapshot,
            validation_at=at,
            max_bytes=limits.max_file_bytes,
            max_depth=limits.max_json_depth,
            system_grants=environment.system_grants,
        )
    except (BundleIntegrityError, BundleArtifactError) as error:
        errors.append(
            _issue(
                error.code, str(error), member=error.path, location=error.json_location
            )
        )
    except ProjectionValidationError as error:
        errors.append(_issue(error.code, error.message, location=error.path))
    else:
        for check in semantics.checks:
            if check.status in ("invalid", "incomplete"):
                errors.extend(check.issues)
            elif check.status == "deferred":
                warnings.extend(check.issues)
        capabilities = semantics.capabilities
        return _decision(
            manifest, environment, evidence, capabilities.report, errors, warnings
        )

    # If closure or semantics could not complete, we can still assess required
    # manifest declarations. Optional uses require semantic proof.
    capabilities = assess_capabilities(manifest, snapshot, validation_at=at)
    errors.extend(capabilities.errors)
    return _decision(
        manifest, environment, evidence, capabilities.report, errors, warnings
    )


def _decision(
    manifest: BundleManifest,
    environment: AdmissionEnvironment,
    evidence: AdmissionEvidence,
    capabilities: CapabilityReport,
    errors: list[ValidationIssue],
    warnings: list[ValidationIssue],
) -> AdmissionVerification:
    return AdmissionVerification(
        result=IdentifiedAdmission(
            kind="identified",
            receipt=AdmissionReceipt(
                schema_version="1.0.0",
                receipt_id=f"admission-{uuid4().hex}",
                bundle_id=manifest.bundle_id,
                bundle_version=manifest.bundle_version,
                bundle_digest=manifest.integrity.root_digest,
                runtime_version=environment.capabilities.runtime_version,
                runtime_identity=environment.runtime_identity,
                accepted=not errors,
                capability_report=capabilities,
                errors=_bounded(errors),
                warnings=_bounded(warnings),
                timestamp=environment.validation_at,
            ),
        ),
        evidence=evidence,
    )


def admit_bundle(
    raw: bytes,
    *,
    operation_id: RecordId,
    actor: ActorRef,
    limits: BundleLimits,
    environment: AdmissionEnvironment,
    store: LocalBundleStore,
) -> StoredAdmission:
    """Validate and persist a ZIP submission, using an already authenticated actor.

    Each call verifies the actual submission using the current host inputs. An
    exact retry can return the persisted acknowledgment after revalidation.
    Historical acknowledgments are available through the store's read methods.
    """
    if type(raw) is not bytes:
        raise TypeError("submitted ZIP must be immutable bytes")
    operation = _RECORD_ID.validate_python(operation_id)
    principal = ActorRef.model_validate(actor)
    trust_state: TrustSnapshot | TrustError
    try:
        trust_state = resolve_trust_snapshot(
            environment.trust, validation_at=environment.validation_at
        )
    except TrustError as error:
        trust_state = error
    verification = _verify_submission(raw, limits, environment, trust_state)
    trust_input = (
        trust_state.configuration.model_dump(mode="json")
        if isinstance(trust_state, TrustSnapshot)
        else trust_state.issue.model_dump(mode="json")
    )
    # The store separately binds the operation to the raw ZIP hash and actor.
    # The fresh receipt ID is generated here and may be discarded on a retry.
    request = {
        "action": "jimu.bundle.admit.v1",
        "limits": asdict(limits),
        "capabilities": environment.capabilities.model_dump(mode="json"),
        "system_grants": [
            grant.model_dump(mode="json") for grant in environment.system_grants
        ],
        "trust_input": trust_input,
        "verification": verification.model_dump(
            mode="json", exclude={"result": {"receipt": {"receipt_id"}}}
        ),
    }
    request_bytes = json.dumps(request, sort_keys=True, separators=(",", ":")).encode()
    return store._admit(
        operation,
        raw,
        actor=principal,
        parameters_digest=sha256(request_bytes).hexdigest(),
        verify=lambda _submission: verification,
    )
