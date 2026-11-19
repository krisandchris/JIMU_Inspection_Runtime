"""Opt-in local fixture host. All operational trust is supplied here explicitly."""

import argparse
import asyncio
import os
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from secrets import compare_digest
from typing import Literal

import uvicorn
from sqlalchemy.ext.asyncio import AsyncEngine
from synthetic_provider import MODEL_ID, PROVIDER_PORT, synthetic_provider

from examples.nonmotor_vehicle_illegal_parking.compilable import scene
from examples.nonmotor_vehicle_illegal_parking.compilable.lifecycle import (
    ACTOR,
    CAMERA,
    DEVICE,
    NEXT_RELEASE_ID,
    RELEASE_ID,
    SCOPE,
    exercise_lifecycle,
    local_submission,
)
from jimu_runtime.api.app import create_app
from jimu_runtime.api.auth import Action, Identity, Resource
from jimu_runtime.api.contracts import EvidenceCoordinates
from jimu_runtime.api.lifecycle import LifecycleEvidence
from jimu_runtime.bundle.archive import read_bundle
from jimu_runtime.bundle.capabilities import CapabilitySnapshot
from jimu_runtime.bundle.catalogue import build_catalogue
from jimu_runtime.bundle.integrity import sha256_digest
from jimu_runtime.bundle.releases import (
    ReleaseAction,
    ReleaseManager,
    ReleaseTarget,
    ResolvedTaskScope,
)
from jimu_runtime.bundle.semantic_inputs import require_definition
from jimu_runtime.bundle.store import LocalBundleStore
from jimu_runtime.bundle.trust import TrustPin
from jimu_runtime.deployment.database import app_url, database_target, read_secret
from jimu_runtime.deployment.service import Service, mount_service, wait_for_database
from jimu_runtime.domain.definition_enums import ModelCapability
from jimu_runtime.domain.definition_support import VocabularyMemberRef
from jimu_runtime.domain.definitions.capabilities import (
    ModelBindingContentRef,
    ProfileDefinition,
)
from jimu_runtime.domain.definitions.models import ModelBinding
from jimu_runtime.domain.definitions.ontology import RegionDefinition
from jimu_runtime.domain.definitions.tools import ToolDefinition
from jimu_runtime.domain.enums import ArtifactKind, RuntimeRecordKind
from jimu_runtime.domain.runtime.authority import ActorRef
from jimu_runtime.domain.runtime.executions import NodeId
from jimu_runtime.domain.runtime.intake import (
    AcceptedTask,
    IdempotencyScope,
    RegistryRecordRef,
    TaskSubmission,
)
from jimu_runtime.domain.runtime.media import ImageDimensions, MediaAsset, MediaRef
from jimu_runtime.domain.runtime.replay import (
    ExecutionRef,
    InferenceReplayRequest,
    ReplayExecutionSource,
    ReplayKind,
)
from jimu_runtime.gateway.bridge import CapabilityGateway, ModelCallLimits
from jimu_runtime.gateway.bridge_tools import ToolExecutionAdapter
from jimu_runtime.gateway.http import ChatEndpoint, HttpLimits, HttpModelTransport
from jimu_runtime.gateway.journal import DispatchJournal
from jimu_runtime.gateway.routing import (
    CurrentRoutingState,
    DeploymentOwnership,
    DeploymentOwnershipRegistration,
)
from jimu_runtime.inspection.geometry import CPU_ARTIFACT, synthetic_cpu_implementations
from jimu_runtime.inspection.geometry.node import GeometryNodeAdapter
from jimu_runtime.inspection.media import LocalMediaStore
from jimu_runtime.inspection.media.retention import LocalEvidenceLifecycle
from jimu_runtime.inspection.media.retention_models import (
    EvidenceAccessGrant,
    EvidenceLocator,
)
from jimu_runtime.inspection.media.retention_policy import HistoricalEvidencePolicies
from jimu_runtime.inspection.perception import build_chat_contract
from jimu_runtime.replay.artifact import ArtifactReplayService
from jimu_runtime.replay.inference import InferenceReplayService
from jimu_runtime.tools.executor import ToolExecutor
from jimu_runtime.tools.registry import ToolWorkerRegistry
from jimu_runtime.tools.step_attempts import StepAttemptStore
from jimu_runtime.workers.resource_inventory import (
    HostInventory,
    RemoteCapacity,
    RemoteQuota,
    ResourceScope,
)
from jimu_runtime.workers.resources import ResourceAdmission
from jimu_runtime.workflow import repository
from jimu_runtime.workflow.intake import (
    IntakeAuthorizationError,
    IntakeIdentity,
    IntakeService,
)
from jimu_runtime.workflow.mock_support import VerifiedExecutionScope
from jimu_runtime.workflow.pins import ResolvedExecutionPins
from jimu_runtime.workflow.postgres import create_postgres_engine
from jimu_runtime.workflow.scheduling import (
    JobBudget,
    NodeBudgetOverride,
    SchedulingLimits,
)
from jimu_runtime.workflow.worker_capabilities import ToolOwnershipPin
from jimu_runtime.workflow.worker_inspection import (
    VerifiedMediaWorkerHost,
    inspection_worker,
)

