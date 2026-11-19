"""Connect supervised Tool calls to the Workflow's durable parent and step fences."""

from asyncio import CancelledError, create_task, shield
from collections.abc import Callable, Mapping
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from functools import partial
from typing import Literal, Protocol, cast

from jimu_runtime.bundle.capabilities import CapabilitySnapshot
from jimu_runtime.bundle.integrity import sha256_digest
from jimu_runtime.domain.definition_enums import ModelCapability
from jimu_runtime.domain.definitions.models import ModelBindingContentRef
from jimu_runtime.domain.definitions.tools import ToolDefinition, ToolPlanDefinition
from jimu_runtime.domain.fact_schemas.canonicalization import JSONValue, canonical_bytes
from jimu_runtime.domain.fact_selectors import ToolContentRef
from jimu_runtime.domain.nodes.tool_execution import ToolNodeInput
from jimu_runtime.domain.policies import (
    ToolExecutionPolicyArtifact,
    ToolSkipPolicyArtifact,
)
from jimu_runtime.domain.runtime.intake import IdempotencyScope
from jimu_runtime.domain.runtime.invocations import ModelInvocation
from jimu_runtime.domain.runtime.tool_executions import (
    FailedToolAttempt,
    FailedToolResult,
    ReturnedToolAttempt,
    ToolError,
    ToolExecution,
    UndispatchedToolAttempt,
)
from jimu_runtime.gateway.bridge import CapabilityGateway, ModelCall
from jimu_runtime.gateway.journal import (
    DispatchIntent,
    DispatchJournal,
    DispatchLease,
    ModelDispatchResult,
    NestedModelDispatchTarget,
    ToolDispatchResult,
    ToolDispatchTarget,
    ToolImplementationObservation,
)
from jimu_runtime.tools.executor import (
    AncestorToolEvidence,
    GroundingCall,
    PlanStepCall,
    RecordResponse,
    ToolCallResult,
    ToolCancelled,
    ToolExecutor,
)
from jimu_runtime.tools.registry import (
    CPUWorkerExchange,
    NestedModelRequest,
    NestedModelResponse,
    ToolDispatch,
)
from jimu_runtime.tools.step_attempts import (
    RecoveredToolStep,
    StepAttempt,
    StepAttemptStore,
    StepCompletion,
    StepResult,
)
from jimu_runtime.workers.resources import ResourceLease
from jimu_runtime.workflow.mock_support import record_digest
from jimu_runtime.workflow.tool_dependencies import guard_fact_selectors
from jimu_runtime.workflow.worker import (
    AdapterRejected,
    ClaimedNode,
    RetryableNodeFailure,
)
from jimu_runtime.workflow.worker_capabilities import ToolOwnershipPin


class CurrentToolRegistry(Protocol):
    async def __call__(self, scope: IdempotencyScope) -> CapabilitySnapshot: ...


def tool_step_operation_id(parent_node_run_id: str, step_id: str) -> str:
    """Stable audit key for a logical step across worker ownership and Attempts."""
    digest = sha256_digest(canonical_bytes([parent_node_run_id, step_id]))
    return f"operation.tool_step.{digest}"


def _tool_deadline(
    started_at: datetime, parent_deadline: datetime, timeout_ms: int
) -> datetime:
    return min(started_at + timedelta(milliseconds=timeout_ms), parent_deadline)


def _tool_error(tool: ToolExecution) -> ToolError | None:
    outcome = tool.attempt_result
    if isinstance(outcome, (UndispatchedToolAttempt, FailedToolAttempt)):
        return outcome.error
    if isinstance(outcome, ReturnedToolAttempt) and isinstance(
        outcome.result, FailedToolResult
    ):
        return outcome.result.error
    return None


