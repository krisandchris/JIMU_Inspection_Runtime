"""Scoped PostgreSQL persistence. The caller owns the transaction and lock order."""

import json
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from hashlib import sha256
from uuid import uuid4

from pydantic import TypeAdapter
from sqlalchemy import text
from sqlalchemy.engine import RowMapping
from sqlalchemy.ext.asyncio import AsyncConnection

from jimu_runtime.domain.base import ContractModel
from jimu_runtime.domain.definitions.models import PolicyContentRef
from jimu_runtime.domain.enums import (
    AttemptStatus,
    ExecutionStatus,
    NodeRunStatus,
    RuntimeRecordKind,
    TaskStatus,
)
from jimu_runtime.domain.nodes.envelopes import SucceededNodeOutput
from jimu_runtime.domain.runtime.executions import (
    Attempt,
    AttemptTransitionEvent,
    ExecutionEvent,
    ExecutionEventPayload,
    ExecutionRecord,
    ExecutionTransitionEvent,
    LateResponseEvent,
    NodeId,
    NodeRun,
    NodeTransitionEvent,
    SystemOutputEvent,
    TaskTransitionEvent,
    TraceContext,
)
from jimu_runtime.domain.runtime.facts import AttemptRef, NodeRunRef
from jimu_runtime.domain.runtime.intake import (
    AcceptedTask,
    IdempotencyScope,
    TaskSubmission,
)
from jimu_runtime.domain.validation.transitions import (
    AttemptTransitionGuard,
    NodeTransitionGuard,
    validate_attempt_update,
    validate_event_append,
    validate_execution_update,
    validate_node_predecessor,
    validate_node_update,
    validate_task_transition,
)
from jimu_runtime.workflow.scheduling import JobBudget, SchedulingLimits
from jimu_runtime.workflow.worker_capabilities import (
    HistoricalCapabilities,
    record_historical_capabilities,
)


class RepositoryError(ValueError):
    """A workflow proposal conflicts with the persisted scoped records."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class IntentConflict(RepositoryError):
    """A scoped intake key belongs to a different submission or principal."""


class IntentStatus(StrEnum):
    PENDING = "pending"
    ACCEPTED = "accepted"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class IntakeIntent:
    submission: TaskSubmission
    submission_digest: str
    issuer: str
    subject: str
    created_at: datetime
    status: IntentStatus
    failure_code: str | None
    task_id: str | None
    execution_id: str | None


@dataclass(frozen=True, slots=True)
class LockedExecution:
    task: AcceptedTask
    task_status: TaskStatus
    execution: ExecutionRecord
    execution_policy_ref: PolicyContentRef
    cancel_requested_at: datetime | None
    scheduling_limits: SchedulingLimits
    scheduling_limits_digest: str
    is_inference_replay: bool = False


@dataclass(frozen=True, slots=True)
class NewJob:
    job_id: str
    ready_at: datetime
    budget: JobBudget


def _scope(scope: IdempotencyScope) -> dict[str, object]:
    return {"project_id": scope.project_id, "client_id": scope.client_id}


def _execution(scope: IdempotencyScope, execution_id: str) -> dict[str, object]:
    return {**_scope(scope), "execution_id": execution_id}


def _node(
    scope: IdempotencyScope, execution_id: str, node_run_id: str
) -> dict[str, object]:
    return {**_execution(scope, execution_id), "node_run_id": node_run_id}


def _attempt(
    scope: IdempotencyScope, execution_id: str, node_run_id: str, attempt_id: str
) -> dict[str, object]:
    return {**_node(scope, execution_id, node_run_id), "attempt_id": attempt_id}


def _digest(body: str) -> str:
    return sha256(body.encode("utf-8")).hexdigest()


def _checked_json(record: ContractModel) -> str:
    return type(record).model_validate(record).stable_json()


_INTENT_COLUMNS = """
    project_id, client_id, request_id, idempotency_key, submission::text AS submission,
    submission_digest, issuer, subject, created_at,
    status, failure_code, task_id, execution_id
