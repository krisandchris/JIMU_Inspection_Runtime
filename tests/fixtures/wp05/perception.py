"""Local PostgreSQL, signed parking scene and host-controlled exact model bytes."""

import json
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

from sqlalchemy.ext.asyncio import AsyncEngine

from examples.nonmotor_vehicle_illegal_parking.compilable.lifecycle import (
    ACTOR,
    ReopenedScene,
    exercise_lifecycle,
    local_submission,
    prepare_scene,
)
from examples.nonmotor_vehicle_illegal_parking.compilable.scene import (
    AT,
    DIRECTORY,
    LIMITS,
    VERSION,
    capability_snapshot,
    load_source,
    with_source_provenance,
)
from jimu_runtime.bundle.capabilities import CapabilitySnapshot
from jimu_runtime.bundle.compiler import BundleCompileInput, SourceIdentity
from jimu_runtime.bundle.integrity import sha256_digest
from jimu_runtime.bundle.releases import ReleaseAction, ReleaseManager
from jimu_runtime.bundle.semantic_inputs import require_definition
from jimu_runtime.bundle.store import LocalBundleStore
from jimu_runtime.bundle.trust import TrustPin
from jimu_runtime.domain.definition_enums import EvidenceType, ModelCapability
from jimu_runtime.domain.definitions.capabilities import (
    ModelBindingContentRef,
    ProfileDefinition,
)
from jimu_runtime.domain.definitions.models import ModelBinding
from jimu_runtime.domain.definitions.prompts import PromptTemplateDefinition
from jimu_runtime.domain.definitions.tools import ToolDefinition
from jimu_runtime.domain.enums import ArtifactKind
from jimu_runtime.domain.nodes.observation import is_region_observation_template
from jimu_runtime.domain.policies import EvidencePolicyArtifact
from jimu_runtime.domain.runtime.evidence import (
    EvidenceAsset,
    NonSpatialEvidence,
    ObjectEvidenceScope,
    RetentionPolicyRef,
    TaskEvidenceAccess,
)
from jimu_runtime.domain.runtime.executions import NodeId
from jimu_runtime.domain.runtime.facts import ObjectRef, PolicyContentRef
from jimu_runtime.domain.runtime.intake import (
    AcceptedTask,
    IdempotencyScope,
    RegistryRecordRef,
    TaskSubmission,
)
from jimu_runtime.domain.runtime.media import (
    FinalizedAsset,
    ImageDimensions,
    MediaAsset,
    MediaRef,
    StorageReference,
)
from jimu_runtime.domain.runtime.perception import ObjectRecord
from jimu_runtime.gateway.bridge import CapabilityGateway, ModelCallLimits
from jimu_runtime.gateway.http import (
    ChatEndpoint,
    HttpLimits,
    HttpModelTransport,
    SecretResolver,
)
from jimu_runtime.gateway.http_wire import VerifiedImage
from jimu_runtime.gateway.journal import DispatchJournal
from jimu_runtime.gateway.routing import (
    CurrentRoutingState,
    DeploymentOwnership,
    DeploymentOwnershipRegistration,
)
from jimu_runtime.inspection.geometry import CPU_ARTIFACT
from jimu_runtime.inspection.media import LocalMediaStore
from jimu_runtime.inspection.perception import (
    ConfiguredPerception,
    RequestEvidenceKind,
    build_chat_contract,
)
from jimu_runtime.workers.resource_inventory import (
    HostInventory,
    RemoteCapacity,
    RemoteQuota,
    ResourceScope,
)
from jimu_runtime.workers.resources import ResourceAdmission
from jimu_runtime.workflow.intake import (
    IntakeAuthorizationError,
    IntakeIdentity,
    IntakeResult,
    IntakeService,
)
from jimu_runtime.workflow.mock_support import K, VerifiedExecutionScope
from jimu_runtime.workflow.pins import ResolvedExecutionPins, resolve_execution_pins
from jimu_runtime.workflow.scheduling import (
    JobBudget,
    NodeBudgetOverride,
    SchedulingLimits,
)
from jimu_runtime.workflow.worker import ClaimedNode
from jimu_runtime.workflow.worker_capabilities import ToolOwnershipPin
from jimu_runtime.workflow.worker_source import TrustedWorkerSource
from tests.fixtures.wp03.intake.support import migrated_application

