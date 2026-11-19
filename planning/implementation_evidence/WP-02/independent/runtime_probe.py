"""Independent public-entry CPU Bundle/Release probe with ephemeral keys and stores."""

import json
import sqlite3
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from hashlib import sha256
from pathlib import Path
from tempfile import TemporaryDirectory

import rfc8785
from fixtures.wp02.admission.support import ACTOR, RUNTIME_ACTOR, rebuild_unsigned
from fixtures.wp02.compiler import scenario
from fixtures.wp02.trust.support import (
    AT,
    VERSION,
    FixtureTrustSource,
    make_signatures,
    pin,
    trust_source,
)

from jimu_runtime.bundle.admission import AdmissionEnvironment, admit_bundle
from jimu_runtime.bundle.archive import read_archive, read_bundle, write_archive
from jimu_runtime.bundle.catalogue import build_catalogue
from jimu_runtime.bundle.compiler import (
    ReferenceSlot,
    SourceAssetBytes,
    SourceIdentity,
    attach_signature_refs,
    compile_bundle,
)
from jimu_runtime.bundle.releases import (
    ReleaseAction,
    ReleaseManager,
    ReleaseOperationError,
    ResolvedTaskScope,
)
from jimu_runtime.bundle.store import LocalBundleStore, StoreCorruption
from jimu_runtime.bundle.trust import SourceEvidence
from jimu_runtime.domain.enums import ArtifactKind
from jimu_runtime.domain.runtime.bundle_manifest import SourceArtifactRef
from jimu_runtime.domain.runtime.intake import TaskSubmission
from jimu_runtime.domain.runtime.releases import GlobalActivationScope

LIMITS = scenario.LIMITS
ASSET_PATH = "resources/assets/data/boundary.bin"
ASSET_BYTES = b"\x00\xffJIMU synthetic source drawing\r\n"
ROOT = Path.cwd()


def digest(data):
    return sha256(data).hexdigest()


def report(check, **values):
    print(json.dumps({"check": check, **values}, sort_keys=True), flush=True)


def extended_source():
    original = scenario.source_scenario()
    region = next(row for row in original.artifacts if row.kind is ArtifactKind.REGION)
    payload = json.loads(region.content)
    payload.update(asset_refs=[None], geometry_ref=None)
    region = replace(
        region,
        content=json.dumps(payload).encode(),
        reference_slots=(
            *region.reference_slots,
            ReferenceSlot(
                location=("asset_refs", 0),
                target=(ArtifactKind.STATIC_ASSET, "drawing", VERSION),
            ),
            ReferenceSlot(
                location=("geometry_ref",),
                target=(ArtifactKind.GEOMETRY_RESOURCE, "boundary", VERSION),
            ),
        ),
    )
    additions = (
        scenario._definition(
            "vocabulary",
            "coordinate",
            "vocabularies/coordinate.json",
            "vocabulary_id",
            semantic_domain="coordinate_system",
            members=["original_pixel"],
        ),
        scenario._definition(
            "vocabulary",
            "units",
            "vocabularies/units.json",
            "vocabulary_id",
            semantic_domain="unit",
            members=["pixel"],
        ),
        scenario._definition(
            "static_asset",
            "drawing",
            "resources/assets/drawing.json",
            "asset_id",
            data_path=ASSET_PATH,
            sha256=digest(ASSET_BYTES),
            mime_type="application/octet-stream",
            byte_size=len(ASSET_BYTES),
        ),
        scenario._definition(
            "geometry_resource",
            "boundary",
            "resources/geometry/boundary.json",
            "geometry_resource_id",
            coordinate_system=scenario._member("coordinate", "original_pixel"),
            units=scenario._member("units", "pixel"),
            geometry={
                "kind": "polygon",
                "vertices": [
                    {"x": 0, "y": 0},
                    {"x": 10, "y": 0},
                    {"x": 10, "y": 20},
                    {"x": 0, "y": 20},
                ],
            },
            provenance={"source_asset_ref": scenario._ref("static_asset", "drawing")},
        ),
    )
    artifacts = tuple(
        region if row.kind is ArtifactKind.REGION else row for row in original.artifacts
    )
    artifacts += additions
    source_asset = SourceAssetBytes(
        path=ASSET_PATH,
        content=ASSET_BYTES,
        origin=SourceIdentity(artifact_id="source-drawing", version=VERSION),
    )
    return declare_source_bytes(
        replace(original, artifacts=artifacts, assets=(source_asset,))
    )