"""


def _intent_row(row: RowMapping) -> IntakeIntent:
    body: str = row["submission"]
    digest: str = row["submission_digest"]
    submission = TaskSubmission.model_validate_json(body)
    if (
        body != submission.stable_json()
        or _digest(body) != digest
        or submission.request_id != row["request_id"]
        or submission.idempotency_key.encode("utf-8") != row["idempotency_key"]
        or submission.idempotency_scope.project_id != row["project_id"]
        or submission.idempotency_scope.client_id != row["client_id"]
    ):
        raise RepositoryError("intake_submission_corrupt")
    return IntakeIntent(
        submission,
        digest,
        row["issuer"],
        row["subject"],
        row["created_at"],
        IntentStatus(row["status"]),
        row["failure_code"],
        row["task_id"],
        row["execution_id"],
    )


async def reserve_intent(
    connection: AsyncConnection,
    scope: IdempotencyScope,
    submission: TaskSubmission,
    *,
    issuer: str,
    subject: str,
) -> IntakeIntent:
    body = _checked_json(submission)
    if submission.idempotency_scope != scope:
        raise IntentConflict("intake_scope_mismatch")
    if any(not value or "\x00" in value for value in (issuer, subject)):
        raise IntentConflict("invalid_intake_identity_or_key")
    params = {
        **_scope(scope),
        "request_id": submission.request_id,
        "idempotency_key": submission.idempotency_key.encode("utf-8"),
        "submission": body,
        "digest": _digest(body),
        "issuer": issuer,
        "subject": subject,
    }
    await connection.execute(
        text("""
        INSERT INTO jimu_workflow.intake_intents
            (project_id, client_id, request_id, idempotency_key,
            submission, submission_digest, issuer, subject)
        VALUES (:project_id, :client_id, :request_id, :idempotency_key,
            CAST(:submission AS json), :digest, :issuer, :subject)
        ON CONFLICT DO NOTHING
    """),
        params,
    )
    rows = (
        (
            await connection.execute(
                text(f"""
        SELECT {_INTENT_COLUMNS}
        FROM jimu_workflow.intake_intents
        WHERE project_id = :project_id AND client_id = :client_id
            AND (request_id = :request_id OR idempotency_key = :idempotency_key)
    """),
                params,
            )
        )
        .mappings()
        .all()
    )
    if len(rows) != 1:
        raise IntentConflict("intake_idempotency_conflict")
    existing = _intent_row(rows[0])
    if (existing.submission_digest, existing.submission.stable_json()) != (
        params["digest"],
        body,
    ):
        raise IntentConflict("intake_idempotency_conflict")
    if (existing.issuer, existing.subject) != (issuer, subject):
        raise IntentConflict("intake_principal_mismatch")
    return existing


async def get_intent(
    connection: AsyncConnection,
    scope: IdempotencyScope,
    request_id: str,
    *,
    for_update: bool = False,
) -> IntakeIntent | None:
    lock = "FOR UPDATE" if for_update else ""
    row = (
        (
            await connection.execute(
                text(f"""
        SELECT {_INTENT_COLUMNS}
        FROM jimu_workflow.intake_intents
        WHERE project_id = :project_id AND client_id = :client_id AND request_id = :request_id
        {lock}
    """),
                {**_scope(scope), "request_id": request_id},
            )
        )
        .mappings()
        .one_or_none()
    )
    if row is None:
        return None
    intent = _intent_row(row)
    if (scope, request_id) != (
        intent.submission.idempotency_scope,
        intent.submission.request_id,
    ):
        raise RepositoryError("intake_submission_index_mismatch")
    return intent


async def list_pending_request_ids(
    connection: AsyncConnection,
    scope: IdempotencyScope,
    *,
    after_request_id: str | None,
    limit: int,
) -> tuple[str, ...]:
    """Read a bounded scope-local recovery page in stable byte order."""
    if type(limit) is not int or not 1 <= limit <= 1025:
        raise RepositoryError("invalid_pending_intake_limit")
    rows = await connection.execute(
        text("""
            SELECT request_id FROM jimu_workflow.intake_intents
            WHERE project_id = :project_id AND client_id = :client_id AND status = 'pending'
                AND (CAST(:after_request_id AS text) IS NULL
                    OR request_id COLLATE "C" > CAST(:after_request_id AS text))
            ORDER BY request_id COLLATE "C"
            LIMIT :limit
        """),
        {**_scope(scope), "after_request_id": after_request_id, "limit": limit},
    )
    return tuple(rows.scalars())


async def _match_intent(
    connection: AsyncConnection, scope: IdempotencyScope, intent: IntakeIntent
) -> IntakeIntent:
    current = await get_intent(
        connection, scope, intent.submission.request_id, for_update=True
    )
    if current is None or (
        current.submission,
        current.submission_digest,
        current.issuer,
        current.subject,
    ) != (intent.submission, intent.submission_digest, intent.issuer, intent.subject):
        raise IntentConflict("intake_identity_changed")
    return current


async def fail_intent(
    connection: AsyncConnection,
    scope: IdempotencyScope,
    intent: IntakeIntent,
    *,
    failure_code: str,
) -> IntakeIntent:
    current = await _match_intent(connection, scope, intent)
    if current.status is not IntentStatus.PENDING:
        return current
    if not failure_code or "\x00" in failure_code:
        raise RepositoryError("invalid_intake_failure")
    row = (
        (
            await connection.execute(
                text(f"""
        UPDATE jimu_workflow.intake_intents SET status = 'failed', failure_code = :failure_code
        WHERE project_id = :project_id AND client_id = :client_id AND request_id = :request_id
        RETURNING {_INTENT_COLUMNS}
    """),
                {
                    **_scope(scope),
                    "request_id": current.submission.request_id,
                    "failure_code": failure_code,
                },
            )
        )
        .mappings()
        .one()
    )
    return _intent_row(row)


_EXECUTION_STATE = {
    "started_at",
    "ended_at",
    "context_snapshot",
    "model_invocation_refs",
}
_NODE_STATE = {"started_at", "ended_at", "reason", "attempt_history", "output_refs"}


def _state(record: ContractModel, fields: set[str]) -> str:
    return json.dumps(
        record.model_dump(mode="json", include=fields),
        sort_keys=True,
        separators=(",", ":"),
    )


def _restore_record[T: ContractModel](
    model: type[T], body: str, state: str, status: str, state_fields: set[str]
) -> T:
    mutable: dict[str, object] = json.loads(state)
    if mutable.keys() != state_fields:
        raise RepositoryError("invalid_mutable_projection")
    immutable: dict[str, object] = json.loads(body)
    return model.model_validate_json(
        json.dumps({**immutable, **mutable, "status": status})
    )


def _execution_row(
    row: RowMapping, scope: IdempotencyScope, execution_id: str
) -> LockedExecution:
    task = AcceptedTask.model_validate_json(row["task_record"])
    execution = _restore_record(
        ExecutionRecord, row["record"], row["state"], row["status"], _EXECUTION_STATE
    )
    limits_body: str = row["scheduling_limits"]
    limits = SchedulingLimits.model_validate_json(limits_body)
    if (
        task.idempotency_scope != scope
        or task.task_id != execution.task_id
        or task.task_id != row["task_id"]
        or task.request_id != row["request_id"]
        or task.release_id != row["task_release_id"]
        or task.bundle_digest != row["task_bundle_digest"]
        or execution.execution_id != execution_id
        or execution.pins.release_id != row["release_id"]
        or execution.pins.bundle_digest != row["bundle_digest"]
        or limits.version != row["scheduling_limits_version"]
        or _digest(limits_body) != row["scheduling_limits_digest"]
    ):
        raise RepositoryError("execution_index_mismatch")
    return LockedExecution(
        task,
        TaskStatus(row["task_status"]),
        execution,
        PolicyContentRef.model_validate_json(row["execution_policy_ref"]),
        row["cancel_requested_at"],
        limits,
        row["scheduling_limits_digest"],
        row["is_inference_replay"],
    )


async def _read_execution(
    connection: AsyncConnection,
    scope: IdempotencyScope,
    execution_id: str,
    *,
    lock: str = "",
    replay_only: bool = False,
) -> LockedExecution | None:
    replay_filter = (
        "AND i.execution_id IS NOT NULL AND i.execution_id <> e.execution_id"
        if replay_only
        else ""
    )
    row = (
        (
            await connection.execute(
                text(f"""
        SELECT e.record::text AS record, e.state::text AS state, e.status, e.release_id,
            e.bundle_digest, e.execution_policy_ref::text AS execution_policy_ref,
            e.cancel_requested_at, e.scheduling_limits::text AS scheduling_limits,
            e.scheduling_limits_version, e.scheduling_limits_digest,
            t.record::text AS task_record, t.status AS task_status, t.task_id, t.request_id,
            t.release_id AS task_release_id, t.bundle_digest AS task_bundle_digest,
            (i.execution_id IS NOT NULL AND i.execution_id <> e.execution_id)
                AS is_inference_replay
        FROM jimu_workflow.executions AS e
        JOIN jimu_workflow.tasks AS t USING (project_id, client_id, task_id)
        JOIN jimu_workflow.intake_intents AS i
            ON (i.project_id, i.client_id, i.request_id) =
               (t.project_id, t.client_id, t.request_id)
        WHERE e.project_id = :project_id AND e.client_id = :client_id
            AND e.execution_id = :execution_id
            {replay_filter}
        {lock}
    """),
                _execution(scope, execution_id),
            )
        )
        .mappings()
        .one_or_none()
    )
    return _execution_row(row, scope, execution_id) if row is not None else None


async def get_execution(
    connection: AsyncConnection, scope: IdempotencyScope, execution_id: str
) -> LockedExecution | None:
    return await _read_execution(connection, scope, execution_id)


async def lock_execution(
    connection: AsyncConnection,
    scope: IdempotencyScope,
    execution_id: str,
    *,
    skip_locked: bool = False,
) -> LockedExecution | None:
    lock_suffix = "SKIP LOCKED" if skip_locked else ""
    # Task state writers contend with one another without blocking the
    # foreign-key key-share checks of the Task's independent replay rows.
    task_id = (
        await connection.execute(
            text(f"""
        SELECT t.task_id
        FROM jimu_workflow.tasks AS t
        JOIN jimu_workflow.executions AS e USING (project_id, client_id, task_id)
        JOIN jimu_workflow.intake_intents AS i
            ON (i.project_id, i.client_id, i.request_id) =
               (t.project_id, t.client_id, t.request_id)
        WHERE (i.execution_id IS NULL OR i.execution_id = e.execution_id)
            AND e.project_id = :project_id AND e.client_id = :client_id
            AND e.execution_id = :execution_id
        FOR NO KEY UPDATE OF t {lock_suffix}
    """),
            _execution(scope, execution_id),
        )
    ).scalar_one_or_none()
    # A replay updates its own Execution only. When the intake Task was
    # skipped because another transaction holds it, preserve the original
    # Task-before-Execution lock order by excluding its primary Execution.
    return await _read_execution(
        connection,
        scope,
        execution_id,
        lock=f"FOR NO KEY UPDATE OF e {lock_suffix}",
        replay_only=task_id is None,
    )


def _check_acceptance(
    current: IntakeIntent,
    task: AcceptedTask,
    execution: ExecutionRecord,
    policy_ref: PolicyContentRef,
    limits: SchedulingLimits,
    root: NodeRun,
    job: NewJob,
) -> None:
    submission = current.submission
    if (
        (
            task.idempotency_scope,
            task.request_id,
            task.idempotency_key,
            task.media_ref,
            task.context,
            task.trace,
        )
        != (
            submission.idempotency_scope,
            submission.request_id,
            submission.idempotency_key,
            submission.media_ref,
            submission.context,
            submission.trace,
        )
        or (
            submission.requested_release_id is not None
            and task.release_id != submission.requested_release_id
        )
        or (
            execution.task_id,
            execution.pins.release_id,
            execution.pins.bundle_digest,
            execution.trace,
        )
        != (task.task_id, task.release_id, task.bundle_digest, task.trace)
        or execution.status is not ExecutionStatus.INITIALIZED
        or (
            root.execution_id,
            root.node_id,
            root.status,
            root.config_ref,
            root.input_digest,
        )
        != (
            execution.execution_id,
            NodeId.N01,
            NodeRunStatus.PENDING,
            policy_ref,
            execution.pins.input_digest,
        )
        or root.created_at < execution.created_at
        or root.upstream_inputs
        or root.predecessor_ref is not None
        or root.trace.trace_id != execution.trace.trace_id
        or job.budget != limits.for_node(NodeId.N01)
        or job.ready_at < root.created_at
    ):
        raise RepositoryError("intake_execution_pin_mismatch")


async def accept_intent(
    connection: AsyncConnection,
    scope: IdempotencyScope,
    intent: IntakeIntent,
    task: AcceptedTask,
    execution: ExecutionRecord,
    execution_policy_ref: PolicyContentRef,
    scheduling_limits: SchedulingLimits,
    root: NodeRun,
    job: NewJob,
    *,
    historical_capabilities: HistoricalCapabilities | None = None,
) -> LockedExecution:
    current = await _match_intent(connection, scope, intent)
    if current.status is IntentStatus.FAILED:
        raise RepositoryError(current.failure_code or "intake_intent_failed")
    if current.status is IntentStatus.ACCEPTED:
        saved = await get_execution(connection, scope, current.execution_id or "")
        if saved is None or saved.task != task or current.task_id != task.task_id:
            raise IntentConflict("intake_result_mismatch")
        return saved
    task_body = _checked_json(task)
    _check_acceptance(
        current, task, execution, execution_policy_ref, scheduling_limits, root, job
    )
    params = {
        **_execution(scope, execution.execution_id),
        "task_id": task.task_id,
        "request_id": task.request_id,
        "release_id": task.release_id,
        "bundle_digest": task.bundle_digest,
        "task": task_body,
    }
    await connection.execute(
        text("""
        INSERT INTO jimu_workflow.tasks
            (project_id, client_id, task_id, request_id, release_id, bundle_digest, record, status)
        VALUES (:project_id, :client_id, :task_id, :request_id, :release_id, :bundle_digest,
            CAST(:task AS json), 'accepted')
    """),
        params,
    )
    scheduling_digest = await insert_initial_execution(
        connection,
        scope,
        execution,
        execution_policy_ref,
        scheduling_limits,
        root,
        historical_capabilities=historical_capabilities,
    )
    now = root.created_at
    await append_event(
        connection,
        scope,
        execution.execution_id,
        execution.trace,
        TaskTransitionEvent(
            event_kind="task_transition",
            task_id=task.task_id,
            previous_status=TaskStatus.CREATED,
            next_status=TaskStatus.ACCEPTED,
        ),
        now=now,
    )
    await transition_task(
        connection,
        scope,
        execution.execution_id,
        task.task_id,
        TaskStatus.ACCEPTED,
        TaskStatus.QUEUED,
        now=now,
        trace=execution.trace,
    )
    await ready_root_job(connection, scope, root, job)
    await connection.execute(
        text("""
        UPDATE jimu_workflow.intake_intents
        SET status = 'accepted', task_id = :task_id, execution_id = :execution_id
        WHERE project_id = :project_id AND client_id = :client_id AND request_id = :request_id
    """),
        params,
    )
    return LockedExecution(
        task,
        TaskStatus.QUEUED,
        execution,
        execution_policy_ref,
        None,
        scheduling_limits,
        scheduling_digest,
    )


async def insert_initial_execution(
    connection: AsyncConnection,
    scope: IdempotencyScope,
    execution: ExecutionRecord,
    execution_policy_ref: PolicyContentRef,
    scheduling_limits: SchedulingLimits,
    root: NodeRun,
    *,
    historical_capabilities: HistoricalCapabilities | None,
) -> str:
    """Create an Execution and its pending N01 for an existing scoped Task."""
    scheduling_body = _checked_json(scheduling_limits)
    scheduling_digest = _digest(scheduling_body)
    params = {
        **_execution(scope, execution.execution_id),
        "task_id": execution.task_id,
        "release_id": execution.pins.release_id,
        "bundle_digest": execution.pins.bundle_digest,
        "execution": _checked_json(execution),
        "state": _state(execution, _EXECUTION_STATE),
        "policy_ref": _checked_json(execution_policy_ref),
        "scheduling_limits": scheduling_body,
        "scheduling_limits_version": scheduling_limits.version,
        "scheduling_limits_digest": scheduling_digest,
    }
    await connection.execute(
        text("""
        INSERT INTO jimu_workflow.executions
            (project_id, client_id, execution_id, task_id, release_id, bundle_digest,
            execution_policy_ref, scheduling_limits, scheduling_limits_version,
            scheduling_limits_digest, record, state, status)
        VALUES (:project_id, :client_id, :execution_id, :task_id, :release_id, :bundle_digest,
            CAST(:policy_ref AS json), CAST(:scheduling_limits AS json), :scheduling_limits_version,
            :scheduling_limits_digest, CAST(:execution AS json), CAST(:state AS json), 'initialized')
    """),
        params,
    )
    if historical_capabilities is not None:
        await record_historical_capabilities(
            connection, scope, execution, historical_capabilities
        )
    await insert_node(
        connection, scope, execution.execution_id, root, branch_key="global"
    )
    return scheduling_digest


async def ready_root_job(
    connection: AsyncConnection, scope: IdempotencyScope, root: NodeRun, job: NewJob
) -> None:
    """Transition the already stored N01 and queue its durable job."""
    ready = root.model_copy(update={"status": NodeRunStatus.READY})
    await transition_node(
        connection,
        scope,
        root.execution_id,
        root,
        ready,
        now=root.created_at,
        guard=NodeTransitionGuard.PREREQUISITES_AVAILABLE,
    )
    await insert_job(connection, scope, root.execution_id, root.node_run_id, job)


_NODE_COLUMNS = """
    node_run_id, node_id, branch_key, input_digest, predecessor_id,
    record::text AS record, state::text AS state, status
