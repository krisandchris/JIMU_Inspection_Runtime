"""Run one isolated inspection through FastAPI and a disposable PostgreSQL database."""

import asyncio
import json
import secrets
from collections.abc import Callable
from contextlib import ExitStack
from dataclasses import dataclass, field, replace
from enum import Enum
from io import BytesIO
from pathlib import Path
from uuid import uuid4

import httpx
from PIL import Image
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from examples.nonmotor_vehicle_illegal_parking.compilable.lifecycle import (
    ACTOR,
    local_submission,
)
from examples.nonmotor_vehicle_illegal_parking.compilable.scene import (
    LIMITS,
    capability_snapshot,
    configuration_pin,
    with_source_provenance,
)
from jimu_runtime.api.app import create_app
from jimu_runtime.api.auth import Action, Identity, Resource
from jimu_runtime.api.contracts import TaskAcceptance, TracePage
from jimu_runtime.bundle.capabilities import CapabilitySnapshot
from jimu_runtime.bundle.compiler import BundleCompileInput
from jimu_runtime.bundle.trust import TrustPin
from jimu_runtime.domain.enums import ArtifactKind
from jimu_runtime.domain.runtime.intake import TaskContext
from jimu_runtime.domain.runtime.media import ImageDimensions
from jimu_runtime.domain.values import RecordValue, StringValue
from jimu_runtime.gateway.bridge_tools import ToolExecutionAdapter
from jimu_runtime.gateway.http import ChatEndpoint, HttpLimits, SecretResolver
from jimu_runtime.inspection.geometry.node import GeometryNodeAdapter
from jimu_runtime.tools.executor import ToolExecutor
from jimu_runtime.tools.registry import ToolImplementation, ToolWorkerRegistry
from jimu_runtime.tools.step_attempts import StepAttemptStore
from jimu_runtime.workflow import repository
from jimu_runtime.workflow.intake import IntakeResult, IntakeService
from jimu_runtime.workflow.worker import ClaimedNodeInvoker, WorkerStep
from jimu_runtime.workflow.worker_inspection import inspection_worker
from jimu_runtime.workflow.worker_state import load_records
from tests.fixtures.wp05.perception import (
    _LIMITS as TEST_SCHEDULING_LIMITS,
)
from tests.fixtures.wp05.perception import (
    PerceptionFixture,
    SceneHost,
    perception_fixture,
)

from .providers.live_wire_bridge import (
    BRIDGE_VERSION,
    MODEL_ID,
    GroundingAttestation,
    ProfileRoute,
    start_bridge,
)
from .scenes import build_mask_compile_input, build_sidewalk_goods_compile_input
from .scenes.bundles import REGION_PROJECTION_BUNDLE_VERSION
from .scenes.trusted_scope import (
    MASK_SCOPE_FIELD,
    SCOPE_EVIDENCE_SHA256,
    SCOPE_IMAGE_SHA256,
    SIDEWALK_SCOPE_FIELD,
    kitchen_roster,
    sidewalk_metadata,
)

_MIB = 1024 * 1024
_TIMEOUT_MS = 95_000
_ACTIONS = frozenset(
    {
        Action.ACCESS_SCOPE,
        Action.TASK_SUBMIT,
        Action.TASK_READ,
        Action.TASK_RESULT,
        Action.EXECUTION_READ,
        Action.EXECUTION_TRACE,
    }
)
_REGISTERED_SCOPE_FIELDS = frozenset(
    {MASK_SCOPE_FIELD, SIDEWALK_SCOPE_FIELD, SCOPE_EVIDENCE_SHA256}
)


class CaptureScope(Enum):
    KITCHEN_ROSTER = "kitchen_roster"
    SIDEWALK_POLICY = "sidewalk_policy"
    SIDEWALK_NO_POLICY = "sidewalk_no_policy"


@dataclass(frozen=True, slots=True)
class _RegisteredCapture:
    metadata: RecordValue
    routes: dict[str, ProfileRoute]
    grounding_attestations: dict[str, GroundingAttestation]