def declare_source_bytes(source):
    provenance = source.metadata.source.model_copy(
        update={
            "artifacts": tuple(
                SourceArtifactRef(
                    artifact_id=item.origin.artifact_id,
                    version=item.origin.version,
                    digest=digest(item.content),
                )
                for item in (*source.artifacts, *source.assets)
            )
        }
    )
    return replace(source, metadata=replace(source.metadata, source=provenance))


@dataclass(frozen=True)
class AttestationRegistry:
    rows: tuple[SourceEvidence, ...]

    def verify(self, provenance, root_digest, validation_at: datetime):
        if validation_at != AT:
            return None
        return next(
            (row for row in self.rows if row.bundle_digest == root_digest), None
        )


def release_manager(store, scope, state):
    def authorize(principal, action, target):
        if principal is not state["controller"] or target.scope != scope:
            return None
        if action is ReleaseAction.REQUEST_RELEASE:
            return None
        return ACTOR

    return ReleaseManager(
        store,
        authorize=authorize,
        scope_precedence=("device", "region", "scene", "global"),
        resolve_task_scope=lambda principal, submission: state["resolved"]
        if principal is state["controller"]
        else None,
        allow_new_task=lambda release, activation, resolved, at: release.release_id
        not in state["revoked_releases"],
        clock=lambda: AT + timedelta(seconds=1),
    )


def reject(call, code):
    try:
        call()
    except ReleaseOperationError as error:
        assert error.code == code, error
        report(code)
    else:
        raise AssertionError(f"successful operation should have failed with {code}")


def compile_checked(source, directory):
    compiled = compile_bundle(
        source, limits=LIMITS, environment=scenario.compile_environment(directory)
    )
    assert compiled.bundle is not None, compiled.errors
    report(
        "compile",
        root=compiled.bundle.bundle_ref.bundle_digest,
        checks={row.check_id: row.status for row in compiled.semantic_checks},
        warnings=sorted({row.code for row in compiled.warnings}),
    )
    return compiled.bundle


def without_producer(manifest, payloads):
    path = next(key for key in payloads if key == "facts/producers.json")
    body = json.loads(payloads[path])
    assert len(body["declarations"]) == 2, body
    body["declarations"] = body["declarations"][1:]
    payloads[path] = rfc8785.dumps(body)
    entry = next(row for row in manifest["artifacts"] if row["path"] == path)
    entry["content_ref"]["digest"] = digest(payloads[path])


def submission(identity, *, requested=None):
    payload = json.loads(
        (ROOT / "tests/fixtures/wp01c/intake/task-submission-valid.json").read_bytes()
    )
    payload["context"]["region_id"] = "test_parking"
    payload["request_id"] = f"independent-request-{identity}"
    payload["idempotency_key"] = f"independent-frame-{identity}"
    if requested:
        payload["requested_release_id"] = requested
    return TaskSubmission.model_validate_json(json.dumps(payload))


