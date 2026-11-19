"""Pure validation of lifecycle proposals and append-only audit data."""

from enum import unique
from types import MappingProxyType
from typing import TypeVar

from jimu_runtime.domain.enums import (
    AttemptStatus,
    ExecutionStatus,
    NodeRunStatus,
    TaskStatus,
    WireEnum,
)
from jimu_runtime.domain.runtime.executions import (
    Attempt,
    AttemptTransitionEvent,
    ExecutionEvent,
    ExecutionRecord,
    ExecutionTransitionEvent,
    LateResponseEvent,
    NodeRun,
    NodeTransitionEvent,
    SystemOutputEvent,
    TaskTransitionEvent,
)
from jimu_runtime.domain.validation.authority_inputs import SystemAttemptOwner

_TASK_EDGES = frozenset(
    {
        (TaskStatus.CREATED, TaskStatus.ACCEPTED),
        (TaskStatus.ACCEPTED, TaskStatus.QUEUED),
        (TaskStatus.QUEUED, TaskStatus.RUNNING),
        (TaskStatus.RUNNING, TaskStatus.COMPLETED),
        (TaskStatus.RUNNING, TaskStatus.PARTIAL_FAILED),
        (TaskStatus.RUNNING, TaskStatus.FAILED),
        (TaskStatus.RUNNING, TaskStatus.CANCELLED),
    }
)

_EXECUTION_EDGES = frozenset(
    {
        (ExecutionStatus.INITIALIZED, ExecutionStatus.RUNNING),
        (ExecutionStatus.RUNNING, ExecutionStatus.COMPLETED),
        (ExecutionStatus.RUNNING, ExecutionStatus.PARTIAL_FAILED),
        (ExecutionStatus.RUNNING, ExecutionStatus.FAILED),
        (ExecutionStatus.RUNNING, ExecutionStatus.CANCELLED),
    }
)


@unique
class NodeTransitionGuard(WireEnum):
    PREREQUISITES_AVAILABLE = "prerequisites_available"
    PREREQUISITE_UNAVAILABLE = "prerequisite_unavailable"
    DETERMINISTIC_SKIP = "deterministic_skip"
    PRE_START_CANCELLATION = "pre_start_cancellation"
    FIRST_ATTEMPT_STARTED = "first_attempt_started"
    AUTHORITATIVE_OUTPUT_VALIDATED = "authoritative_output_validated"
    NONRETRYABLE_FAILURE = "nonretryable_failure"
    RETRY_BUDGET_EXHAUSTED = "retry_budget_exhausted"
    FENCED_CANCELLATION = "fenced_cancellation"


@unique
class AttemptTransitionGuard(WireEnum):
    CLAIM_VALID_AND_INVOCATION_STARTED = "claim_valid_and_invocation_started"
    PRE_START_CANCELLATION = "pre_start_cancellation"
    CLAIM_REPLACED = "claim_replaced"
    RESPONSE_VALIDATED_WITH_COMMIT_RIGHTS = "response_validated_with_commit_rights"
    NORMALIZED_FAILURE = "normalized_failure"
    DEADLINE_ELAPSED = "deadline_elapsed"
    FENCED_CANCELLATION = "fenced_cancellation"
    AUTHORITATIVE_FENCE_LOST = "authoritative_fence_lost"


