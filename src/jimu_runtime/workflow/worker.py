"""Public durable worker entry point for the six-node workflow."""

from asyncio import Event, TaskGroup, to_thread, wait_for
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum
from typing import Literal

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from jimu_runtime.bundle.archive import BundleLimits
from jimu_runtime.bundle.releases import ReleaseManager
from jimu_runtime.bundle.store import LocalBundleStore, StoreBusy
from jimu_runtime.domain.base import ContractModel
from jimu_runtime.domain.enums import ExecutionStatus, NodeRunStatus
from jimu_runtime.domain.nodes.context import SuccessfulContextBuild
from jimu_runtime.domain.nodes.envelopes import (
    FailedNodeOutput,
    NodeError,
    SucceededNodeOutput,
)
from jimu_runtime.domain.runtime.applicability import UnresolvedObligation
from jimu_runtime.domain.runtime.evidence import EvidenceAsset
from jimu_runtime.domain.runtime.executions import (
    Attempt,
    ExecutionRecord,
    NodeId,
    NodeRun,
    PinnedRuntimeInput,
)
from jimu_runtime.domain.runtime.intake import IdempotencyScope
from jimu_runtime.domain.runtime.invocations import ModelInvocation
from jimu_runtime.domain.validation.coverage_sources import CoverageAuthorityInput
from jimu_runtime.replay.inference_repository import read_inference_replay_for_target
from jimu_runtime.tools.step_attempts import load_parent_step_history
from jimu_runtime.workflow import repository as repo
from jimu_runtime.workflow.engine import BranchKey, ExecutionFinalization
from jimu_runtime.workflow.intake import IntakeAuthorizationError, IntakeHostUnavailable
from jimu_runtime.workflow.jobs import (
    CompletionDisposition,
    DurableJobs,
    JobClaim,
    ResponseEvidence,
    StagedDecision,
)
from jimu_runtime.workflow.mock_support import VERSION, K, artifact, synthetic_id
from jimu_runtime.workflow.pins import ExecutionPinError
from jimu_runtime.workflow.scheduling import validated_milliseconds
from jimu_runtime.workflow.worker_capabilities import read_historical_capabilities
from jimu_runtime.workflow.worker_completion import (
    finalize,
    persist_result,
    runtime_authority_records,
)
from jimu_runtime.workflow.worker_coverage import build_coverage_authority
from jimu_runtime.workflow.worker_nodes import (
    BranchInputs,
    NodeResult,
    failed_discovery,
    invoke,
)
from jimu_runtime.workflow.worker_poll import runnable_executions
from jimu_runtime.workflow.worker_records import (
    DecisionMessage,
    DiscoveryMessage,
    GroundingMessage,
    ObservationMessage,
    ToolMessage,
)
from jimu_runtime.workflow.worker_schedule import (
    ProfileGate,
    check_configured_gate,
    schedule_after_terminal,
)
from jimu_runtime.workflow.worker_source import (
    OpenExecution,
    WorkerHost,
    open_execution,
)
from jimu_runtime.workflow.worker_state import (
    load_attempts,
    load_records,
    read_context_message,
    read_observation_for_tools,
    read_observation_retry,
    read_replay_capture_message,
)
from jimu_runtime.workflow.worker_validation import check_result

_ACTIVE = frozenset((ExecutionStatus.INITIALIZED, ExecutionStatus.RUNNING))
type NodeInvoker = Callable[
    [OpenExecution, BranchInputs, BranchKey, NodeRun, Attempt], Awaitable[NodeResult]
]

type AttemptCheckpoint = Callable[[JobClaim, Attempt], Awaitable[None]]


@dataclass(frozen=True, slots=True)
class ClaimedNode:
    """An actually started NodeRun and its inputs, available outside the locks."""

    claim: JobClaim
    attempt: Attempt
    node: NodeRun
    branch: BranchKey
    opened: OpenExecution
    previous: BranchInputs
    execution: ExecutionRecord
    deadline: datetime
    authority: CoverageAuthorityInput | None = None
    evidence: tuple[EvidenceAsset, ...] = ()
    evidence_availability: dict[EvidenceAsset, bool] = field(
        default_factory=dict, compare=False, repr=False
    )
    await_observation: Callable[[], Awaitable[ObservationMessage | None]] | None = (
        field(default=None, compare=False, repr=False)
    )


