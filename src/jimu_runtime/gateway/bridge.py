"""Run one physically audited model operation from a committed Workflow claim."""

from asyncio import CancelledError, create_task, get_running_loop, shield, to_thread
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from typing import Protocol, cast, runtime_checkable
from uuid import uuid4

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from jimu_runtime.bundle.integrity import sha256_digest
from jimu_runtime.domain.base import ContractModel
from jimu_runtime.domain.definition_enums import ModelCapability
from jimu_runtime.domain.definition_support import reference_identity
from jimu_runtime.domain.definitions.capabilities import ProfileDefinition
from jimu_runtime.domain.definitions.models import ModelBinding, ModelBindingContentRef
from jimu_runtime.domain.definitions.tools import ToolDefinition
from jimu_runtime.domain.enums import AttemptStatus, RuntimeRecordKind
from jimu_runtime.domain.policies import EvaluatedFallbackPolicy, TriggerError
from jimu_runtime.domain.runtime.executions import Attempt, NodeId, NodeRun
from jimu_runtime.domain.runtime.facts import (
    AttemptRef,
    EvidenceRef,
    ModelInvocationRef,
    NodeRunRef,
    ObjectRef,
    ProfileContentRef,
)
from jimu_runtime.domain.runtime.intake import IdempotencyScope
from jimu_runtime.domain.runtime.invocations import (
    ConfiguredProviderIdentity,
    FailedInvocation,
    InvocationContractSnapshot,
    InvocationError,
    InvocationNodeId,
    InvocationPurpose,
    ModelInvocation,
    PrimaryProviderRoute,
    UndispatchedInvocation,
)
from jimu_runtime.domain.validation.authority_inputs import (
    AttemptDescriptor,
    NodeRunDescriptor,
)
from jimu_runtime.domain.validation.invocations import (
    NestedModelStepLineage,
    NestedModelStepOwner,
)
from jimu_runtime.gateway.http import HttpModelTransport
from jimu_runtime.gateway.http_wire import VerifiedImage, WireError
from jimu_runtime.gateway.invocation import (
    DispatchBoundary,
    DispatchOutcome,
    DispatchRequest,
    ModelTransport,
    TransportCancelled,
    TransportResult,
    project_dispatch,
)
from jimu_runtime.gateway.journal import (
    DispatchIntent,
    DispatchJournal,
    DispatchLease,
    DispatchLiveGuard,
    ModelDispatchResult,
    ModelDispatchTarget,
    NestedModelDispatchTarget,
    ProviderDispatchObservation,
    StoredDispatch,
    UnselectedModelDispatchTarget,
    UnselectedNestedModelDispatchTarget,
)
from jimu_runtime.gateway.routing import (
    CurrentRoutingState,
    DeploymentBudget,
    PlannedRoute,
    RecordedInvocation,
    RouteBudget,
    RouteOperation,
    RoutePolicy,
    RouteRejected,
    plan_fallback,
    plan_primary,
    plan_retry,
)
from jimu_runtime.tools.step_attempts import (
    NestedToolAttemptLink,
    NestedToolRetryHistory,
    StepAttempt,
    StepAttemptStore,
)
from jimu_runtime.workers.ownership import acquire_with_compensation
from jimu_runtime.workers.resources import (
    ResourceAdmission,
    ResourceLease,
    ResourceUnavailable,
)
from jimu_runtime.workflow import repository
from jimu_runtime.workflow.mock_support import record_digest
from jimu_runtime.workflow.worker import (
    AdapterRejected,
    ClaimedNode,
    ClaimedNodeInvoker,
    RetryableNodeFailure,
)
from jimu_runtime.workflow.worker_nodes import NodeResult
from jimu_runtime.workflow.worker_state import read_observation_retry


class PreparedModelTransport(ModelTransport, Protocol):
    def prepare(
        self, route: PlannedRoute, prompt: str, media: VerifiedImage | None = None
    ) -> bytes: ...


@runtime_checkable
class AsyncPreparingModelTransport(Protocol):
    async def prepare_async(
        self, route: PlannedRoute, prompt: str, media: VerifiedImage | None = None
    ) -> bytes: ...


class ModelResponseWriter(Protocol):
    async def stage_model_response(
        self,
        scope: IdempotencyScope,
        execution_id: str,
        dispatch_id: str,
        receipt: TransportResult,
    ) -> object: ...


class CurrentModelRegistry(Protocol):
    async def __call__(self, scope: IdempotencyScope) -> CurrentRoutingState: ...


@dataclass(frozen=True, slots=True)
class ModelCallLimits:
    """Explicit host ceilings, applied to physical send intents."""

    node: int
    operation: int
    deployment: int
    retryable: tuple[TriggerError, ...]
    retry_ambiguous_primary: bool = True

    def __post_init__(self) -> None:
        if any(
            type(value) is not int or not 1 <= value <= 1024
            for value in (self.node, self.operation, self.deployment)
        ):
            raise ValueError("model call ceilings must be between 1 and 1024")


@dataclass(frozen=True, slots=True)
class ModelCall:
    binding_ref: ModelBindingContentRef
    purposes: tuple[InvocationPurpose, ...]
    profile_refs: tuple[ProfileContentRef, ...]
    prompt: str = field(repr=False)
    resource_scope_id: str
    image: VerifiedImage | None = None
    object_ref: ObjectRef | None = None
    source_discovery_invocation_ref: ModelInvocationRef | None = None
    contract_snapshot: InvocationContractSnapshot | None = None
    evidence_refs: tuple[EvidenceRef, ...] = ()


class _LogicalModelInput(ContractModel):
    node_input_digest: str
    binding_ref: ModelBindingContentRef
    purposes: tuple[InvocationPurpose, ...]
    profile_refs: tuple[ProfileContentRef, ...]
    prompt_digest: str
    image_digest: str | None
    object_ref: ObjectRef | None
    source_discovery_invocation_ref: ModelInvocationRef | None
    contract_snapshot: InvocationContractSnapshot | None


@dataclass(frozen=True, slots=True)
class _OperationHistory:
    receipts: tuple[StoredDispatch, ...]
    invocations: tuple[RecordedInvocation, ...]
    unresolved_after_witness: bool
    consumer_retry: bool = False