def main(directory):
    compile_one, compile_two, signatures = (
        directory / name for name in ("compile-one", "compile-two", "signatures")
    )
    for path in (compile_one, compile_two, signatures):
        path.mkdir(mode=0o700)
    source = extended_source()
    a = compile_checked(source, compile_one)
    repeated = compile_checked(
        replace(source, artifacts=source.artifacts[::-1]), compile_two
    )
    assert a.archive_bytes == repeated.archive_bytes
    # For §12 a payload changes, so change the resource itself as well as release version.
    second_asset = ASSET_BYTES + b"second approved drawing"
    descriptor = next(
        row for row in source.artifacts if row.kind is ArtifactKind.STATIC_ASSET
    )
    descriptor_body = json.loads(descriptor.content)
    descriptor_body.update(sha256=digest(second_asset), byte_size=len(second_asset))
    updated = replace(descriptor, content=json.dumps(descriptor_body).encode())
    next_source = declare_source_bytes(
        replace(
            source,
            metadata=replace(source.metadata, bundle_version="1.0.1"),
            assets=(replace(source.assets[0], content=second_asset),),
            artifacts=tuple(
                updated if row is descriptor else row for row in source.artifacts
            ),
        )
    )
    second_provenance = next_source.metadata.source
    drift = compile_bundle(
        replace(source, assets=(replace(source.assets[0], content=second_asset),)),
        limits=LIMITS,
        environment=scenario.compile_environment(compile_two),
    )
    drift_codes = [error.code for error in drift.errors]
    assert not drift.succeeded and "source_provenance_digest_mismatch" in drift_codes, (
        drift_codes
    )
    report("source-raw-bytes-drift", issues=drift_codes)
    b = compile_checked(next_source, compile_two)
    root_a, root_b = a.bundle_ref.bundle_digest, b.bundle_ref.bundle_digest
    assert root_a != root_b
    verified_a = read_bundle(a.archive_bytes, LIMITS)
    assert verified_a.payloads[ASSET_PATH] == ASSET_BYTES
    assert read_bundle(b.archive_bytes, LIMITS).payloads[ASSET_PATH] == second_asset
    catalogue = build_catalogue(
        verified_a.manifest,
        verified_a.payloads,
        max_bytes=LIMITS.max_file_bytes,
        max_depth=LIMITS.max_json_depth,
    )
    region = catalogue.of_kind(ArtifactKind.REGION)[0].value
    assert region.geometry_ref is not None
    assert (
        catalogue.resolve(region.geometry_ref).value.provenance.source_asset_ref
        == region.asset_refs[0]
    )
    report(
        "closed_fixture",
        artifacts=len(verified_a.manifest.artifacts),
        asset_sha256=digest(ASSET_BYTES),
        region=region.region_id,
        producer_inventory=[
            entry.content_ref.artifact_id
            for entry in verified_a.manifest.artifacts
            if entry.content_ref.artifact_kind is ArtifactKind.FACT_PRODUCER_CATALOG
        ],
    )

    mutant, mutant_root = rebuild_unsigned(a.archive_bytes, without_producer)
    expected_sources = (
        (a.archive_bytes, root_a, source.metadata.source, "edition-a"),
        (b.archive_bytes, root_b, second_provenance, "edition-b"),
        (mutant, mutant_root, source.metadata.source, "missing-producer"),
    )
    signed = {}
    key_pairs, evidence, proofs = [], [], []
    for raw, root, provenance, name in expected_sources:
        key, public_key, (signed_root,) = make_signatures(
            signatures,
            root=root,
            key_id=f"key-{name}",
            signer_id=f"publisher-{name}",
            signature_ids=(f"signature-{name}",),
        )
        signed[name] = attach_signature_refs(raw, (signed_root.ref,), limits=LIMITS)
        key_pairs.append((key, public_key))
        evidence.append(signed_root)
        proofs.append(
            SourceEvidence(
                pin=pin(f"attestation-{name}"),
                provenance=provenance,
                bundle_digest=root,
            )
        )
    host = trust_source(
        signatures,
        tuple(key_pairs),
        tuple(evidence),
        authority=AttestationRegistry(tuple(proofs)),
        source_policy="independent_attestation",
    )
    assert host.environment == "isolated_test"
    env = AdmissionEnvironment(
        runtime_identity=RUNTIME_ACTOR,
        capabilities=scenario.capability_snapshot(),
        trust=host,
        validation_at=AT,
    )
    path = directory / "runtime-store"
    store = LocalBundleStore(path)

    def submit(raw, operation, environment=env):
        return admit_bundle(
            raw,
            operation_id=operation,
            actor=ACTOR,
            limits=LIMITS,
            environment=environment,
            store=store,
        )

    def admission_reject(raw, operation, code, environment=env):
        entry = submit(raw, operation, environment)
        decision = entry.verification.result
        outcome = decision.receipt if entry.receipt is not None else decision
        errors = [error.code for error in outcome.errors]
        assert not entry.accepted and code in errors, errors
        assert digest(raw) == entry.archive_sha256
        report(operation, issues=errors)

    accepted = []
    for name, root in (("edition-a", root_a), ("edition-b", root_b)):
        raw = signed[name]
        row = submit(raw, name)
        assert row.accepted, row.receipt.errors if row.receipt else row.verification
        assert row.receipt.bundle_digest == root
        assert row.archive_sha256 == digest(raw)
        assert len(row.verification.evidence.signatures) == 1
        assert row.verification.evidence.source_evidence_pin == pin(
            f"attestation-{name}"
        )
        assert LocalBundleStore(path).read_archive(row.receipt.receipt_id) == raw
        accepted.append(row.receipt)
        report(
            "signed_admission",
            edition=name,
            root=root,
            zip_sha256=digest(raw),
            receipt=row.receipt.receipt_id,
            verification_profile=host.snapshot.configuration.policy.profile_id,
        )

    admission_reject(
        signed["edition-a"],
        "empty-trust",
        "trust.snapshot_missing",
        replace(env, trust=FixtureTrustSource(None)),
    )
    unproven = trust_source(
        signatures,
        tuple(key_pairs),
        tuple(evidence),
        source_policy="independent_attestation",
    )
    admission_reject(
        signed["edition-a"],
        "missing-source-proof",
        "trust.source_authority_missing",
        replace(env, trust=unproven),
    )
    admission_reject(a.archive_bytes, "unsigned-same-root", "trust.signature_missing")
    admission_reject(
        signed["missing-producer"],
        "valid-signature-missing-producer",
        "producer.catalogue_mismatch",
    )

    files = read_archive(signed["edition-a"], LIMITS)
    files[ASSET_PATH] = ASSET_BYTES + b"tamper"
    admission_reject(
        write_archive(files, LIMITS), "tampered-resource", "checksum_mismatch"
    )

    scope = GlobalActivationScope(scope="global")
    state = {
        "controller": object(),
        "revoked_releases": set(),
        "resolved": ResolvedTaskScope(
            device_id="rover-01",
            camera_id="front-camera",
            region_id=region.region_id,
            scene_type=region.scene_type,
        ),
    }
    controller = state["controller"]
    manager = release_manager(store, scope, state)
    registrations, approvals = {}, {}
    for ref, receipt in zip(("release-a", "release-b"), accepted, strict=True):
        registrations[ref] = manager.register_release(
            operation_key=f"register-{ref}",
            release_id=ref,
            admission_receipt_id=receipt.receipt_id,
            principal=controller,
        )
        approvals[ref] = manager.approve_release(
            operation_key=f"approve-{ref}",
            release_id=ref,
            scope=scope,
            principal=controller,
        )

    def activate(edition, previous, principal=controller):
        return manager.activate(
            operation_key=f"activate-{edition}",
            release_id=f"release-{edition}",
            scope=scope,
            activation_id=f"activation-{edition}",
            expected_previous_activation_id=previous,
            principal=principal,
        )

    reject(lambda: activate("a", None, ACTOR), "unauthorized_release_action")
    a_event = activate("a", None)
    task_a = manager.accept_task(submission("a"), principal=controller)
    assert task_a.release_id == "release-a" and task_a.bundle_digest == root_a
    b_event = activate("b", "activation-a")
    task_b = manager.accept_task(submission("b"), principal=controller)
    assert task_b.release_id == "release-b" and task_b.bundle_digest == root_b
    assert manager.accept_task(submission("a"), principal=controller) == task_a
    reject(
        lambda: manager.accept_task(
            submission("explicit", requested="release-b"), principal=controller
        ),
        "unauthorized_release_action",
    )
    rollback = manager.rollback(
        operation_key="rollback-a",
        scope=scope,
        target_activation_id="activation-a",
        activation_id="activation-return-a",
        expected_previous_activation_id="activation-b",
        principal=controller,
    )
    assert rollback.bundle_ref.bundle_digest == root_a
    reopened = LocalBundleStore(path)
    reopened_manager = release_manager(reopened, scope, state)
    assert reopened_manager.get_task(task_a.task_id) == task_a
    assert reopened_manager.get_task(task_b.task_id) == task_b
    for ref in registrations:
        assert reopened_manager.get_release(ref) == registrations[ref]
        assert reopened_manager.get_release(ref, scope=scope) == approvals[ref]
    assert reopened_manager.get_activation("activation-a") == a_event
    assert reopened_manager.get_activation("activation-b") == b_event
    assert reopened_manager.active_activation(scope) == rollback
    assert (
        reopened_manager.accept_task(
            submission("after-rollback"), principal=controller
        ).bundle_digest
        == root_a
    )
    assert reopened.read_archive(accepted[1].receipt_id) == signed["edition-b"]
    report(
        "switch_rollback_reopen",
        before=root_a,
        switched=root_b,
        rolled_back=root_a,
        pinned_task_digests=[task_a.bundle_digest, task_b.bundle_digest],
    )

    state["revoked_releases"].add("release-a")
    reject(
        lambda: reopened_manager.accept_task(
            submission("revoked"), principal=controller
        ),
        "new_task_disallowed",
    )
    assert reopened_manager.accept_task(submission("a"), principal=controller) == task_a
    state["revoked_releases"].clear()
    with sqlite3.connect(path / "bundle-store.sqlite3") as con:
        con.execute(
            "UPDATE archives SET raw = ? WHERE sha256 = ?",
            (b"damaged", digest(signed["edition-a"])),
        )
    for label, operation in (
        ("readback", lambda: reopened.read_archive(accepted[0].receipt_id)),
        (
            "task-intake",
            lambda: reopened_manager.accept_task(
                submission("corrupt"), principal=controller
            ),
        ),
    ):
        try:
            operation()
        except StoreCorruption as error:
            report("unreadable-admitted-bytes", via=label, diagnostic=str(error))
        else:
            raise AssertionError(f"Corrupted archive was served via {label}")


if __name__ == "__main__":
    with TemporaryDirectory(prefix="jimu-wp02-independent-") as temporary:
        main(Path(temporary))