def _registered_capture(
    scope: CaptureScope,
    source: BundleCompileInput,
    image: bytes,
    context: TaskContext,
) -> _RegisteredCapture:
    identity = {
        "device_id": context.device_id,
        "camera_id": context.camera_id,
        "region_id": context.region_id,
    }
    region_projection = (
        source.metadata.bundle_version == REGION_PROJECTION_BUNDLE_VERSION
    )
    if scope is CaptureScope.KITCHEN_ROSTER:
        if source != build_mask_compile_input(region_projection=region_projection):
            raise ValueError("The registered roster requires the exact mask Bundle")
        roster = kitchen_roster(image, **identity)
        profile = "food_service_worker"
        return _RegisteredCapture(
            roster.metadata,
            {profile: ProfileRoute("person", stage1_target="person")},
            {profile: roster.match},
        )
    if scope in (CaptureScope.SIDEWALK_POLICY, CaptureScope.SIDEWALK_NO_POLICY):
        if source != build_sidewalk_goods_compile_input(
            region_projection=region_projection
        ):
            raise ValueError(
                "The registered placement policy requires the exact goods Bundle"
            )
        metadata = sidewalk_metadata(image, **identity)
        if scope is CaptureScope.SIDEWALK_NO_POLICY:
            values = dict(metadata.value)
            del values[SIDEWALK_SCOPE_FIELD]
            metadata = RecordValue(kind="record", value=values)
        return _RegisteredCapture(
            metadata, {"sidewalk_goods": ProfileRoute("cardboard boxes")}, {}
        )
    raise ValueError("Unknown capture scope registration")


def _write_json(path: Path, value: object) -> None:
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


def live_resource_budgets(source: BundleCompileInput) -> BundleCompileInput:
    """Pin the limits for real image bytes and the bounded upstream request time."""
    artifacts = []
    for item in source.artifacts:
        if item.kind is ArtifactKind.POLICY and item.artifact_id in {
            "remote-resources",
            "resources",
            "tool-execution",
        }:
            body = json.loads(item.content)
            policy = body["policy"]
            policy.update(timeout_ms=85_000, input_byte_limit=12 * _MIB)
            if "output_byte_limit" in policy:
                policy["output_byte_limit"] = 2 * _MIB
            item = replace(item, content=json.dumps(body).encode())
        artifacts.append(item)
    return with_source_provenance(replace(source, artifacts=tuple(artifacts)))


@dataclass(frozen=True, slots=True)
class _BridgeEndpoints:
    url: str
    supports_region_projection: bool

    def __call__(self, snapshot: CapabilitySnapshot) -> tuple[ChatEndpoint, ...]:
        return tuple(
            ChatEndpoint(
                endpoint_id=f"live.bridge.{index}",
                endpoint_version=BRIDGE_VERSION,
                provider_id=deployment.identity.provider_id,
                deployment_id=deployment.identity.deployment_id,
                provider_kind=deployment.identity.provider_kind,
                model_id=MODEL_ID,
                url=self.url,
                protocol="openai_chat_completions",
                allow_plaintext_loopback=True,
                retain_complete_response=True,
                supports_region_projection=self.supports_region_projection,
                limits=HttpLimits(
                    timeout_ms=85_000,
                    io_timeout_ms=84_000,
                    request_bytes=12 * _MIB,
                    response_bytes=2 * _MIB,
                    image_bytes=8 * _MIB,
                    max_retry_after_seconds=1,
                ),
            )
            for index, deployment in enumerate(snapshot.model_deployments)
        )


def bridge_model_endpoints(
    url: str, *, supports_region_projection: bool = False
) -> _BridgeEndpoints:
    """Bind Runtime to the loopback bridge. Provider wire evidence is logged separately."""
    return _BridgeEndpoints(url, supports_region_projection)


