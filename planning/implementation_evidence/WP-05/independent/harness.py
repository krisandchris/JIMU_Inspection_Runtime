"""Independent trusted host. The product owns all nodes and decisions."""

import json
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path

from cases import Frame
from sqlalchemy.ext.asyncio import AsyncEngine

from examples.nonmotor_vehicle_illegal_parking.compilable import scene
from examples.nonmotor_vehicle_illegal_parking.compilable.lifecycle import (
    CAMERA,
    DEVICE,
    SCOPE,
    local_submission,
)
from examples.nonmotor_vehicle_illegal_parking.compilable.local_trust import (
    LocalEd25519,
    sign_bundle,
)
from jimu_runtime.bundle.admission import AdmissionEnvironment, admit_bundle
from jimu_runtime.bundle.archive import read_bundle
from jimu_runtime.bundle.capabilities import CapabilitySnapshot
from jimu_runtime.bundle.catalogue import build_catalogue
from jimu_runtime.bundle.compiler import (
    BundleCompileInput,
    CompileEnvironment,
    SourceArtifact,
    SourceIdentity,
    attach_signature_refs,
    compile_bundle,
)
from jimu_runtime.bundle.releases import (
    ReleaseAction,
    ReleaseManager,
    ReleaseTarget,
    ResolvedTaskScope,
)
from jimu_runtime.bundle.semantic_inputs import require_definition
from jimu_runtime.bundle.store import LocalBundleStore
from jimu_runtime.bundle.trust import TrustPin, verify_bundle_trust
from jimu_runtime.domain.definition_support import ReviewReportRef
from jimu_runtime.domain.definitions.ontology import RegionDefinition
from jimu_runtime.domain.enums import ArtifactKind
from jimu_runtime.domain.runtime.authority import ActorRef
from jimu_runtime.domain.runtime.executions import NodeId
from jimu_runtime.domain.runtime.intake import (
    AcceptedTask,
    IdempotencyScope,
    RegistryRecordRef,
    TaskSubmission,
)
from jimu_runtime.domain.runtime.media import ImageDimensions, MediaAsset, MediaRef
from jimu_runtime.domain.runtime.releases import ActivationRecord
from jimu_runtime.gateway.http import ChatEndpoint, HttpLimits
from jimu_runtime.gateway.routing import (
    CurrentRoutingState,
    DeploymentOwnership,
    DeploymentOwnershipRegistration,
)
from jimu_runtime.inspection.geometry import CPU_ARTIFACT
from jimu_runtime.inspection.media import LocalMediaStore, MediaError
from jimu_runtime.workflow.intake import (
    IntakeAuthorizationError,
    IntakeIdentity,
    IntakeResult,
    IntakeService,
)
from jimu_runtime.workflow.pins import ResolvedExecutionPins
from jimu_runtime.workflow.scheduling import (
    JobBudget,
    NodeBudgetOverride,
    SchedulingLimits,
)
from jimu_runtime.workflow.worker_capabilities import ToolOwnershipPin
from jimu_runtime.workflow.worker_source import TrustedWorkerSource

VERSION = "1.0.0"
DIMENSIONS = ImageDimensions(width=64, height=48)
ACTOR = ActorRef(
    actor_id="independent.wp05.host",
    actor_kind="service",
    identity_registry_id="independent.registry",
)
AUTH = IntakeIdentity(issuer="jimu-independent", subject="edge.test")
HOST_START = datetime(2025, 1, 1, tzinfo=UTC)
HOST_END = datetime(2100, 1, 1, tzinfo=UTC)


def digest(value: bytes) -> str:
    return sha256(value).hexdigest()


def _allow_second_tool_attempt(source: BundleCompileInput) -> BundleCompileInput:
    (policy,) = [
        artifact
        for artifact in source.artifacts
        if artifact.artifact_id == "tool-execution"
    ]
    version = "1.0.1"
    body = json.loads(policy.content)
    assert body["policy"]["max_attempts"] == 1
    body["version"] = version
    body["policy"]["max_attempts"] = 2
    body["policy"]["retry_backoff_ms"] = [20]
    body["policy"]["retryable_error_classes"] = ["transport", "timeout"]
    revised = replace(
        policy,
        version=version,
        content=json.dumps(body, sort_keys=True).encode(),
        origin=SourceIdentity(
            artifact_id="independent.wp05.tool-execution-restart", version=version
        ),
    )
    return _rebind_artifact(source, policy, revised)