type ClaimedNodeInvoker = Callable[[ClaimedNode], Awaitable[NodeResult]]


@dataclass(frozen=True, slots=True)
class WorkflowAdapters:
    """Host-composed real node ports and independent validation of their durable output."""

    invoke: ClaimedNodeInvoker
    profile_gate: ProfileGate
    validate_observation: Callable[[ClaimedNode, ObservationMessage], Awaitable[None]]
    validate_decision: Callable[[ClaimedNode, DecisionMessage], Awaitable[None]]
    verify_invocation: Callable[[ClaimedNode, ModelInvocation], Awaitable[None]]
    verify_late_invocation: (
        Callable[[ClaimedNode, ModelInvocation], Awaitable[None]] | None
    ) = None
    validate_perception: (
        Callable[[ClaimedNode, DiscoveryMessage | GroundingMessage], Awaitable[None]]
        | None
    ) = None
    decision_unresolved: (
        Callable[[DecisionMessage], tuple[UnresolvedObligation, ...]] | None
    ) = None


class WorkerStep(Enum):
    IDLE = "idle"
    APPLIED = "applied"
    STAGED = "staged"
    RETRY_SCHEDULED = "retry_scheduled"
    DUPLICATE = "duplicate"
    LATE_RECORDED = "late_recorded"
    LEASE_LOST = "lease_lost"
    FINALIZED = "finalized"
    CANCELLED = "cancelled"
    WAITING_FOR_OBSERVATION = "waiting_for_observation"


class _ObservationWorkYield(Exception):
    def __init__(self, step: WorkerStep) -> None:
        self.step = step
        super().__init__(step.value)


class RetryableNodeFailure(RuntimeError):
    """A trusted adapter reported a known transient invocation failure."""

    def __init__(
        self, error_class: Literal["transport", "timeout", "resource"]
    ) -> None:
        self.error_class = error_class
        super().__init__(error_class)


class AdapterRejected(ValueError):
    """Trusted, stable diagnostic for an operation that cannot publish output."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def _failed_node(node: NodeRun, attempt: Attempt, error: Exception) -> FailedNodeOutput:
    error_class: Literal["transport", "timeout", "resource", "validation", "internal"]
    if isinstance(error, RetryableNodeFailure):
        error_class = error.error_class
    elif isinstance(error, TimeoutError):
        error_class = "timeout"
    elif isinstance(error, ConnectionError):
        error_class = "transport"
    elif isinstance(error, ValueError):
        error_class = "validation"
    else:
        error_class = "internal"
    code = (
        error.code
        if isinstance(error, AdapterRejected)
        else "invalid_output"
        if error_class == "validation"
        else error_class
    )
    assert attempt.started_at is not None
    return FailedNodeOutput(
        schema_version=VERSION,
        node_id=node.node_id,
        node_run_id=node.node_run_id,
        attempt_id=attempt.attempt_id,
        trace=node.trace,
        output_artifacts=(),
        started_at=attempt.started_at,
        finished_at=attempt.started_at,
        duration_ms=0,
        status=NodeRunStatus.FAILED,
        error=NodeError(
            code=f"worker_provider_{code}",
            error_class=error_class,
            retryable=error_class in {"transport", "timeout", "resource"},
            message="Worker node adapter failed before producing a validated result.",
            validation_issues=(),
        ),
    )


class WorkerSourceError(ValueError):
    """A scoped Execution's historical inputs could not be opened."""

    def __init__(self, code: str, *, retryable: bool) -> None:
        self.code = code
        self.retryable = retryable
        super().__init__(code)


@dataclass(frozen=True, slots=True)
class PollIssue:
    execution_id: str
    code: str
    retryable: bool


@dataclass(frozen=True, slots=True)
class WorkerPoll:
    step: WorkerStep
    execution_id: str | None
    after_execution_id: str | None
    issues: tuple[PollIssue, ...]


