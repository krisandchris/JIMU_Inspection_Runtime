"""Rejections for incomplete lifecycle evidence and historical rewrites."""

import pytest
from fixtures.wp01c.executions.support import (
    CREATED,
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
from pydantic import ValidationError

from jimu_runtime.domain.enums import (
    ExecutionStatus,
    NodeRunStatus,
    TaskStatus,
)
from jimu_runtime.domain.validation.transitions import (
    AttemptTransitionGuard,
    NodeTransitionGuard,
    validate_attempt_update,
    validate_event_append,
    validate_execution_transition,
    validate_execution_update,
    validate_node_predecessor,
    validate_node_transition,
    validate_node_update,
)


@pytest.mark.parametrize(
    "patch",
    [
        {"node_id": "N07"},
        {
            "status": "blocked",
            "started_at": None,
            "attempt_history": [],
            "ended_at": ENDED,
        },
        {"status": "skipped", "reason": {"code": "empty"}, "ended_at": ENDED},
        {"status": "cancelled", "started_at": None, "ended_at": ENDED},
        {"status": "succeeded", "ended_at": ENDED},
        {"started_at": "2026-11-18T03:00:01+01:00"},
        {"created_at": ENDED},
        {"trace": {"trace_id": "trace", "parent_span_id": "parent"}},
        {"predecessor_ref": ref("node_run", "node-1")},
        {
            "attempt_history": [
                {"attempt_ref": ref("attempt", "a-1"), "attempt_number": 2},
                {"attempt_ref": ref("attempt", "a-2"), "attempt_number": 2},
            ]
        },
        {
            "attempt_history": [
                {"attempt_ref": ref("attempt", "a-1"), "attempt_number": 2},
                {"attempt_ref": ref("attempt", "a-1"), "attempt_number": 3},
            ]
        },
    ],
)
def test_invalid_node_shape(patch):
    with pytest.raises(ValidationError):
        node(**patch)


@pytest.mark.parametrize(
    "patch",
    [
        {"status": "created"},
        {"status": "running"},
        {"status": "failed", "reason": None},
        {"status": "timed_out", "started_at": None},
        {"status": "cancelled", "ended_at": None},
        {"attempt_number": 0},
        {"attempt_number": True},
        {"owner": {"owner_kind": "node", "node_run_ref": ref("task", "task-1")}},
    ],
)
def test_invalid_attempt_shape(patch):
    with pytest.raises(ValidationError):
        attempt(**patch)


@pytest.mark.parametrize(
    "patch",
    [
        {"bundle_digest": "not-a-hash"},
        {"runtime_version": "latest"},
        {"workflow_version": "1.0"},
        {"model_pins": []},
        {"tool_pins": []},
    ],
)
def test_bad_pin_or_duplicate_definition_identity(patch):
    pins = fixture()["execution"]["pins"]
    if "model_pins" in patch:
        patch = {"model_pins": pins["model_pins"] * 2}
    if "tool_pins" in patch:
        patch = {"tool_pins": pins["tool_pins"] * 2}
    with pytest.raises(ValidationError):
        execution(pins=pins | patch)


def test_missing_primary_or_unregistered_version_cannot_be_fabricated():
    pins = fixture()["execution"]["pins"]
    pin = pins["model_pins"][0]
    for invalid in (
        pin | {"primary_deployment_id": "missing"},
        pin | {"deployments": pin["deployments"] * 2},
        pin | {"actual_provider": {"model_version": "1.0.0"}},
    ):
        with pytest.raises(ValidationError):
            execution(pins=pins | {"model_pins": [invalid]})

    with pytest.raises(ValidationError):
        execution(model_invocation_refs=[ref("model_invocation", "invocation-1")])


def test_execution_update_cannot_rewrite_pins_audit_or_context():
    context = {
        "snapshot_ref": ref("context_snapshot", "context-1"),
        "snapshot_digest": "d" * 64,
    }
    observed = [
        ref("model_invocation", "invocation-1"),
        ref("model_invocation", "invocation-2"),
    ]
    current = execution(
        status="running",
        started_at=STARTED,
        context_snapshot=context,
        model_invocation_refs=observed,
    )
    for invalid in (
        execution(status="running", started_at=STARTED, model_invocation_refs=observed),
        execution(
            status="running",
            started_at=STARTED,
            context_snapshot=context,
            model_invocation_refs=observed[::-1],
        ),
        execution(
            status="running",
            started_at=STARTED,
            context_snapshot=context,
            model_invocation_refs=observed[:1],
        ),
        execution(
            status="running",
            started_at=STARTED,
            context_snapshot=context,
            model_invocation_refs=observed,
            pins=fixture()["execution"]["pins"] | {"release_id": "new-release"},
        ),
    ):
        with pytest.raises(ValueError):
            validate_execution_update(current, invalid)
    with pytest.raises(ValueError, match="context can be attached once"):
        validate_execution_update(execution(), current)
    with pytest.raises(ValidationError, match="unique"):
        execution(
            status="running", started_at=STARTED, model_invocation_refs=observed[:1] * 2
        )


def test_wrong_status_or_guard_domain_cannot_authorize_transition():
    with pytest.raises(ValueError, match="illegal Execution"):
        validate_execution_transition(TaskStatus.RUNNING, ExecutionStatus.FAILED)
    with pytest.raises(ValueError, match="matching guard"):
        validate_node_transition(
            NodeRunStatus.READY,
            NodeRunStatus.CANCELLED,
            AttemptTransitionGuard.PRE_START_CANCELLATION,
        )


def test_first_node_attempt_requires_actual_started_owner():
    ready = node(status="ready", started_at=None, attempt_history=[])
    for bad_attempt in (
        None,
        attempt(status="running", ended_at=None, execution_id="exec-2"),
        attempt(status="created", started_at=None, ended_at=None),
        attempt(status="running", started_at=ENDED, ended_at=None),
    ):
        with pytest.raises(ValueError, match="Attempt"):
            validate_node_update(
                ready,
                node(),
                guard=NodeTransitionGuard.FIRST_ATTEMPT_STARTED,
                appended_attempt=bad_attempt,
            )


def test_retry_requires_previous_run_history_and_permission():
    previous = node()
    replacement = attempt(
        attempt_id="attempt-2",
        attempt_number=2,
        status="created",
        created_at=ENDED,
        started_at=None,
        ended_at=None,
    )
    updated = node(
        attempt_history=[
            *fixture()["node"]["attempt_history"],
            {"attempt_ref": ref("attempt", "attempt-2"), "attempt_number": 2},
        ]
    )
    with pytest.raises(ValueError, match="eligible prior Attempt"):
        validate_node_update(
            previous, updated, appended_attempt=replacement, prior_attempt=attempt()
        )
    with pytest.raises(ValueError, match="eligible prior Attempt"):
        validate_node_update(
            previous,
            updated,
            appended_attempt=replacement,
            prior_attempt=attempt(status="succeeded"),
            retry_authorized=True,
        )
    with pytest.raises(ValueError, match="eligible prior Attempt"):
        validate_node_update(
            previous,
            updated,
            appended_attempt=replacement,
            prior_attempt=attempt(attempt_id="wrong"),
            retry_authorized=True,
        )


def test_terminal_node_history_and_attempt_timestamps_cannot_be_rewritten():
    terminal = node(status="failed", ended_at=ENDED, reason={"code": "failed"})
    with pytest.raises(ValueError, match="terminal NodeRun"):
        validate_node_update(terminal, node(status="failed", ended_at=LATE))
    with pytest.raises(ValueError, match="terminal predecessor"):
        validate_node_predecessor(
            terminal, node(status="pending", started_at=None, attempt_history=[])
        )

    running = attempt(status="running", ended_at=None)
    with pytest.raises(ValueError, match="Attempt start"):
        validate_attempt_update(
            running,
            attempt(started_at=CREATED),
            AttemptTransitionGuard.DEADLINE_ELAPSED,
        )
    with pytest.raises(ValueError, match="illegal Attempt"):
        validate_attempt_update(
            attempt(),
            attempt(status="succeeded"),
            AttemptTransitionGuard.RESPONSE_VALIDATED_WITH_COMMIT_RIGHTS,
        )


def test_late_response_rejects_wrong_scope_unstarted_attempt_and_replayed_event():
    late = event()
    with pytest.raises(ValueError, match="matching Attempt"):
        validate_event_append((), late, attempt=attempt(execution_id="exec-other"))
    with pytest.raises(ValueError, match="started terminal"):
        validate_event_append(
            (), late, attempt=attempt(status="superseded", started_at=None)
        )
    with pytest.raises(ValueError, match="started terminal"):
        validate_event_append(
            (), late, attempt=attempt(status="running", ended_at=None)
        )
    with pytest.raises(ValueError, match="unique ID"):
        validate_event_append((late,), late, attempt=attempt())
    with pytest.raises(ValueError, match="unique ID"):
        validate_event_append(
            (late,),
            event(execution_event_id="later", event_number=2, recorded_at=ENDED),
            attempt=attempt(),
        )


def test_system_output_keeps_system_producer_and_implementation_pins():
    system = event("system_event")
    with pytest.raises(ValueError, match="pinned producer"):
        validate_event_append((), system, attempt=attempt(attempt_id="system-1"))
    payload = fixture()["system_event"]["payload"]
    with pytest.raises(ValidationError, match="allowed implementation version"):
        event(
            "system_event", payload=payload | {"actual_implementation_version": "9.9.9"}
        )


def test_transition_audit_cannot_record_unlisted_edge():
    forged = event(
        payload={
            "event_kind": "node_transition",
            "node_run_ref": ref("node_run", "node-1"),
            "previous_status": "failed",
            "next_status": "running",
        }
    )
    with pytest.raises(ValueError, match="illegal NodeRun transition event"):
        validate_event_append((), forged)


def test_node_retry_authorization_cannot_be_coerced_from_text():
    running = node()
    with pytest.raises(ValueError, match="explicit boolean"):
        validate_node_update(running, running, retry_authorized="false")
