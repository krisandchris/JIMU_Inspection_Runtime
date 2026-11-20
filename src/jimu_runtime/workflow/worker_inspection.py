"""Programmatic composition of the configured six-node inspection worker."""

from asyncio import get_running_loop, run_coroutine_threadsafe
from collections.abc import Awaitable, Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime
from functools import partial
from typing import cast

from sqlalchemy.ext.asyncio import AsyncEngine

from jimu_runtime.bundle.archive import BundleLimits
from jimu_runtime.bundle.capabilities import CapabilitySnapshot
from jimu_runtime.bundle.releases import ReleaseManager
from jimu_runtime.bundle.store import LocalBundleStore
from jimu_runtime.domain.definition_enums import EvidenceType
from jimu_runtime.domain.definitions.capabilities import ProfileDefinition
from jimu_runtime.domain.definitions.models import ModelBindingContentRef
from jimu_runtime.domain.runtime.evidence import EvidenceAsset, OriginalImageEvidence
from jimu_runtime.domain.runtime.executions import NodeId
from jimu_runtime.domain.runtime.facts import EvidenceRef
from jimu_runtime.domain.runtime.intake import (
    AcceptedTask,
    ContextSnapshot,
    IdempotencyScope,
    RegistryRecordRef,
)
from jimu_runtime.domain.runtime.media import MediaAsset, MediaRef
from jimu_runtime.domain.runtime.perception import (
    GroundingOutcome,
    ModelGroundingProvenance,
)
from jimu_runtime.domain.runtime.tool_executions import (
    FailedToolResult,
    ReturnedToolAttempt,
)
from jimu_runtime.gateway.bridge import CapabilityGateway
from jimu_runtime.gateway.http_wire import ImageMediaType, VerifiedImage
from jimu_runtime.inspection.decision import (
    DecisionSourceSnapshot,
    build_decision,
    build_profile_gate,
    candidate_binding,
    decision_unresolved,
    validate_decision,
)
from jimu_runtime.inspection.media import LocalMediaStore, MediaError, ToolSidecarSlot
from jimu_runtime.inspection.perception import (
    CandidateModelResult,
    ConfiguredPerception,
    candidate_model_call,
    project_candidate_result,
    verify_discovery_rejection,
    verify_observation_result,
)
from jimu_runtime.inspection.perception.grounding import (
    verify_grouped_grounding_response,
)
from jimu_runtime.inspection.perception.observation import observation_model_call
from jimu_runtime.workflow.mock_support import K, VerifiedExecutionScope, artifact
from jimu_runtime.workflow.worker import (
    AdapterRejected,
    ClaimedNode,
    ClaimedNodeInvoker,
    WorkflowAdapters,
    WorkflowWorker,
)
from jimu_runtime.workflow.worker_nodes import NodeResult, invocation_envelope
from jimu_runtime.workflow.worker_records import (
    DecisionMessage,
    DiscoveryMessage,
    GroundingMessage,
    ObservationMessage,
)
from jimu_runtime.workflow.worker_request_evidence import LocalRequestEvidence
from jimu_runtime.workflow.worker_source import (
    HistoricalWorkerHost,
    TrustedWorkerSource,
    WorkerHost,
)

# Decision threads wait for verified file reads scheduled by the event loop.
# They must not occupy the default executor that LocalMediaStore uses for I/O.
_DECISION_EXECUTOR = ThreadPoolExecutor(
    max_workers=4, thread_name_prefix="jimu-decision"
)

type CaptureRegistry = Callable[
    [IdempotencyScope, AcceptedTask],
    Awaitable[tuple[RegistryRecordRef, RegistryRecordRef]],
]


