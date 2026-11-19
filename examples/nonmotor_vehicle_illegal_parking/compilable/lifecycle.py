"""Exercise the production compiler, local admission and historical pin reopen."""

from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path

from jimu_runtime.bundle.admission import AdmissionEnvironment, admit_bundle
from jimu_runtime.bundle.archive import read_bundle
from jimu_runtime.bundle.catalogue import build_catalogue
from jimu_runtime.bundle.compiler import (
    BundleCompileInput,
    CompiledBundle,
    CompileEnvironment,
    attach_signature_refs,
    compile_bundle,
)
from jimu_runtime.bundle.integrity import sha256_digest
from jimu_runtime.bundle.releases import (
    ReleaseAction,
    ReleaseManager,
    ReleaseTarget,
    ResolvedTaskScope,
)
from jimu_runtime.bundle.semantic_inputs import require_definition
from jimu_runtime.bundle.store import LocalBundleStore
from jimu_runtime.bundle.trust import verify_bundle_trust
from jimu_runtime.domain.definitions.ontology import RegionDefinition
from jimu_runtime.domain.enums import ArtifactKind
from jimu_runtime.domain.runtime.authority import ActorRef
from jimu_runtime.domain.runtime.intake import AcceptedTask, TaskSubmission
from jimu_runtime.domain.runtime.releases import (
    ActivationRecord,
    AdmissionReceipt,
    RegionActivationScope,
    ReleaseRecord,
)
from jimu_runtime.workflow.pins import ResolvedExecutionPins, resolve_execution_pins

from .local_trust import LocalEd25519, sign_bundle
from .scene import (
    AT,
    DIRECTORY,
    JSON_LIMITS,
    LIMITS,
    REGION,
    VERSION,
    capability_snapshot,
    load_source,
)

ACTOR = ActorRef(
    actor_id="synthetic.scene.operator",
    actor_kind="service",
    identity_registry_id="synthetic.scene.identities",
)
SCOPE = RegionActivationScope(scope="region", region_id=REGION)
DEVICE = "synthetic.device"
CAMERA = "synthetic.fixed.camera"
PROJECT = "synthetic.nonmotor.parking"
CLIENT = "synthetic.edge"
RELEASE_ID = "synthetic.parking.release"
NEXT_RELEASE_ID = "synthetic.parking.release.next"


@dataclass(frozen=True, slots=True)
class ReopenedScene:
    bundle: CompiledBundle
    signed_archive: bytes
    release: ReleaseRecord
    activation: ActivationRecord
    current_activation: ActivationRecord
    task: AcceptedTask
    execution: ResolvedExecutionPins


def local_submission(media: bytes) -> TaskSubmission:
    return TaskSubmission.model_validate(
        {
            "schema_version": VERSION,
            "request_id": "synthetic.scene.request",
            "idempotency_scope": {"project_id": PROJECT, "client_id": CLIENT},
            "idempotency_key": "synthetic.scene.frame",
            "media_ref": {
                "media_id": "synthetic.scene.image",
                "sha256": sha256_digest(media),
            },
            "context": {
                "device_id": DEVICE,
                "camera_id": CAMERA,
                "region_id": REGION,
                "clock": {
                    "captured_at": AT,
                    "original_timestamp": AT.isoformat(),
                    "original_timezone": "UTC",
                    "source": "device_clock",
                    "clock_id": DEVICE,
                },
                "metadata": {"kind": "record", "value": {}},
            },
            "trace": {"trace_id": "synthetic.scene.trace"},
        }
    )