_ROOT = Path("/var/lib/jimu")
_VERSION: Literal["1.0.0"] = "1.0.0"
_VALID_FROM = datetime(2025, 1, 1, tzinfo=UTC)
_VALID_UNTIL = datetime(2100, 1, 1, tzinfo=UTC)
_ID = IntakeIdentity(issuer="jimu.compose.local", subject="synthetic.inspector")
_ACTOR = ActorRef(
    actor_kind="service",
    actor_id=_ID.subject,
    identity_registry_id="jimu.compose.local",
)
_ACTIONS = frozenset(
    {
        Action.ACCESS_SCOPE,
        Action.TASK_SUBMIT,
        Action.TASK_READ,
        Action.TASK_RESULT,
        Action.EXECUTION_READ,
        Action.EXECUTION_TRACE,
        Action.ARTIFACT_REPLAY,
        Action.INFERENCE_REPLAY,
        Action.EVIDENCE_READ,
        Action.EVIDENCE_NORMALIZED,
        Action.EVIDENCE_RAW,
    }
)


def _frame() -> bytes:
    mode = os.environ.get("JIMU_SYNTHETIC_FRAME_MODE")
    if mode is None:
        return (scene.DIRECTORY / "assets/synthetic-frame.png").read_bytes()
    if mode != "approved":
        raise ValueError("unknown synthetic frame mode")
    with Path("/run/secrets/approved_frame").open("rb") as stream:
        frame = stream.read(16385)
    if not 0 < len(frame) <= 16384:
        raise ValueError("approved synthetic frame exceeds the local byte budget")
    return frame


def _submission(frame: bytes) -> TaskSubmission:
    submission = local_submission(frame)
    digest = submission.media_ref.sha256
    return TaskSubmission.model_validate(
        {
            **submission.model_dump(),
            "request_id": f"synthetic.request.{digest}",
            "idempotency_key": f"synthetic.frame.{digest}",
            "media_ref": {"media_id": f"synthetic.rgb.{digest}", "sha256": digest},
            "trace": {"trace_id": f"synthetic.trace.{digest}"},
        }
    )


def _scheduling() -> SchedulingLimits:
    default = JobBudget(
        max_attempts=2,
        timeout_ms=15000,
        retry_backoff_ms=(50,),
        retryable_error_classes=("transport", "timeout", "resource"),
        retry_on_lease_loss=True,
    )
    observation = JobBudget.model_validate(
        {
            **default.model_dump(),
            "retryable_error_classes": (
                *default.retryable_error_classes,
                "validation",
            ),
        }
    )
    return SchedulingLimits(
        schema_version=_VERSION,
        version=_VERSION,
        default=default,
        overrides=(NodeBudgetOverride(node_id=NodeId.N04, budget=observation),),
    )


def _capabilities() -> CapabilitySnapshot:
    base = scene.capability_snapshot(scene.load_source())
    window = {"valid_from": _VALID_FROM, "valid_until": _VALID_UNTIL}
    return base.model_copy(
        update={
            **window,
            "capabilities": tuple(
                item.model_copy(update=window) for item in base.capabilities
            ),
            "model_deployments": tuple(
                item.model_copy(update=window) for item in base.model_deployments
            ),
            "runtime_protocols": tuple(
                item.model_copy(update=window) for item in base.runtime_protocols
            ),
        }
    )