@dataclass(frozen=True, slots=True)
class VerifiedMediaWorkerHost:
    """Read registered media bytes and retain the capture refs selected by N01."""

    media: LocalMediaStore
    current_capabilities: Callable[[IdempotencyScope], Awaitable[CapabilitySnapshot]]
    capture_registry: CaptureRegistry

    async def capabilities(self, scope: IdempotencyScope) -> CapabilitySnapshot:
        return await self.current_capabilities(scope)

    async def verified_original(
        self, scope: IdempotencyScope, media_ref: MediaRef
    ) -> MediaAsset:
        return await self.media.verified_original(scope, media_ref)

    async def worker_source(
        self, scope: IdempotencyScope, task: AcceptedTask
    ) -> TrustedWorkerSource:
        original = await self.media.verified_task_original(scope, task)
        device, camera = await self.capture_registry(scope, task)
        return TrustedWorkerSource(original.asset, device, camera)

    async def worker_source_for_snapshot(
        self, scope: IdempotencyScope, task: AcceptedTask, snapshot: ContextSnapshot
    ) -> TrustedWorkerSource:
        if task.idempotency_scope != scope:
            raise MediaError("original_media_scope_mismatch")
        original = await self.media.scoped_original_metadata(scope, task.media_ref)
        task.validate_media(original)
        return TrustedWorkerSource(original, snapshot.device_ref, snapshot.camera_ref)