_NODE_GUARDS = MappingProxyType(
    {
        (NodeRunStatus.PENDING, NodeRunStatus.READY): (
            NodeTransitionGuard.PREREQUISITES_AVAILABLE,
        ),
        (NodeRunStatus.PENDING, NodeRunStatus.BLOCKED): (
            NodeTransitionGuard.PREREQUISITE_UNAVAILABLE,
        ),
        (NodeRunStatus.READY, NodeRunStatus.BLOCKED): (
            NodeTransitionGuard.PREREQUISITE_UNAVAILABLE,
        ),
        (NodeRunStatus.PENDING, NodeRunStatus.SKIPPED): (
            NodeTransitionGuard.DETERMINISTIC_SKIP,
        ),
        (NodeRunStatus.READY, NodeRunStatus.SKIPPED): (
            NodeTransitionGuard.DETERMINISTIC_SKIP,
        ),
        (NodeRunStatus.PENDING, NodeRunStatus.CANCELLED): (
            NodeTransitionGuard.PRE_START_CANCELLATION,
        ),
        (NodeRunStatus.READY, NodeRunStatus.CANCELLED): (
            NodeTransitionGuard.PRE_START_CANCELLATION,
        ),
        (NodeRunStatus.READY, NodeRunStatus.RUNNING): (
            NodeTransitionGuard.FIRST_ATTEMPT_STARTED,
        ),
        (NodeRunStatus.RUNNING, NodeRunStatus.SUCCEEDED): (
            NodeTransitionGuard.AUTHORITATIVE_OUTPUT_VALIDATED,
        ),
        (NodeRunStatus.RUNNING, NodeRunStatus.FAILED): (
            NodeTransitionGuard.NONRETRYABLE_FAILURE,
            NodeTransitionGuard.RETRY_BUDGET_EXHAUSTED,
        ),
        (NodeRunStatus.RUNNING, NodeRunStatus.CANCELLED): (
            NodeTransitionGuard.FENCED_CANCELLATION,
        ),
    }
)

_ATTEMPT_GUARDS = MappingProxyType(
    {
        (AttemptStatus.CREATED, AttemptStatus.RUNNING): (
            AttemptTransitionGuard.CLAIM_VALID_AND_INVOCATION_STARTED
        ),
        (AttemptStatus.CREATED, AttemptStatus.CANCELLED): (
            AttemptTransitionGuard.PRE_START_CANCELLATION
        ),
        (AttemptStatus.CREATED, AttemptStatus.SUPERSEDED): (
            AttemptTransitionGuard.CLAIM_REPLACED
        ),
        (AttemptStatus.RUNNING, AttemptStatus.SUCCEEDED): (
            AttemptTransitionGuard.RESPONSE_VALIDATED_WITH_COMMIT_RIGHTS
        ),
        (AttemptStatus.RUNNING, AttemptStatus.FAILED): (
            AttemptTransitionGuard.NORMALIZED_FAILURE
        ),
        (AttemptStatus.RUNNING, AttemptStatus.TIMED_OUT): (
            AttemptTransitionGuard.DEADLINE_ELAPSED
        ),
        (AttemptStatus.RUNNING, AttemptStatus.CANCELLED): (
            AttemptTransitionGuard.FENCED_CANCELLATION
        ),
        (AttemptStatus.RUNNING, AttemptStatus.SUPERSEDED): (
            AttemptTransitionGuard.AUTHORITATIVE_FENCE_LOST
        ),
    }
)

_TERMINAL_ATTEMPTS = frozenset(AttemptStatus) - {
    AttemptStatus.CREATED,
    AttemptStatus.RUNNING,
}
_TERMINAL_NODES = frozenset(NodeRunStatus) - {
    NodeRunStatus.PENDING,
    NodeRunStatus.READY,
    NodeRunStatus.RUNNING,
}
_RETRYABLE_ATTEMPT_STATES = frozenset(
    {AttemptStatus.FAILED, AttemptStatus.TIMED_OUT, AttemptStatus.SUPERSEDED}
)


def validate_task_transition(previous: TaskStatus, following: TaskStatus) -> None:
    if (previous, following) not in _TASK_EDGES:
        raise ValueError("illegal Task transition")


def validate_execution_transition(
    previous: ExecutionStatus, following: ExecutionStatus
) -> None:
    if (previous, following) not in _EXECUTION_EDGES:
        raise ValueError("illegal Execution transition")


def validate_node_transition(
    previous: NodeRunStatus, following: NodeRunStatus, guard: NodeTransitionGuard
) -> None:
    required = _NODE_GUARDS.get((previous, following))
    if required is None:
        raise ValueError("illegal NodeRun transition")
    if guard not in required:
        raise ValueError("NodeRun transition requires the matching guard")


