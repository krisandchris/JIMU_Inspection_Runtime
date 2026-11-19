"""Independent submission setup around the complete compiler scenario."""

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import cast

from jimu_runtime.bundle.admission import AdmissionEnvironment, admit_bundle
from jimu_runtime.bundle.archive import BundleLimits, read_archive, write_archive
from jimu_runtime.bundle.compiler import (
    CompiledBundle,
    attach_signature_refs,
    compile_bundle,
)
from jimu_runtime.bundle.integrity import (
    CHECKSUMS_PATH,
    MANIFEST_PATH,
    build_checksums,
    canonical_json,
    compute_root_digest,
    read_canonical_json,
)
from jimu_runtime.bundle.store import LocalBundleStore, StoredAdmission
from jimu_runtime.bundle.trust import SignatureEvidence, SigningKeyGrant
from jimu_runtime.domain.fact_schemas.canonicalization import JSONValue
from jimu_runtime.domain.runtime.authority import ActorRef

from ..compiler.scenario import (
    LIMITS,
    capability_snapshot,
    compile_environment,
    source_scenario,
)
from ..trust.support import AT, FixtureTrustSource, make_signatures, trust_source

ACTOR = ActorRef(
    actor_kind="human",
    actor_id="fixture-submitter",
    identity_registry_id="fixture-users",
)
RUNTIME_ACTOR = ActorRef(
    actor_kind="service",
    actor_id="fixture-runtime",
    identity_registry_id="fixture-services",
)
JSON_BUDGET = {"max_bytes": LIMITS.max_file_bytes, "max_depth": LIMITS.max_json_depth}


@dataclass(frozen=True)
class SignedBundle:
    archive: bytes
    unsigned: bytes
    digest: str
    key: SigningKeyGrant
    public_key: bytes
    signature: SignatureEvidence
    trust: FixtureTrustSource

    @property
    def environment(self) -> AdmissionEnvironment:
        return AdmissionEnvironment(
            capabilities=capability_snapshot(),
            runtime_identity=RUNTIME_ACTOR,
            trust=self.trust,
            validation_at=AT,
        )


def build_scenario(tmp_path: Path) -> CompiledBundle:
    report = compile_bundle(
        source_scenario(), limits=LIMITS, environment=compile_environment(tmp_path)
    )
    assert report.bundle is not None, report.errors
    return report.bundle


def sign_bundle(
    tmp_path: Path, raw: bytes, root: str, *, signing_root: str | None = None
) -> SignedBundle:
    key, public_key, (signature,) = make_signatures(tmp_path, root=signing_root or root)
    host = trust_source(tmp_path, ((key, public_key),), (signature,))
    archive = attach_signature_refs(raw, (signature.ref,), limits=LIMITS)
    return SignedBundle(archive, raw, root, key, public_key, signature, host)


def submit(
    store: LocalBundleStore,
    raw: bytes,
    operation_id: str,
    environment: AdmissionEnvironment,
    *,
    limits: BundleLimits = LIMITS,
) -> StoredAdmission:
    return admit_bundle(
        raw,
        operation_id=operation_id,
        actor=ACTOR,
        limits=limits,
        environment=environment,
        store=store,
    )


def rebuild_unsigned(
    raw: bytes,
    edit: Callable[[dict[str, JSONValue], dict[str, bytes]], None],
) -> tuple[bytes, str]:
    """Create byte-consistent mutants through package primitives, bypassing compile."""
    payloads = read_archive(raw, LIMITS)
    manifest = cast(
        dict[str, JSONValue],
        read_canonical_json(payloads.pop(MANIFEST_PATH), **JSON_BUDGET),
    )
    payloads.pop(CHECKSUMS_PATH)
    edit(manifest, payloads)
    integrity = cast(dict[str, JSONValue], manifest["integrity"])
    integrity["signatures"] = []
    checksums = build_checksums(payloads)
    root = compute_root_digest(manifest, checksums, **JSON_BUDGET)
    integrity["root_digest"] = root
    return (
        write_archive(
            {
                **payloads,
                CHECKSUMS_PATH: canonical_json(
                    checksums.model_dump(mode="json"), **JSON_BUDGET
                ),
                MANIFEST_PATH: canonical_json(manifest, **JSON_BUDGET),
            },
            LIMITS,
        ),
        root,
    )