def _bridge_registration(
    source: BundleCompileInput, snapshot: CapabilitySnapshot
) -> tuple[BundleCompileInput, CapabilitySnapshot]:
    if snapshot.model_reviews or len(snapshot.model_deployments) != 1:
        raise ValueError("Bridge examples require one unreviewed loopback deployment")
    (deployment,) = snapshot.model_deployments
    old = deployment.identity
    if old.provider_kind != "remote":
        raise ValueError("Bridge examples require a remote protocol binding")
    bridge = old.model_copy(
        update={
            "provider_id": "live.inspection.protocol_bridge",
            "deployment_id": "live.inspection.protocol_bridge.loopback",
            "deployment_version": BRIDGE_VERSION,
            "model_version": BRIDGE_VERSION,
        }
    )
    artifacts = []
    for item in source.artifacts:
        if item.kind is ArtifactKind.POLICY:
            body = json.loads(item.content)
            policy = body["policy"]
            if policy["policy_kind"] == "fallback" and policy["mode"] != "disabled":
                raise ValueError("Bridge examples require disabled model fallback")
            if policy["policy_kind"] == "provider":
                (route,) = policy["deployments"]
                if (route["provider_id"], route["deployment_id"]) != (
                    old.provider_id,
                    old.deployment_id,
                ):
                    raise ValueError(
                        "Bundle route does not match its trusted loopback deployment"
                    )
                route.update(
                    provider_id=bridge.provider_id,
                    deployment_id=bridge.deployment_id,
                    actual_version_constraints={
                        "allowed_deployment_versions": [BRIDGE_VERSION],
                        "allowed_model_versions": [BRIDGE_VERSION],
                    },
                )
                policy["primary_deployment_id"] = bridge.deployment_id
                item = replace(item, content=json.dumps(body).encode())
        artifacts.append(item)
    required = tuple(
        row.model_copy(update={"capability_id": bridge.deployment_id})
        if row.family.value == "model_deployment"
        and row.capability_id == old.deployment_id
        else row
        for row in source.metadata.required_capabilities
    )
    capabilities = tuple(
        row.model_copy(update={"capability_id": bridge.deployment_id})
        if row.family.value == "model_deployment"
        and row.capability_id == old.deployment_id
        else row
        for row in snapshot.capabilities
    )
    rebound = snapshot.model_copy(
        update={
            "capabilities": capabilities,
            "model_deployments": (deployment.model_copy(update={"identity": bridge}),),
        }
    )
    rebound = rebound.model_copy(update={"pin": configuration_pin(rebound)})
    configured = with_source_provenance(
        replace(
            source,
            artifacts=tuple(artifacts),
            metadata=replace(source.metadata, required_capabilities=required),
        )
    )
    return configured, rebound


@dataclass(frozen=True, slots=True)
class _CaseAuthority:
    host: SceneHost
    bearer: str = field(repr=False)

    async def authenticate(self, credential: str) -> Identity | None:
        if credential.isascii() and secrets.compare_digest(self.bearer, credential):
            return Identity(self.host.principal, ACTOR)
        return None

    async def allows(self, identity: Identity, resource: Resource) -> bool:
        return (
            identity.principal is self.host.principal
            and identity.actor == ACTOR
            and resource.scope == self.host.submission.idempotency_scope
            and resource.action in _ACTIONS
        )