def _without_candidate(source: BundleCompileInput) -> BundleCompileInput:
    (rule_set,) = [
        artifact
        for artifact in source.artifacts
        if artifact.artifact_id == "parking-rules"
    ]
    version = "1.0.1"
    body = json.loads(rule_set.content)
    assert "candidate_binding_ref" in body
    del body["candidate_binding_ref"]
    body["version"] = version
    revised = replace(
        rule_set,
        version=version,
        content=json.dumps(body, sort_keys=True).encode(),
        origin=SourceIdentity(
            artifact_id="independent.wp05.rules-without-candidate", version=version
        ),
        reference_slots=tuple(
            slot
            for slot in rule_set.reference_slots
            if slot.location != ("candidate_binding_ref",)
        ),
    )
    assert len(revised.reference_slots) == len(rule_set.reference_slots) - 1
    return _rebind_artifact(source, rule_set, revised)


def _rebind_artifact(
    source: BundleCompileInput, original: SourceArtifact, revised: SourceArtifact
) -> BundleCompileInput:
    return scene.with_source_provenance(
        replace(
            source,
            artifacts=tuple(
                revised
                if artifact is original
                else replace(
                    artifact,
                    reference_slots=tuple(
                        replace(slot, target=revised.identity)
                        if slot.target == original.identity
                        else slot
                        for slot in artifact.reference_slots
                    ),
                )
                for artifact in source.artifacts
            ),
        )
    )


@dataclass(frozen=True, slots=True)
class ReleaseEvidence:
    activation: ActivationRecord
    bundle_digest: str