class InspectionNodes:
    """Compose N01–N04, the ToolPlan adapter, and configured N06 with verified sources."""

    def __init__(
        self,
        media: LocalMediaStore,
        requests: LocalRequestEvidence,
        model_gateway: CapabilityGateway,
        execute_tools: ClaimedNodeInvoker,
        *,
        model_resource_scope_id: str,
        max_evidence_bytes: int,
    ) -> None:
        if (
            type(max_evidence_bytes) is not int
            or not 0 < max_evidence_bytes <= 64 * 1024 * 1024
        ):
            raise ValueError("invalid inspection evidence byte budget")
        if not model_gateway.retains_response_evidence:
            raise ValueError(
                "configured inspection requires a model response evidence writer"
            )
        self._media = media
        self._requests = requests
        self._gateway = model_gateway
        self._tools = execute_tools
        self._budget = max_evidence_bytes
        self._perception = ConfiguredPerception(
            self._load_image,
            requests.stage,
            model_resource_scope_id,
        )

    def ports(self) -> WorkflowAdapters:
        return WorkflowAdapters(
            invoke=self.invoke,
            profile_gate=build_profile_gate,
            validate_observation=self._validate_observation,
            validate_decision=self._validate_decision,
            verify_invocation=self._gateway.verify_invocation,
            verify_late_invocation=self._gateway.verify_late_invocation,
            validate_perception=self._validate_perception,
            decision_unresolved=decision_unresolved,
        )

    async def _load_image(self, scope: VerifiedExecutionScope) -> VerifiedImage:
        original = await self._media.verified_task_original(
            scope.task.idempotency_scope, scope.task
        )
        if original.asset != scope.media or original.asset.mime_type not in {
            "image/png",
            "image/jpeg",
        }:
            raise AdapterRejected("inspection_original_image_mismatch")
        return VerifiedImage(
            original.content,
            cast(ImageMediaType, original.asset.mime_type),
            original.asset.media_ref.sha256,
        )

    async def invoke(self, claimed: ClaimedNode) -> NodeResult:
        node_id = claimed.node.node_id
        if node_id is NodeId.N05:
            return await self._tools(claimed)
        if node_id is not NodeId.N06:
            return await self._perception.execute(claimed, self._gateway)
        obj = claimed.previous.object_record(claimed.branch)
        binding = candidate_binding(claimed.opened, obj)
        candidate = (
            None
            if binding is None
            else await self._perception.predict_candidates(
                claimed, self._gateway, binding
            )
        )
        assert claimed.attempt.started_at is not None
        return await get_running_loop().run_in_executor(
            _DECISION_EXECUTOR,
            build_decision,
            claimed.opened,
            claimed.previous,
            invocation_envelope(
                claimed.opened, claimed.branch, claimed.node, claimed.attempt
            ),
            obj,
            claimed.attempt.started_at,
            self._decision_sources(claimed),
            candidate,
        )

    async def _validate_perception(
        self, claimed: ClaimedNode, message: DiscoveryMessage | GroundingMessage
    ) -> None:
        if isinstance(message, GroundingMessage):
            scope = claimed.opened.scope
            invocations = {row.model_invocation_id: row for row in message.invocations}
            for record in message.records:
                profile = scope.resolve(record.profile_ref, ProfileDefinition)
                if profile.grounding_grouping is None or record.outcome not in (
                    GroundingOutcome.OBJECTS,
                    GroundingOutcome.NO_TARGET,
                ):
                    continue
                if not isinstance(record.producer, ModelGroundingProvenance):
                    raise AdapterRejected(
                        "inspection.grouped_grounding_producer_mismatch"
                    )
                dispatch_id = record.producer.model_invocation_ref.record_id
                reply = await self._media.read_model_response(
                    claimed.claim.scope, scope.execution_id, dispatch_id, late=False
                )
                if (
                    reply.normalized is None
                    or dispatch_id not in invocations
                    or reply.receipt.invocation != invocations[dispatch_id]
                ):
                    raise AdapterRejected(
                        "inspection.grouped_grounding_response_unavailable"
                    )
                verify_grouped_grounding_response(
                    scope, message, record, reply.normalized
                )
            for grounding_gap in message.undispatched_profiles:
                await self._gateway.verify_model_diagnostic(
                    claimed,
                    grounding_gap.dispatch_id,
                    ModelBindingContentRef.model_validate(
                        grounding_gap.binding_ref.model_dump()
                    ),
                    (grounding_gap.profile_ref,),
                    grounding_gap.boundary,
                    grounding_gap.error.code,
                    None,
                )
            return
        for gap in message.group_failures:
            matched = next(
                (
                    item
                    for item in message.invocations
                    if item.model_invocation_id == gap.dispatch_id
                ),
                None,
            )
            transport_succeeded = (
                matched is not None and matched.outcome.status == "succeeded"
            )
            await self._gateway.verify_model_diagnostic(
                claimed,
                gap.dispatch_id,
                ModelBindingContentRef.model_validate(gap.binding_ref.model_dump()),
                gap.profile_refs,
                gap.boundary,
                None if transport_succeeded else gap.error.code,
                gap.model_invocation_ref,
            )
            if transport_succeeded:
                response = await self._media.read_model_response(
                    claimed.claim.scope,
                    claimed.claim.execution_id,
                    gap.dispatch_id,
                    late=False,
                )
                if response.normalized is None:
                    raise AdapterRejected(
                        "inspection_discovery_rejection_bytes_unavailable"
                    )
                verify_discovery_rejection(
                    claimed.opened.scope, gap, response.normalized
                )

    async def _validate_observation(
        self, claimed: ClaimedNode, message: ObservationMessage
    ) -> None:
        scope = claimed.opened.scope
        discovery = claimed.previous.discovery
        if discovery is None:
            raise AdapterRejected("inspection_observation_selection_missing")
        (selection,) = tuple(
            item
            for item in discovery.selections
            if item.selection_id
            == message.inputs.object_record.question_selection_ref.record_id
        )
        prompt_bytes = None
        for row in message.durable_request_evidence:
            content = await self._requests.read(scope.task, row)
            if row.evidence_id == message.inputs.compiled_prompt_evidence_ref.record_id:
                prompt_bytes = content
        original = message.original_image_evidence
        if (
            original is not None
            and await self._read_evidence(claimed, original) is None
        ):
            raise AdapterRejected("inspection_observation_original_unavailable")
        if prompt_bytes is None:
            raise AdapterRejected("inspection.observation_prompt_unavailable")
        call = observation_model_call(
            message.inputs,
            prompt_bytes.decode("utf-8"),
            await self._load_image(scope),
            self._perception.resource_scope_id,
            attempt_number=claimed.attempt.attempt_number,
            original=original,
        )
        await self._gateway.verify_model_call(
            claimed,
            message.invocation,
            call,
            late=message.batch.arrival_qualification == "late",
        )
        known = await self._media.read_model_response(
            claimed.claim.scope,
            scope.execution_id,
            message.invocation.model_invocation_id,
            late=message.batch.arrival_qualification == "late",
        )
        if known.receipt.invocation != message.invocation:
            raise AdapterRejected("inspection_observation_invocation_unqualified")
        if (
            known.receipt.acknowledgement == "http_headers"
            and known.receipt.response_digest is not None
            and known.receipt.response_bytes_omission_reason is None
            and known.raw is None
        ):
            raise AdapterRejected("inspection_observation_response_unavailable")
        verify_observation_result(
            scope,
            message,
            claimed.previous.snapshot(),
            selection,
            known.normalized,
            retry_witness=claimed.previous.observation_retry,
            failure=known.receipt.error,
            slot_receipt=None
            if known.receipt.normalized_response_digest is None
            else (
                known.receipt.invalid_question_slots,
                known.receipt.missing_question_slots,
            ),
            records=claimed.previous,
        )

    async def _validate_decision(
        self, claimed: ClaimedNode, message: DecisionMessage
    ) -> None:
        candidate = None
        invocation = message.invocation
        if invocation is not None:
            binding = candidate_binding(
                claimed.opened, message.draft.inputs.object_record
            )
            if binding is None:
                raise AdapterRejected("inspection_candidate_binding_missing")
            call = candidate_model_call(
                claimed,
                binding,
                await self._load_image(claimed.opened.scope),
                self._perception.resource_scope_id,
            )
            await self._gateway.verify_model_call(claimed, invocation, call)
            response = await self._media.read_model_response(
                claimed.claim.scope,
                claimed.claim.execution_id,
                invocation.model_invocation_id,
                late=False,
            )
            if response.receipt.invocation != invocation:
                raise AdapterRejected("inspection_candidate_invocation_unqualified")
            if (
                response.receipt.response_evidence_error is not None
                and invocation.outcome.status == "succeeded"
            ):
                candidate = CandidateModelResult(
                    (), invocation, response.receipt.response_evidence_error
                )
            else:
                candidate = project_candidate_result(
                    claimed,
                    binding,
                    invocation,
                    response.normalized,
                    failure_code=None
                    if response.receipt.error is None
                    else response.receipt.error.code,
                )
        assert claimed.attempt.started_at is not None
        await get_running_loop().run_in_executor(
            _DECISION_EXECUTOR,
            partial(
                validate_decision,
                claimed.opened,
                claimed.previous,
                message,
                self._decision_sources(claimed),
                envelope=invocation_envelope(
                    claimed.opened, claimed.branch, claimed.node, claimed.attempt
                ),
                object_record=claimed.previous.object_record(claimed.branch),
                at=claimed.attempt.started_at,
                candidate=candidate,
            ),
        )

    def _decision_sources(self, claimed: ClaimedNode) -> DecisionSourceSnapshot:
        authority = claimed.authority
        if authority is None:
            raise AdapterRejected("inspection_authority_snapshot_missing")
        expected_pins = {
            pin.ref.record_id: pin.digest
            for pin in claimed.node.upstream_inputs
            if pin.ref.record_kind is K.EVIDENCE
        }
        assets = claimed.evidence
        if expected_pins != {
            row.evidence_id: artifact(K.EVIDENCE, row.evidence_id, row).pin.digest
            for row in assets
        }:
            raise AdapterRejected("inspection_evidence_snapshot_mismatch")
        loop = get_running_loop()
        allowed = frozenset(assets)

        def read(asset: EvidenceAsset, limit: int) -> bytes | None:
            # The pure guard runs in a worker thread. It asks for only exact,
            # policy-relevant assets after checking each Rule's own byte budget.
            if asset not in allowed or asset.byte_size > min(limit, self._budget):
                return None
            prior = claimed.evidence_availability.get(asset)
            if prior is False:
                return None
            remaining = (claimed.deadline - datetime.now(UTC)).total_seconds()
            content = None
            if remaining > 0:
                pending = run_coroutine_threadsafe(
                    self._read_evidence(claimed, asset), loop
                )
                try:
                    content = pending.result(timeout=min(remaining, 10))
                except MediaError:
                    pass
                except TimeoutError:
                    pending.cancel()
            if content is not None and len(content) == asset.byte_size:
                claimed.evidence_availability[asset] = True
                return content
            if prior is True:
                raise AdapterRejected("inspection_evidence_changed_during_decision")
            claimed.evidence_availability[asset] = False
            return None

        return DecisionSourceSnapshot(
            authority,
            tuple(
                EvidenceRef(record_kind=K.EVIDENCE, record_id=row.evidence_id)
                for row in assets
            ),
            assets,
            read,
            self._budget,
        )

    async def _read_evidence(
        self, claimed: ClaimedNode, row: EvidenceAsset
    ) -> bytes | None:
        task = claimed.opened.scope.task
        scope, eid = claimed.claim.scope, claimed.claim.execution_id
        if isinstance(row.spatial, OriginalImageEvidence):
            original = await self._media.verified_task_original(scope, task)
            row.validate_original_media(original.asset)
            return (
                original.content
                if (
                    row.storage == original.asset.storage
                    and row.stage == original.asset.stage
                    and row.spatial.coordinate_system == original.coordinate_system
                )
                else None
            )
        if row.evidence_type is EvidenceType.MODEL_REQUEST:
            return await self._requests.read(task, row)
        tool = claimed.previous.tool
        if tool is None:
            return None
        for execution in tool.executions:
            reply = execution.attempt_result
            if not isinstance(reply, ReturnedToolAttempt) or isinstance(
                reply.result, FailedToolResult
            ):
                continue
            for named in reply.result.evidence:
                if named.name not in {
                    "wheel_mask",
                    "tactile_paving_mask",
                    "geometry",
                } or all(
                    ref.record_id != row.evidence_id for ref in named.evidence_refs
                ):
                    continue
                try:
                    loaded = await self._media.read_tool_sidecar(
                        scope,
                        eid,
                        execution.tool_execution_id,
                        cast(ToolSidecarSlot, named.name),
                        expected_asset=row,
                    )
                except MediaError:
                    continue
                return loaded.content
        return None