class _TaskApi:
    def __init__(self, directory: Path) -> None:
        self._directory = directory
        self._client: httpx.AsyncClient | None = None
        self._audit: list[dict[str, object]] = []

    async def _request(
        self, method: str, suffix: str, **kwargs: object
    ) -> httpx.Response:
        assert self._client is not None
        response = await self._client.request(method, suffix, **kwargs)
        self._audit.append(
            {
                "method": method,
                "path": response.request.url.path,
                "query": response.request.url.query.decode(),
                "status": response.status_code,
                "body": response.json(),
            }
        )
        _write_json(self._directory / "api.json", self._audit)
        return response

    async def submit(
        self, engine: AsyncEngine, service: IntakeService, host: SceneHost
    ) -> IntakeResult:
        scope = host.submission.idempotency_scope
        bearer = secrets.token_urlsafe(32)
        self._client = httpx.AsyncClient(
            transport=httpx.ASGITransport(
                app=create_app(
                    engine,
                    service,
                    authority=_CaseAuthority(host, bearer),
                )
            ),
            base_url="http://live-inspection.local/api/v1/",
            headers={
                "Authorization": f"Bearer {bearer}",
                "X-Jimu-Project": scope.project_id,
                "X-Jimu-Client": scope.client_id,
            },
        )
        response = await self._request(
            "POST",
            "inference/tasks",
            content=host.submission.stable_json(),
            headers={"Content-Type": "application/json"},
        )
        response.raise_for_status()
        if response.status_code != 202:
            raise RuntimeError("Task POST must return 202")
        receipt = TaskAcceptance.model_validate_json(response.content)
        async with engine.connect() as connection:
            stored = await repository.get_execution(
                connection, scope, receipt.execution.execution_id
            )
        if (
            stored is None
            or stored.task != receipt.task
            or stored.execution.pins != receipt.execution.pins
        ):
            raise RuntimeError("Task POST did not produce the reported durable records")
        return IntakeResult(stored.task, stored.execution)

    async def read(
        self,
        accepted: IntakeResult,
        expected_execution: object,
        expected_result: object,
    ) -> None:
        execution_id = accepted.execution.execution_id
        route = f"executions/{execution_id}"
        execution = await self._request("GET", route)
        execution.raise_for_status()
        result = await self._request(
            "GET", f"inference/tasks/{accepted.task.task_id}/result"
        )
        if execution.json() != expected_execution or result.json() != expected_result:
            if expected_result is not None:
                raise RuntimeError(
                    "Public result or execution differs from the stored records"
                )
            if execution.json() != expected_execution:
                raise RuntimeError("Public execution differs from the stored record")
        through = None
        after = 0
        while True:
            params = {"limit": 25, "after_event_number": after}
            if through is not None:
                params["through_event_number"] = through
            response = await self._request("GET", route + "/trace", params=params)
            response.raise_for_status()
            page = TracePage.model_validate_json(response.content)
            if through is None:
                through = page.through_event_number
            if (
                page.execution_id != execution_id
                or page.trace_id != accepted.task.trace.trace_id
                or page.through_event_number != through
                or any(
                    event.number != number
                    for number, event in enumerate(page.events, after + 1)
                )
            ):
                raise RuntimeError("Trace pagination lost identity, ordering or events")
            last = page.events[-1].number if page.events else after
            if page.next_after_event_number is None:
                if last != through:
                    raise RuntimeError("Trace ended before its frozen high water mark")
                return
            if page.next_after_event_number != last or last <= after:
                raise RuntimeError("Trace cursor failed to advance")
            after = last

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()


