"""Apply fenced artifacts and close one Execution with its shared Coverage ledger."""

from collections.abc import Callable
from datetime import datetime

from sqlalchemy.ext.asyncio import AsyncConnection

from jimu_runtime.domain.enums import ExecutionStatus, NodeRunStatus, TaskStatus
from jimu_runtime.domain.nodes.context import SuccessfulContextBuild
from jimu_runtime.domain.nodes.envelopes import FailedNodeOutput, SucceededNodeOutput
from jimu_runtime.domain.references import RuntimeRecordRef
from jimu_runtime.domain.runtime.applicability import UnresolvedObligation
from jimu_runtime.domain.runtime.authority import AuthoritativeResultSelection
from jimu_runtime.domain.runtime.evidence import EvidenceAsset
from jimu_runtime.domain.runtime.executions import Attempt, ContextSnapshotPin, NodeRun
from jimu_runtime.domain.runtime.facts import FactRecord
from jimu_runtime.domain.runtime.intake import IdempotencyScope
from jimu_runtime.domain.runtime.invocations import ModelInvocation
from jimu_runtime.domain.runtime.observations import ObservationBatch
from jimu_runtime.domain.runtime.perception import GroundingOutcome
from jimu_runtime.domain.runtime.tool_executions import ToolExecution
from jimu_runtime.domain.validation.runtime_authority import RuntimeAuthorityRecords
from jimu_runtime.tools.step_attempts import load_parent_step_history
from jimu_runtime.workflow import repository as repo
from jimu_runtime.workflow.engine import DecisionDraft, plan_work
from jimu_runtime.workflow.jobs import StagedDecision
from jimu_runtime.workflow.mock_decision import finish_decision
from jimu_runtime.workflow.mock_support import K, SyntheticArtifact, artifact
from jimu_runtime.workflow.worker_coverage import (
    build_coverage_authority,
    finalize_execution,
)
from jimu_runtime.workflow.worker_nodes import NodeResult
from jimu_runtime.workflow.worker_records import (
    MESSAGE_KEY,
    ContextMessage,
    DecisionMessage,
    DiscoveryMessage,
    GroundingMessage,
    ObservationMessage,
    ToolMessage,
)
from jimu_runtime.workflow.worker_source import OpenExecution
from jimu_runtime.workflow.worker_state import (
    WorkerRecords,
    load_attempts,
    load_records,
)
from jimu_runtime.workflow.worker_validation import check_tool_history


async def persist_result(
    connection: AsyncConnection,
    scope: IdempotencyScope,
    locked: repo.LockedExecution,
    node: NodeRun,
    attempt: Attempt,
    outcome: SucceededNodeOutput | FailedNodeOutput,
    now: datetime,
    result: NodeResult,
    *,
    configured: bool = False,
) -> None:
    execution = locked.execution
    message = result.message
    env = message.input_envelope()
    if (
        message.output_envelope() != outcome
        or message.worker_format is not None
        or (env.execution_id, env.node_run_id, env.attempt_id)
        != (node.execution_id, node.node_run_id, attempt.attempt_id)
        or not {pin for pin in outcome.output_artifacts}
        <= {item.pin for item in result.artifacts}
        or len({item.ref for item in result.artifacts}) != len(result.artifacts)
    ):
        raise ValueError(
            "validated node result requires the exact fenced artifact manifest"
        )
    archived: dict[RuntimeRecordRef, SyntheticArtifact] = {}
    if isinstance(message, ToolMessage):
        history = await load_parent_step_history(connection, scope, message.inputs)
        if history is not None:
            check_tool_history(message, history)
            for entry in history.entries:
                execution_record = entry.execution
                for child_artifact in (
                    artifact(
                        K.TOOL_EXECUTION,
                        execution_record.tool_execution_id,
                        execution_record,
                    ),
                    *(artifact(K.FACT, row.fact_id, row) for row in entry.facts),
                    *(
                        artifact(K.EVIDENCE, row.evidence_id, row)
                        for row in entry.evidence
                    ),
                    *(
                        artifact(K.MODEL_INVOCATION, row.model_invocation_id, row)
                        for row in entry.invocations
                    ),
                    *(
                        ()
                        if entry.selection is None
                        else (
                            artifact(
                                K.AUTHORITATIVE_RESULT_SELECTION,
                                entry.selection.selection_id,
                                entry.selection,
                            ),
                        )
                    ),
                ):
                    existing = archived.setdefault(child_artifact.ref, child_artifact)
                    if existing != child_artifact:
                        raise ValueError(
                            "child outputs disagree for a repeated artifact reference"
                        )
    await repo.store_output(
        connection,
        scope,
        node.execution_id,
        node.node_run_id,
        attempt.attempt_id,
        output_key=MESSAGE_KEY,
        output_kind="worker_message",
        record=message.model_copy(update={"worker_format": "configured-v1"})
        if configured
        else message,
    )
    for item in result.artifacts:
        original = archived.get(item.ref)
        if original is not None:
            if item != original:
                raise ValueError(
                    "parent manifest conflicts with the archived child result"
                )
            continue
        await repo.store_output(
            connection,
            scope,
            node.execution_id,
            node.node_run_id,
            attempt.attempt_id,
            output_key=f"{item.ref.record_kind.value}.{item.ref.record_id}",
            output_kind=item.ref.record_kind.value,
            record=item.record,
        )
    pin = execution.context_snapshot
    if isinstance(message, ContextMessage) and isinstance(
        message.output, SuccessfulContextBuild
    ):
        pin = ContextSnapshotPin(
            snapshot_ref=message.output.context_snapshot_ref,
            snapshot_digest=message.output.context_snapshot_digest,
        )
    added_invocations = tuple(
        ref for ref in result.model_refs if ref not in execution.model_invocation_refs
    )
    if pin != execution.context_snapshot or added_invocations:
        following = execution.model_copy(
            update={
                "context_snapshot": pin,
                "model_invocation_refs": (
                    *execution.model_invocation_refs,
                    *added_invocations,
                ),
            }
        )
        await repo.transition_execution(
            connection, scope, execution, following, now=now
        )