class ToolExecutionAdapter:
    """Invoke a bound N03 Tool or create a separately fenced N05 child round."""

    def __init__(
        self,
        executor: ToolExecutor,
        steps: StepAttemptStore,
        journal: DispatchJournal,
        current: CurrentToolRegistry,
        *,
        record_response: RecordResponse | None = None,
        model_gateway: CapabilityGateway | None = None,
        model_resource_scope: Callable[[ClaimedNode, ToolDefinition], str]
        | None = None,
    ) -> None:
        if (model_gateway is None) != (model_resource_scope is None):
            raise ValueError(
                "nested model capability requires a registered resource scope source"
            )
        self._executor = executor
        self._steps = steps
        self._journal = journal
        self._current = current
        self._record_response = record_response
        self._model_gateway = model_gateway
        self._model_resource_scope = model_resource_scope

    def _binding(
        self, claimed: ClaimedNode, tool_ref: ToolContentRef
    ) -> tuple[ToolDefinition, ToolOwnershipPin]:
        historical = claimed.opened.historical
        pin = (
            None
            if historical is None
            else next(
                (
                    item
                    for item in historical.tool_ownership
                    if item.tool_ref == tool_ref
                ),
                None,
            )
        )
        if pin is None:
            raise AdapterRejected("tool_historical_ownership_unavailable")
        return claimed.opened.scope.resolve(tool_ref, ToolDefinition), pin

    async def _available(
        self, claimed: ClaimedNode, tool: ToolDefinition, pin: ToolOwnershipPin
    ) -> bool:
        snapshot = CapabilitySnapshot.model_validate(
            await self._current(claimed.claim.scope)
        )
        now = datetime.now(UTC)
        requirement = tool.implementation_requirement
        features = set(requirement.required_features)
        return snapshot.is_current(now) and any(
            row.is_current(now)
            and (
                row.family,
                row.capability_id,
                row.contract_version,
                row.implementation_version,
            )
            == (
                requirement.family,
                requirement.capability_id,
                requirement.contract_version,
                pin.implementation_version,
            )
            and features <= set(row.supported_features)
            and (
                requirement.evaluation_ref is None
                or any(
                    entry.ref == requirement.evaluation_ref
                    and entry.is_current(now)
                    and features <= set(entry.reviewed_features)
                    for entry in row.evaluations
                )
            )
            for row in snapshot.capabilities
        )

    async def recover_selected(
        self, claimed: ClaimedNode, inputs: ToolNodeInput
    ) -> tuple[RecoveredToolStep, ...] | None:
        """Reopen prior selected results for audit before touching new input bytes."""
        return await self._steps.recover_selected(claimed.claim, inputs)

    async def execute_step(
        self,
        claimed: ClaimedNode,
        inputs: ToolNodeInput,
        step_id: str,
        input_fields: Mapping[str, object],
        *,
        peers: tuple[ToolExecution, ...] = (),
        ancestor_evidence: tuple[AncestorToolEvidence, ...] = (),
        cpu_exchange: CPUWorkerExchange | None = None,
    ) -> StepCompletion:
        """Return the whole durable output group, reusing it only with exact input."""
        scope = claimed.opened.scope
        plan = scope.resolve(inputs.tool_plan_ref, ToolPlanDefinition)
        step = next((item for item in plan.steps if item.step_id == step_id), None)
        if step is None:
            raise AdapterRejected("tool_step_not_in_plan")
        definition, pin = self._binding(claimed, step.tool_ref)
        policy = scope.resolve(
            definition.execution_policy_ref, ToolExecutionPolicyArtifact
        ).policy
        fields = dict(input_fields)
        exact_request = self._executor.prepare_step_input(
            claimed.opened.resolved,
            PlanStepCall(inputs, step_id),
            claimed.opened.source.media,
            fields,
        )
        input_digest = sha256_digest(exact_request)
        handle = await self._steps.begin(
            claimed.claim,
            inputs,
            step,
            input_digest,
            timeout_ms=policy.timeout_ms,
            max_attempts=policy.max_attempts,
            guard_selectors=guard_fact_selectors(
                scope.resolve(step.skip_policy_ref, ToolSkipPolicyArtifact).policy
            ),
            ancestor_evidence=ancestor_evidence,
            ancestor_tools=tuple(
                (ref, scope.resolve(ref, ToolDefinition))
                for ref in dict.fromkeys(
                    proof.completion.result.execution.tool_ref
                    for proof in ancestor_evidence
                )
            ),
        )
        if handle is None:
            raise AdapterRejected("tool_step_fence_lost")
        if isinstance(handle, StepCompletion):
            return handle
        result = await self._execute(
            claimed,
            PlanStepCall(handle.inputs, step.step_id),
            definition,
            pin,
            fields,
            input_digest,
            handle=handle,
            peers=peers,
            ancestor_evidence=ancestor_evidence,
            cpu_exchange=cpu_exchange,
        )
        if result.execution.status != "not_dispatched" and not await self._available(
            claimed, definition, pin
        ):
            raise AdapterRejected("tool_live_registration_revoked")
        completion = await self._steps.complete(
            handle,
            StepResult(
                schema_version="1.0.0",
                execution=result.execution,
                facts=result.facts,
                evidence=result.evidence,
                invocations=result.nested_invocations,
            ),
        )
        if completion is None:
            raise AdapterRejected("tool_step_fence_lost")
        if result.retryable:
            retry_tool_step(completion)
        return completion

    async def execute_grounding(
        self,
        claimed: ClaimedNode,
        call: GroundingCall,
        input_fields: Mapping[str, object],
        *,
        peers: tuple[ToolExecution, ...] = (),
    ) -> ToolCallResult:
        from jimu_runtime.domain.definitions.capabilities import ToolGroundingBinding

        binding = next(
            (
                item.grounding_binding
                for item in call.inputs.profiles
                if item.hit.profile_ref == call.profile_ref
            ),
            None,
        )
        if not isinstance(binding, ToolGroundingBinding):
            raise AdapterRejected("tool_grounding_binding_unavailable")
        definition, pin = self._binding(claimed, binding.tool_ref)
        result = await self._execute(
            claimed,
            call,
            definition,
            pin,
            input_fields,
            sha256_digest(canonical_bytes(cast(JSONValue, dict(input_fields)))),
            peers=peers,
        )
        if not await self._available(
            claimed, definition, pin
        ) or not await self._journal.is_current(
            claimed.claim, claimed.attempt.attempt_id
        ):
            raise AdapterRejected("tool_grounding_authority_unavailable")
        return result

    async def _execute(
        self,
        claimed: ClaimedNode,
        call: GroundingCall | PlanStepCall,
        definition: ToolDefinition,
        pin: ToolOwnershipPin,
        fields: Mapping[str, object],
        input_digest: str,
        *,
        peers: tuple[ToolExecution, ...],
        handle: StepAttempt | None = None,
        ancestor_evidence: tuple[AncestorToolEvidence, ...] = (),
        cpu_exchange: CPUWorkerExchange | None = None,
    ) -> ToolCallResult:
        claim = claimed.claim if handle is None else handle.claim
        node = claimed.node if handle is None else handle.node
        attempt = claimed.attempt if handle is None else handle.attempt
        policy = claimed.opened.scope.resolve(
            definition.execution_policy_ref, ToolExecutionPolicyArtifact
        ).policy
        started_at = self._executor.clock() if handle is None else attempt.started_at
        assert started_at is not None
        deadline = _tool_deadline(started_at, claimed.deadline, policy.timeout_ms)
        requirement = definition.implementation_requirement
        intended: DispatchIntent | None = None
        clearance_failure: str | None = None
        captured: list[ModelInvocation] = []
        nested_requests = 0
        nested_outputs = 0
        model_ref = definition.model_binding_ref

        async def invoke_nested(request: NestedModelRequest) -> NestedModelResponse:
            nonlocal nested_requests, nested_outputs
            if (
                handle is None
                or model_ref is None
                or intended is None
                or self._model_gateway is None
                or self._model_resource_scope is None
            ):
                raise AdapterRejected("tool_nested_gateway_unavailable")
            target = intended.target
            assert (
                isinstance(target, ToolDispatchTarget) and target.object_ref is not None
            )
            nested_requests += 1
            outcome = await self._model_gateway.invoke_model(
                claimed,
                ModelCall(
                    binding_ref=ModelBindingContentRef.model_validate(
                        model_ref.model_dump()
                    ),
                    purposes=(ModelCapability.VISION_AUXILIARY,),
                    profile_refs=(target.profile_ref,),
                    object_ref=target.object_ref,
                    prompt=request.prompt,
                    resource_scope_id=self._model_resource_scope(claimed, definition),
                    image=request.media,
                ),
                nested=(self._steps, handle),
            )
            invoice = outcome.invocation
            if invoice is not None:
                receipt = await self._journal.get_dispatch(
                    claim.scope, claim.execution_id, invoice.model_invocation_id
                )
                if (
                    receipt is None
                    or receipt.node_run_id != node.node_run_id
                    or receipt.attempt_id != attempt.attempt_id
                    or not isinstance(receipt.intent.target, NestedModelDispatchTarget)
                    or not any(
                        row.fence_qualified
                        and not row.late
                        and isinstance(row.result, ModelDispatchResult)
                        and row.result.invocation == invoice
                        for row in receipt.results
                    )
                ):
                    # A late physical result remains in the journal but does
                    # not become a child completion or a fallback source.
                    raise AdapterRejected("tool_nested_receipt_unqualified")
                captured.append(invoice)
            if outcome.output is not None:
                nested_outputs += 1
            return NestedModelResponse(
                invocation=invoice, content=outcome.output, error=outcome.error
            )

        def qualify_tool_candidate(result: ToolCallResult) -> ToolCallResult:
            actual = tuple(captured)
            proposed = result.nested_invocations
            error_code = clearance_failure
            if proposed and (
                len(set(proposed)) != len(proposed) or not set(proposed) <= set(actual)
            ):
                error_code = "tool_nested_invocation_unaudited"
            if (
                result.execution.status in ("succeeded", "partial")
                and model_ref is not None
                and (
                    not actual
                    or nested_outputs != nested_requests
                    or set(actual) != set(proposed)
                )
            ):
                error_code = error_code or "tool_nested_output_unqualified"
            if error_code is not None:
                tool = result.execution
                undispatched = isinstance(tool.attempt_result, UndispatchedToolAttempt)
                error = ToolError(
                    error_class="association", code=error_code, diagnostic_refs=()
                )
                record = tool.model_dump(exclude={"authority_observation"})
                record.update(
                    status="not_dispatched" if undispatched else "failed",
                    attempt_result=UndispatchedToolAttempt(
                        disposition="not_dispatched", error=error
                    )
                    if undispatched
                    else FailedToolAttempt(disposition="failed", error=error),
                    output_refs=(),
                    generated_fact_refs=(),
                )
                tool = ToolExecution.model_validate(record)
                return replace(
                    result,
                    execution=tool,
                    facts=(),
                    nested_invocations=actual,
                    retryable=False,
                )
            return replace(result, nested_invocations=actual)

        guard = (
            None
            if handle is None
            else partial(self._steps.dispatch_live, handle=handle)
        )

        def intent_for(
            tool_id: str,
            target: ToolDispatchTarget,
            request_digest: str,
            lease: DispatchLease | None,
        ) -> DispatchIntent:
            return DispatchIntent(
                dispatch_id=tool_id,
                operation_id=(
                    tool_step_operation_id(
                        handle.parent_claim.node_run_id, call.step_id
                    )
                    if handle is not None and isinstance(call, PlanStepCall)
                    else f"operation.{tool_id}"
                ),
                trace_id=node.trace.trace_id,
                request_digest=request_digest,
                input_digest=input_digest,
                response_contract_digest=definition.output_schema_ref.digest,
                resource_lease=lease,
                target=target,
            )

        async def before_dispatch(dispatch: ToolDispatch, lease: ResourceLease) -> bool:
            nonlocal intended, clearance_failure
            if not await self._available(claimed, definition, pin):
                clearance_failure = "tool_live_registration_revoked"
                return False
            logical_digest = sha256_digest(dispatch.request)
            if handle is not None and logical_digest != input_digest:
                raise AdapterRejected("tool_prepared_input_digest_changed")
            candidate = intent_for(
                dispatch.tool_execution_id,
                ToolDispatchTarget(
                    tool_ref=dispatch.tool_ref,
                    invocation_origin=dispatch.origin,
                    profile_ref=dispatch.profile_ref,
                    object_ref=dispatch.object_ref,
                    capability_id=dispatch.capability_id,
                    contract_version=dispatch.contract_version,
                    implementation_version=dispatch.implementation_version,
                    implementation_proof_ref=dispatch.implementation_proof_ref,
                    input_refs=dispatch.input_refs,
                    input_schema_ref=dispatch.input_schema_ref,
                    output_schema_ref=dispatch.output_schema_ref,
                    parent_node_run_id=None
                    if handle is None
                    else handle.parent_claim.node_run_id,
                    parent_attempt_id=None
                    if handle is None
                    else handle.parent_attempt_id,
                ),
                sha256_digest(
                    dispatch.request
                    if dispatch.physical_request is None
                    else dispatch.physical_request
                ),
                DispatchLease(
                    lease_id=lease.lease_id,
                    owner_id=lease.owner_id,
                    scope_id=lease.scope_id,
                ),
            )
            admission = create_task(
                self._journal.begin(
                    claim,
                    attempt.attempt_id,
                    candidate,
                    live_guard=guard,
                )
            )
            try:
                admitted = await shield(admission)
            except CancelledError:
                if await shield(admission):
                    intended = candidate
                raise
            if not admitted:
                return False
            intended = candidate
            if not await self._available(claimed, definition, pin):
                clearance_failure = "tool_live_registration_revoked"
                return False
            if not await self._journal.is_dispatch_current(
                claim, attempt.attempt_id, candidate
            ):
                clearance_failure = "tool_dispatch_fence_lost"
                return False
            return True

        async def retain(result: ToolCallResult) -> None:
            execution = result.execution
            error = _tool_error(execution)
            boundary: Literal["not_dispatched", "acknowledged", "sent_unknown"] = (
                "not_dispatched"
                if isinstance(execution.attempt_result, UndispatchedToolAttempt)
                else "acknowledged"
                if result.response_digest is not None
                else "sent_unknown"
            )
            record = ToolDispatchResult(
                boundary=boundary,
                acknowledgement="tool_process" if boundary == "acknowledged" else None,
                error=error,
                response_digest=result.response_digest,
                observed_response_bytes=result.response_byte_count or 0,
                execution=execution,
                implementation=None
                if execution.actual_implementation_version is None
                else ToolImplementationObservation(
                    implementation_version=execution.actual_implementation_version,
                    proof_ref=pin.implementation_proof_ref,
                ),
                tool_execution_digest=record_digest(execution),
            )
            if intended is not None:
                await self._journal.append_result(
                    claim, attempt.attempt_id, intended.dispatch_id, record
                )
                return
            if boundary != "not_dispatched":
                raise AdapterRejected("tool_missing_dispatch_intent")
            target = ToolDispatchTarget(
                tool_ref=execution.tool_ref,
                invocation_origin=execution.invocation_origin,
                profile_ref=execution.profile_ref,
                object_ref=execution.object_ref,
                capability_id=requirement.capability_id,
                contract_version=requirement.contract_version,
                implementation_version=pin.implementation_version,
                implementation_proof_ref=pin.implementation_proof_ref,
                input_refs=execution.input_refs,
                input_schema_ref=definition.input_schema_ref,
                output_schema_ref=definition.output_schema_ref,
                parent_node_run_id=None
                if handle is None
                else handle.parent_claim.node_run_id,
                parent_attempt_id=None if handle is None else handle.parent_attempt_id,
            )
            await self._journal.reject(
                claim,
                attempt.attempt_id,
                intent_for(
                    execution.tool_execution_id,
                    target,
                    result.request_digest or sha256_digest(b""),
                    None,
                ),
                record,
                live_guard=guard,
            )

        try:
            result = await self._executor.execute(
                resolved=claimed.opened.resolved,
                execution=claimed.execution,
                node=node,
                attempt=attempt,
                call=call,
                media=claimed.opened.source.media,
                input_fields=fields,
                implementation_proof=pin.implementation_proof_ref,
                deadline=deadline,
                before_dispatch=before_dispatch,
                peers=peers,
                record_response=self._record_response,
                invoke_nested_model=invoke_nested if model_ref is not None else None,
                ancestor_evidence=ancestor_evidence,
                cpu_exchange=cpu_exchange,
            )
        except ToolCancelled as exc:
            await retain(qualify_tool_candidate(exc.result))
            original = exc.__cause__
            if isinstance(original, CancelledError):
                raise original from None
            raise CancelledError from None
        result = qualify_tool_candidate(result)
        await retain(result)
        return result


def retry_tool_step(step: StepCompletion) -> None:
    """Map a failed step for the parent scheduler, keeping its child evidence."""
    error = _tool_error(step.result.execution)
    if error is None:
        return
    if error.error_class in ("transport", "unavailable"):
        raise RetryableNodeFailure("transport")
    if error.error_class == "timeout":
        raise RetryableNodeFailure("timeout")
    if error.error_class in ("rate_limit", "resource_exhausted"):
        raise RetryableNodeFailure("resource")
    raise AdapterRejected(error.code)