async def run_case(
    output_dir: Path,
    image_path: Path,
    *,
    source: BundleCompileInput,
    model_endpoints: Callable[[CapabilitySnapshot], tuple[ChatEndpoint, ...]]
    | None = None,
    capability_builder: Callable[
        [BundleCompileInput], CapabilitySnapshot
    ] = capability_snapshot,
    region_id: str | None = None,
    capture_scope: CaptureScope | None = None,
    trusted_metadata: RecordValue | None = None,
    tool_entries: Callable[[PerceptionFixture], tuple[ToolImplementation, ...]]
    | None = None,
    tool_proof_overrides: dict[str, TrustPin] | None = None,
    execute_tools: Callable[
        [PerceptionFixture, ToolExecutionAdapter], ClaimedNodeInvoker
    ]
    | None = None,
    model_secrets: SecretResolver | None = None,
    allow_model_network: bool = False,
    qwen_enable_thinking: bool | None = None,
    max_worker_steps: int = 256,
) -> dict[str, object]:
    """Admit a caller-approved scene and export the durable result and bounded trace."""
    if (
        trusted_metadata is not None
        and _REGISTERED_SCOPE_FIELDS & trusted_metadata.value.keys()
    ):
        raise ValueError("Scene scope fields require a trusted capture registration")
    if capture_scope is not None and trusted_metadata is not None:
        raise ValueError("A registered capture supplies all trusted metadata")
    if (capture_scope is None) == (model_endpoints is None):
        raise ValueError(
            "Provide either a registered capture or an independent model endpoint registry"
        )
    if qwen_enable_thinking is not None and (
        capture_scope is None
        or source.metadata.bundle_version != REGION_PROJECTION_BUNDLE_VERSION
    ):
        raise ValueError("Qwen thinking override requires a registered regional bridge")
    if max_worker_steps < 1:
        raise ValueError("Positive worker step budget required")
    with image_path.open("rb") as stream:
        image = stream.read(8 * _MIB + 1)
    if len(image) > 8 * _MIB:
        raise ValueError("The registered PNG exceeds the image byte budget")
    with Image.open(BytesIO(image), formats=("PNG",)) as opened:
        if opened.mode != "RGB":
            raise ValueError("One privacy-reviewed RGB PNG is required")
        dimensions = ImageDimensions(width=opened.width, height=opened.height)
        opened.verify()

    submission = local_submission(image)
    context_update = {}
    if region_id is not None:
        context_update["region_id"] = region_id
    registered = None
    if capture_scope is not None:
        context = submission.context.model_copy(update=context_update)
        registered = _registered_capture(capture_scope, source, image, context)
        context_update["metadata"] = registered.metadata
    if trusted_metadata is not None:
        capture_digest = trusted_metadata.value.get(SCOPE_IMAGE_SHA256)
        if capture_digest is not None and (
            not isinstance(capture_digest, StringValue)
            or capture_digest.value != submission.media_ref.sha256
        ):
            raise ValueError("Trusted scope metadata is bound to different image bytes")
        context_update["metadata"] = trusted_metadata
    case_id = f"live.{uuid4().hex}"
    submission = submission.model_copy(
        update={
            "context": submission.context.model_copy(update=context_update),
            "idempotency_key": case_id,
            "request_id": case_id,
            "trace": submission.trace.model_copy(update={"trace_id": case_id}),
        }
    )
    submission = type(submission).model_validate_json(submission.stable_json())
    scheduling = TEST_SCHEDULING_LIMITS.model_copy(
        update={
            "default": TEST_SCHEDULING_LIMITS.default.model_copy(
                update={"timeout_ms": _TIMEOUT_MS}
            ),
            "overrides": tuple(
                row.model_copy(
                    update={
                        "budget": row.budget.model_copy(
                            update={"timeout_ms": _TIMEOUT_MS}
                        )
                    }
                )
                for row in TEST_SCHEDULING_LIMITS.overrides
            ),
        }
    )
    output_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    runtime_files = (
        "api.json",
        "intake.json",
        "media",
        "releases",
        "resources.sqlite",
        "result.json",
        "signed-bundle.zip",
    )
    if any((output_dir / name).exists() for name in runtime_files):
        raise FileExistsError(
            "The isolated output directory already contains a Runtime run"
        )
    (output_dir / ".runtime-output").mkdir()
    api = _TaskApi(output_dir)
    source = live_resource_budgets(source)
    snapshot = capability_builder(source)
    try:
        with ExitStack() as services:
            if registered is not None:
                region_projection = (
                    source.metadata.bundle_version == REGION_PROJECTION_BUNDLE_VERSION
                )
                bridge_options = {}
                if region_projection:
                    bridge_options = {
                        "enable_region_projection": True,
                        "qwen_enable_thinking": qwen_enable_thinking,
                    }
                bridge = services.enter_context(
                    start_bridge(
                        output_dir / "provider",
                        profile_routes=registered.routes,
                        grounding_attestations=registered.grounding_attestations,
                        **bridge_options,
                    )
                )
                model_endpoints = bridge_model_endpoints(
                    bridge.url, supports_region_projection=region_projection
                )
            assert model_endpoints is not None
            if isinstance(model_endpoints, _BridgeEndpoints):
                source, snapshot = _bridge_registration(source, snapshot)
            async with perception_fixture(
                output_dir,
                with_media=True,
                source=source,
                capability_builder=lambda _: snapshot,
                image=image,
                dimensions=dimensions,
                submission=submission,
                model_endpoints=model_endpoints,
                model_secrets=model_secrets,
                allow_model_network=allow_model_network,
                tool_proof_overrides=tool_proof_overrides,
                scheduling_limits=scheduling,
                submit=api.submit,
            ) as fixture:
                return await _run_worker(
                    output_dir,
                    fixture,
                    api,
                    tool_entries,
                    execute_tools,
                    max_worker_steps,
                )
    finally:
        await api.close()