class Harness:
    def __init__(self, db: AsyncEngine, root: Path, endpoint_url: str) -> None:
        self.db = db
        self.root = root
        self.root.mkdir(exist_ok=True)
        self.ledger = LocalBundleStore(root / "releases")
        self.media = LocalMediaStore(root / "media", db)
        self.principal = object()
        self.scene_type = None
        self.revision = VERSION
        seed = scene.capability_snapshot(scene.load_source())
        self.capability_seed = seed
        temporal = {"valid_from": HOST_START, "valid_until": HOST_END}
        self.snapshot = seed.model_copy(
            update={
                **temporal,
                "capabilities": tuple(
                    row.model_copy(update=temporal) for row in seed.capabilities
                ),
                "model_deployments": tuple(
                    row.model_copy(update=temporal) for row in seed.model_deployments
                ),
                "runtime_protocols": tuple(
                    row.model_copy(update=temporal) for row in seed.runtime_protocols
                ),
            }
        )
        registered = self.snapshot.model_deployments[0].identity
        self.endpoint = ChatEndpoint(
            endpoint_id="independent.parking.endpoint",
            endpoint_version=VERSION,
            provider_id=registered.provider_id,
            provider_kind=registered.provider_kind,
            deployment_id=registered.deployment_id,
            model_id="independent-parking-vision",
            url=endpoint_url,
            protocol="openai_chat_completions",
            allow_plaintext_loopback=True,
            retain_complete_response=True,
            limits=HttpLimits(
                timeout_ms=1700,
                io_timeout_ms=1600,
                request_bytes=131072,
                response_bytes=32768,
                image_bytes=16384,
                max_retry_after_seconds=1,
            ),
        )
        self.ownership = DeploymentOwnership(
            deployment=registered,
            owner_ref=TrustPin(
                identity="wp05.independent.model-owner",
                version=VERSION,
                sha256=digest(b"independent loopback deployment"),
            ),
            endpoint_descriptor_ref=self.endpoint.descriptor_pin,
            model_id=self.endpoint.model_id,
            version_evidence=ReviewReportRef(
                report_id="wp05.independent.model-version",
                version=VERSION,
                sha256=digest(b"independent loopback version registry"),
                review_registry_id="independent.registry",
            ),
        )
        self.scope = local_submission(b"scope construction only").idempotency_scope
        self.model_revoked_at: datetime | None = None

    def _authorize(
        self, principal: object, action: ReleaseAction, target: ReleaseTarget
    ):
        if principal is not self.principal or not target.release_id.startswith(
            "independent.release."
        ):
            return None
        if action is ReleaseAction.REGISTER and target.scope.scope == "global":
            return ACTOR
        if (
            action
            in {
                ReleaseAction.APPROVE,
                ReleaseAction.ACTIVATE,
                ReleaseAction.ACCEPT_TASK,
            }
            and target.scope == SCOPE
        ):
            return ACTOR
        return None

    def _resolve(self, principal: object, request: TaskSubmission):
        if principal is not self.principal or not self._allowed(request):
            return None
        assert self.scene_type is not None
        return ResolvedTaskScope(
            device_id=DEVICE,
            camera_id=CAMERA,
            region_id=scene.REGION,
            scene_type=self.scene_type,
        )

    def _allowed(self, request: TaskSubmission) -> bool:
        return (
            request.idempotency_scope,
            request.context.device_id,
            request.context.camera_id,
            request.context.region_id,
        ) == (
            self.scope,
            DEVICE,
            CAMERA,
            scene.REGION,
        )

    def manager(self) -> ReleaseManager:
        return ReleaseManager(
            LocalBundleStore(self.root / "releases"),
            authorize=self._authorize,
            resolve_task_scope=self._resolve,
            scope_precedence=("device", "region", "scene", "global"),
            allow_new_task=lambda _r, activation, scope, _at: activation.scope == SCOPE
            and scope.region_id == scene.REGION,
            clock=lambda: scene.AT,
        )

    def release(
        self,
        number: int,
        *,
        allow_second_tool_attempt: bool = False,
        omit_candidate: bool = False,
    ) -> ReleaseEvidence:
        stage = self.root / f"bundle-{number}"
        stage.mkdir()
        source = scene.load_source()
        if allow_second_tool_attempt:
            source = _allow_second_tool_attempt(source)
        if omit_candidate:
            source = _without_candidate(source)
        source = replace(
            source, metadata=replace(source.metadata, bundle_version=f"1.0.{number}")
        )
        compiled = compile_bundle(
            source,
            limits=scene.LIMITS,
            environment=CompileEnvironment(
                capabilities=self.capability_seed,
                trust=LocalEd25519(stage),
                validation_at=scene.AT,
            ),
        )
        assert compiled.bundle is not None, compiled.errors
        expected = compiled.bundle.bundle_ref.bundle_digest
        verifier, signature = sign_bundle(stage, expected)
        archive = attach_signature_refs(
            compiled.bundle.archive_bytes, (signature,), limits=scene.LIMITS
        )
        files = read_bundle(archive, scene.LIMITS)
        trusted = verify_bundle_trust(
            files.manifest, expected, verifier, validation_at=scene.AT
        )
        assert len(trusted.signatures) == 1
        admitted = admit_bundle(
            archive,
            store=self.ledger,
            operation_id=f"wp05.admit.{number}",
            actor=ACTOR,
            limits=scene.LIMITS,
            environment=AdmissionEnvironment(
                runtime_identity=ACTOR,
                capabilities=self.capability_seed,
                trust=verifier,
                validation_at=scene.AT,
            ),
        )
        assert admitted.accepted and admitted.receipt is not None, admitted
        catalogue = build_catalogue(files.manifest, files.payloads, **scene.JSON_LIMITS)
        (region,) = catalogue.of_kind(ArtifactKind.REGION)
        self.scene_type = require_definition(
            catalogue, region.ref, RegionDefinition
        ).scene_type
        manager = self.manager()
        previous = manager.active_activation(SCOPE)
        release_id = f"independent.release.{number}"
        manager.register_release(
            operation_key=f"wp05.register.{number}",
            release_id=release_id,
            admission_receipt_id=admitted.receipt.receipt_id,
            principal=self.principal,
        )
        manager.approve_release(
            operation_key=f"wp05.approve.{number}",
            release_id=release_id,
            scope=SCOPE,
            principal=self.principal,
        )
        active = manager.activate(
            operation_key=f"wp05.activate.{number}",
            release_id=release_id,
            activation_id=f"wp05.activation.{number}",
            scope=SCOPE,
            expected_previous_activation_id=None
            if previous is None
            else previous.activation_id,
            principal=self.principal,
        )
        assert self.manager().active_activation(SCOPE) == active
        return ReleaseEvidence(active, expected)

    def request(self, label: str, frame: Frame) -> TaskSubmission:
        request = local_submission(frame.content)
        return TaskSubmission.model_validate(
            {
                **request.model_dump(),
                "request_id": f"wp05.request.{label}",
                "idempotency_key": f"wp05.input.{label}",
                "media_ref": MediaRef(
                    media_id=f"wp05.media.{label}", sha256=frame.digest
                ),
            }
        )

    def service(self, *, retries: bool, recover_tool: bool = False) -> IntakeService:
        overrides = []
        for enabled, node, conditions in (
            (retries, NodeId.N04, ("validation", "transport", "timeout")),
            (recover_tool, NodeId.N05, ("transport", "timeout")),
        ):
            if enabled:
                overrides.append(
                    NodeBudgetOverride(
                        node_id=node,
                        budget=JobBudget(
                            max_attempts=2,
                            timeout_ms=15000,
                            retry_backoff_ms=(20,),
                            retryable_error_classes=conditions,
                            retry_on_lease_loss=True,
                        ),
                    )
                )
        return IntakeService(
            self.db,
            self.manager(),
            self.ledger,
            host=self,
            bundle_limits=scene.LIMITS,
            scheduling_limits=SchedulingLimits(
                schema_version=VERSION,
                version=VERSION,
                default=JobBudget(
                    max_attempts=1,
                    timeout_ms=15000,
                    retry_backoff_ms=(),
                    retryable_error_classes=(),
                    retry_on_lease_loss=False,
                ),
                overrides=tuple(overrides),
            ),
            max_submission_bytes=65536,
            historical_ownership=self._pin_models,
            historical_tool_ownership=self._pin_tools,
        )

    async def accept(
        self,
        request: TaskSubmission,
        frame: Frame,
        *,
        retries: bool = False,
        recover_tool: bool = False,
    ) -> IntakeResult:
        asset = await self.media.publish_original(
            self.scope,
            request.media_ref,
            mime_type="image/png",
            dimensions=DIMENSIONS,
            content=frame.content,
        )
        assert asset.media_ref.sha256 == frame.digest
        service = self.service(retries=retries, recover_tool=recover_tool)
        result = await service.submit(request, principal=self.principal)
        assert await service.submit(request, principal=self.principal) == result
        return result

    async def _pin_models(self, scope: IdempotencyScope, registry: CapabilitySnapshot):
        assert scope == self.scope and registry == self.snapshot
        return (self.ownership,)

    def tool_proofs(self) -> dict[str, TrustPin]:
        proof = digest(CPU_ARTIFACT.read_bytes())
        return {
            capability.capability_id: TrustPin(
                identity=capability.capability_id, version=VERSION, sha256=proof
            )
            for capability in self.snapshot.capabilities
            if capability.capability_id.startswith("synthetic.cpu.")
        }

    async def _pin_tools(
        self, scope: IdempotencyScope, resolved: ResolvedExecutionPins
    ):
        assert scope == self.scope
        proofs = self.tool_proofs()
        return tuple(
            ToolOwnershipPin(
                tool_ref=item.tool_ref,
                implementation_version=item.implementation_version,
                implementation_proof_ref=proofs[
                    f"synthetic.cpu.{item.tool_ref.artifact_id}"
                ],
            )
            for item in resolved.pins.tool_pins
        )

    async def current_models(self, scope: IdempotencyScope) -> CurrentRoutingState:
        return CurrentRoutingState(
            capabilities=await self.capabilities(scope),
            ownership=(
                DeploymentOwnershipRegistration(
                    ownership=self.ownership,
                    valid_from=HOST_START,
                    valid_until=HOST_END,
                    revoked_at=self.model_revoked_at,
                ),
            ),
        )

    async def authorize_submission(
        self, principal: object, submission: TaskSubmission
    ) -> IntakeIdentity:
        if principal is not self.principal or not self._allowed(submission):
            raise IntakeAuthorizationError()
        return AUTH

    async def restore_principal(
        self, identity: IntakeIdentity, submission: TaskSubmission
    ) -> object:
        if identity != AUTH or not self._allowed(submission):
            raise IntakeAuthorizationError()
        return self.principal

    async def verified_original(
        self, scope: IdempotencyScope, media_ref: MediaRef
    ) -> MediaAsset:
        if scope != self.scope:
            raise IntakeAuthorizationError()
        try:
            return await self.media.verified_original(scope, media_ref)
        except MediaError:
            raise IntakeAuthorizationError() from None

    async def capabilities(self, scope: IdempotencyScope) -> CapabilitySnapshot:
        if scope != self.scope:
            raise IntakeAuthorizationError()
        return self.snapshot

    async def worker_source(
        self, scope: IdempotencyScope, task: AcceptedTask
    ) -> TrustedWorkerSource:
        if scope != self.scope:
            raise IntakeAuthorizationError()
        verified = await self.media.verified_task_original(scope, task)
        device, camera = (
            RegistryRecordRef(
                record_id=rid,
                version=self.revision,
                digest=digest(f"{rid}/{self.revision}".encode()),
            )
            for rid in (DEVICE, CAMERA)
        )
        return TrustedWorkerSource(media=verified.asset, device=device, camera=camera)