IMAGE = (DIRECTORY / "assets/synthetic-frame.png").read_bytes()
_IMAGE_DIMENSIONS = ImageDimensions(width=64, height=48)
PROOF = TrustPin(
    identity="wp05.loopback.host",
    version=VERSION,
    sha256=sha256_digest(b"local-perception-host"),
)
_IDENTITY = IntakeIdentity(
    issuer="https://local.invalid/registry", subject=ACTOR.actor_id
)
_RESOURCE_SCOPE = "model"
_BUDGET = JobBudget(
    max_attempts=2,
    timeout_ms=8000,
    retry_backoff_ms=(10,),
    retryable_error_classes=("transport", "timeout"),
    retry_on_lease_loss=True,
)
_LIMITS = SchedulingLimits(
    schema_version="1.0.0",
    version=VERSION,
    default=_BUDGET,
    overrides=(
        NodeBudgetOverride(
            node_id=NodeId.N04,
            budget=JobBudget(
                max_attempts=2,
                timeout_ms=8000,
                retry_backoff_ms=(10,),
                retryable_error_classes=("transport", "timeout", "validation"),
                retry_on_lease_loss=True,
            ),
        ),
    ),
)


@dataclass(frozen=True)
class SceneHost:
    principal: object
    submission: TaskSubmission
    media: MediaAsset
    current: CapabilitySnapshot

    async def authorize_submission(
        self, principal: object, submission: TaskSubmission
    ) -> IntakeIdentity:
        if principal is not self.principal or submission != self.submission:
            raise IntakeAuthorizationError()
        return _IDENTITY

    async def restore_principal(
        self, identity: IntakeIdentity, submission: TaskSubmission
    ) -> object:
        if identity != _IDENTITY or submission != self.submission:
            raise IntakeAuthorizationError()
        return self.principal

    async def verified_original(
        self, scope: IdempotencyScope, media_ref: MediaRef
    ) -> MediaAsset:
        if (
            scope != self.submission.idempotency_scope
            or media_ref != self.media.media_ref
        ):
            raise IntakeAuthorizationError()
        return self.media

    async def capabilities(self, scope: IdempotencyScope) -> CapabilitySnapshot:
        if scope != self.submission.idempotency_scope:
            raise IntakeAuthorizationError()
        return self.current

    async def worker_source(
        self, scope: IdempotencyScope, task: AcceptedTask
    ) -> TrustedWorkerSource:
        return TrustedWorkerSource(
            await self.verified_original(scope, task.media_ref),
            RegistryRecordRef(
                record_id=task.context.device_id,
                version="1.0.2",
                digest=sha256_digest(b"device.v1.0.2"),
            ),
            RegistryRecordRef(
                record_id=task.context.camera_id,
                version="1.0.3",
                digest=sha256_digest(b"camera.v1.0.3"),
            ),
        )


@dataclass(frozen=True)
class PerceptionFixture:
    scene: ReopenedScene
    application: AsyncEngine
    releases: ReleaseManager
    store: LocalBundleStore
    host: SceneHost
    accepted: IntakeResult
    gateway: CapabilityGateway
    adapter: ConfiguredPerception
    journal: DispatchJournal
    model_id: str
    candidate_ref: ModelBindingContentRef
    resources: ResourceAdmission
    media_store: LocalMediaStore | None
    tool_proofs: dict[str, TrustPin]
    model_endpoints: tuple[ChatEndpoint, ...]


def original_media(
    scene: ReopenedScene,
    *,
    image: bytes = IMAGE,
    dimensions: ImageDimensions = _IMAGE_DIMENSIONS,
) -> MediaAsset:
    return _original_media(scene.task.media_ref, image, dimensions)