def _terminal(data: WorkerRecords) -> tuple[ExecutionStatus, TaskStatus]:
    failure = any(
        node.status is NodeRunStatus.FAILED for node in data.runs.values()
    ) or any(
        isinstance(message, DiscoveryMessage)
        and message.output.outcome == "partial"
        or isinstance(message, ObservationMessage)
        and message.batch.outcome == "partial"
        or isinstance(message, ToolMessage)
        and message.output.summary.status == "partial"
        or isinstance(message, GroundingMessage)
        and (
            bool(message.undispatched_profiles)
            or any(
                record.outcome in (GroundingOutcome.PARTIAL, GroundingOutcome.FAILED)
                for record in message.records
            )
        )
        for message in data.messages.values()
    )
    if not failure:
        return ExecutionStatus.COMPLETED, TaskStatus.COMPLETED
    if any(isinstance(message, DecisionMessage) for message in data.messages.values()):
        return ExecutionStatus.PARTIAL_FAILED, TaskStatus.PARTIAL_FAILED
    return ExecutionStatus.FAILED, TaskStatus.FAILED


def runtime_authority_records(
    opened: OpenExecution,
    data: WorkerRecords,
    attempts: tuple[Attempt, ...],
    *,
    object_id: str | None = None,
) -> tuple[RuntimeAuthorityRecords, tuple[EvidenceAsset, ...]]:
    """Project typed producer results from one scoped PostgreSQL snapshot."""
    sources = data.coverage_sources(attempts)
    if data.gate is None:
        raise ValueError("authority requires the selected Profile gate")
    facts: list[FactRecord] = []
    authorities: list[AuthoritativeResultSelection] = []
    evidence: dict[str, EvidenceAsset] = {}
    invocations: list[ModelInvocation] = []
    observations: list[ObservationBatch] = []
    tool_executions: list[ToolExecution] = []
    for branch, message in data.messages.items():
        if object_id is not None and branch.object_id not in {None, object_id}:
            continue
        if isinstance(message, DiscoveryMessage):
            invocations.extend(message.invocations)
        elif isinstance(message, GroundingMessage):
            invocations.extend(message.invocations)
            invocations.extend(message.nested_invocations)
            tool_executions.extend(message.tool_executions)
        elif isinstance(message, ObservationMessage):
            facts.extend(message.facts)
            if message.authority is not None:
                authorities.append(message.authority)
            invocations.append(message.invocation)
            observations.append(message.batch)
        elif isinstance(message, ToolMessage):
            facts.extend(message.facts)
            authorities.extend(message.authorities)
            tool_executions.extend(message.executions)
            invocations.extend(message.nested_invocations)
        elif isinstance(message, DecisionMessage) and message.invocation is not None:
            invocations.append(message.invocation)
        if isinstance(
            message,
            GroundingMessage | ObservationMessage | ToolMessage | DecisionMessage,
        ):
            for item in message.evidence:
                if evidence.setdefault(item.evidence_id, item) != item:
                    raise ValueError(
                        "durable Evidence identity has conflicting metadata"
                    )
    records_objects = sources.objects
    records_nodes = sources.node_runs
    if object_id is not None:
        records_objects = tuple(
            item for item in records_objects if item.object_id == object_id
        )
        tool_node_ids = {item.node_run_ref.record_id for item in tool_executions}
        records_nodes = (
            *(
                run
                for branch, run in data.runs.items()
                if branch.object_id in {None, object_id}
            ),
            *(run for run in data.tool_step_nodes if run.node_run_id in tool_node_ids),
        )
        records_attempt_ids = {
            link.attempt_ref.record_id
            for run in records_nodes
            for link in run.attempt_history
        }
        attempts = tuple(
            item for item in attempts if item.attempt_id in records_attempt_ids
        )
    return RuntimeAuthorityRecords(
        execution=data.locked.execution,
        release=opened.release,
        manifest=opened.resolved.files.manifest,
        eligibility=data.gate.evaluation,
        objects=records_objects,
        node_runs=records_nodes,
        attempts=attempts,
        question_selections=sources.question_selections,
        observations=tuple(observations),
        tool_executions=tuple(tool_executions),
        model_invocations=tuple(invocations),
        execution_events=(),
        facts=tuple(facts),
        authority_selections=tuple(authorities),
    ), tuple(evidence.values())