"""


def _node_row(row: RowMapping, execution_id: str) -> NodeRun:
    node = _restore_record(
        NodeRun, row["record"], row["state"], row["status"], _NODE_STATE
    )
    if (
        node.execution_id != execution_id
        or node.node_run_id != row["node_run_id"]
        or node.node_id.value != row["node_id"]
        or node.input_digest != row["input_digest"]
        or (node.predecessor_ref.record_id if node.predecessor_ref else None)
        != row["predecessor_id"]
    ):
        raise RepositoryError("node_index_mismatch")
    return node


async def get_node(
    connection: AsyncConnection,
    scope: IdempotencyScope,
    execution_id: str,
    node_run_id: str,
    *,
    for_update: bool = False,
) -> NodeRun | None:
    lock = "FOR UPDATE" if for_update else ""
    row = (
        (
            await connection.execute(
                text(f"""
        SELECT {_NODE_COLUMNS}
        FROM jimu_workflow.node_runs
        WHERE project_id = :project_id AND client_id = :client_id
            AND execution_id = :execution_id AND node_run_id = :node_run_id
        {lock}
    """),
                _node(scope, execution_id, node_run_id),
            )
        )
        .mappings()
        .one_or_none()
    )
    return _node_row(row, execution_id) if row is not None else None


async def get_branch(
    connection: AsyncConnection,
    scope: IdempotencyScope,
    execution_id: str,
    node_id: NodeId,
    branch_key: str,
) -> NodeRun | None:
    row = (
        (
            await connection.execute(
                text(f"""
                SELECT {_NODE_COLUMNS}
                FROM jimu_workflow.node_runs
                WHERE project_id = :project_id AND client_id = :client_id
                    AND execution_id = :execution_id
                    AND node_id = :node_id AND branch_key = :branch_key
            """),
                {
                    **_execution(scope, execution_id),
                    "node_id": node_id.value,
                    "branch_key": branch_key,
                },
            )
        )
        .mappings()
        .one_or_none()
    )
    return None if row is None else _node_row(row, execution_id)


async def list_branches(
    connection: AsyncConnection,
    scope: IdempotencyScope,
    execution_id: str,
    *,
    for_update: bool = False,
) -> tuple[tuple[str, NodeRun], ...]:
    lock = "FOR UPDATE" if for_update else ""
    rows = (
        await connection.execute(
            text(f"""
        SELECT {_NODE_COLUMNS}
        FROM jimu_workflow.node_runs
        WHERE project_id = :project_id AND client_id = :client_id AND execution_id = :execution_id
        ORDER BY node_run_id {lock}
    """),
            _execution(scope, execution_id),
        )
    ).mappings()
    return tuple((row["branch_key"], _node_row(row, execution_id)) for row in rows)


async def list_nodes(
    connection: AsyncConnection,
    scope: IdempotencyScope,
    execution_id: str,
    *,
    for_update: bool = False,
) -> tuple[NodeRun, ...]:
    return tuple(
        node
        for _, node in await list_branches(
            connection,
            scope,
            execution_id,
            for_update=for_update,
        )
    )


async def insert_node(
    connection: AsyncConnection,
    scope: IdempotencyScope,
    execution_id: str,
    node: NodeRun,
    *,
    branch_key: str,
) -> NodeRun:
    body = _checked_json(node)
    owner = await get_execution(connection, scope, execution_id)
    if (
        owner is None
        or execution_id != node.execution_id
        or node.trace.trace_id != owner.execution.trace.trace_id
        or node.created_at < owner.execution.created_at
        or node.status is not NodeRunStatus.PENDING
        or not branch_key
        or len(branch_key) > 256
        or "\x00" in branch_key
    ):
        raise RepositoryError("invalid_new_node")
    if node.predecessor_ref is not None:
        predecessor = await get_node(
            connection, scope, execution_id, node.predecessor_ref.record_id
        )
        if predecessor is None:
            raise RepositoryError("missing_node_predecessor")
        validate_node_predecessor(predecessor, node)
    params = {
        **_node(scope, execution_id, node.node_run_id),
        "node_id": node.node_id.value,
        "branch_key": branch_key,
        "input_digest": node.input_digest,
        "predecessor_id": node.predecessor_ref.record_id
        if node.predecessor_ref
        else None,
        "record": body,
        "state": _state(node, _NODE_STATE),
    }
    inserted = (
        await connection.execute(
            text("""
        INSERT INTO jimu_workflow.node_runs
            (project_id, client_id, execution_id, node_run_id, node_id, branch_key,
            input_digest, predecessor_id, record, state, status)
        VALUES (:project_id, :client_id, :execution_id, :node_run_id, :node_id, :branch_key,
            :input_digest, :predecessor_id, CAST(:record AS json), CAST(:state AS json), 'pending')
        ON CONFLICT DO NOTHING
        RETURNING node_run_id
    """),
            params,
        )
    ).scalar_one_or_none()
    if inserted is not None:
        return node
    row = (
        (
            await connection.execute(
                text(f"""
        SELECT {_NODE_COLUMNS}
        FROM jimu_workflow.node_runs
        WHERE project_id = :project_id AND client_id = :client_id AND execution_id = :execution_id
            AND node_id = :node_id AND branch_key = :branch_key
    """),
                params,
            )
        )
        .mappings()
        .one_or_none()
    )
    if row is None:
        raise RepositoryError("node_identity_conflict")
    original = NodeRun.model_validate_json(row["record"])
    if original != node.model_copy(update={"created_at": original.created_at}):
        raise RepositoryError("node_branch_conflict")
    return _node_row(row, execution_id)


async def insert_job(
    connection: AsyncConnection,
    scope: IdempotencyScope,
    execution_id: str,
    node_run_id: str,
    job: NewJob,
) -> None:
    node = await get_node(connection, scope, execution_id, node_run_id)
    execution = await get_execution(connection, scope, execution_id)
    if (
        node is None
        or execution is None
        or job.budget != execution.scheduling_limits.for_node(node.node_id)
        or not job.job_id
        or "\x00" in job.job_id
        or job.ready_at < node.created_at
    ):
        raise RepositoryError("job_budget_or_owner_mismatch")
    budget = JobBudget.model_validate(job.budget)
    params = {
        **_node(scope, execution_id, node_run_id),
        "job_id": job.job_id,
        "ready_at": job.ready_at,
        "max_attempts": budget.max_attempts,
        "timeout_ms": budget.timeout_ms,
        "retry_backoff_ms": list(budget.retry_backoff_ms),
        "retryable_error_classes": list(budget.retryable_error_classes),
        "retry_on_lease_loss": budget.retry_on_lease_loss,
    }
    inserted = (
        await connection.execute(
            text("""
        INSERT INTO jimu_workflow.jobs
            (project_id, client_id, execution_id, node_run_id, job_id, ready_at, max_attempts,
            timeout_ms, retry_backoff_ms, retryable_error_classes, retry_on_lease_loss)
        VALUES (:project_id, :client_id, :execution_id, :node_run_id, :job_id, :ready_at, :max_attempts,
            :timeout_ms, :retry_backoff_ms, :retryable_error_classes, :retry_on_lease_loss)
        ON CONFLICT DO NOTHING
        RETURNING job_id
    """),
            params,
        )
    ).scalar_one_or_none()
    if inserted is not None:
        return
    existing = (
        (
            await connection.execute(
                text("""
        SELECT job_id, max_attempts, timeout_ms, retry_backoff_ms, retryable_error_classes, retry_on_lease_loss
        FROM jimu_workflow.jobs
        WHERE project_id = :project_id AND client_id = :client_id
            AND execution_id = :execution_id AND node_run_id = :node_run_id
    """),
                params,
            )
        )
        .mappings()
        .one_or_none()
    )
    if existing is None or any(
        params[name] != value for name, value in existing.items()
    ):
        raise RepositoryError("job_identity_conflict")


async def get_attempt(
    connection: AsyncConnection,
    scope: IdempotencyScope,
    execution_id: str,
    node_run_id: str,
    attempt_id: str,
) -> Attempt | None:
    row = (
        (
            await connection.execute(
                text("""
        SELECT COALESCE(r.record, a.record)::text AS record, a.attempt_number, r.status
        FROM jimu_workflow.attempts AS a
        LEFT JOIN jimu_workflow.attempt_results AS r
            USING (project_id, client_id, execution_id, node_run_id, attempt_id)
        WHERE a.project_id = :project_id AND a.client_id = :client_id AND a.execution_id = :execution_id
            AND a.node_run_id = :node_run_id AND a.attempt_id = :attempt_id
    """),
                _attempt(scope, execution_id, node_run_id, attempt_id),
            )
        )
        .mappings()
        .one_or_none()
    )
    if row is None:
        return None
    attempt = Attempt.model_validate_json(row["record"])
    node = await get_node(connection, scope, execution_id, node_run_id)
    if (
        node is None
        or not attempt.belongs_to_node(node)
        or attempt.attempt_id != attempt_id
        or attempt.attempt_number != row["attempt_number"]
        or attempt.status.value != (row["status"] or AttemptStatus.RUNNING.value)
    ):
        raise RepositoryError("attempt_index_mismatch")
    return attempt


async def insert_attempt(
    connection: AsyncConnection,
    scope: IdempotencyScope,
    execution_id: str,
    node_run_id: str,
    attempt: Attempt,
    *,
    job_id: str,
    fence: int,
    lease_owner: str,
) -> None:
    body = _checked_json(attempt)
    node = await get_node(connection, scope, execution_id, node_run_id)
    if (
        node is None
        or not attempt.belongs_to_node(node)
        or attempt.status is not AttemptStatus.RUNNING
        or attempt.started_at is None
        or attempt.created_at < node.created_at
        or attempt.attempt_number != len(node.attempt_history) + 1
    ):
        raise RepositoryError("invalid_attempt_start")
    params = {
        **_attempt(scope, execution_id, node_run_id, attempt.attempt_id),
        "record": body,
        "number": attempt.attempt_number,
        "job_id": job_id,
        "fence": fence,
        "lease_owner": lease_owner,
    }
    inserted = (
        await connection.execute(
            text("""
        INSERT INTO jimu_workflow.attempts
            (project_id, client_id, execution_id, node_run_id, attempt_id,
            attempt_number, job_id, fence, lease_owner, record)
        SELECT project_id, client_id, execution_id, node_run_id, :attempt_id,
            :number, job_id, fence, lease_owner, CAST(:record AS json)
        FROM jimu_workflow.jobs
        WHERE project_id = :project_id AND client_id = :client_id
            AND execution_id = :execution_id AND node_run_id = :node_run_id
            AND job_id = :job_id AND fence = :fence AND lease_owner = :lease_owner
            AND status = 'leased' AND current_attempt_id IS NULL AND attempt_count + 1 = :number
            AND :number <= max_attempts + (observation_wait_node_id IS NOT NULL)::integer
        RETURNING attempt_id
    """),
            params,
        )
    ).scalar_one_or_none()
    if inserted is None:
        raise RepositoryError("attempt_job_fence_mismatch")
    await append_event(
        connection,
        scope,
        execution_id,
        node.trace,
        AttemptTransitionEvent(
            event_kind="attempt_transition",
            attempt_ref=AttemptRef(
                record_kind=RuntimeRecordKind.ATTEMPT, record_id=attempt.attempt_id
            ),
            previous_status=AttemptStatus.CREATED,
            next_status=AttemptStatus.RUNNING,
        ),
        now=attempt.started_at,
    )


async def finish_attempt(
    connection: AsyncConnection,
    scope: IdempotencyScope,
    execution_id: str,
    node_run_id: str,
    previous: Attempt,
    following: Attempt,
    guard: AttemptTransitionGuard,
    *,
    now: datetime,
) -> None:
    body = _checked_json(following)
    validate_attempt_update(previous, following, guard)
    node = await get_node(connection, scope, execution_id, node_run_id)
    stored = await get_attempt(
        connection, scope, execution_id, node_run_id, previous.attempt_id
    )
    if node is None or stored != previous or following.ended_at != now:
        raise RepositoryError("attempt_finish_conflict")
    await connection.execute(
        text("""
        INSERT INTO jimu_workflow.attempt_results
            (project_id, client_id, execution_id, node_run_id, attempt_id, status, record)
        VALUES (:project_id, :client_id, :execution_id, :node_run_id, :attempt_id,
            :status, CAST(:record AS json))
    """),
        {
            **_attempt(scope, execution_id, node_run_id, following.attempt_id),
            "status": following.status.value,
            "record": body,
        },
    )
    await append_event(
        connection,
        scope,
        execution_id,
        node.trace,
        AttemptTransitionEvent(
            event_kind="attempt_transition",
            attempt_ref=AttemptRef(
                record_kind=RuntimeRecordKind.ATTEMPT, record_id=following.attempt_id
            ),
            previous_status=previous.status,
            next_status=following.status,
        ),
        now=now,
    )


async def transition_task(
    connection: AsyncConnection,
    scope: IdempotencyScope,
    execution_id: str,
    task_id: str,
    previous: TaskStatus,
    following: TaskStatus,
    *,
    now: datetime,
    trace: TraceContext,
) -> None:
    validate_task_transition(previous, following)
    changed = (
        await connection.execute(
            text("""
        UPDATE jimu_workflow.tasks AS t SET status = :following
        FROM jimu_workflow.executions AS e, jimu_workflow.intake_intents AS i
        WHERE (i.project_id, i.client_id, i.request_id) =
              (t.project_id, t.client_id, t.request_id)
            AND (i.execution_id IS NULL OR i.execution_id = e.execution_id)
            AND t.project_id = :project_id AND t.client_id = :client_id AND t.task_id = :task_id
            AND t.status = :previous AND e.project_id = t.project_id AND e.client_id = t.client_id
            AND e.task_id = t.task_id AND e.execution_id = :execution_id
        RETURNING t.task_id
    """),
            {
                **_execution(scope, execution_id),
                "task_id": task_id,
                "previous": previous.value,
                "following": following.value,
            },
        )
    ).scalar_one_or_none()
    if changed is None:
        raise RepositoryError("task_transition_conflict")
    await append_event(
        connection,
        scope,
        execution_id,
        trace,
        TaskTransitionEvent(
            event_kind="task_transition",
            task_id=task_id,
            previous_status=previous,
            next_status=following,
        ),
        now=now,
    )


async def transition_execution(
    connection: AsyncConnection,
    scope: IdempotencyScope,
    previous: ExecutionRecord,
    following: ExecutionRecord,
    *,
    now: datetime,
) -> None:
    following = ExecutionRecord.model_validate(following)
    validate_execution_update(previous, following)
    stored = await get_execution(connection, scope, previous.execution_id)
    if stored is None or stored.execution != previous:
        raise RepositoryError("execution_transition_conflict")
    changed = (
        await connection.execute(
            text("""
        UPDATE jimu_workflow.executions
        SET status = :following, state = CAST(:state AS json)
        WHERE project_id = :project_id AND client_id = :client_id AND execution_id = :execution_id
            AND status = :previous AND state::text = :previous_state
        RETURNING execution_id
    """),
            {
                **_execution(scope, previous.execution_id),
                "previous": previous.status.value,
                "following": following.status.value,
                "state": _state(following, _EXECUTION_STATE),
                "previous_state": _state(previous, _EXECUTION_STATE),
            },
        )
    ).scalar_one_or_none()
    if changed is None:
        raise RepositoryError("execution_transition_conflict")
    if previous.status is not following.status:
        await append_event(
            connection,
            scope,
            previous.execution_id,
            previous.trace,
            ExecutionTransitionEvent(
                event_kind="execution_transition",
                previous_status=previous.status,
                next_status=following.status,
            ),
            now=now,
        )


async def transition_node(
    connection: AsyncConnection,
    scope: IdempotencyScope,
    execution_id: str,
    previous: NodeRun,
    following: NodeRun,
    *,
    now: datetime,
    guard: NodeTransitionGuard | None = None,
    appended_attempt: Attempt | None = None,
    prior_attempt: Attempt | None = None,
    retry_authorized: bool = False,
) -> None:
    following = NodeRun.model_validate(following)
    validate_node_update(
        previous,
        following,
        guard=guard,
        appended_attempt=appended_attempt,
        prior_attempt=prior_attempt,
        retry_authorized=retry_authorized,
    )
    stored = await get_node(connection, scope, execution_id, previous.node_run_id)
    if stored != previous:
        raise RepositoryError("node_transition_conflict")
    for attempt_record in (appended_attempt, prior_attempt):
        if attempt_record is not None and attempt_record != await get_attempt(
            connection,
            scope,
            execution_id,
            previous.node_run_id,
            attempt_record.attempt_id,
        ):
            raise RepositoryError("node_attempt_state_mismatch")
    changed = (
        await connection.execute(
            text("""
        UPDATE jimu_workflow.node_runs
        SET status = :following, state = CAST(:state AS json)
        WHERE project_id = :project_id AND client_id = :client_id AND execution_id = :execution_id
            AND node_run_id = :node_run_id AND status = :previous AND state::text = :previous_state
        RETURNING node_run_id
    """),
            {
                **_node(scope, execution_id, previous.node_run_id),
                "previous": previous.status.value,
                "following": following.status.value,
                "state": _state(following, _NODE_STATE),
                "previous_state": _state(previous, _NODE_STATE),
            },
        )
    ).scalar_one_or_none()
    if changed is None:
        raise RepositoryError("node_transition_conflict")
    if previous.status is not following.status:
        await append_event(
            connection,
            scope,
            execution_id,
            previous.trace,
            NodeTransitionEvent(
                event_kind="node_transition",
                node_run_ref=NodeRunRef(
                    record_kind=RuntimeRecordKind.NODE_RUN,
                    record_id=previous.node_run_id,
                ),
                previous_status=previous.status,
                next_status=following.status,
            ),
            now=now,
        )


async def append_event(
    connection: AsyncConnection,
    scope: IdempotencyScope,
    execution_id: str,
    trace: TraceContext,
    payload: ExecutionEventPayload,
    *,
    now: datetime,
    attempt: Attempt | None = None,
) -> ExecutionEvent:
    params = _execution(scope, execution_id)
    execution_row = (
        (
            await connection.execute(
                text("""
        SELECT next_event_number, record::text AS record
        FROM jimu_workflow.executions
        WHERE project_id = :project_id AND client_id = :client_id AND execution_id = :execution_id
        FOR NO KEY UPDATE
    """),
                params,
            )
        )
        .mappings()
        .one_or_none()
    )
    if execution_row is None:
        raise RepositoryError("event_execution_not_found")
    execution = ExecutionRecord.model_validate_json(execution_row["record"])
    if execution.trace.trace_id != trace.trace_id or now < execution.created_at:
        raise RepositoryError("event_execution_mismatch")
    event_number: int = execution_row["next_event_number"]
    event = ExecutionEvent(
        schema_version="1.0.0",
        execution_event_id=uuid4().hex,
        execution_id=execution_id,
        event_number=event_number,
        trace=trace,
        recorded_at=now,
        payload=payload,
    )
    task_id = None
    node_run_id = None
    attempt_id = None
    match payload:
        case TaskTransitionEvent():
            task_id = payload.task_id
            if task_id != execution.task_id:
                raise RepositoryError("event_task_mismatch")
        case NodeTransitionEvent():
            node_run_id = payload.node_run_ref.record_id
        case AttemptTransitionEvent() | LateResponseEvent() | SystemOutputEvent():
            attempt_id = payload.attempt_ref.record_id
            node_run_id = (
                await connection.execute(
                    text("""
                SELECT node_run_id FROM jimu_workflow.attempts
                WHERE project_id = :project_id AND client_id = :client_id
                    AND execution_id = :execution_id AND attempt_id = :attempt_id
            """),
                    {**params, "attempt_id": attempt_id},
                )
            ).scalar_one_or_none()
            if node_run_id is None:
                raise RepositoryError("event_attempt_not_found")
            if isinstance(payload, LateResponseEvent | SystemOutputEvent):
                stored = await get_attempt(
                    connection, scope, execution_id, node_run_id, attempt_id
                )
                if stored is None or stored != attempt:
                    raise RepositoryError("event_attempt_mismatch")
    previous = (
        await connection.execute(
            text("""
        SELECT record::text FROM jimu_workflow.execution_events
        WHERE project_id = :project_id AND client_id = :client_id AND execution_id = :execution_id
            AND event_number = :previous_number
    """),
            {**params, "previous_number": event_number - 1},
        )
    ).scalar_one_or_none()
    if previous is None and event_number != 1:
        raise RepositoryError("event_sequence_gap")
    history = (
        (ExecutionEvent.model_validate_json(previous),) if previous is not None else ()
    )
    validate_event_append(history, event, attempt=attempt)
    await connection.execute(
        text("""
        INSERT INTO jimu_workflow.execution_events
            (project_id, client_id, execution_id, event_number, event_id, recorded_at,
            task_id, node_run_id, attempt_id, record)
        VALUES (:project_id, :client_id, :execution_id, :event_number, :event_id, :recorded_at,
            :task_id, :node_run_id, :attempt_id, CAST(:record AS json))
    """),
        {
            **params,
            "event_number": event_number,
            "event_id": event.execution_event_id,
            "recorded_at": now,
            "task_id": task_id,
            "node_run_id": node_run_id,
            "attempt_id": attempt_id,
            "record": event.stable_json(),
        },
    )
    await connection.execute(
        text("""
        UPDATE jimu_workflow.executions SET next_event_number = next_event_number + 1
        WHERE project_id = :project_id AND client_id = :client_id AND execution_id = :execution_id
    """),
        params,
    )
    return event


async def list_events(
    connection: AsyncConnection, scope: IdempotencyScope, execution_id: str
) -> tuple[ExecutionEvent, ...]:
    rows = (
        await connection.execute(
            text("""
        SELECT event_number, event_id, recorded_at, record::text AS record
        FROM jimu_workflow.execution_events
        WHERE project_id = :project_id AND client_id = :client_id AND execution_id = :execution_id
        ORDER BY event_number
    """),
            _execution(scope, execution_id),
        )
    ).mappings()
    events: list[ExecutionEvent] = []
    for row in rows:
        event = ExecutionEvent.model_validate_json(row["record"])
        if (
            event.execution_id != execution_id
            or event.event_number != len(events) + 1
            or event.event_number != row["event_number"]
            or event.execution_event_id != row["event_id"]
            or event.recorded_at != row["recorded_at"]
        ):
            raise RepositoryError("event_index_mismatch")
        events.append(event)
    return tuple(events)


def _output_body(
    record: ContractModel,
    execution_id: str,
    node_run_id: str | None,
    attempt_id: str | None,
) -> str:
    body = _checked_json(record)
    if getattr(record, "schema_version", None) != "1.0.0":
        raise RepositoryError("output_requires_schema_version")
    for field, value in (
        ("execution_id", execution_id),
        ("node_run_id", node_run_id),
        ("attempt_id", attempt_id),
    ):
        if value is not None and getattr(record, field, value) != value:
            raise RepositoryError("output_owner_mismatch")
    for field, value in (("node_run_ref", node_run_id), ("attempt_ref", attempt_id)):
        ref = getattr(record, field, None)
        if value is not None and ref is not None and ref.record_id != value:
            raise RepositoryError("output_owner_mismatch")
    return body


def _validate_names(*names: str) -> None:
    if any(not name or len(name) > 256 or "\x00" in name for name in names):
        raise RepositoryError("invalid_output_identity")


def _read_typed[T: ContractModel](
    model: type[T] | TypeAdapter[T],
    body: str,
    digest: str,
    execution_id: str,
    node_run_id: str | None = None,
    attempt_id: str | None = None,
) -> T:
    if _digest(body) != digest:
        raise RepositoryError("output_digest_mismatch")
    record = (
        model.validate_json(body)
        if isinstance(model, TypeAdapter)
        else model.model_validate_json(body)
    )
    if _output_body(record, execution_id, node_run_id, attempt_id) != body:
        raise RepositoryError("output_representation_mismatch")
    return record


async def store_output(
    connection: AsyncConnection,
    scope: IdempotencyScope,
    execution_id: str,
    node_run_id: str,
    attempt_id: str,
    *,
    output_key: str,
    output_kind: str,
    record: ContractModel,
) -> str:
    _validate_names(output_key, output_kind)
    body = _output_body(record, execution_id, node_run_id, attempt_id)
    digest = _digest(body)
    params = {
        **_attempt(scope, execution_id, node_run_id, attempt_id),
        "output_key": output_key,
        "output_kind": output_kind,
        "digest": digest,
        "record": body,
    }
    await connection.execute(
        text("""
        INSERT INTO jimu_workflow.produced_outputs
            (project_id, client_id, execution_id, node_run_id, attempt_id, output_key,
            output_kind, output_digest, record)
        VALUES (:project_id, :client_id, :execution_id, :node_run_id, :attempt_id, :output_key,
            :output_kind, :digest, CAST(:record AS json))
        ON CONFLICT DO NOTHING
    """),
        params,
    )
    row = (
        await connection.execute(
            text("""
        SELECT record::text AS record, output_digest, output_kind
        FROM jimu_workflow.produced_outputs
        WHERE project_id = :project_id AND client_id = :client_id AND execution_id = :execution_id
            AND node_run_id = :node_run_id AND attempt_id = :attempt_id AND output_key = :output_key
    """),
            params,
        )
    ).one()
    if (row.record, row.output_digest, row.output_kind) != (body, digest, output_kind):
        raise RepositoryError("output_identity_conflict")
    return digest


async def read_output[T: ContractModel](
    connection: AsyncConnection,
    scope: IdempotencyScope,
    execution_id: str,
    node_run_id: str,
    attempt_id: str,
    output_key: str,
    model: type[T] | TypeAdapter[T],
) -> T | None:
    row = (
        await connection.execute(
            text("""
        SELECT record::text AS record, output_digest
        FROM jimu_workflow.produced_outputs
        WHERE project_id = :project_id AND client_id = :client_id AND execution_id = :execution_id
            AND node_run_id = :node_run_id AND attempt_id = :attempt_id AND output_key = :output_key
    """),
            {
                **_attempt(scope, execution_id, node_run_id, attempt_id),
                "output_key": output_key,
            },
        )
    ).one_or_none()
    return (
        _read_typed(
            model, row.record, row.output_digest, execution_id, node_run_id, attempt_id
        )
        if row
        else None
    )


async def select_authority(
    connection: AsyncConnection,
    scope: IdempotencyScope,
    execution_id: str,
    node_run_id: str,
    attempt_id: str,
    *,
    output_key: str,
    output_digest: str,
) -> None:
    if output_key != "node.output":
        raise RepositoryError("invalid_authority_output_key")
    node = await get_node(connection, scope, execution_id, node_run_id)
    output = await read_output(
        connection,
        scope,
        execution_id,
        node_run_id,
        attempt_id,
        output_key,
        SucceededNodeOutput,
    )
    if (
        node is None
        or output is None
        or output.node_id is not node.node_id
        or output.trace.trace_id != node.trace.trace_id
        or _digest(output.stable_json()) != output_digest
    ):
        raise RepositoryError("invalid_authority_output")
    params = {
        **_attempt(scope, execution_id, node_run_id, attempt_id),
        "output_key": output_key,
        "output_digest": output_digest,
    }
    await connection.execute(
        text("""
        INSERT INTO jimu_workflow.authority_outputs
            (project_id, client_id, execution_id, node_run_id, attempt_id, output_key, output_digest)
        VALUES (:project_id, :client_id, :execution_id, :node_run_id, :attempt_id, :output_key, :output_digest)
        ON CONFLICT DO NOTHING
    """),
        params,
    )
    row = (
        await connection.execute(
            text("""
        SELECT attempt_id, output_key, output_digest FROM jimu_workflow.authority_outputs
        WHERE project_id = :project_id AND client_id = :client_id AND execution_id = :execution_id
            AND node_run_id = :node_run_id
    """),
            params,
        )
    ).one()
    if (row.attempt_id, row.output_key, row.output_digest) != (
        attempt_id,
        output_key,
        output_digest,
    ):
        raise RepositoryError("authority_selection_conflict")


async def read_authority[T: ContractModel](
    connection: AsyncConnection,
    scope: IdempotencyScope,
    execution_id: str,
    node_run_id: str,
    model: type[T] | TypeAdapter[T],
) -> T | None:
    row = (
        await connection.execute(
            text("""
        SELECT p.record::text AS record, output_digest, attempt_id
        FROM jimu_workflow.authority_outputs AS a
        JOIN jimu_workflow.produced_outputs AS p
            USING (project_id, client_id, execution_id, node_run_id, attempt_id,
                output_key, output_digest, output_kind)
        WHERE a.project_id = :project_id AND a.client_id = :client_id AND a.execution_id = :execution_id
            AND a.node_run_id = :node_run_id
    """),
            _node(scope, execution_id, node_run_id),
        )
    ).one_or_none()
    return (
        _read_typed(
            model,
            row.record,
            row.output_digest,
            execution_id,
            node_run_id,
            row.attempt_id,
        )
        if row
        else None
    )


async def store_engine_output(
    connection: AsyncConnection,
    scope: IdempotencyScope,
    execution_id: str,
    node_run_id: str,
    *,
    output_key: str,
    output_kind: str,
    record: ContractModel,
) -> str:
    _validate_names(output_key, output_kind)
    body = _output_body(record, execution_id, node_run_id, None)
    digest = _digest(body)
    params = {
        **_node(scope, execution_id, node_run_id),
        "output_key": output_key,
        "output_kind": output_kind,
        "digest": digest,
        "record": body,
    }
    await connection.execute(
        text("""
        INSERT INTO jimu_workflow.engine_outputs
            (project_id, client_id, execution_id, node_run_id, output_key, output_kind, output_digest, record)
        VALUES (:project_id, :client_id, :execution_id, :node_run_id, :output_key, :output_kind, :digest, CAST(:record AS json))
        ON CONFLICT DO NOTHING
    """),
        params,
    )
    row = (
        await connection.execute(
            text("""
        SELECT record::text AS record, output_digest, output_kind
        FROM jimu_workflow.engine_outputs
        WHERE project_id = :project_id AND client_id = :client_id AND execution_id = :execution_id
            AND node_run_id = :node_run_id AND output_key = :output_key
    """),
            params,
        )
    ).one()
    if (row.record, row.output_digest, row.output_kind) != (body, digest, output_kind):
        raise RepositoryError("engine_output_identity_conflict")
    return digest


async def read_engine_output[T: ContractModel](
    connection: AsyncConnection,
    scope: IdempotencyScope,
    execution_id: str,
    node_run_id: str,
    output_key: str,
    model: type[T] | TypeAdapter[T],
) -> T | None:
    row = (
        await connection.execute(
            text("""
        SELECT record::text AS record, output_digest
        FROM jimu_workflow.engine_outputs
        WHERE project_id = :project_id AND client_id = :client_id AND execution_id = :execution_id
            AND node_run_id = :node_run_id AND output_key = :output_key
    """),
            {**_node(scope, execution_id, node_run_id), "output_key": output_key},
        )
    ).one_or_none()
    return (
        _read_typed(model, row.record, row.output_digest, execution_id, node_run_id)
        if row
        else None
    )


async def store_decision_draft(
    connection: AsyncConnection,
    scope: IdempotencyScope,
    execution_id: str,
    node_run_id: str,
    attempt_id: str,
    *,
    job_id: str,
    fence: int,
    draft_key: str,
    record: ContractModel,
) -> str:
    _validate_names(draft_key)
    node = await get_node(connection, scope, execution_id, node_run_id)
    if node is None or node.node_id is not NodeId.N06:
        raise RepositoryError("decision_draft_requires_n06")
    body = _output_body(record, execution_id, node_run_id, attempt_id)
    digest = _digest(body)
    params = {
        **_attempt(scope, execution_id, node_run_id, attempt_id),
        "job_id": job_id,
        "fence": fence,
        "draft_key": draft_key,
        "digest": digest,
        "record": body,
    }
    await connection.execute(
        text("""
        INSERT INTO jimu_workflow.decision_drafts
            (project_id, client_id, execution_id, node_run_id, attempt_id,
            job_id, fence, draft_key, draft_digest, record)
        VALUES (:project_id, :client_id, :execution_id, :node_run_id, :attempt_id,
            :job_id, :fence, :draft_key, :digest, CAST(:record AS json))
        ON CONFLICT DO NOTHING
    """),
        params,
    )
    row = (
        await connection.execute(
            text("""
        SELECT record::text AS record, draft_digest, draft_key, job_id, fence
        FROM jimu_workflow.decision_drafts
        WHERE project_id = :project_id AND client_id = :client_id AND execution_id = :execution_id
            AND node_run_id = :node_run_id AND attempt_id = :attempt_id
    """),
            params,
        )
    ).one_or_none()
    if row is None or (
        row.record,
        row.draft_digest,
        row.draft_key,
        row.job_id,
        row.fence,
    ) != (body, digest, draft_key, job_id, fence):
        raise RepositoryError("decision_draft_identity_conflict")
    return digest


async def read_decision_draft[T: ContractModel](
    connection: AsyncConnection,
    scope: IdempotencyScope,
    execution_id: str,
    node_run_id: str,
    attempt_id: str,
    model: type[T] | TypeAdapter[T],
) -> T | None:
    row = (
        await connection.execute(
            text("""
        SELECT record::text AS record, draft_digest
        FROM jimu_workflow.decision_drafts
        WHERE project_id = :project_id AND client_id = :client_id AND execution_id = :execution_id
            AND node_run_id = :node_run_id AND attempt_id = :attempt_id
    """),
            _attempt(scope, execution_id, node_run_id, attempt_id),
        )
    ).one_or_none()
    return (
        _read_typed(
            model, row.record, row.draft_digest, execution_id, node_run_id, attempt_id
        )
        if row
        else None
    )


async def store_finalization(
    connection: AsyncConnection,
    scope: IdempotencyScope,
    execution_id: str,
    record: ContractModel,
) -> str:
    body = _output_body(record, execution_id, None, None)
    digest = _digest(body)
    params = {**_execution(scope, execution_id), "digest": digest, "record": body}
    await connection.execute(
        text("""
        INSERT INTO jimu_workflow.execution_finalizations
            (project_id, client_id, execution_id, finalization_digest, record)
        VALUES (:project_id, :client_id, :execution_id, :digest, CAST(:record AS json))
        ON CONFLICT DO NOTHING
    """),
        params,
    )
    row = (
        await connection.execute(
            text("""
        SELECT record::text AS record, finalization_digest FROM jimu_workflow.execution_finalizations
        WHERE project_id = :project_id AND client_id = :client_id AND execution_id = :execution_id
    """),
            params,
        )
    ).one()
    if (row.record, row.finalization_digest) != (body, digest):
        raise RepositoryError("finalization_identity_conflict")
    return digest


async def read_finalization[T: ContractModel](
    connection: AsyncConnection,
    scope: IdempotencyScope,
    execution_id: str,
    model: type[T] | TypeAdapter[T],
) -> T | None:
    row = (
        await connection.execute(
            text("""
        SELECT record::text AS record, finalization_digest FROM jimu_workflow.execution_finalizations
        WHERE project_id = :project_id AND client_id = :client_id AND execution_id = :execution_id
    """),
            _execution(scope, execution_id),
        )
    ).one_or_none()
    return (
        _read_typed(model, row.record, row.finalization_digest, execution_id)
        if row
        else None
    )


async def link_successor(
    connection: AsyncConnection,
    scope: IdempotencyScope,
    execution_id: str,
    upstream_node_run_id: str,
    downstream_node_run_id: str,
) -> bool:
    result = await connection.execute(
        text("""
        INSERT INTO jimu_workflow.successors
            (project_id, client_id, execution_id, upstream_node_run_id, downstream_node_run_id)
        VALUES (:project_id, :client_id, :execution_id, :upstream, :downstream)
        ON CONFLICT DO NOTHING
        RETURNING downstream_node_run_id
    """),
        {
            **_execution(scope, execution_id),
            "upstream": upstream_node_run_id,
            "downstream": downstream_node_run_id,
        },
    )
    return result.scalar_one_or_none() is not None