def _original_media(
    media_ref: MediaRef, image: bytes, dimensions: ImageDimensions
) -> MediaAsset:
    return MediaAsset(
        schema_version="1.0.0",
        media_ref=media_ref,
        storage=StorageReference(store_id="local-test", object_key="image.png"),
        stage=FinalizedAsset(
            stage="finalized",
            staged_at=AT,
            verified_at=AT,
            object_version=VERSION,
            verified_sha256=media_ref.sha256,
        ),
        mime_type="image/png",
        byte_size=len(image),
        dimensions=dimensions,
        color_space="RGB",
    )


def release_scope(
    scene: ReopenedScene, media: MediaAsset | None = None
) -> VerifiedExecutionScope:
    return VerifiedExecutionScope(
        scene.task,
        "wp05.admitted.perception",
        scene.execution.pins,
        media if media is not None else original_media(scene),
        scene.execution.files,
        LIMITS.max_file_bytes,
        LIMITS.max_json_depth,
    )


def staging(
    directory: Path,
) -> Callable[
    [ClaimedNode, ObjectRecord, RequestEvidenceKind, bytes, PolicyContentRef],
    Awaitable[EvidenceAsset],
]:
    async def stage(
        claimed: ClaimedNode,
        obj: ObjectRecord,
        kind: RequestEvidenceKind,
        content: bytes,
        policy_ref: PolicyContentRef,
    ) -> EvidenceAsset:
        scope = claimed.opened.scope
        digest = sha256_digest(content)
        identity = "request." + sha256_digest(
            f"{scope.execution_id}/{obj.object_id}/{kind}/{digest}".encode()
        )
        policy = scope.resolve(policy_ref, EvidencePolicyArtifact).policy
        destination = directory / identity
        destination.write_bytes(content)
        assert destination.read_bytes() == content
        # The content identity survives retry. Its original registration time does too.
        return EvidenceAsset(
            schema_version="1.0.0",
            evidence_id=identity,
            evidence_type=EvidenceType.MODEL_REQUEST,
            scope=ObjectEvidenceScope(
                scope="object",
                execution_id=obj.execution_id,
                profile_ref=obj.profile_ref,
                object_ref=ObjectRef(record_kind=K.OBJECT, record_id=obj.object_id),
            ),
            storage=StorageReference(store_id="local-test", object_key=identity),
            sha256=digest,
            mime_type="text/plain"
            if kind == "compiled_prompt"
            else "application/schema+json",
            byte_size=len(content),
            stage=FinalizedAsset(
                stage="finalized",
                staged_at=AT,
                verified_at=AT,
                object_version=VERSION,
                verified_sha256=digest,
            ),
            spatial=NonSpatialEvidence(kind="non_spatial"),
            producer_id="local-test",
            producer_version=VERSION,
            access_scope=TaskEvidenceAccess(
                scope="task",
                project_id=scope.task.idempotency_scope.project_id,
                task_id=scope.task.task_id,
            ),
            retention=RetentionPolicyRef(
                evidence_policy_ref=policy_ref, retention_class=policy.retention_class
            ),
            created_at=AT,
        )

    return stage


def grouped_profile_source() -> BundleCompileInput:
    """Three independent bindings under the same reviewed Profile/DTO policy."""
    source = load_source()
    (profile,) = (row for row in source.artifacts if row.kind is ArtifactKind.PROFILE)
    (primary,) = (
        row
        for row in source.artifacts
        if row.kind is ArtifactKind.MODEL_BINDING
        and row.artifact_id == "profile_discovery"
    )
    added = []
    for suffix in ("secondary", "tertiary"):
        binding_id = f"profile_discovery_{suffix}"
        body = {**json.loads(primary.content), "model_binding_id": binding_id}
        added.append(
            replace(
                primary,
                artifact_id=binding_id,
                content=json.dumps(body).encode(),
                origin=SourceIdentity(
                    artifact_id=f"group-fixture.{binding_id}", version=VERSION
                ),
            )
        )
        profile_id = f"nonmotor_vehicle_{suffix}"
        body = {**json.loads(profile.content), "profile_id": profile_id}
        slots = tuple(
            replace(item, target=(ArtifactKind.MODEL_BINDING, binding_id, VERSION))
            if item.location == ("discovery_binding_ref",)
            else item
            for item in profile.reference_slots
        )
        added.append(
            replace(
                profile,
                artifact_id=profile_id,
                content=json.dumps(body).encode(),
                reference_slots=slots,
                origin=SourceIdentity(
                    artifact_id=f"group-fixture.{profile_id}", version=VERSION
                ),
            )
        )
    return with_source_provenance(
        replace(source, artifacts=(*source.artifacts, *added))
    )