def validate_attempt_transition(
    previous: AttemptStatus, following: AttemptStatus, guard: AttemptTransitionGuard
) -> None:
    required = _ATTEMPT_GUARDS.get((previous, following))
    if required is None:
        raise ValueError("illegal Attempt transition")
    if guard is not required:
        raise ValueError("Attempt transition requires the matching guard")


_Item = TypeVar("_Item")


def _appended(
    previous: tuple[_Item, ...], following: tuple[_Item, ...]
) -> tuple[_Item, ...]:
    if following[: len(previous)] != previous:
        raise ValueError("audit history must preserve the complete prior prefix")
    return following[len(previous) :]


def validate_execution_update(
    previous: ExecutionRecord, following: ExecutionRecord
) -> None:
    """Validate two snapshots; invocation refs are audit, without result authority."""
    if (
        previous.execution_id,
        previous.task_id,
        previous.pins,
        previous.trace,
        previous.created_at,
    ) != (
        following.execution_id,
        following.task_id,
        following.pins,
        following.trace,
        following.created_at,
    ):
        raise ValueError("Execution identity and creation pins are immutable")
    if previous.status is not following.status:
        validate_execution_transition(previous.status, following.status)
    if (
        previous.started_at is not None and previous.started_at != following.started_at
    ) or (previous.ended_at is not None and previous.ended_at != following.ended_at):
        raise ValueError("existing Execution timestamps are immutable")
    if previous.context_snapshot != following.context_snapshot and (
        previous.context_snapshot is not None
        or previous.status is not ExecutionStatus.RUNNING
    ):
        raise ValueError("context can be attached once before Execution terminates")
    _appended(previous.model_invocation_refs, following.model_invocation_refs)


def validate_node_update(
    previous: NodeRun,
    following: NodeRun,
    *,
    guard: NodeTransitionGuard | None = None,
    appended_attempt: Attempt | None = None,
    prior_attempt: Attempt | None = None,
    retry_authorized: bool = False,
) -> None:
    """Check immutable inputs, guarded state edges and one new Attempt at a time."""
    if type(retry_authorized) is not bool:
        raise ValueError("retry authorization must be an explicit boolean")
    if (
        previous.node_run_id,
        previous.execution_id,
        previous.node_id,
        previous.trace,
        previous.config_ref,
        previous.upstream_inputs,
        previous.input_digest,
        previous.created_at,
        previous.predecessor_ref,
    ) != (
        following.node_run_id,
        following.execution_id,
        following.node_id,
        following.trace,
        following.config_ref,
        following.upstream_inputs,
        following.input_digest,
        following.created_at,
        following.predecessor_ref,
    ):
        raise ValueError("NodeRun identity, inputs and config are immutable")
    if previous.status is not following.status:
        if guard is None:
            raise ValueError("NodeRun transition requires a guard")
        validate_node_transition(previous.status, following.status, guard)
    elif guard is not None:
        raise ValueError("NodeRun guard requires a state transition")
    if previous.status in _TERMINAL_NODES and previous != following:
        raise ValueError("terminal NodeRun is immutable")
    if previous.started_at is not None and previous.started_at != following.started_at:
        raise ValueError("existing NodeRun start is immutable")
    added = _appended(previous.attempt_history, following.attempt_history)
    _appended(previous.output_refs, following.output_refs)
    if (
        not added
        and appended_attempt is None
        and prior_attempt is None
        and not retry_authorized
    ):
        return
    if (
        len(added) != 1
        or appended_attempt is None
        or not appended_attempt.belongs_to_node(following)
        or added[0].attempt_ref.record_id != appended_attempt.attempt_id
        or added[0].attempt_number != appended_attempt.attempt_number
    ):
        raise ValueError("new Attempt and its NodeRun history link must agree")
    if following.status is not NodeRunStatus.RUNNING:
        raise ValueError("a new Attempt requires a running NodeRun")
    if previous.attempt_history:
        last = previous.attempt_history[-1]
        if (
            not retry_authorized
            or prior_attempt is None
            or not prior_attempt.belongs_to_node(previous)
            or prior_attempt.status not in _RETRYABLE_ATTEMPT_STATES
            or last.attempt_ref.record_id != prior_attempt.attempt_id
            or last.attempt_number != prior_attempt.attempt_number
            or prior_attempt.ended_at is None
            or appended_attempt.created_at < prior_attempt.ended_at
            or appended_attempt.status
            not in (AttemptStatus.CREATED, AttemptStatus.RUNNING)
        ):
            raise ValueError(
                "a retry requires an eligible prior Attempt and authorization"
            )
    elif (
        prior_attempt is not None
        or retry_authorized
        or appended_attempt.status is not AttemptStatus.RUNNING
        or appended_attempt.started_at != following.started_at
        or appended_attempt.created_at < following.created_at
    ):
        raise ValueError("first Attempt must start with the NodeRun")