@dataclass(frozen=True, slots=True)
class LocalAuthority:
    submission: TaskSubmission
    media: LocalMediaStore
    token: str = field(repr=False)
    principal: object = field(default_factory=object, repr=False)

    def verify_scope(self, scope: IdempotencyScope) -> None:
        if scope != self.submission.idempotency_scope:
            raise IntakeAuthorizationError()

    async def authenticate(self, credential: str) -> Identity | None:
        if not credential.isascii() or not compare_digest(credential, self.token):
            return None
        return Identity(self.principal, _ACTOR)

    async def allows(self, identity: Identity, resource: Resource) -> bool:
        return (
            identity.principal is self.principal
            and resource.scope == self.submission.idempotency_scope
            and resource.action in _ACTIONS
        )

    async def authorize_submission(
        self, principal: object, submission: TaskSubmission
    ) -> IntakeIdentity:
        self.verify_scope(submission.idempotency_scope)
        expected = self.submission
        if principal is not self.principal or (
            submission.media_ref,
            submission.context,
        ) != (expected.media_ref, expected.context):
            raise IntakeAuthorizationError()
        return _ID

    async def restore_principal(
        self, identity: IntakeIdentity, submission: TaskSubmission
    ) -> object:
        if identity != _ID:
            raise IntakeAuthorizationError()
        await self.authorize_submission(self.principal, submission)
        return self.principal

    async def authorize_inference_replay(
        self,
        principal: object,
        scope: IdempotencyScope,
        request: InferenceReplayRequest,
        source_task: AcceptedTask,
    ) -> IntakeIdentity:
        self.verify_scope(scope)
        if (
            principal is not self.principal
            or source_task.idempotency_scope != scope
            or source_task.task_id != request.source.task_id
            or source_task.media_ref != self.submission.media_ref
            or source_task.context != self.submission.context
        ):
            raise IntakeAuthorizationError()
        return _ID

    async def verified_original(
        self, scope: IdempotencyScope, media_ref: MediaRef
    ) -> MediaAsset:
        self.verify_scope(scope)
        return await self.media.verified_original(scope, media_ref)

    async def references(
        self, scope: IdempotencyScope, task: AcceptedTask
    ) -> tuple[RegistryRecordRef, RegistryRecordRef]:
        self.verify_scope(scope)
        return (
            RegistryRecordRef(
                record_id=task.context.device_id,
                version=_VERSION,
                digest=sha256_digest(DEVICE.encode()),
            ),
            RegistryRecordRef(
                record_id=task.context.camera_id,
                version=_VERSION,
                digest=sha256_digest(CAMERA.encode()),
            ),
        )


def _manager(
    store: LocalBundleStore,
    authority: LocalAuthority,
    scene_type: VocabularyMemberRef | None,
) -> ReleaseManager:
    def authorize(
        principal: object, action: ReleaseAction, target: ReleaseTarget
    ) -> ActorRef | None:
        return (
            ACTOR
            if principal is authority.principal
            and action is ReleaseAction.ACCEPT_TASK
            and (
                target.scope == SCOPE
                and target.release_id in {RELEASE_ID, NEXT_RELEASE_ID}
            )
            else None
        )

    def resolve(principal: object, request: TaskSubmission) -> ResolvedTaskScope | None:
        if (
            principal is not authority.principal
            or scene_type is None
            or request.idempotency_scope != authority.submission.idempotency_scope
        ):
            return None
        return ResolvedTaskScope(
            device_id=DEVICE,
            camera_id=CAMERA,
            region_id=scene.REGION,
            scene_type=scene_type,
        )

    return ReleaseManager(
        store,
        authorize=authorize,
        clock=lambda: datetime.now(UTC),
        resolve_task_scope=resolve,
        scope_precedence=("device", "region", "scene", "global"),
        allow_new_task=lambda _release, activation, resolved, _now: activation.scope
        == SCOPE
        and resolved.region_id == scene.REGION,
    )