def _response(
    result: NodeResult,
    *,
    configured: bool = False,
) -> tuple[ResponseEvidence | None, ContractModel | None]:
    if configured and isinstance(message := result.message, ObservationMessage):
        response = artifact(
            K.OBSERVATION_BATCH, message.batch.observation_batch_id, message.batch
        )
        return ResponseEvidence(
            schema_version=VERSION,
            execution_id=message.batch.execution_id,
            attempt_id=message.batch.attempt_ref.record_id,
            response_key=synthetic_id(
                "response", message.invocation.model_invocation_id
            ),
            response=response.pin,
            external_request_id=message.invocation.provider_request_id,
        ), message.batch
    if not result.artifacts:
        return None, None
    if isinstance(result.message, ToolMessage) and (
        not result.message.executions
        or any(
            execution.node_run_ref.record_id
            != result.message.inputs.envelope.node_run_id
            for execution in result.message.executions
        )
    ):
        # Physical receipts belong to the invoked child, if any.
        return None, None
    response = next(
        (item for item in result.artifacts if isinstance(item.record, ModelInvocation)),
        result.artifacts[0],
    )
    invocation = response.record
    env = result.message.input_envelope()
    return ResponseEvidence(
        schema_version=VERSION,
        execution_id=env.execution_id,
        attempt_id=env.attempt_id,
        response_key=synthetic_id(
            "response", result.message.input_envelope().attempt_id
        ),
        response=response.pin,
        external_request_id=invocation.provider_request_id
        if isinstance(invocation, ModelInvocation)
        else None,
    ), response.record


async def _synthetic_invocation(
    opened: OpenExecution,
    inputs: BranchInputs,
    key: BranchKey,
    node: NodeRun,
    attempt: Attempt,
) -> NodeResult:
    return await to_thread(invoke, opened, inputs, key, node, attempt)


async def _renew(
    jobs: DurableJobs,
    claim: JobClaim,
    lease_ms: int,
    stopped: Event,
) -> None:
    interval = lease_ms / 3_000
    while not stopped.is_set():
        try:
            await wait_for(stopped.wait(), timeout=interval)
        except TimeoutError:
            renewed = await jobs.heartbeat(claim, lease_ms)
            if renewed is None:
                return
            claim = renewed