async def finalize(
    connection: AsyncConnection,
    scope: IdempotencyScope,
    locked: repo.LockedExecution,
    staged: tuple[StagedDecision, ...],
    now: datetime,
    opened: OpenExecution,
    *,
    decision_unresolved: Callable[[DecisionMessage], tuple[UnresolvedObligation, ...]]
    | None = None,
) -> bool:
    eid = locked.execution.execution_id
    data = await load_records(connection, scope, locked)
    state = data.planner_state(opened)
    work = plan_work(state).coverage
    if not work.ready_to_finalize:
        return False
    decision_messages = tuple(
        item for item in data.messages.values() if isinstance(item, DecisionMessage)
    )
    stages = tuple(item.result() for item in decision_messages)
    if {item.draft for item in stages} != {
        DecisionDraft.model_validate_json(item.draft_json) for item in staged
    }:
        raise ValueError(
            "execution publication requires the exact fenced set of staged Decisions"
        )
    attempts = await load_attempts(connection, scope, data)
    sources = data.coverage_sources(attempts)
    gate = None if data.gate is None else data.gate.result()
    records, evidence = (
        runtime_authority_records(opened, data, attempts)
        if gate is not None
        else (None, ())
    )
    authority = (
        build_coverage_authority(opened.resolved, records)
        if records is not None and (records.facts or records.authority_selections)
        else None
    )
    result = finalize_execution(
        state,
        work,
        scope=opened.scope,
        sources=sources,
        gate=gate,
        stages=stages,
        facts=() if records is None else records.facts,
        authorities=() if records is None else records.authority_selections,
        evidence=evidence,
        invocations=() if records is None else records.model_invocations,
        authority=authority,
        tool_step_nodes=data.tool_step_nodes,
        additional_unresolved=(
            ()
            if decision_unresolved is None
            else tuple(
                row
                for message in decision_messages
                for row in decision_unresolved(message)
            )
        ),
        now=now,
    )
    if gate is not None:
        for stage in stages:
            decision_output = finish_decision(
                opened.scope, stage, result.coverage, gate
            )
            env = decision_output.envelope
            await repo.store_output(
                connection,
                scope,
                eid,
                env.node_run_id,
                env.attempt_id,
                output_key="worker.decision.final",
                output_kind="decision_output",
                record=decision_output,
            )
    await repo.store_finalization(connection, scope, eid, result)
    status, task_status = _terminal(data)
    execution = state.execution
    await repo.transition_execution(
        connection,
        scope,
        execution,
        execution.model_copy(update={"status": status, "ended_at": now}),
        now=now,
    )
    if not locked.is_inference_replay:
        await repo.transition_task(
            connection,
            scope,
            eid,
            execution.task_id,
            locked.task_status,
            task_status,
            now=now,
            trace=execution.trace,
        )
    return True