async def serve(engine: AsyncEngine, frame: bytes) -> None:
    target = database_target()
    await wait_for_database(engine, target.database_comment)
    store = LocalBundleStore(_ROOT / "releases")
    media = LocalMediaStore(_ROOT / "media", engine)
    submission = _submission(frame)
    scope = submission.idempotency_scope
    authority = LocalAuthority(submission, media, read_secret("fixture_token"))
    releases = _manager(store, authority, None)
    activation = releases.active_activation(SCOPE)
    if activation is None:
        await asyncio.to_thread(exercise_lifecycle, _ROOT)
        activation = releases.active_activation(SCOPE)
    if activation is None or activation.release_ref.record_id not in {
        RELEASE_ID,
        NEXT_RELEASE_ID,
    }:
        raise ValueError("unexpected local Release activation")
    release = releases.get_release(activation.release_ref.record_id, scope=SCOPE)
    if release is None or release.admission_receipt is None:
        raise ValueError("local Release admission unavailable")
    bundle = read_bundle(
        store.read_archive(release.admission_receipt.receipt_id), scene.LIMITS
    )
    catalogue = build_catalogue(bundle.manifest, bundle.payloads, **scene.JSON_LIMITS)
    (region_ref,) = (
        artifact.ref
        for artifact in catalogue.of_kind(ArtifactKind.REGION)
        if artifact.ref.artifact_id == scene.REGION
    )
    region = require_definition(catalogue, region_ref, RegionDefinition)
    releases = _manager(store, authority, region.scene_type)
    original = await media.publish_original(
        scope,
        submission.media_ref,
        content=frame,
        mime_type="image/png",
        dimensions=ImageDimensions(width=64, height=48),
    )
    contract_submission = TaskSubmission.model_validate(
        {
            **submission.model_dump(),
            "request_id": "local.compose.contract",
            "idempotency_key": "local.compose.contract",
        }
    )
    accepted = releases.accept_task(contract_submission, principal=authority.principal)
    caps = _capabilities()

    async def capabilities(scope: IdempotencyScope) -> CapabilitySnapshot:
        authority.verify_scope(scope)
        return caps

    endpoint = ChatEndpoint(
        endpoint_id="synthetic.compose.endpoint",
        endpoint_version=_VERSION,
        provider_id=caps.model_deployments[0].identity.provider_id,
        provider_kind="remote",
        deployment_id=caps.model_deployments[0].identity.deployment_id,
        model_id=MODEL_ID,
        url=f"http://127.0.0.1:{PROVIDER_PORT}/v1/chat/completions",
        allow_plaintext_loopback=True,
        protocol="openai_chat_completions",
        retain_complete_response=True,
        limits=HttpLimits(
            timeout_ms=5000,
            io_timeout_ms=4500,
            request_bytes=131072,
            response_bytes=32768,
            image_bytes=16384,
            max_retry_after_seconds=1,
        ),
    )
    ownership = DeploymentOwnership(
        deployment=caps.model_deployments[0].identity,
        owner_ref=TrustPin(
            identity=endpoint.endpoint_id,
            version=_VERSION,
            sha256=endpoint.descriptor_pin.sha256,
        ),
        endpoint_descriptor_ref=endpoint.descriptor_pin,
        model_id=MODEL_ID,
        version_evidence=None,
    )
    routing = CurrentRoutingState(
        capabilities=caps,
        ownership=(
            DeploymentOwnershipRegistration(
                ownership=ownership,
                valid_from=_VALID_FROM,
                valid_until=_VALID_UNTIL,
                revoked_at=None,
            ),
        ),
    )

    async def current_routing(scope: IdempotencyScope) -> CurrentRoutingState:
        authority.verify_scope(scope)
        return routing

    async def model_pins(
        request_scope: IdempotencyScope, actual: CapabilitySnapshot
    ) -> tuple[DeploymentOwnership, ...]:
        authority.verify_scope(request_scope)
        if actual != caps:
            raise ValueError("capability snapshot changed")
        return (ownership,)

    from jimu_runtime.workflow.pins import resolve_execution_pins

    pins = resolve_execution_pins(
        releases,
        store,
        accepted,
        scope=scope,
        host=caps,
        validation_at=datetime.now(UTC),
        limits=scene.LIMITS,
    )
    bound = VerifiedExecutionScope(
        accepted,
        "local.compose.contract",
        pins.pins,
        original,
        bundle,
        scene.LIMITS.max_file_bytes,
        scene.LIMITS.max_json_depth,
    )
    profile = bound.resolve(
        bound.catalogue.of_kind(ArtifactKind.PROFILE)[0].ref, ProfileDefinition
    )
    contracts = []
    for item in bound.catalogue.of_kind(ArtifactKind.MODEL_BINDING):
        binding = bound.resolve(item.ref, ModelBinding)
        questions = (
            profile.question_refs
            if binding.capability is ModelCapability.OBJECT_OBSERVATION
            else ()
        )
        contracts.append(
            build_chat_contract(
                bound,
                ModelBindingContentRef.model_validate(item.ref.model_dump()),
                questions,
            )
        )
    definitions = tuple(
        bound.resolve(item.ref, ToolDefinition)
        for item in bound.catalogue.of_kind(ArtifactKind.TOOL)
    )
    proofs = {
        tool.implementation_requirement.capability_id: TrustPin(
            identity=tool.implementation_requirement.capability_id,
            version=_VERSION,
            sha256=sha256_digest(CPU_ARTIFACT.read_bytes()),
        )
        for tool in definitions
    }

    async def tool_pins(
        request_scope: IdempotencyScope, actual: ResolvedExecutionPins
    ) -> tuple[ToolOwnershipPin, ...]:
        authority.verify_scope(request_scope)
        return tuple(
            ToolOwnershipPin(
                tool_ref=pin.tool_ref,
                implementation_version=pin.implementation_version,
                implementation_proof_ref=proofs[
                    bound.resolve(
                        pin.tool_ref, ToolDefinition
                    ).implementation_requirement.capability_id
                ],
            )
            for pin in actual.pins.tool_pins
        )

    inventory = HostInventory(
        "jimu.compose.local",
        lease_ttl_ms=18000,
        cpu_slots=1,
        scopes=(
            ResourceScope(
                "model", "remote", 1, pool_or_quota_id="synthetic.loopback.quota"
            ),
            ResourceScope("cpu", "cpu", 1),
        ),
        remote_quotas=(
            RemoteQuota("synthetic.loopback.quota", 1, RemoteCapacity.LOCAL_TRANSPORT),
        ),
    )
    resource_path = _ROOT / "resources.sqlite"
    resources = (
        ResourceAdmission(resource_path, inventory)
        if resource_path.exists()
        else ResourceAdmission.provision(resource_path, inventory)
    )
    journal = DispatchJournal(engine)
    gateway = CapabilityGateway(
        engine,
        journal,
        resources,
        HttpModelTransport((endpoint,), tuple(contracts), max_concurrency=1),
        current_routing,
        ModelCallLimits(8, 4, 8, ("transport", "timeout")),
        response_writer=media,
    )
    registry = ToolWorkerRegistry(
        synthetic_cpu_implementations(
            resources,
            "cpu",
            "compose-tools",
            definitions,
            proofs,
            synthetic_image_digests=frozenset({original.media_ref.sha256}),
        )
    )
    tools = GeometryNodeAdapter(
        media,
        ToolExecutionAdapter(
            ToolExecutor(registry),
            StepAttemptStore(engine, actor=ACTOR),
            journal,
            capabilities,
        ),
    )
    worker = inspection_worker(
        engine,
        releases,
        store,
        host=VerifiedMediaWorkerHost(media, capabilities, authority.references),
        bundle_limits=scene.LIMITS,
        media=media,
        gateway=gateway,
        execute_tools=tools.execute,
        model_resource_scope_id="model",
    )
    # The fixture host deliberately uses the same current registry for Intake and the Worker.
    intake = IntakeService(
        engine,
        releases,
        store,
        host=FixtureIntake(authority, caps),
        bundle_limits=scene.LIMITS,
        scheduling_limits=_scheduling(),
        max_submission_bytes=65536,
        historical_ownership=model_pins,
        historical_tool_ownership=tool_pins,
    )

    def grant(
        request_scope: IdempotencyScope, actor: ActorRef, task_id: str
    ) -> EvidenceAccessGrant:
        authority.verify_scope(request_scope)
        return EvidenceAccessGrant(scope=request_scope, actor=actor, task_id=task_id)

    def locator(coords: EvidenceCoordinates) -> EvidenceLocator:
        return EvidenceLocator.model_validate(coords.model_dump())

    evidence = LifecycleEvidence(
        LocalEvidenceLifecycle(
            media, HistoricalEvidencePolicies(releases, store, scene.LIMITS)
        ),
        grant,
        locator,
    )
    app = mount_service(
        create_app(
            engine,
            intake,
            authority=authority,
            evidence=evidence,
            artifact_replay=ArtifactReplayService(engine),
            inference_replay=InferenceReplayService(
                engine, releases, store, host=authority, bundle_limits=scene.LIMITS
            ),
        ),
        Service(engine, worker, (scope,), "compose-local", target.database_comment),
    )
    config = uvicorn.Config(
        app,
        host="0.0.0.0",
        port=8080,
        access_log=False,
        proxy_headers=False,
        server_header=False,
        timeout_graceful_shutdown=20,
        limit_concurrency=32,
    )
    server = uvicorn.Server(config)
    await server.serve()
    if not server.started:
        raise ValueError("service failed to start")