def inspection_worker(
    engine: AsyncEngine,
    release_manager: ReleaseManager,
    bundle_store: LocalBundleStore,
    *,
    host: WorkerHost,
    bundle_limits: BundleLimits,
    media: LocalMediaStore,
    gateway: CapabilityGateway,
    execute_tools: ClaimedNodeInvoker,
    model_resource_scope_id: str,
    max_evidence_bytes: int = 32 * 1024 * 1024,
) -> WorkflowWorker:
    """Open a real six-node worker with caller-provisioned trusted host adapters."""
    nodes = InspectionNodes(
        media,
        LocalRequestEvidence(media),
        gateway,
        execute_tools,
        model_resource_scope_id=model_resource_scope_id,
        max_evidence_bytes=max_evidence_bytes,
    )
    active_host = host
    if not isinstance(host, HistoricalWorkerHost):

        async def capture_registry(
            scope: IdempotencyScope, task: AcceptedTask
        ) -> tuple[RegistryRecordRef, RegistryRecordRef]:
            registration = await host.worker_source(scope, task)
            return registration.device, registration.camera

        active_host = VerifiedMediaWorkerHost(
            media, host.capabilities, capture_registry
        )
    return WorkflowWorker(
        engine,
        release_manager,
        bundle_store,
        host=active_host,
        bundle_limits=bundle_limits,
        adapters=nodes.ports(),
    )