def exercise_lifecycle(directory: Path) -> ReopenedScene:
    """Compile, admit, activate and reopen in a caller-owned temporary directory."""
    source = load_source()
    host = capability_snapshot(source)
    compile_environment = CompileEnvironment(
        capabilities=host,
        trust=LocalEd25519(directory).as_trust_source(),
        validation_at=AT,
    )
    store_dir = directory / "releases"
    store = LocalBundleStore(store_dir)

    def admit(
        candidate: BundleCompileInput, suffix: str
    ) -> tuple[CompiledBundle, bytes, AdmissionReceipt]:
        result = compile_bundle(
            candidate, limits=LIMITS, environment=compile_environment
        )
        bundle = result.bundle
        if bundle is None:
            raise RuntimeError(f"Scene compilation failed: {result.errors}")
        verifier, signature = sign_bundle(directory, bundle.bundle_ref.bundle_digest)
        signed = attach_signature_refs(
            bundle.archive_bytes, (signature,), limits=LIMITS
        )
        files = read_bundle(signed, LIMITS)
        trust = verifier.as_trust_source()
        verification = verify_bundle_trust(
            files.manifest, bundle.bundle_ref.bundle_digest, trust, validation_at=AT
        )
        if len(verification.signatures) != 1:
            raise RuntimeError("Expected one authenticated detached signature")
        admitted = admit_bundle(
            signed,
            operation_id=f"admit.synthetic.scene.{suffix}",
            actor=ACTOR,
            limits=LIMITS,
            environment=AdmissionEnvironment(
                runtime_identity=ACTOR, capabilities=host, trust=trust, validation_at=AT
            ),
            store=store,
        )
        if not admitted.accepted or admitted.receipt is None:
            raise RuntimeError(f"Scene admission failed: {admitted}")
        return bundle, signed, admitted.receipt

    bundle, signed, receipt = admit(source, "initial")
    files = read_bundle(signed, LIMITS)
    catalogue = build_catalogue(files.manifest, files.payloads, **JSON_LIMITS)
    (region_ref,) = (
        item.ref
        for item in catalogue.of_kind(ArtifactKind.REGION)
        if item.ref.artifact_id == REGION
    )
    region = require_definition(catalogue, region_ref, RegionDefinition)
    principal = object()
    submission = local_submission(
        (DIRECTORY / "assets" / "synthetic-frame.png").read_bytes()
    )

    def authorize(
        caller: object, action: ReleaseAction, target: ReleaseTarget
    ) -> ActorRef | None:
        if caller is not principal or target.release_id not in (
            RELEASE_ID,
            NEXT_RELEASE_ID,
        ):
            return None
        if action is ReleaseAction.REGISTER and target.scope.scope == "global":
            return ACTOR
        if (
            action
            in (
                ReleaseAction.APPROVE,
                ReleaseAction.ACTIVATE,
                ReleaseAction.ACCEPT_TASK,
            )
            and target.scope == SCOPE
        ):
            return ACTOR
        return None

    def resolve(caller: object, request: TaskSubmission) -> ResolvedTaskScope | None:
        if caller is not principal or request != submission:
            return None
        return ResolvedTaskScope(
            device_id=DEVICE,
            camera_id=CAMERA,
            region_id=REGION,
            scene_type=region.scene_type,
        )

    def permit(
        release: ReleaseRecord,
        activation: ActivationRecord,
        scope: ResolvedTaskScope,
        at: datetime,
    ) -> bool:
        return (
            release.bundle_ref == bundle.bundle_ref
            and activation.scope == SCOPE
            and scope.region_id == REGION
            and at == AT
        )

    def manager(ledger: LocalBundleStore) -> ReleaseManager:
        return ReleaseManager(
            ledger,
            authorize=authorize,
            resolve_task_scope=resolve,
            scope_precedence=("device", "region", "scene", "global"),
            allow_new_task=permit,
            clock=lambda: AT,
        )

    releases = manager(store)
    releases.register_release(
        operation_key="register.synthetic.scene",
        release_id=RELEASE_ID,
        admission_receipt_id=receipt.receipt_id,
        principal=principal,
    )
    release = releases.approve_release(
        operation_key="approve.synthetic.scene",
        release_id=RELEASE_ID,
        scope=SCOPE,
        principal=principal,
    )
    activated = releases.activate(
        operation_key="activate.synthetic.scene",
        release_id=RELEASE_ID,
        activation_id="synthetic.parking.activation",
        scope=SCOPE,
        expected_previous_activation_id=None,
        principal=principal,
    )
    task = releases.accept_task(submission, principal=principal)

    next_source = replace(
        source, metadata=replace(source.metadata, bundle_version="1.0.1")
    )
    next_bundle, _, next_receipt = admit(next_source, "next")
    releases.register_release(
        operation_key="register.synthetic.scene.next",
        release_id=NEXT_RELEASE_ID,
        admission_receipt_id=next_receipt.receipt_id,
        principal=principal,
    )
    releases.approve_release(
        operation_key="approve.synthetic.scene.next",
        release_id=NEXT_RELEASE_ID,
        scope=SCOPE,
        principal=principal,
    )
    next_activation = releases.activate(
        operation_key="activate.synthetic.scene.next",
        release_id=NEXT_RELEASE_ID,
        activation_id="synthetic.parking.activation.next",
        scope=SCOPE,
        expected_previous_activation_id=activated.activation_id,
        principal=principal,
    )

    reopened_store = LocalBundleStore(store_dir)
    reopened_manager = manager(reopened_store)
    reopened_task = reopened_manager.get_task(task.task_id)
    current = reopened_manager.active_activation(SCOPE)
    if (
        reopened_task != task
        or reopened_manager.get_task_activation(reopened_task) != activated
        or current != next_activation
        or current.bundle_ref != next_bundle.bundle_ref
        or current.bundle_ref.bundle_digest == task.bundle_digest
    ):
        raise RuntimeError(
            "The reopened ledger must keep distinct historical and current Releases"
        )
    execution = resolve_execution_pins(
        reopened_manager,
        reopened_store,
        reopened_task,
        scope=submission.idempotency_scope,
        host=host,
        validation_at=AT,
        limits=LIMITS,
    )
    if (
        execution.pins.release_id != task.release_id
        or execution.pins.bundle_digest != task.bundle_digest
        or execution.files.manifest.integrity.root_digest != task.bundle_digest
    ):
        raise RuntimeError(
            "The reopened execution must keep its historical Release pin"
        )
    return ReopenedScene(bundle, signed, release, activated, current, task, execution)
