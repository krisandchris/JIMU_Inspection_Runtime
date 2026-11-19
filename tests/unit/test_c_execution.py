"""Permitted lifecycle edges and immutable audit updates."""

from itertools import product

import pytest
from fixtures.wp01c.executions.support import (
    ENDED,
    LATE,
    STARTED,
    attempt,
    event,
    execution,
    fixture,
    node,
    ref,
)

from jimu_runtime.domain.enums import (
    AttemptStatus,
    ExecutionStatus,
    NodeRunStatus,
    TaskStatus,
)
from jimu_runtime.domain.validation.transitions import (
    AttemptTransitionGuard,
    NodeTransitionGuard,
    validate_attempt_transition,
    validate_attempt_update,
    validate_event_append,
    validate_execution_transition,
    validate_execution_update,
    validate_node_predecessor,
    validate_node_transition,
    validate_node_update,
    validate_task_transition,
)

# Expectations list the wire edges independently of the implementation.
TASK_EDGES = {
    ("created", "accepted"),
    ("accepted", "queued"),
    ("queued", "running"),
    ("running", "completed"),
    ("running", "partial_failed"),
    ("running", "failed"),
    ("running", "cancelled"),
}
EXECUTION_EDGES = {
    ("initialized", "running"),
    ("running", "completed"),
    ("running", "partial_failed"),
    ("running", "failed"),
    ("running", "cancelled"),
}
NODE_EDGES = {
    ("pending", "ready"): {"prerequisites_available"},
    ("pending", "blocked"): {"prerequisite_unavailable"},
    ("ready", "blocked"): {"prerequisite_unavailable"},
    ("pending", "skipped"): {"deterministic_skip"},
    ("ready", "skipped"): {"deterministic_skip"},
    ("pending", "cancelled"): {"pre_start_cancellation"},
    ("ready", "cancelled"): {"pre_start_cancellation"},
    ("ready", "running"): {"first_attempt_started"},
    ("running", "succeeded"): {"authoritative_output_validated"},
    ("running", "failed"): {"nonretryable_failure", "retry_budget_exhausted"},
    ("running", "cancelled"): {"fenced_cancellation"},
}
ATTEMPT_EDGES = {
    ("created", "running"): {"claim_valid_and_invocation_started"},
    ("created", "cancelled"): {"pre_start_cancellation"},
    ("created", "superseded"): {"claim_replaced"},
    ("running", "succeeded"): {"response_validated_with_commit_rights"},
    ("running", "failed"): {"normalized_failure"},
    ("running", "timed_out"): {"deadline_elapsed"},
    ("running", "cancelled"): {"fenced_cancellation"},
    ("running", "superseded"): {"authoritative_fence_lost"},
}


@pytest.mark.parametrize("previous,following", product(TaskStatus, repeat=2))
def test_task_transition_matrix(previous, following):
    if (previous.value, following.value) in TASK_EDGES:
        validate_task_transition(previous, following)
    else:
        with pytest.raises(ValueError, match="illegal Task"):
            validate_task_transition(previous, following)


@pytest.mark.parametrize("previous,following", product(ExecutionStatus, repeat=2))
def test_execution_transition_matrix(previous, following):
    if (previous.value, following.value) in EXECUTION_EDGES:
        validate_execution_transition(previous, following)
    else:
        with pytest.raises(ValueError, match="illegal Execution"):
            validate_execution_transition(previous, following)


@pytest.mark.parametrize("previous,following", product(NodeRunStatus, repeat=2))
def test_node_transition_matrix_checks_all_guards(previous, following):
    allowed = NODE_EDGES.get((previous.value, following.value), set())
    for guard in NodeTransitionGuard:
        if guard.value in allowed:
            validate_node_transition(previous, following, guard)
        else:
            with pytest.raises(ValueError, match="transition"):
                validate_node_transition(previous, following, guard)


@pytest.mark.parametrize("previous,following", product(AttemptStatus, repeat=2))
def test_attempt_transition_matrix_checks_all_guards(previous, following):
    allowed = ATTEMPT_EDGES.get((previous.value, following.value), set())
    for guard in AttemptTransitionGuard:
        if guard.value in allowed:
            validate_attempt_transition(previous, following, guard)
        else:
            with pytest.raises(ValueError, match="transition"):
                validate_attempt_transition(previous, following, guard)


def test_context_attaches_once_and_late_invocation_refs_extend_audit():
    created = execution()
    running = execution(status="running", started_at=STARTED)
    context = {
        "snapshot_ref": ref("context_snapshot", "context-1"),
        "snapshot_digest": "d" * 64,
    }
    with_context = execution(
        status="running", started_at=STARTED, context_snapshot=context
    )
    first = ref("model_invocation", "invocation-1")
    terminal = execution(
        status="completed",
        started_at=STARTED,
        ended_at=ENDED,
        context_snapshot=context,
        model_invocation_refs=[first],
    )
    audited = execution(
        status="completed",
        started_at=STARTED,
        ended_at=ENDED,
        context_snapshot=context,
        model_invocation_refs=[first, ref("model_invocation", "invocation-2")],
    )
    for previous, following in (
        (created, running),
        (running, with_context),
        (with_context, terminal),
        (terminal, audited),
    ):
        validate_execution_update(previous, following)
    assert terminal.status is audited.status
    assert terminal.ended_at == audited.ended_at