def validate_attempt_update(
    previous: Attempt, following: Attempt, guard: AttemptTransitionGuard
) -> None:
    if (
        previous.attempt_id,
        previous.execution_id,
        previous.owner,
        previous.attempt_number,
        previous.created_at,
    ) != (
        following.attempt_id,
        following.execution_id,
        following.owner,
        following.attempt_number,
        following.created_at,
    ):
        raise ValueError("Attempt identity and ownership are immutable")
    validate_attempt_transition(previous.status, following.status, guard)
    if (
        previous.status is AttemptStatus.RUNNING
        or following.status is not AttemptStatus.RUNNING
    ):
        if previous.started_at != following.started_at:
            raise ValueError("Attempt start may be set only when entering running")


def validate_node_predecessor(predecessor: NodeRun, new_run: NodeRun) -> None:
    if (
        predecessor.status not in _TERMINAL_NODES
        or new_run.status is not NodeRunStatus.PENDING
        or new_run.node_id is not predecessor.node_id
        or new_run.node_run_id == predecessor.node_run_id
        or new_run.predecessor_ref is None
        or new_run.predecessor_ref.record_id != predecessor.node_run_id
        or predecessor.ended_at is None
        or new_run.created_at < predecessor.ended_at
    ):
        raise ValueError("explicit new NodeRun must reference its terminal predecessor")


def validate_event_append(
    history: tuple[ExecutionEvent, ...],
    event: ExecutionEvent,
    *,
    attempt: Attempt | None = None,
) -> None:
    """Check a new audit event against the already validated prefix."""
    if any(
        prior.execution_id != event.execution_id
        or prior.trace.trace_id != event.trace.trace_id
        or prior.execution_event_id == event.execution_event_id
        or prior.event_number >= event.event_number
        or prior.recorded_at > event.recorded_at
        for prior in history
    ):
        raise ValueError(
            "new event must append with a unique ID and increasing sequence"
        )
    payload = event.payload
    match payload:
        case TaskTransitionEvent():
            validate_task_transition(payload.previous_status, payload.next_status)
        case ExecutionTransitionEvent():
            validate_execution_transition(payload.previous_status, payload.next_status)
        case NodeTransitionEvent():
            if (payload.previous_status, payload.next_status) not in _NODE_GUARDS:
                raise ValueError("illegal NodeRun transition event")
        case AttemptTransitionEvent():
            if (payload.previous_status, payload.next_status) not in _ATTEMPT_GUARDS:
                raise ValueError("illegal Attempt transition event")
        case LateResponseEvent() | SystemOutputEvent():
            if (
                attempt is None
                or attempt.attempt_id != payload.attempt_ref.record_id
                or attempt.execution_id != event.execution_id
            ):
                raise ValueError(
                    "event requires its matching Attempt in the same Execution"
                )
            if isinstance(payload, LateResponseEvent) and (
                attempt.status not in _TERMINAL_ATTEMPTS
                or attempt.started_at is None
                or attempt.ended_at is None
                or event.recorded_at < attempt.ended_at
            ):
                raise ValueError(
                    "late response requires a previously started terminal Attempt"
                )
            if isinstance(payload, SystemOutputEvent) and (
                not isinstance(attempt.owner, SystemAttemptOwner)
                or attempt.owner.requirement != payload.requirement
                or attempt.started_at is None
                or event.recorded_at < attempt.started_at
            ):
                raise ValueError(
                    "system output requires a started Attempt for its pinned producer"
                )