@dataclass(frozen=True, slots=True)
class FixtureIntake:
    authority: LocalAuthority
    snapshot: CapabilitySnapshot

    async def authorize_submission(
        self, principal: object, submission: TaskSubmission
    ) -> IntakeIdentity:
        return await self.authority.authorize_submission(principal, submission)

    async def restore_principal(
        self, identity: IntakeIdentity, submission: TaskSubmission
    ) -> object:
        return await self.authority.restore_principal(identity, submission)

    async def verified_original(
        self, scope: IdempotencyScope, ref: MediaRef
    ) -> MediaAsset:
        return await self.authority.verified_original(scope, ref)

    async def capabilities(self, scope: IdempotencyScope) -> CapabilitySnapshot:
        self.authority.verify_scope(scope)
        return self.snapshot


async def replay_operator(action: str, identity: str, frame: bytes) -> str:
    """Trusted local operator command, exactly scoped to the configured frame."""
    target = database_target()
    engine = create_postgres_engine(app_url(target))
    try:
        await wait_for_database(engine, target.database_comment)
        submission = _submission(frame)
        scope = submission.idempotency_scope
        artifact = ArtifactReplayService(engine)
        run = (
            await artifact.get_run(scope, identity)
            if action == "artifact-score"
            else None
        )
        if action == "artifact-score" and run is None:
            raise ValueError("artifact run unavailable")
        execution_id = (
            identity if run is None else run.request.source.execution_ref.record_id
        )
        async with engine.connect() as connection:
            row = await repository.get_execution(connection, scope, execution_id)
        if (
            row is None
            or row.task.media_ref != submission.media_ref
            or row.task.context != submission.context
        ):
            raise IntakeAuthorizationError()
        source = ReplayExecutionSource(
            execution_ref=ExecutionRef(
                record_kind=RuntimeRecordKind.EXECUTION, record_id=execution_id
            ),
            task_id=row.task.task_id,
            pins=row.execution.pins,
        )
        if action == "artifact-request":
            return (await artifact.prepare_request(scope, source)).model_dump_json()
        if run is None:
            return InferenceReplayRequest(
                schema_version=_VERSION,
                kind=ReplayKind.INFERENCE,
                source=source,
                target_pins=source.pins,
            ).model_dump_json()
        if run.request.source != source:
            raise ValueError("artifact run source does not match")
        evidence = await artifact.read_scoring_evidence(scope, run.scoring_evidence)
        if evidence is None:
            raise ValueError("scoring evidence unavailable")
        return evidence.model_dump_json()
    finally:
        await engine.dispose()


def main() -> None:
    cli = argparse.ArgumentParser()
    cli.add_argument(
        "action",
        choices=(
            "serve",
            "submission",
            "artifact-request",
            "artifact-score",
            "inference-request",
        ),
    )
    cli.add_argument("identity", nargs="?")
    args = cli.parse_args()
    action: str = args.action
    identity: str | None = args.identity
    if os.environ.get("JIMU_LOCAL_SYNTHETIC") != "1":
        raise SystemExit("Fixture mode requires explicit local opt-in")
    frame = _frame()
    if action == "submission":
        print(_submission(frame).model_dump_json())
        return
    try:
        if action in {"artifact-request", "artifact-score", "inference-request"}:
            if identity is None:
                cli.error("a scoped execution or artifact run ID is required")
            print(asyncio.run(replay_operator(action, identity, frame)))
            return
        if identity is not None:
            cli.error("this action does not accept an identity")
        with synthetic_provider(frame):
            asyncio.run(
                serve(create_postgres_engine(app_url(database_target())), frame)
            )
    except Exception as error:
        import logging

        logging.getLogger(__name__).error(
            "Local runtime command failed (%s)", type(error).__name__
        )
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