def _operation_history(
    records: tuple[StoredDispatch, ...],
    verified: tuple[RecordedInvocation, ...],
    ranks: dict[str, int],
) -> _OperationHistory:
    unresolved_after = False
    if verified:
        last = ranks[verified[-1].invocation.model_invocation_id]
        unresolved_after = any(
            ranks[row.intent.dispatch_id] > last
            and row.send_admitted
            and not any(
                item.result.boundary == "not_dispatched" for item in row.results
            )
            for row in records
        )
    return _OperationHistory(records, verified, unresolved_after)


_HARD_BUDGET_CODES = frozenset(
    {
        "model.call_budget_exhausted",
        "model.node_budget_exhausted",
        "model.operation_budget_exhausted",
        "model.deployment_budget_exhausted",
    }
)


class ModelCallFailure(AdapterRejected):
    """Normalized route or pre-send rejection suited to a Worker diagnostic."""

    pass


def require_model_output(result: DispatchOutcome) -> bytes:
    """Translate dispatch failure for the Worker without losing its raw error class."""
    if result.output is not None:
        return result.output
    error = result.error
    if error is None:
        raise ModelCallFailure("model.output_unavailable")
    if error.code in _HARD_BUDGET_CODES:
        raise ModelCallFailure(error.code)
    if error.error_class in ("transport", "unavailable"):
        raise RetryableNodeFailure("transport")
    if error.error_class == "timeout":
        raise RetryableNodeFailure("timeout")
    if error.error_class in ("rate_limit", "resource_exhausted"):
        raise RetryableNodeFailure("resource")
    raise ModelCallFailure(error.code)


def _attempt_descriptor(attempt: Attempt) -> AttemptDescriptor:
    return AttemptDescriptor(
        attempt_ref=AttemptRef(
            record_kind=RuntimeRecordKind.ATTEMPT, record_id=attempt.attempt_id
        ),
        execution_id=attempt.execution_id,
        owner=attempt.owner,
        attempt_number=attempt.attempt_number,
        status=attempt.status,
    )


def _node_descriptor(node: NodeRun) -> NodeRunDescriptor:
    return NodeRunDescriptor(
        node_run_ref=NodeRunRef(
            record_kind=RuntimeRecordKind.NODE_RUN, record_id=node.node_run_id
        ),
        execution_id=node.execution_id,
        node_id=node.node_id.value,
        status=node.status,
    )


def _step_owner(
    lineage: NestedModelStepLineage, link: NestedToolAttemptLink
) -> NestedModelStepOwner:
    return NestedModelStepOwner(
        lineage,
        _attempt_descriptor(link.parent_attempt),
        _node_descriptor(link.child_node),
        _attempt_descriptor(link.child_attempt),
    )


def _nested_lineage(
    history: NestedToolRetryHistory, binding_ref: ModelBindingContentRef
) -> NestedModelStepLineage:
    identity = history.identity
    return NestedModelStepLineage(
        execution_id=identity.execution_id,
        parent_node_run_ref=identity.parent_node_run_ref,
        tool_plan_ref=identity.tool_plan_ref,
        step_id=identity.step_id,
        tool_ref=identity.tool_ref,
        profile_ref=identity.profile_ref,
        object_ref=identity.object_ref,
        binding_ref=binding_ref,
        bound_input_digest=identity.bound_input_digest,
    )


def _error(error_class: str, code: str) -> InvocationError:
    return InvocationError.model_validate(
        {"error_class": error_class, "code": code, "diagnostic_refs": ()}
    )


def _dispatch_lease(lease: ResourceLease | None) -> DispatchLease | None:
    if lease is None:
        return None
    return DispatchLease(
        lease_id=lease.lease_id, owner_id=lease.owner_id, scope_id=lease.scope_id
    )


def _model_intent(
    request: DispatchRequest,
    lease: ResourceLease | None,
    nested_owner: NestedModelStepOwner | None,
    *,
    retain_complete_response: bool | None = None,
) -> DispatchIntent:
    route, selected = request.route, request.route.selected
    operation = route.operation
    ordinary = ModelDispatchTarget(
        node_id=cast(InvocationNodeId, operation.node_run.node_id),
        configured_provider=route.configured,
        route_trace=route.trace,
        purposes=operation.purposes,
        profile_refs=operation.profile_refs,
        object_ref=operation.object_ref,
        selected_provider_id=selected.provider_id,
        selected_provider_kind=selected.provider_kind,
        selected_deployment_id=selected.deployment_id,
        selected_deployment_version=selected.deployment_version,
        selected_model_id=route.ownership.model_id,
        selected_model_version=selected.model_version,
        endpoint_descriptor_ref=route.ownership.endpoint_descriptor_ref,
        owner_ref=route.ownership.owner_ref,
        version_evidence=route.ownership.version_evidence,
    )
    target = (
        ordinary
        if nested_owner is None
        else NestedModelDispatchTarget.model_validate(
            _with_nested_target(ordinary, nested_owner, kind="nested_model")
        )
    )
    return DispatchIntent(
        dispatch_id=request.dispatch_id,
        operation_id=operation.operation_id,
        trace_id=operation.trace_id,
        request_digest=request.request_digest,
        input_digest=operation.input_digest,
        response_contract_digest=operation.response_contract_digest,
        resource_lease=_dispatch_lease(lease),
        retain_complete_response=retain_complete_response,
        target=target,
    )


def _with_nested_target(
    target: ModelDispatchTarget | UnselectedModelDispatchTarget,
    owner: NestedModelStepOwner,
    *,
    kind: str,
) -> dict[str, object]:
    lineage = owner.lineage
    return {
        **target.model_dump(),
        "kind": kind,
        "parent_node_run_id": lineage.parent_node_run_ref.record_id,
        "parent_attempt_id": owner.parent_attempt.attempt_ref.record_id,
        "tool_ref": lineage.tool_ref,
        "tool_plan_ref": lineage.tool_plan_ref,
        "step_id": lineage.step_id,
        "bound_input_digest": lineage.bound_input_digest,
    }