@pytest.mark.parametrize(
    "source,target,guard",
    [
        ("pending", "blocked", NodeTransitionGuard.PREREQUISITE_UNAVAILABLE),
        ("ready", "skipped", NodeTransitionGuard.DETERMINISTIC_SKIP),
        ("ready", "cancelled", NodeTransitionGuard.PRE_START_CANCELLATION),
    ],
)
def test_node_pre_start_terminal_states_leave_attempt_history_empty(
    source, target, guard
):
    previous = node(status=source, started_at=None, attempt_history=[])
    following = node(
        status=target,
        started_at=None,
        ended_at=ENDED,
        reason={"code": guard.value},
        attempt_history=[],
    )
    validate_node_update(previous, following, guard=guard)
    assert following.started_at is None
    assert following.attempt_history == ()


def test_first_attempt_starts_node_and_retry_keeps_running():
    ready = node(status="ready", started_at=None, attempt_history=[])
    running = node()
    first_running = attempt(status="running", ended_at=None, reason=None)
    first_failed = attempt()
    validate_node_update(
        ready,
        running,
        guard=NodeTransitionGuard.FIRST_ATTEMPT_STARTED,
        appended_attempt=first_running,
    )
    validate_attempt_update(
        first_running, first_failed, AttemptTransitionGuard.DEADLINE_ELAPSED
    )
    replacement = attempt(
        attempt_id="attempt-4",
        attempt_number=4,
        status="created",
        created_at=ENDED,
        started_at=None,
        ended_at=None,
        reason=None,
    )
    after_retry = node(
        attempt_history=[
            *fixture()["node"]["attempt_history"],
            {"attempt_ref": ref("attempt", "attempt-4"), "attempt_number": 4},
        ],
    )
    validate_node_update(
        running,
        after_retry,
        appended_attempt=replacement,
        prior_attempt=first_failed,
        retry_authorized=True,
    )
    assert after_retry.status is NodeRunStatus.RUNNING
    assert [link.attempt_number for link in after_retry.attempt_history] == [1, 4]


def test_terminal_node_reexecution_links_new_pending_identity():
    terminal = node(
        status="failed", ended_at=ENDED, reason={"code": "budget_exhausted"}
    )
    successor = node(
        node_run_id="node-2",
        status="pending",
        created_at=LATE,
        started_at=None,
        predecessor_ref=ref("node_run", "node-1"),
        attempt_history=[],
    )
    validate_node_predecessor(terminal, successor)


def test_late_response_and_system_output_append_as_separate_audit_events():
    late = event()
    timed_out = attempt()
    validate_event_append((), late, attempt=timed_out)
    assert timed_out.status is AttemptStatus.TIMED_OUT
    system = event("system_event")
    system_owner = {
        "owner_kind": "system",
        "requirement": fixture()["system_event"]["payload"]["requirement"],
    }
    system_attempt = attempt(
        attempt_id="system-1", owner=system_owner, status="succeeded", reason=None
    )
    validate_event_append((late,), system, attempt=system_attempt)


@pytest.mark.parametrize(
    "source,target,guard",
    [
        ("created", "cancelled", AttemptTransitionGuard.PRE_START_CANCELLATION),
        ("created", "superseded", AttemptTransitionGuard.CLAIM_REPLACED),
        ("running", "cancelled", AttemptTransitionGuard.FENCED_CANCELLATION),
        ("running", "superseded", AttemptTransitionGuard.AUTHORITATIVE_FENCE_LOST),
    ],
)
def test_attempt_cancellation_and_replacement_preserve_start_provenance(
    source, target, guard
):
    started = None if source == "created" else STARTED
    validate_attempt_update(
        attempt(status=source, started_at=started, ended_at=None, reason=None),
        attempt(status=target, started_at=started, ended_at=ENDED, reason=None),
        guard,
    )


@pytest.mark.parametrize(
    "payload",
    [
        {
            "event_kind": "task_transition",
            "task_id": "task-1",
            "previous_status": "accepted",
            "next_status": "queued",
        },
        {
            "event_kind": "execution_transition",
            "previous_status": "initialized",
            "next_status": "running",
        },
        {
            "event_kind": "node_transition",
            "node_run_ref": ref("node_run", "node-1"),
            "previous_status": "pending",
            "next_status": "blocked",
        },
        {
            "event_kind": "attempt_transition",
            "attempt_ref": ref("attempt", "attempt-1"),
            "previous_status": "running",
            "next_status": "timed_out",
        },
    ],
)
def test_transition_events_audit_each_independent_state_domain(payload):
    validate_event_append((), event(payload=payload))