class WorkflowWorker:
    """Run one durable action for a host-authorized Task Execution.

    Run again with the same scope and Execution ID after service restart. Each
    call opens the original admitted Bundle, and reconstructs selected inputs
    from PostgreSQL. Multiple Worker instances can serve the same Execution.
    """

    def __init__(
        self,
        engine: AsyncEngine,
        release_manager: ReleaseManager,
        bundle_store: LocalBundleStore,
        *,
        host: WorkerHost,
        bundle_limits: BundleLimits,
        invoke_node: NodeInvoker = _synthetic_invocation,
        invoke_claimed_node: ClaimedNodeInvoker | None = None,
        adapters: WorkflowAdapters | None = None,
    ) -> None:
        if (
            invoke_claimed_node is not None
            and invoke_node is not _synthetic_invocation
            or adapters is not None
            and (
                invoke_claimed_node is not None
                or invoke_node is not _synthetic_invocation
            )
        ):
            raise ValueError("choose one node adapter for this worker")
        self._engine = engine
        self._manager = release_manager
        self._store = bundle_store
        self._host = host
        self._limits = bundle_limits
        self._invoke_node = invoke_node
        self._invoke_claimed_node = (
            invoke_claimed_node if adapters is None else adapters.invoke
        )
        self._adapters = adapters

    async def request_cancel(self, scope: IdempotencyScope, execution_id: str) -> bool:
        """Persist a host-authorized cancellation for worker reconciliation."""
        return await DurableJobs(self._engine).request_cancel(
            IdempotencyScope.model_validate(scope), execution_id
        )

    async def read_finalization(
        self, scope: IdempotencyScope, execution_id: str
    ) -> ExecutionFinalization | None:
        """Read the effects of committed atomic publication in the caller's scope."""
        async with self._engine.connect() as connection:
            return await repo.read_finalization(
                connection,
                IdempotencyScope.model_validate(scope),
                execution_id,
                ExecutionFinalization,
            )

    async def pending_finalizations(
        self, scope: IdempotencyScope, *, limit: int = 32
    ) -> tuple[str, ...]:
        """Find scoped Executions ready for publication during service recovery."""
        return await DurableJobs(self._engine).pending_finalizations(
            IdempotencyScope.model_validate(scope), limit=limit
        )

    async def run_next(
        self,
        scope: IdempotencyScope,
        *,
        owner: str,
        lease_ms: int,
        after_execution_id: str | None = None,
        limit: int = 32,
    ) -> WorkerPoll:
        """Advance reachable work without a remembered Execution ID.

        Continue with the returned cursor. A null cursor starts a fresh scan.
        Unavailable historical inputs are reported while other Executions proceed.
        """
        scope = IdempotencyScope.model_validate(scope)
        validated_milliseconds(lease_ms)
        if type(limit) is not int or not 1 <= limit <= 1024:
            raise ValueError("worker scan limit must be between 1 and 1024")
        async with self._engine.connect() as connection:
            candidates = await runnable_executions(
                connection, scope, after_execution_id, limit + 1
            )
        issues: list[PollIssue] = []
        page = candidates[:limit]
        for index, execution_id in enumerate(page):
            try:
                step = await self.run_once(
                    scope, execution_id, owner=owner, lease_ms=lease_ms
                )
            except WorkerSourceError as error:
                issues.append(PollIssue(execution_id, error.code, error.retryable))
                continue
            if step is not WorkerStep.IDLE:
                cursor = execution_id if index + 1 < len(candidates) else None
                return WorkerPoll(step, execution_id, cursor, tuple(issues))
        cursor = page[-1] if len(candidates) > limit else None
        return WorkerPoll(WorkerStep.IDLE, None, cursor, tuple(issues))

    async def run_once(
        self,
        scope: IdempotencyScope,
        execution_id: str,
        *,
        owner: str,
        lease_ms: int,
        after_start: AttemptCheckpoint | None = None,
    ) -> WorkerStep:
        """Claim/invoke one branch, reconcile a cancellation, or publish Coverage."""
        scope = IdempotencyScope.model_validate(scope)
        validated_milliseconds(lease_ms)
        async with self._engine.connect() as connection:
            locked = await repo.get_execution(connection, scope, execution_id)
            at = (
                await connection.execute(text("SELECT pg_catalog.clock_timestamp()"))
            ).scalar_one()
        if locked is None or locked.execution.status not in _ACTIVE:
            return WorkerStep.IDLE
        if locked.cancel_requested_at is not None:
            changed = await DurableJobs(self._engine).reconcile(scope, execution_id)
            return WorkerStep.CANCELLED if changed else WorkerStep.IDLE

        try:
            historical = None
            historical_context = None
            capture_execution_id = None
            require_historical = (
                self._invoke_claimed_node is not None or locked.is_inference_replay
            )
            async with self._engine.connect() as connection:
                context = await read_context_message(connection, scope, locked)
                if locked.is_inference_replay:
                    replay = await read_inference_replay_for_target(
                        connection, scope, execution_id
                    )
                    if replay is None:
                        raise WorkerSourceError(
                            "worker_inference_replay_lineage_missing",
                            retryable=False,
                        )
                    capture = await read_replay_capture_message(
                        connection, scope, locked, replay
                    )
                    if context is None:
                        context = capture
                        capture_execution_id = replay.capture_execution_id
                    else:
                        assert (
                            capture.snapshot is not None
                            and context.snapshot is not None
                        )
                        if (
                            capture.snapshot.device_ref,
                            capture.snapshot.camera_ref,
                            capture.snapshot.media_ref,
                            capture.snapshot.region_ref,
                            capture.snapshot.clock,
                        ) != (
                            context.snapshot.device_ref,
                            context.snapshot.camera_ref,
                            context.snapshot.media_ref,
                            context.snapshot.region_ref,
                            context.snapshot.clock,
                        ):
                            raise WorkerSourceError(
                                "worker_inference_replay_capture_mismatch",
                                retryable=False,
                            )
                if context is not None:
                    if (
                        self._adapters is None
                        and context.worker_format == "configured-v1"
                    ):
                        raise WorkerSourceError(
                            "worker_message_requires_configured_reader",
                            retryable=False,
                        )
                    if self._adapters is not None or locked.is_inference_replay:
                        historical_context = context.snapshot
                if require_historical:
                    historical = await read_historical_capabilities(
                        connection, scope, locked.execution
                    )
                    if historical is None:
                        raise WorkerSourceError(
                            "worker_historical_capabilities_unavailable",
                            retryable=False,
                        )
            opened = await open_execution(
                locked,
                scope,
                self._manager,
                self._store,
                self._host,
                self._limits,
                at,
                historical=historical,
                require_historical=require_historical,
                historical_context=historical_context,
                capture_execution_id=capture_execution_id,
            )
        except WorkerSourceError:
            raise
        except (StoreBusy, IntakeHostUnavailable, OSError) as error:
            raise WorkerSourceError(
                "worker_source_unavailable", retryable=True
            ) from error
        except ExecutionPinError as error:
            retryable = error.code in {
                "tool_implementation_unavailable",
                "workflow_version_unavailable",
            }
            raise WorkerSourceError(error.code, retryable=retryable) from error
        except (IntakeAuthorizationError, ValueError) as error:
            raise WorkerSourceError("worker_source_invalid", retryable=False) from error

        async def on_terminal(
            connection: AsyncConnection,
            job_scope: IdempotencyScope,
            state: repo.LockedExecution,
            node: NodeRun,
            attempt: Attempt,
            outcome: SucceededNodeOutput | FailedNodeOutput | None,
            now: datetime,
        ) -> None:
            await schedule_after_terminal(
                connection,
                job_scope,
                opened,
                state.execution.execution_id,
                now,
                profile_gate=None
                if self._adapters is None
                else self._adapters.profile_gate,
            )

        async def publish(
            connection: AsyncConnection,
            job_scope: IdempotencyScope,
            state: repo.LockedExecution,
            drafts: tuple[StagedDecision, ...],
            now: datetime,
        ) -> bool:
            return await finalize(
                connection,
                job_scope,
                state,
                drafts,
                now,
                opened,
                decision_unresolved=None
                if self._adapters is None
                else self._adapters.decision_unresolved,
            )

        jobs = DurableJobs(self._engine, on_terminal=on_terminal)
        claim = await jobs.claim(scope, owner, lease_ms, execution_id=execution_id)
        if claim is None:
            if await jobs.finalize_staged(scope, execution_id, publish):
                return WorkerStep.FINALIZED
            return WorkerStep.IDLE
        return await self._execute_claim(
            jobs, opened, claim, lease_ms, after_start=after_start
        )

    async def _await_observation(
        self,
        jobs: DurableJobs,
        claim: JobClaim,
        previous: BranchInputs,
        object_pin: PinnedRuntimeInput,
        observation_node_id: str,
    ) -> ObservationMessage | None:
        async def read() -> tuple[bool, ObservationMessage | None]:
            async with self._engine.begin() as connection:
                await connection.execute(
                    text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
                )
                current = await repo.get_execution(
                    connection, claim.scope, claim.execution_id
                )
                if (
                    current is None
                    or current.cancel_requested_at is not None
                    or current.execution.status not in _ACTIVE
                ):
                    raise _ObservationWorkYield(WorkerStep.LEASE_LOST)
                return await read_observation_for_tools(
                    connection, claim.scope, current, object_pin, observation_node_id
                )

        settled, observation = await read()
        if not settled:
            parked = await jobs.defer_tool_observation(
                claim, observation_node_id, object_pin
            )
            if parked is None:
                raise _ObservationWorkYield(WorkerStep.LEASE_LOST)
            if parked:
                raise _ObservationWorkYield(WorkerStep.WAITING_FOR_OBSERVATION)
            # N04 settled between the read and the fenced defer. Its terminal
            # result and selected authority are now immutable.
            settled, observation = await read()
            if not settled:
                raise AdapterRejected("worker_observation_dependency_unresolved")
        previous.observation = observation
        return observation

    async def _execute_claim(
        self,
        jobs: DurableJobs,
        opened: OpenExecution,
        claim: JobClaim,
        lease_ms: int,
        *,
        after_start: AttemptCheckpoint | None = None,
    ) -> WorkerStep:
        scope, execution_id = claim.scope, claim.execution_id
        attempt = await jobs.start(claim)
        if attempt is None:
            return WorkerStep.LEASE_LOST
        stopped = Event()
        async with TaskGroup() as tasks:
            tasks.create_task(_renew(jobs, claim, lease_ms, stopped))
            try:
                if after_start is not None:
                    await after_start(claim, attempt)
                async with self._engine.begin() as connection:
                    # Read one consistent snapshot before any adapter work.
                    await connection.execute(
                        text(
                            "SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY"
                        )
                    )
                    current = await repo.get_execution(connection, scope, execution_id)
                    if current is None or current.execution.status not in _ACTIVE:
                        return WorkerStep.LEASE_LOST
                    recovery_node = await repo.get_node(
                        connection,
                        scope,
                        execution_id,
                        claim.node_run_id,
                    )
                    if recovery_node is None or not attempt.belongs_to_node(
                        recovery_node
                    ):
                        return WorkerStep.LEASE_LOST
                    recovery_failure: FailedNodeOutput | None
                    try:
                        data = await load_records(connection, scope, current)
                        (branch,) = tuple(
                            key
                            for key, node in data.runs.items()
                            if node.node_run_id == claim.node_run_id
                        )
                        node = data.runs[branch]
                        previous = data.inputs(branch.object_id)
                        authority = None
                        evidence: tuple[EvidenceAsset, ...] = ()
                        if self._adapters is not None and node.node_id is NodeId.N04:
                            previous.observation_retry = await read_observation_retry(
                                connection, scope, node, attempt
                            )
                        if self._adapters is not None and node.node_id is NodeId.N06:
                            records, _ = runtime_authority_records(
                                opened,
                                data,
                                await load_attempts(connection, scope, data),
                                object_id=branch.object_id,
                            )
                            authority = build_coverage_authority(
                                opened.resolved, records
                            )
                            evidence = previous.selected_evidence(branch.object_id)
                    except ValueError:
                        recovery_failure = _failed_node(
                            recovery_node,
                            attempt,
                            AdapterRejected("worker_recovered_inputs_invalid"),
                        )
                    else:
                        recovery_failure = None
                if recovery_failure is not None:
                    disposition = await jobs.complete(claim, recovery_failure)
                    return WorkerStep(disposition.value)
                assert attempt.started_at is not None
                attempt_deadline = attempt.started_at + timedelta(
                    milliseconds=data.locked.scheduling_limits.for_node(
                        node.node_id
                    ).timeout_ms
                )
                observation_node = data.runs.get(
                    BranchKey(NodeId.N04, branch.object_id)
                )
                observation_reader = None
                if (
                    self._adapters is not None
                    and branch.node_id is NodeId.N05
                    and branch.object_id is not None
                    and observation_node is not None
                ):
                    object_id = branch.object_id
                    observation_node_id = observation_node.node_run_id

                    async def await_observation() -> ObservationMessage | None:
                        obj = previous.object_record(branch)
                        object_artifact = artifact(K.OBJECT, object_id, obj).pin
                        (object_pin,) = tuple(
                            pin
                            for pin in node.upstream_inputs
                            if pin.stable_json() == object_artifact.stable_json()
                        )
                        return await self._await_observation(
                            jobs, claim, previous, object_pin, observation_node_id
                        )

                    observation_reader = await_observation
                result: NodeResult | None
                try:
                    claimed = ClaimedNode(
                        claim,
                        attempt,
                        node,
                        branch,
                        opened,
                        previous,
                        data.locked.execution,
                        attempt_deadline,
                        authority,
                        evidence,
                        await_observation=observation_reader,
                    )
                    if self._adapters is not None and previous.gate is not None:
                        context = previous.context
                        if context is None or not isinstance(
                            context.output, SuccessfulContextBuild
                        ):
                            raise AdapterRejected("worker_gate_context_unavailable")
                        check_configured_gate(opened, context.output, previous.gate)
                        if previous.gate != self._adapters.profile_gate(
                            opened, context.inputs, context.output, previous.snapshot()
                        ):
                            raise AdapterRejected("worker_gate_revalidation_failed")
                    candidate = (
                        await self._invoke_node(opened, previous, branch, node, attempt)
                        if self._invoke_claimed_node is None
                        else await self._invoke_claimed_node(claimed)
                    )
                    history = None
                    if isinstance(candidate.message, ToolMessage):
                        async with self._engine.connect() as connection:
                            history = await load_parent_step_history(
                                connection, claim.scope, candidate.message.inputs
                            )
                    result = check_result(
                        opened,
                        previous,
                        branch,
                        node,
                        attempt,
                        candidate,
                        tool_step_history=history,
                        configured=self._adapters is not None,
                    )
                    if self._adapters is not None:
                        late = (
                            isinstance(result.message, ObservationMessage)
                            and result.message.batch.arrival_qualification == "late"
                        )
                        verify = (
                            self._adapters.verify_late_invocation
                            if late
                            else self._adapters.verify_invocation
                        )
                        if verify is None:
                            raise AdapterRejected("worker_late_invocation_unqualified")
                        for item in result.artifacts:
                            invoice = item.record
                            if (
                                isinstance(invoice, ModelInvocation)
                                and invoice.node_run_ref.record_id == node.node_run_id
                            ):
                                await verify(claimed, invoice)
                        if isinstance(result.message, ObservationMessage):
                            await self._adapters.validate_observation(
                                claimed, result.message
                            )
                        elif isinstance(result.message, DecisionMessage):
                            await self._adapters.validate_decision(
                                claimed, result.message
                            )
                        elif isinstance(
                            result.message, DiscoveryMessage | GroundingMessage
                        ):
                            if self._adapters.validate_perception is not None:
                                await self._adapters.validate_perception(
                                    claimed, result.message
                                )
                            elif (
                                isinstance(result.message, DiscoveryMessage)
                                and result.message.group_failures
                                or isinstance(result.message, GroundingMessage)
                                and result.message.undispatched_profiles
                            ):
                                raise AdapterRejected(
                                    "worker_model_group_diagnostics_unverified"
                                )
                    failure = None
                except _ObservationWorkYield as deferred:
                    return deferred.step
                except Exception as error:
                    failure = _failed_node(node, attempt, error)
                    result = (
                        failed_discovery(
                            opened, previous, branch, node, attempt, failure
                        )
                        if node.node_id is NodeId.N02
                        else None
                    )
            finally:
                stopped.set()
        if result is not None:
            disposition = await self._commit(
                jobs, claim, result, configured=self._adapters is not None
            )
        else:
            assert failure is not None
            disposition = await jobs.complete(claim, failure)
        return WorkerStep(disposition.value)

    @staticmethod
    async def _commit(
        jobs: DurableJobs, claim: JobClaim, result: NodeResult, *, configured: bool
    ) -> CompletionDisposition:
        async def persist(
            connection: AsyncConnection,
            scope: IdempotencyScope,
            locked: repo.LockedExecution,
            node: NodeRun,
            attempt: Attempt,
            outcome: SucceededNodeOutput | FailedNodeOutput,
            now: datetime,
        ) -> None:
            await persist_result(
                connection,
                scope,
                locked,
                node,
                attempt,
                outcome,
                now,
                result,
                configured=configured,
            )

        response, response_record = _response(result, configured=configured)
        if (
            configured
            and isinstance(result.message, ObservationMessage)
            and result.message.batch.arrival_qualification == "late"
        ):
            assert response is not None and response_record is not None
            return await jobs.record_late(
                claim, response, response_record=response_record
            )
        outcome = result.message.output_envelope()
        if isinstance(result.message, DecisionMessage):
            if not isinstance(outcome, SucceededNodeOutput):
                raise ValueError("staged decisions require a successful node envelope")
            return await jobs.stage_decision(
                claim,
                outcome,
                result.message.draft,
                response=response,
                response_record=response_record,
                persist=persist,
            )
        if not isinstance(outcome, (SucceededNodeOutput, FailedNodeOutput)):
            raise ValueError("offline nodes produce a success or failure envelope")
        return await jobs.complete(
            claim,
            outcome,
            response=response,
            response_record=response_record,
            persist=persist,
        )