def _model_record(outcome: DispatchOutcome) -> ModelDispatchResult:
    transport = outcome.transport_result
    observation = transport.observation
    invocation = outcome.invocation
    return ModelDispatchResult(
        boundary=outcome.boundary,
        error=outcome.error,
        response_digest=outcome.response_digest,
        observed_response_bytes=transport.observed_response_bytes or 0,
        response_status=transport.response_status,
        response_bytes_omission_reason=transport.response_bytes_omission_reason,
        normalized_response_digest=(
            transport.normalized_response_digest if outcome.error is None else None
        ),
        normalized_response_bytes=(
            transport.normalized_response_bytes if outcome.error is None else None
        ),
        invalid_question_slots=(
            transport.invalid_question_slots
            if outcome.error is None
            and transport.normalized_response_digest is not None
            else ()
        ),
        missing_question_slots=(
            transport.missing_question_slots
            if outcome.error is None
            and transport.normalized_response_digest is not None
            else ()
        ),
        acknowledgement=(
            "http_headers"
            if transport.response_status is not None
            else "provider_receipt"
            if outcome.boundary == "acknowledged"
            else None
        ),
        actual_provider=None if invocation is None else invocation.actual_provider,
        provider_request_id=None
        if observation is None
        else observation.provider_request_id,
        provider_observation=None
        if observation is None
        else ProviderDispatchObservation(
            model_id=observation.model_id,
            deployment_version=observation.deployment_version,
            model_version=observation.model_version,
        ),
        invocation=invocation,
    )