@asynccontextmanager
async def perception_fixture(
    directory: Path,
    url: str | None = None,
    *,
    with_media: bool = False,
    multi_profile: bool = False,
    source: BundleCompileInput | None = None,
    capability_builder: Callable[
        [BundleCompileInput], CapabilitySnapshot
    ] = capability_snapshot,
    image: bytes = IMAGE,
    dimensions: ImageDimensions = _IMAGE_DIMENSIONS,
    submission: TaskSubmission | None = None,
    model_endpoints: Callable[[CapabilitySnapshot], tuple[ChatEndpoint, ...]]
    | None = None,
    model_secrets: SecretResolver | None = None,
    allow_model_network: bool = False,
    tool_proof_overrides: dict[str, TrustPin] | None = None,
    scheduling_limits: SchedulingLimits = _LIMITS,
    submit: Callable[[AsyncEngine, IntakeService, SceneHost], Awaitable[IntakeResult]]
    | None = None,
) -> AsyncIterator[PerceptionFixture]:
    if source is not None and multi_profile:
        raise ValueError("Provide a source or use the multiple-Profile fixture")
    if source is None:
        source = grouped_profile_source() if multi_profile else load_source()
    if (url is None) == (model_endpoints is None):
        raise ValueError("Provide one model URL or an explicit endpoint registry")
    submission = submission if submission is not None else local_submission(image)
    if submission.media_ref.sha256 != sha256_digest(image):
        raise ValueError("Registered submission media digest differs from image bytes")
    caps = capability_builder(source)
    scene = None
    if submit is not None:
        # This path exercises a genuinely fresh POST. No task is preaccepted.
        prepared = prepare_scene(directory, source, caps, submission)
        principal, store, releases = (
            prepared.principal,
            prepared.store,
            prepared.releases,
        )
        catalogue = prepared.catalogue
    else:
        # Existing tests exercise a reopened historical task after a newer Release.
        prepared = None
        scene = exercise_lifecycle(
            directory,
            source=source,
            capability_builder=lambda _: caps,
            submission=submission,
        )
        catalogue = scene.execution.catalogue
        principal = object()
        store = LocalBundleStore(directory / "releases")
        releases = ReleaseManager(
            store,
            authorize=lambda given, action, target: ACTOR
            if (
                given is principal
                and action is ReleaseAction.ACCEPT_TASK
                and target.release_id == scene.release.release_id
                and target.scope == scene.activation.scope
            )
            else None,
            resolve_task_scope=lambda *_: None,
            allow_new_task=lambda *_: False,
            scope_precedence=("device", "region", "scene", "global"),
            clock=lambda: AT,
        )
    now = datetime.now(UTC)
    validity = {
        "valid_from": now - timedelta(days=1),
        "valid_until": now + timedelta(days=1),
    }
    current = caps.model_copy(
        update={
            **validity,
            "capabilities": tuple(
                row.model_copy(update=validity) for row in caps.capabilities
            ),
            "model_deployments": tuple(
                row.model_copy(update=validity) for row in caps.model_deployments
            ),
            "runtime_protocols": tuple(
                row.model_copy(update=validity) for row in caps.runtime_protocols
            ),
        }
    )
    host = SceneHost(
        principal,
        submission,
        _original_media(submission.media_ref, image, dimensions),
        current,
    )
    deployments = {
        (row.identity.provider_id, row.identity.deployment_id): row.identity
        for row in current.model_deployments
    }
    if model_endpoints is None:
        (deployment,) = deployments.values()
        assert url is not None
        endpoints = (
            ChatEndpoint(
                endpoint_id="wp05.loopback.model",
                endpoint_version=VERSION,
                provider_id=deployment.provider_id,
                deployment_id=deployment.deployment_id,
                provider_kind=deployment.provider_kind,
                model_id="wp05/parking",
                url=url,
                protocol="openai_chat_completions",
                allow_plaintext_loopback=True,
                retain_complete_response=True,
                limits=HttpLimits(
                    timeout_ms=3500,
                    io_timeout_ms=3000,
                    request_bytes=65536,
                    response_bytes=8192,
                    image_bytes=4096,
                    max_retry_after_seconds=1,
                ),
            ),
        )
    else:
        endpoints = model_endpoints(current)
    endpoint_keys = {(item.provider_id, item.deployment_id) for item in endpoints}
    if len(endpoint_keys) != len(endpoints) or endpoint_keys != set(deployments):
        raise ValueError(
            "Endpoint registry must exactly cover registered model deployments"
        )
    ownership = tuple(
        DeploymentOwnership(
            deployment=deployments[(item.provider_id, item.deployment_id)],
            owner_ref=PROOF,
            endpoint_descriptor_ref=item.descriptor_pin,
            model_id=item.model_id,
            version_evidence=None,
        )
        for item in endpoints
    )
    routing = CurrentRoutingState(
        capabilities=current,
        ownership=tuple(
            DeploymentOwnershipRegistration(
                ownership=item,
                valid_from=validity["valid_from"],
                valid_until=validity["valid_until"],
                revoked_at=None,
            )
            for item in ownership
        ),
    )

    async def current_model(scope: IdempotencyScope) -> CurrentRoutingState:
        assert scope == host.submission.idempotency_scope
        return routing

    async def freeze_models(
        _: IdempotencyScope, capabilities: CapabilitySnapshot
    ) -> tuple[DeploymentOwnership, ...]:
        assert capabilities == current
        return ownership

    tools = tuple(
        require_definition(catalogue, row.ref, ToolDefinition)
        for row in catalogue.of_kind(ArtifactKind.TOOL)
    )
    cpu_digest = sha256_digest(CPU_ARTIFACT.read_bytes()) if with_media else None
    tool_proofs = {
        tool.implementation_requirement.capability_id: (
            TrustPin(
                identity=tool.implementation_requirement.capability_id,
                version=VERSION,
                sha256=cpu_digest,
            )
            if cpu_digest is not None
            else PROOF
        )
        for tool in tools
    }
    if tool_proof_overrides:
        if not tool_proof_overrides.keys() <= tool_proofs.keys():
            raise ValueError("Tool proof override lacks an admitted Tool capability")
        tool_proofs.update(tool_proof_overrides)

    async def freeze_tools(
        _: IdempotencyScope, resolved: ResolvedExecutionPins
    ) -> tuple[ToolOwnershipPin, ...]:
        return tuple(
            ToolOwnershipPin(
                tool_ref=pin.tool_ref,
                implementation_version=pin.implementation_version,
                implementation_proof_ref=tool_proofs[
                    require_definition(
                        catalogue, pin.tool_ref, ToolDefinition
                    ).implementation_requirement.capability_id
                ],
            )
            for pin in resolved.pins.tool_pins
        )

    resources = ResourceAdmission.provision(
        directory / "resources.sqlite",
        HostInventory(
            "wp05.perception",
            6000,
            1,
            (
                ResourceScope(
                    _RESOURCE_SCOPE,
                    "remote",
                    1,
                    pool_or_quota_id="synthetic.loopback.quota",
                ),
                *((ResourceScope("cpu", "cpu", 1),) if with_media else ()),
            ),
            (
                RemoteQuota(
                    "synthetic.loopback.quota", 1, RemoteCapacity.LOCAL_TRANSPORT
                ),
            ),
        ),
    )

    async with migrated_application() as application:
        media_store = (
            LocalMediaStore(directory / "media", application) if with_media else None
        )
        if media_store is not None:
            asset = await media_store.publish_original(
                host.submission.idempotency_scope,
                host.submission.media_ref,
                mime_type="image/png",
                dimensions=host.media.dimensions,
                content=image,
            )
            host = replace(host, media=asset)

        async def load_image(bound: VerifiedExecutionScope) -> VerifiedImage:
            scope = bound.task.idempotency_scope
            if media_store is None:
                assert (
                    await host.verified_original(scope, bound.media.media_ref)
                    == bound.media
                )
                content = image
            else:
                original = await media_store.verified_task_original(scope, bound.task)
                assert original.asset == bound.media
                content = original.content
            return VerifiedImage(content, "image/png", bound.media.media_ref.sha256)

        adapter = ConfiguredPerception(load_image, staging(directory), _RESOURCE_SCOPE)
        intake = IntakeService(
            application,
            releases,
            store,
            host=host,
            bundle_limits=LIMITS,
            scheduling_limits=scheduling_limits,
            max_submission_bytes=65536,
            historical_ownership=freeze_models,
            historical_tool_ownership=freeze_tools,
        )
        accepted = (
            await intake.submit(host.submission, principal=principal)
            if submit is None
            else await submit(application, intake, host)
        )
        if prepared is not None:
            resolved = resolve_execution_pins(
                releases,
                store,
                accepted.task,
                scope=submission.idempotency_scope,
                host=caps,
                validation_at=AT,
                limits=LIMITS,
            )
            scene = ReopenedScene(
                prepared.bundle,
                prepared.signed_archive,
                prepared.release,
                prepared.activation,
                prepared.activation,
                accepted.task,
                resolved,
            )
        assert scene is not None
        scope = release_scope(scene, host.media)
        profiles = tuple(
            scope.resolve(item.ref, ProfileDefinition)
            for item in scope.catalogue.of_kind(ArtifactKind.PROFILE)
        )
        contracts = {}
        candidates = []
        for member in scope.catalogue.of_kind(ArtifactKind.MODEL_BINDING):
            ref = ModelBindingContentRef.model_validate(member.ref.model_dump())
            binding = scope.resolve(ref, ModelBinding)
            slots = ()
            region_base = False
            if binding.capability is ModelCapability.OBJECT_OBSERVATION:
                question_sets = {
                    profile.question_refs
                    for profile in profiles
                    if profile.observation_binding_ref == ref
                }
                template = binding.prompt_template_ref
                if template is not None and is_region_observation_template(
                    scope.resolve(template, PromptTemplateDefinition).template_text
                ):
                    region_base = True
                    slots = tuple(
                        sorted(
                            {q for group in question_sets for q in group},
                            key=lambda q: (q.artifact_id, q.version, q.digest),
                        )
                    )
                elif len(question_sets) == 1:
                    (slots,) = question_sets
                else:
                    raise ValueError(
                        "Each fixture observation binding requires one exact Question set"
                    )
            if binding.capability is ModelCapability.CANDIDATE_PREDICTION:
                candidates.append(ref)
            contract = build_chat_contract(
                scope, ref, slots, register_region_base=region_base
            )
            if region_base and {q.artifact_id for q in slots} - set(
                contract.question_slots
            ):
                raise ValueError("Profile Questions missing from the regional binding")
            key = (
                contract.input_schema_ref,
                contract.output_schema_ref,
                contract.response_schema_digest,
            )
            previous = contracts.setdefault(key, contract)
            if (
                previous.response_model is not contract.response_model
                or previous.response_schema != contract.response_schema
                or previous.accepts_image != contract.accepts_image
                or previous.question_slots != contract.question_slots
            ):
                raise ValueError("Fixture bindings disagree on a shared wire contract")
        (candidate,) = candidates
        transport = HttpModelTransport(
            endpoints,
            tuple(contracts.values()),
            max_concurrency=1,
            secrets=model_secrets,
            allow_network=allow_model_network,
        )
        journal = DispatchJournal(application)
        gateway = CapabilityGateway(
            application,
            journal,
            resources,
            transport,
            current_model,
            ModelCallLimits(8, 4, 8, ("transport", "timeout")),
            response_writer=media_store,
        )
        yield PerceptionFixture(
            scene,
            application,
            releases,
            store,
            host,
            accepted,
            gateway,
            adapter,
            journal,
            endpoints[0].model_id,
            candidate,
            resources,
            media_store,
            tool_proofs,
            endpoints,
        )