async def _run_worker(
    directory: Path,
    fixture: PerceptionFixture,
    api: _TaskApi,
    tool_entries: Callable[[PerceptionFixture], tuple[ToolImplementation, ...]] | None,
    execute_tools: Callable[
        [PerceptionFixture, ToolExecutionAdapter], ClaimedNodeInvoker
    ]
    | None,
    max_worker_steps: int,
) -> dict[str, object]:
    accepted = fixture.accepted
    task, eid = accepted.task, accepted.execution.execution_id
    scope = task.idempotency_scope
    (directory / "signed-bundle.zip").write_bytes(fixture.scene.signed_archive)
    _write_json(
        directory / "intake.json",
        {
            "task": task.model_dump(mode="json"),
            "execution": accepted.execution.model_dump(mode="json"),
        },
    )
    media = fixture.media_store
    assert media is not None
    registry = ToolWorkerRegistry(tool_entries(fixture) if tool_entries else ())
    tools = ToolExecutionAdapter(
        ToolExecutor(registry),
        StepAttemptStore(fixture.application, actor=ACTOR),
        fixture.journal,
        fixture.host.capabilities,
    )
    adapter = (
        execute_tools(fixture, tools)
        if execute_tools
        else GeometryNodeAdapter(media, tools).execute
    )
    worker = inspection_worker(
        fixture.application,
        fixture.releases,
        fixture.store,
        host=fixture.host,
        bundle_limits=LIMITS,
        media=media,
        gateway=fixture.gateway,
        execute_tools=adapter,
        model_resource_scope_id="model",
    )
    dispositions = []
    for index in range(max_worker_steps):
        step = await worker.run_once(
            scope, eid, owner=f"live-{index}", lease_ms=_TIMEOUT_MS + 5_000
        )
        dispositions.append(step.value)
        if step is WorkerStep.FINALIZED:
            break
        if step in {WorkerStep.IDLE, WorkerStep.RETRY_SCHEDULED}:
            await asyncio.sleep(0.15)

    final = await worker.read_finalization(scope, eid)
    async with fixture.application.connect() as connection:
        locked = await repository.get_execution(connection, scope, eid)
        if locked is None:
            raise RuntimeError("Durable Execution is unavailable")
        data = await load_records(connection, scope, locked)
        marked_frame_asset_count = (
            await connection.execute(
                text(
                    "SELECT count(*) FROM jimu_workflow.marked_frame_media "
                    "WHERE project_id = :project AND client_id = :client "
                    "AND execution_id = :execution"
                ),
                {
                    "project": scope.project_id,
                    "client": scope.client_id,
                    "execution": eid,
                },
            )
        ).scalar_one()
        postgres_version = (
            await connection.execute(text("SELECT current_setting('server_version')"))
        ).scalar_one()
    execution_json = locked.execution.model_dump(mode="json")
    final_json = None if final is None else final.model_dump(mode="json")
    result = {
        "task_id": task.task_id,
        "execution_id": eid,
        "bundle_digest": task.bundle_digest,
        "release_id": task.release_id,
        "media": task.media_ref.model_dump(mode="json"),
        "postgres_version": postgres_version,
        "marked_frame_asset_count": marked_frame_asset_count,
        "dispositions": dispositions,
        "execution": execution_json,
        "nodes": [node.model_dump(mode="json") for node in data.runs.values()],
        "tool_step_nodes": [
            node.model_dump(mode="json") for node in data.tool_step_nodes
        ],
        "node_outputs": [out.model_dump(mode="json") for out in data.messages.values()],
        "final": final_json,
        "registered_model_endpoints": [
            {
                "descriptor_pin": endpoint.descriptor_pin.model_dump(mode="json"),
                "model_id": endpoint.model_id,
                "provider_id": endpoint.provider_id,
                "deployment_id": endpoint.deployment_id,
            }
            for endpoint in fixture.model_endpoints
        ],
    }
    _write_json(directory / "result.json", result)
    await api.read(accepted, execution_json, final_json)
    if final is None:
        raise RuntimeError("No finalization record; inspect result.json and api.json")
    return result