class CapabilityGateway:
    """Compose exact route selection, resource admission and immutable send receipts."""

    def __init__(
        self,
        engine: AsyncEngine,
        journal: DispatchJournal,
        resources: ResourceAdmission,
        transport: PreparedModelTransport,
        current: CurrentModelRegistry,
        limits: ModelCallLimits,
        *,
        response_writer: ModelResponseWriter | None = None,
    ) -> None:
        self._engine = engine
        self._journal = journal
        self._resources = resources
        self._transport = transport
        self._current = current
        self._limits = limits
        self._response_writer = response_writer

    @property
    def retains_response_evidence(self) -> bool:
        return self._response_writer is not None

    def worker_adapter(
        self,
        execute: Callable[[ClaimedNode, "CapabilityGateway"], Awaitable[NodeResult]],
    ) -> ClaimedNodeInvoker:
        """Bind trusted node conversion logic to WorkflowWorker's claimed-node port."""

        async def invoke(claimed: ClaimedNode) -> NodeResult:
            return await execute(claimed, self)

        return invoke

    async def verify_invocation(
        self, claimed: ClaimedNode, invocation: ModelInvocation
    ) -> None:
        """Accept only the exact journaled invocation of the current business Attempt."""
        await self._verify_journal_invocation(claimed, invocation, late=False)

    async def verify_late_invocation(
        self, claimed: ClaimedNode, invocation: ModelInvocation
    ) -> None:
        """Prove an invocation is a genuinely late historical receipt."""
        await self._verify_journal_invocation(claimed, invocation, late=True)

    async def _verify_journal_invocation(
        self, claimed: ClaimedNode, invocation: ModelInvocation, *, late: bool
    ) -> None:
        claim = claimed.claim
        receipt = await self._journal.get_dispatch(
            claim.scope, claim.execution_id, invocation.model_invocation_id
        )
        if (
            receipt is None
            or (receipt.node_run_id, receipt.attempt_id, receipt.job_id, receipt.fence)
            != (
                claim.node_run_id,
                claimed.attempt.attempt_id,
                claim.job_id,
                claim.fence,
            )
            or not any(
                item.late is late
                and isinstance(item.result, ModelDispatchResult)
                and item.result.invocation == invocation
                for item in receipt.results
            )
        ):
            raise ModelCallFailure("model.invocation_receipt_unqualified")

    async def verify_model_call(
        self,
        claimed: ClaimedNode,
        invocation: ModelInvocation,
        call: ModelCall,
        *,
        late: bool = False,
    ) -> None:
        """Rebuild a configured consumer's request correspondence without a send."""
        await self._verify_journal_invocation(claimed, invocation, late=late)
        operation = self._operation(claimed, call, None)
        receipt = await self._journal.get_dispatch(
            claimed.claim.scope,
            claimed.claim.execution_id,
            invocation.model_invocation_id,
        )
        if receipt is None or (
            receipt.intent.operation_id,
            receipt.intent.input_digest,
            receipt.intent.response_contract_digest,
            receipt.intent.request_digest,
        ) != (
            operation.operation_id,
            operation.input_digest,
            operation.response_contract_digest,
            invocation.request_digest,
        ):
            raise ModelCallFailure("model.invocation_request_pin_mismatch")

    async def verify_model_diagnostic(
        self,
        claimed: ClaimedNode,
        dispatch_id: str,
        binding_ref: ModelBindingContentRef,
        profile_refs: tuple[ProfileContentRef, ...],
        boundary: DispatchBoundary,
        error_code: str | None,
        invocation_ref: ModelInvocationRef | None,
    ) -> None:
        """Qualify an unusable group without inventing a call or provider identity."""
        claim = claimed.claim
        receipt = await self._journal.get_dispatch(
            claim.scope, claim.execution_id, dispatch_id
        )
        if (
            receipt is None
            or (receipt.node_run_id, receipt.attempt_id, receipt.job_id, receipt.fence)
            != (
                claim.node_run_id,
                claimed.attempt.attempt_id,
                claim.job_id,
                claim.fence,
            )
            or not isinstance(
                receipt.intent.target,
                (ModelDispatchTarget, UnselectedModelDispatchTarget),
            )
            or receipt.intent.target.node_id != claimed.node.node_id.value
            or reference_identity(receipt.intent.target.configured_provider.binding_ref)
            != reference_identity(binding_ref)
            or receipt.intent.target.profile_refs != profile_refs
            or not any(
                not item.late
                and isinstance(item.result, ModelDispatchResult)
                and item.result.boundary == boundary
                and (
                    None
                    if item.result.invocation is None
                    else item.result.invocation.model_invocation_id
                )
                == (None if invocation_ref is None else invocation_ref.record_id)
                and (None if item.result.error is None else item.result.error.code)
                == error_code
                for item in receipt.results
            )
        ):
            raise ModelCallFailure("model.group_diagnostic_receipt_unqualified")

    async def _now(self) -> datetime:
        async with self._engine.connect() as connection:
            timestamp = await connection.scalar(
                text("SELECT pg_catalog.clock_timestamp()")
            )
            return cast(datetime, timestamp).astimezone(UTC)

    def _operation(
        self, claimed: ClaimedNode, call: ModelCall, owner: NestedModelStepOwner | None
    ) -> RouteOperation:
        scope = claimed.opened.scope
        if (
            not claimed.attempt.belongs_to_node(claimed.node)
            or claimed.claim.node_run_id != claimed.node.node_run_id
            or claimed.claim.execution_id != scope.execution_id
            or set(call.profile_refs)
            - {item.rules.profile_ref for item in claimed.opened.profiles}
        ):
            raise ModelCallFailure("model.foreign_claim_or_profile")
        if claimed.node.node_id is NodeId.N02 and call.profile_refs:
            gate = claimed.previous.gate
            if gate is None or not set(call.profile_refs) <= set(
                gate.evaluation.eligible_profiles
            ):
                raise ModelCallFailure("model.ineligible_discovery_profile")
            if any(
                reference_identity(
                    scope.resolve(ref, ProfileDefinition).discovery_binding_ref
                )
                != reference_identity(call.binding_ref)
                for ref in call.profile_refs
            ):
                raise ModelCallFailure("model.discovery_binding_mismatch")
        image_pin, image_type, image_size = (
            scope.media.media_ref,
            scope.media.mime_type,
            scope.media.byte_size,
        )
        if call.object_ref is not None:
            obj = claimed.previous.object_record(claimed.branch)
            if obj.object_id != call.object_ref.record_id or call.profile_refs != (
                obj.profile_ref,
            ):
                raise ModelCallFailure("model.object_scope_mismatch")
        if claimed.node.node_id is NodeId.N04:
            obj = claimed.previous.object_record(claimed.branch)
            if obj.marked_frame is not None:
                frame = obj.marked_frame
                if (
                    call.object_ref is None
                    or call.image is None
                    or obj.execution_id != scope.execution_id
                    or obj.source_media_ref != scope.media.media_ref
                    or call.purposes != (ModelCapability.OBJECT_OBSERVATION,)
                ):
                    raise ModelCallFailure("model.marked_frame_scope_mismatch")
                image_pin, image_type, image_size = (
                    frame.media_ref,
                    frame.mime_type,
                    frame.byte_size,
                )
        if call.image is not None and (
            call.image.sha256 != image_pin.sha256
            or call.image.media_type != image_type
            or len(call.image.content) != image_size
            or sha256_digest(call.image.content) != image_pin.sha256
        ):
            raise ModelCallFailure(
                "model.original_media_mismatch"
                if image_pin == scope.media.media_ref
                else "model.marked_frame_mismatch"
            )
        logical = _LogicalModelInput(
            node_input_digest=claimed.node.input_digest,
            binding_ref=call.binding_ref,
            purposes=call.purposes,
            profile_refs=call.profile_refs,
            prompt_digest=sha256_digest(call.prompt.encode("utf-8")),
            image_digest=None if call.image is None else call.image.sha256,
            object_ref=call.object_ref,
            source_discovery_invocation_ref=call.source_discovery_invocation_ref,
            contract_snapshot=call.contract_snapshot,
        )
        response = (
            call.contract_snapshot.response_schema_sha256
            if call.contract_snapshot is not None
            else scope.resolve(call.binding_ref, ModelBinding).output_schema_ref.digest
        )
        digest = record_digest(logical)
        return RouteOperation(
            operation_id=f"operation.model.{digest}",
            execution_input_digest=scope.pins.input_digest,
            node_run=_node_descriptor(claimed.node),
            nested_step_input_digest=None
            if owner is None
            else owner.lineage.bound_input_digest,
            binding_ref=call.binding_ref,
            input_digest=digest,
            response_contract_digest=response,
            trace_id=claimed.node.trace.trace_id,
            purposes=call.purposes,
            profile_refs=call.profile_refs,
            object_ref=call.object_ref,
            region_ref=claimed.opened.region_ref,
            source_discovery_invocation_ref=call.source_discovery_invocation_ref,
            contract_snapshot=call.contract_snapshot,
        )

    async def _history(
        self, claimed: ClaimedNode, operation: RouteOperation
    ) -> _OperationHistory:
        claim = claimed.claim
        records = await self._journal.get_operation(
            claim.scope,
            claim.execution_id,
            claim.node_run_id,
            operation.operation_id,
            limit=1024,
        )
        if len(records) == 1024:
            raise ModelCallFailure("model.dispatch_history_limit")
        if any(item.attempt_id == claimed.attempt.attempt_id for item in records):
            raise ModelCallFailure("model.duplicate_attempt_operation")
        history = []
        consumer_witness = None
        ranks: dict[str, int] = {}
        async with self._engine.connect() as connection:
            if (
                claimed.node.node_id is NodeId.N04
                and claimed.previous.observation_retry is not None
            ):
                stored_node = await repository.get_node(
                    connection, claim.scope, claim.execution_id, claim.node_run_id
                )
                stored_attempt = await repository.get_attempt(
                    connection,
                    claim.scope,
                    claim.execution_id,
                    claim.node_run_id,
                    claimed.attempt.attempt_id,
                )
                if stored_node is not None and stored_attempt is not None:
                    consumer_witness = await read_observation_retry(
                        connection, claim.scope, stored_node, stored_attempt
                    )
                if consumer_witness != claimed.previous.observation_retry:
                    raise ModelCallFailure(
                        "model.observation_retry_without_committed_batch"
                    )
            for item in records:
                intent = item.intent
                target = intent.target
                if (
                    not isinstance(
                        target, (ModelDispatchTarget, UnselectedModelDispatchTarget)
                    )
                    or (
                        intent.input_digest,
                        intent.response_contract_digest,
                        intent.trace_id,
                    )
                    != (
                        operation.input_digest,
                        operation.response_contract_digest,
                        operation.trace_id,
                    )
                    or (
                        target.configured_provider.binding_ref,
                        target.profile_refs,
                        target.object_ref,
                        target.purposes,
                    )
                    != (
                        operation.binding_ref,
                        operation.profile_refs,
                        operation.object_ref,
                        operation.purposes,
                    )
                ):
                    raise ModelCallFailure("model.dispatch_history_pin_mismatch")
                attempt = await repository.get_attempt(
                    connection,
                    claim.scope,
                    claim.execution_id,
                    claim.node_run_id,
                    item.attempt_id,
                )
                if attempt is None:
                    raise ModelCallFailure("model.dispatch_attempt_missing")
                ranks[item.intent.dispatch_id] = attempt.attempt_number
                if attempt.status not in {
                    AttemptStatus.FAILED,
                    AttemptStatus.TIMED_OUT,
                }:
                    continue
                for seen in item.results:
                    invoice = seen.result
                    if (
                        not seen.late
                        and isinstance(invoice, ModelDispatchResult)
                        and invoice.invocation is not None
                        and isinstance(
                            invoice.invocation.outcome,
                            (FailedInvocation, UndispatchedInvocation),
                        )
                    ):
                        history.append(
                            RecordedInvocation(
                                operation,
                                _attempt_descriptor(attempt),
                                invoice.invocation,
                            )
                        )
        if consumer_witness is not None:
            prior = consumer_witness.message
            consumer_retry = prior.batch.outcome in {"parse_error", "association_error"}
            matches = tuple(
                item
                for item in records
                if item.attempt_id == consumer_witness.attempt.attempt_id
                and item.intent.dispatch_id == prior.invocation.model_invocation_id
                and isinstance(item.intent.target, ModelDispatchTarget)
                and any(
                    not row.late
                    and isinstance(row.result, ModelDispatchResult)
                    and row.result.invocation == prior.invocation
                    for row in item.results
                )
            )
            if (
                len(matches) != 1
                or operation.contract_snapshot != prior.inputs.contract_snapshot
                or operation.object_ref != prior.batch.object_ref
            ):
                raise ModelCallFailure("model.observation_retry_invocation_unqualified")
            if consumer_retry:
                if (
                    not isinstance(prior.invocation.route_trace, PrimaryProviderRoute)
                    or not matches[0].send_admitted
                    or any(
                        ranks[item.intent.dispatch_id]
                        > consumer_witness.attempt.attempt_number
                        for item in records
                    )
                ):
                    raise ModelCallFailure("model.observation_retry_route_unqualified")
                return _OperationHistory(records, (), False, consumer_retry=True)
        return _operation_history(
            records,
            tuple(sorted(history, key=lambda row: row.attempt.attempt_number)),
            ranks,
        )

    async def _nested_history(
        self,
        claimed: ClaimedNode,
        operation: RouteOperation,
        history: NestedToolRetryHistory,
        owner: NestedModelStepOwner,
        parent_ranks: dict[str, int],
    ) -> _OperationHistory:
        claim = claimed.claim
        lineage = owner.lineage
        records = await self._journal.get_nested_model_operation(
            claim.scope,
            claim.execution_id,
            lineage.parent_node_run_ref.record_id,
            operation.operation_id,
            limit=1024,
        )
        if len(records) == 1024:
            raise ModelCallFailure("model.dispatch_history_limit")
        if any(item.attempt_id == claimed.attempt.attempt_id for item in records):
            raise ModelCallFailure("model.duplicate_attempt_operation")
        prior = {
            link.child_attempt.attempt_id: (link, _step_owner(lineage, link))
            for link in history.priors
        }
        verified = []
        ranks: dict[str, int] = {}
        for row in records:
            target = row.intent.target
            link, prior_owner = prior.get(row.attempt_id, (None, None))
            if not isinstance(
                target, (NestedModelDispatchTarget, UnselectedNestedModelDispatchTarget)
            ) or (
                target.parent_node_run_id,
                target.tool_ref,
                target.tool_plan_ref,
                target.step_id,
                target.bound_input_digest,
                target.configured_provider.binding_ref,
                target.object_ref,
                target.profile_refs,
                target.purposes,
                row.intent.input_digest,
                row.intent.response_contract_digest,
                row.intent.trace_id,
            ) != (
                lineage.parent_node_run_ref.record_id,
                lineage.tool_ref,
                lineage.tool_plan_ref,
                lineage.step_id,
                lineage.bound_input_digest,
                operation.binding_ref,
                operation.object_ref,
                operation.profile_refs,
                operation.purposes,
                operation.input_digest,
                operation.response_contract_digest,
                operation.trace_id,
            ):
                raise ModelCallFailure("model.nested_dispatch_history_pin_mismatch")
            ordinal = parent_ranks.get(target.parent_attempt_id)
            if ordinal is None:
                raise ModelCallFailure("model.nested_parent_attempt_missing")
            ranks[row.intent.dispatch_id] = ordinal
            if link is None or prior_owner is None:
                continue
            if (
                target.parent_attempt_id != link.parent_attempt.attempt_id
                or row.node_run_id != link.child_node.node_run_id
            ):
                raise ModelCallFailure("model.nested_dispatch_history_owner_mismatch")
            for seen in row.results:
                result = seen.result
                if (
                    seen.fence_qualified
                    and not seen.late
                    and isinstance(result, ModelDispatchResult)
                    and (
                        result.boundary == "acknowledged"
                        or result.boundary == "sent_unknown"
                        and result.error is not None
                        and result.error.error_class == "timeout"
                    )
                    and result.invocation in link.invocations
                    and result.invocation is not None
                    and isinstance(
                        result.invocation.outcome,
                        (FailedInvocation, UndispatchedInvocation),
                    )
                ):
                    verified.append(
                        RecordedInvocation(
                            operation.model_copy(
                                update={"node_run": prior_owner.child_node}
                            ),
                            prior_owner.child_attempt,
                            result.invocation,
                            nested_step_owner=prior_owner,
                        )
                    )
        return _operation_history(
            records,
            tuple(
                sorted(
                    verified,
                    key=lambda item: ranks[item.invocation.model_invocation_id],
                )
            ),
            ranks,
        )

    def _configured_budget(self, policy: RoutePolicy) -> RouteBudget:
        return RouteBudget(
            node_remaining=self._limits.node,
            operation_remaining=self._limits.operation,
            deployments=tuple(
                DeploymentBudget(
                    deployment_id=entry.deployment_id, remaining=self._limits.deployment
                )
                for entry in policy.resolved.deployments
            ),
        )

    async def _reserve(
        self, route: PlannedRoute, scope_id: str, identity: str
    ) -> ResourceLease:
        return await acquire_with_compensation(
            lambda: self._resources.reserve(
                route.resource_requirements,
                scope_id,
                owner_id=identity,
            ),
            self._resources.release_reserved,
        )

    def _plan(
        self,
        claimed: ClaimedNode,
        policy: RoutePolicy,
        operation: RouteOperation,
        current: CurrentRoutingState,
        at: datetime,
        budget: RouteBudget,
        ledger: _OperationHistory,
        nested_owner: NestedModelStepOwner | None,
    ) -> PlannedRoute:
        if ledger.unresolved_after_witness:
            raise ModelCallFailure("model.newer_dispatch_unresolved")
        records, history = ledger.receipts, ledger.invocations
        descriptor = _attempt_descriptor(claimed.attempt)
        args = policy, operation, descriptor, current, budget, at
        if ledger.consumer_retry:
            return plan_primary(*args, nested_step_owner=nested_owner)
        if history:
            failure = history[-1].invocation
            assert isinstance(
                failure.outcome, (FailedInvocation, UndispatchedInvocation)
            )
            if (
                isinstance(policy.fallback, EvaluatedFallbackPolicy)
                and failure.outcome.error.error_class in policy.fallback.trigger_errors
                and any(
                    edge.from_deployment_id
                    == failure.route_trace.selected_deployment_id
                    for edge in policy.fallback.allowed_transitions
                )
            ):
                return plan_fallback(
                    *args, history=history, nested_step_owner=nested_owner
                )
            return plan_retry(
                *args,
                history=history,
                retryable_errors=self._limits.retryable,
                nested_step_owner=nested_owner,
            )
        selected_records = tuple(
            item
            for item in records
            if isinstance(item.intent.target, ModelDispatchTarget)
        )
        if nested_owner is not None and records:
            # No route switch or duplicate fallback can be inferred from
            # a model receipt that lacks the original terminal child witness.
            raise ModelCallFailure("model.nested_retry_without_terminal_witness")
        if selected_records and (
            not self._limits.retry_ambiguous_primary
            or any(
                not isinstance(item.intent.target, ModelDispatchTarget)
                or not isinstance(item.intent.target.route_trace, PrimaryProviderRoute)
                or any(
                    isinstance(seen.result, ModelDispatchResult)
                    and seen.result.error is not None
                    and seen.result.error.error_class not in self._limits.retryable
                    for seen in item.results
                )
                for item in selected_records
            )
        ):
            raise ModelCallFailure("model.retry_without_terminal_witness")
        return plan_primary(*args, nested_step_owner=nested_owner)

    async def invoke_model(
        self,
        claimed: ClaimedNode,
        call: ModelCall,
        *,
        nested: tuple[StepAttemptStore, StepAttempt] | None = None,
    ) -> DispatchOutcome:
        """Do one physical send for a typed operation in this Attempt."""
        opened = claimed.opened
        call = replace(
            call,
            binding_ref=ModelBindingContentRef.model_validate(
                call.binding_ref.model_dump()
            ),
        )
        if opened.historical is None:
            raise ModelCallFailure("model.history_unavailable")
        resolved = next(
            (
                row.deployments
                for row in opened.resolved.model_deployments
                if row.binding_ref == call.binding_ref
            ),
            (),
        )
        policy = RoutePolicy.from_execution(
            opened.resolved,
            call.binding_ref,
            tuple(
                row for row in opened.historical.ownership if row.deployment in resolved
            ),
        )
        owner = None
        if nested is None:
            operation = self._operation(claimed, call, None)
            ledger = await self._history(claimed, operation)
        else:
            store, handle = nested
            if (
                handle.parent_claim != claimed.claim
                or handle.parent_attempt_id != claimed.attempt.attempt_id
            ):
                raise ModelCallFailure("model.nested_parent_mismatch")
            sealed = await store.nested_retry_history(handle)
            if sealed is None:
                raise ModelCallFailure("model.nested_fence_lost")
            lineage = _nested_lineage(sealed, call.binding_ref)
            selected_tool = opened.scope.resolve(lineage.tool_ref, ToolDefinition)
            if (
                selected_tool.model_binding_ref is None
                or selected_tool.model_binding_ref.stable_json()
                != call.binding_ref.stable_json()
                or call.profile_refs != (lineage.profile_ref,)
                or call.object_ref != lineage.object_ref
                or call.purposes != (ModelCapability.VISION_AUXILIARY,)
            ):
                raise ModelCallFailure("model.nested_binding_scope_mismatch")
            owner = _step_owner(lineage, sealed.current)
            parent_ranks = {
                link.attempt_ref.record_id: link.attempt_number
                for link in claimed.node.attempt_history
            }
            claimed = replace(
                claimed, claim=handle.claim, node=handle.node, attempt=handle.attempt
            )
            operation = self._operation(claimed, call, owner)
            ledger = await self._nested_history(
                claimed, operation, sealed, owner, parent_ranks
            )
        claim = claimed.claim
        budget = self._configured_budget(policy)

        async def live_guard(connection: AsyncConnection) -> bool:
            assert nested is not None
            return await nested[0].dispatch_live(connection, nested[1])

        guard = live_guard if nested is not None else None

        async def current_plan() -> PlannedRoute:
            current = CurrentRoutingState.model_validate(
                await self._current(claim.scope)
            )
            try:
                return self._plan(
                    claimed,
                    policy,
                    operation,
                    current,
                    await self._now(),
                    budget,
                    ledger,
                    owner,
                )
            except RouteRejected as failure:
                raise ModelCallFailure(failure.code) from None

        identity = f"dispatch.{uuid4().hex}"
        try:
            route = await current_plan()
        except ModelCallFailure as failure:
            return await self._reject_unselected(
                claimed,
                policy,
                operation,
                identity,
                failure.code,
                owner,
                guard,
            )
        try:
            payload = (
                await self._transport.prepare_async(route, call.prompt, call.image)
                if isinstance(self._transport, AsyncPreparingModelTransport)
                else self._transport.prepare(route, call.prompt, call.image)
            )
        except WireError as failure:
            return await self._reject(
                claimed,
                DispatchRequest(identity, route, b""),
                _error("validation", failure.code),
                route.approved_at,
                call.evidence_refs,
                owner,
                guard,
            )
        request = DispatchRequest(identity, route, payload)
        try:
            lease = await self._reserve(route, call.resource_scope_id, identity)
        except CancelledError:
            # The allocation is recovered separately by _reserve. A prepared
            # request cancelled before admission is proven not to have been sent.
            try:
                await self._reject(
                    claimed,
                    request,
                    _error("cancelled", "model.cancelled_before_send"),
                    route.approved_at,
                    call.evidence_refs,
                    owner,
                    guard,
                )
            except ModelCallFailure:
                # Durable task cancellation may already have closed the Attempt.
                pass
            raise
        except ResourceUnavailable:
            return await self._reject(
                claimed,
                request,
                _error("resource_exhausted", "model.resource_unavailable"),
                route.approved_at,
                call.evidence_refs,
                owner,
                guard,
            )

        budget_rejection: DispatchOutcome | None = None

        async def budget_guard(
            connection: AsyncConnection,
        ) -> ModelDispatchResult | None:
            nonlocal budget_rejection
            usage = await self._journal.usage(
                claim.scope,
                claim.execution_id,
                claim.node_run_id,
                provider_id=route.selected.provider_id,
                deployment_id=route.selected.deployment_id,
                operation_id=operation.operation_id,
                connection=connection,
                parent_node_run_id=None
                if owner is None
                else owner.lineage.parent_node_run_ref.record_id,
                tool_step_id=None if owner is None else owner.lineage.step_id,
            )
            assert (
                usage.operation_sends is not None and usage.deployment_sends is not None
            )
            if usage.node_sends >= self._limits.node:
                budget_code = "model.node_budget_exhausted"
            elif usage.operation_sends >= self._limits.operation:
                budget_code = "model.operation_budget_exhausted"
            elif usage.deployment_sends >= self._limits.deployment:
                budget_code = "model.deployment_budget_exhausted"
            else:
                return None
            budget_rejection = project_dispatch(
                request,
                TransportResult(
                    False,
                    error=_error("resource_exhausted", budget_code),
                ),
                started_at=route.approved_at,
                completed_at=route.approved_at,
                evidence_refs=call.evidence_refs,
            )
            return _model_record(budget_rejection)

        prepared = False
        activated = False
        admitted = False
        reply_managed = False
        started_at = route.approved_at
        intent = _model_intent(
            request,
            lease,
            owner,
            retain_complete_response=(
                self._transport.raw_response_retention(route)
                if isinstance(self._transport, HttpModelTransport)
                else None
            ),
        )
        resource_deadline = get_running_loop().time() + lease.timeout_ms / 1000

        async def resource_phase(*, activate: bool) -> None:
            nonlocal prepared, activated
            change = (
                self._resources.activate_remote
                if activate
                else self._resources.prepare_remote_send
            )
            pending = create_task(
                to_thread(change, lease, identity, deadline=resource_deadline)
            )
            try:
                await shield(pending)
            except CancelledError:
                # The local transaction can commit after the coroutine stops
                # waiting. Recover its exact phase before deciding cleanup.
                try:
                    await shield(pending)
                except ResourceUnavailable:
                    pass
                else:
                    prepared = True
                    activated = activate
                raise
            prepared = True
            activated = activate

        try:
            admission = create_task(
                self._journal.begin(
                    claim,
                    claimed.attempt.attempt_id,
                    intent,
                    live_guard=guard,
                    budget_guard=budget_guard,
                )
            )
            try:
                admitted = await shield(admission)
            except CancelledError:
                # Wait for the admission transaction's definitive result before
                # classifying cancellation on either side of its commit.
                admitted = await shield(admission)
                raise
            if not admitted:
                if budget_rejection is not None:
                    return budget_rejection
                raise ModelCallFailure("model.dispatch_fence_lost")
            try:
                latest = await current_plan()
                if not _same_route(route, latest):
                    raise ModelCallFailure("model.live_route_changed")
                if not await self._journal.is_dispatch_current(
                    claim, claimed.attempt.attempt_id, intent
                ):
                    raise ModelCallFailure("model.dispatch_fence_lost")
            except ModelCallFailure as failure:
                reply_managed = True  # this path owns and retains the receipt
                return await self._retain_reply(
                    claimed,
                    request,
                    TransportResult(False, error=_error("association", failure.code)),
                    started_at,
                    call.evidence_refs,
                )
            request = DispatchRequest(identity, latest, payload)
            started_at = await self._now()
            try:
                await resource_phase(activate=False)
            except ResourceUnavailable:
                result = TransportResult(
                    False,
                    error=_error("resource_exhausted", "model.resource_unavailable"),
                )
            else:
                wire_resource_unavailable = False

                async def authorized_at_wire() -> bool:
                    try:
                        observed = await current_plan()
                    except ModelCallFailure:
                        return False
                    return (
                        _same_route(latest, observed)
                        and await self._journal.is_dispatch_current(
                            claim, claimed.attempt.attempt_id, intent
                        )
                        and latest.authorization_active(await self._now())
                    )

                async def live_before_wire() -> bool:
                    nonlocal wire_resource_unavailable
                    if not await authorized_at_wire():
                        return False
                    try:
                        await resource_phase(activate=True)
                    except ResourceUnavailable:
                        wire_resource_unavailable = True
                        return False
                    return await authorized_at_wire()

                if isinstance(self._transport, HttpModelTransport):
                    # Stock HTTP calls the guard after its bounded queue and
                    # validation and before opening the connection.
                    request = DispatchRequest(
                        identity, latest, payload, pre_send_allowed=live_before_wire
                    )
                    reply_managed = True
                    result = await self._send(
                        claimed, request, started_at, call.evidence_refs
                    )
                elif await live_before_wire():
                    # The injected transport has no independent wire gate.
                    reply_managed = True
                    result = await self._send(
                        claimed, request, started_at, call.evidence_refs
                    )
                else:
                    result = TransportResult(
                        False,
                        error=_error("association", "model.presend_authorization_lost"),
                    )
                if wire_resource_unavailable and not result.dispatched:
                    result = TransportResult(
                        False,
                        error=_error(
                            "resource_exhausted", "model.resource_unavailable"
                        ),
                    )
            reply_managed = (
                True  # the receipt task owns this result even without a send
            )
            outcome = await self._retain_reply(
                claimed, request, result, started_at, call.evidence_refs
            )
            if outcome.output is not None:
                try:
                    latest = await current_plan()
                    if not _same_route(route, latest):
                        raise ModelCallFailure("model.live_route_changed")
                    if not await self._journal.is_dispatch_current(
                        claim, claimed.attempt.attempt_id, intent
                    ):
                        raise ModelCallFailure("model.output_fence_lost")
                except ModelCallFailure as failure:
                    # Keep the physical result; consumers receive no usable output.
                    return replace(
                        outcome, output=None, error=_error("association", failure.code)
                    )
            return outcome
        except CancelledError:
            if admitted and not reply_managed:
                await self._retain_reply(
                    claimed,
                    request,
                    TransportResult(
                        False,
                        error=_error("cancelled", "model.cancelled_before_send"),
                    ),
                    started_at,
                    call.evidence_refs,
                )
            raise
        finally:
            if activated:
                await to_thread(self._resources.release, lease)
            elif prepared:
                await to_thread(
                    self._resources.abort_remote_before_send, lease, identity
                )
            else:
                await to_thread(self._resources.release_reserved, lease)

    async def _reject_unselected(
        self,
        claimed: ClaimedNode,
        policy: RoutePolicy,
        operation: RouteOperation,
        dispatch_id: str,
        code: str,
        owner: NestedModelStepOwner | None,
        guard: DispatchLiveGuard | None,
    ) -> DispatchOutcome:
        configured = next(
            row
            for row in policy.configured_pin.deployments
            if row.deployment_id == policy.configured_pin.primary_deployment_id
        )
        error = _error("association", code)
        request_digest = sha256_digest(b"")
        ordinary = UnselectedModelDispatchTarget(
            node_id=cast(InvocationNodeId, operation.node_run.node_id),
            configured_provider=ConfiguredProviderIdentity(
                binding_ref=policy.binding.ref,
                provider_policy_ref=policy.provider_registration.ref,
                primary_deployment_id=configured.deployment_id,
                provider_id=configured.provider_id,
                provider_kind=configured.provider_kind,
                logical_model_id=configured.logical_model_id,
            ),
            purposes=operation.purposes,
            profile_refs=operation.profile_refs,
            object_ref=operation.object_ref,
        )
        target = (
            ordinary
            if owner is None
            else UnselectedNestedModelDispatchTarget.model_validate(
                _with_nested_target(ordinary, owner, kind="nested_model_unselected")
            )
        )
        intent = DispatchIntent(
            dispatch_id=dispatch_id,
            operation_id=operation.operation_id,
            trace_id=operation.trace_id,
            input_digest=operation.input_digest,
            response_contract_digest=operation.response_contract_digest,
            request_digest=request_digest,
            target=target,
        )
        if not await self._journal.reject(
            claimed.claim,
            claimed.attempt.attempt_id,
            intent,
            ModelDispatchResult(boundary="not_dispatched", error=error),
            live_guard=guard,
        ):
            raise ModelCallFailure("model.dispatch_fence_lost")
        return DispatchOutcome(
            request_digest=request_digest,
            response_digest=None,
            boundary="not_dispatched",
            error=error,
            transport_result=TransportResult(False, error=error),
            invocation=None,
            dispatch_id=dispatch_id,
        )

    async def _reject(
        self,
        claimed: ClaimedNode,
        request: DispatchRequest,
        error: InvocationError,
        at: datetime,
        evidence_refs: tuple[EvidenceRef, ...],
        owner: NestedModelStepOwner | None,
        guard: DispatchLiveGuard | None,
    ) -> DispatchOutcome:
        outcome = project_dispatch(
            request,
            TransportResult(False, error=error),
            started_at=at,
            completed_at=at,
            evidence_refs=evidence_refs,
        )
        if not await self._journal.reject(
            claimed.claim,
            claimed.attempt.attempt_id,
            _model_intent(request, None, owner),
            _model_record(outcome),
            live_guard=guard,
        ):
            raise ModelCallFailure("model.dispatch_fence_lost")
        return outcome

    async def _send(
        self,
        claimed: ClaimedNode,
        request: DispatchRequest,
        started_at: datetime,
        evidence_refs: tuple[EvidenceRef, ...],
    ) -> TransportResult:
        try:
            return await self._transport.dispatch(request)
        except CancelledError as cancellation:
            result = TransportCancelled.observe(cancellation)
            await self._retain_reply(
                claimed,
                request,
                result
                if result is not None
                else TransportResult(
                    True, error=_error("cancelled", "model.dispatch_cancelled")
                ),
                started_at,
                evidence_refs,
            )
            raise
        except Exception:
            return TransportResult(
                True, error=_error("transport", "model.dispatch_failed")
            )

    async def _retain_reply(
        self,
        claimed: ClaimedNode,
        request: DispatchRequest,
        result: TransportResult,
        started_at: datetime,
        evidence_refs: tuple[EvidenceRef, ...],
    ) -> DispatchOutcome:
        retained = create_task(
            self._record(claimed, request, result, started_at, evidence_refs)
        )
        try:
            return await shield(retained)
        except CancelledError:
            await shield(retained)
            raise

    async def _record(
        self,
        claimed: ClaimedNode,
        request: DispatchRequest,
        result: TransportResult,
        started_at: datetime,
        evidence_refs: tuple[EvidenceRef, ...],
    ) -> DispatchOutcome:
        if result.response_dispatch_id not in {None, request.dispatch_id} or (
            result.response_dispatch_id is None
            and (
                isinstance(self._transport, HttpModelTransport)
                or self._response_writer is not None
            )
            and (result.response_bytes is not None or result.parsed_content is not None)
        ):
            # The foreign receipt is not evidence of this request's response.
            result = TransportResult(
                result.dispatched,
                error=_error("association", "model.response_dispatch_mismatch"),
            )
        outcome = project_dispatch(
            request,
            result,
            started_at=started_at,
            completed_at=await self._now(),
            evidence_refs=evidence_refs,
        )
        record = _model_record(outcome)
        evidence_failure = None
        transport = outcome.transport_result
        if self._response_writer is not None and (
            transport.response_bytes is not None or transport.parsed_content is not None
        ):
            try:
                await self._response_writer.stage_model_response(
                    claimed.claim.scope,
                    claimed.claim.execution_id,
                    request.dispatch_id,
                    transport,
                )
            except Exception:
                # A file/retention failure does not change the actual HTTP receipt.
                evidence_failure = _error(
                    "internal", "model.response_evidence_store_unavailable"
                )
                record = ModelDispatchResult.model_validate(
                    {
                        **record.model_dump(),
                        "response_evidence_error": evidence_failure.code,
                    }
                )
                outcome = replace(outcome, output=None, error=evidence_failure)
        await self._journal.append_result(
            claimed.claim,
            claimed.attempt.attempt_id,
            request.dispatch_id,
            record,
        )
        if (
            claimed.node.node_id is NodeId.N04
            and outcome.boundary == "acknowledged"
            and outcome.invocation is not None
        ):
            archived = await self._journal.get_dispatch(
                claimed.claim.scope, claimed.claim.execution_id, request.dispatch_id
            )
            if archived is not None and any(
                item.late and item.result == record for item in archived.results
            ):
                return replace(outcome, output=None, late=True)
        return outcome


def _same_route(left: PlannedRoute, right: PlannedRoute) -> bool:
    return (left.ownership, left.trace, left.selected) == (
        right.ownership,
        right.trace,
        right.selected,
    )
