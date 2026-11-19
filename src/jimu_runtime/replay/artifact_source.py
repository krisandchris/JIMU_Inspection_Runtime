"""Read and verify the frozen workflow records consumed by artifact scoring."""

from dataclasses import dataclass

from pydantic import TypeAdapter
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from jimu_runtime.domain.base import ContractModel
from jimu_runtime.domain.enums import ExecutionStatus, RuntimeRecordKind
from jimu_runtime.domain.nodes.decision import DecisionOutput
from jimu_runtime.domain.nodes.envelopes import NodeOutputEnvelope
from jimu_runtime.domain.references import RuntimeRecordRef
from jimu_runtime.domain.runtime.authority import (
    AuthoritativeResultSelection,
    ObservationBatchSelection,
    ToolStepSelection,
)
from jimu_runtime.domain.runtime.coverage import EvaluatedCoverageScope
from jimu_runtime.domain.runtime.decisions import (
    BlockedRuleOutcome,
    EvaluatedRuleOutcome,
)
from jimu_runtime.domain.runtime.executions import (
    ExecutionRecord,
    NodeId,
    PinnedRuntimeInput,
)
from jimu_runtime.domain.runtime.facts import (
    ErrorFact,
    FactRecord,
    KnownFact,
    NotApplicableFact,
    NotEvaluatedFact,
    UnknownFact,
)
from jimu_runtime.domain.runtime.intake import IdempotencyScope
from jimu_runtime.domain.runtime.invocations import ModelInvocation
from jimu_runtime.domain.runtime.observations import ObservationBatch
from jimu_runtime.domain.runtime.perception import ObjectRecord
from jimu_runtime.domain.runtime.replay import ReplayExecutionSource
from jimu_runtime.domain.runtime.tool_executions import ToolExecution
from jimu_runtime.tools.step_attempts import load_parent_step_history
from jimu_runtime.workflow import repository as repo
from jimu_runtime.workflow.engine import DecisionDraft, ExecutionFinalization
from jimu_runtime.workflow.jobs import NODE_OUTPUT_KEY
from jimu_runtime.workflow.mock_support import record_digest
from jimu_runtime.workflow.worker_records import (
    ContextMessage,
    DecisionMessage,
    DiscoveryMessage,
    GroundingMessage,
    NodeMessage,
    ObservationMessage,
    ToolMessage,
)
from jimu_runtime.workflow.worker_state import load_records, read_context_snapshot
from jimu_runtime.workflow.worker_validation import check_tool_history

K = RuntimeRecordKind
# One entry in the v1 request is reserved for the aggregated witness.
_MAX_NORMALIZED_ARTIFACTS = 1023
_MAX_BRANCHES = 1024
_NODE_OUTPUT = TypeAdapter[NodeOutputEnvelope](NodeOutputEnvelope)
FACT_TYPES = (ErrorFact, KnownFact, NotApplicableFact, NotEvaluatedFact, UnknownFact)
type ArtifactKey = tuple[RuntimeRecordKind, str]


class ArtifactReplayError(ValueError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True, slots=True)
class HistoricalSource:
    execution: ExecutionRecord
    finalization: ExecutionFinalization
    artifacts: tuple[PinnedRuntimeInput, ...]
    facts: tuple[FactRecord, ...]
    models: tuple[ModelInvocation, ...]
    tools: tuple[ToolExecution, ...]


def _ref(kind: RuntimeRecordKind, identity: str) -> RuntimeRecordRef:
    return RuntimeRecordRef(record_kind=kind, record_id=identity)


def _key(ref: RuntimeRecordRef) -> ArtifactKey:
    return ref.record_kind, ref.record_id


def _pin(ref: RuntimeRecordRef, item: ContractModel) -> PinnedRuntimeInput:
    return PinnedRuntimeInput(ref=ref, digest=record_digest(item))


def _models(rows: tuple[ModelInvocation, ...]) -> dict[ArtifactKey, ContractModel]:
    return {(K.MODEL_INVOCATION, item.model_invocation_id): item for item in rows}


def _tools(rows: tuple[ToolExecution, ...]) -> dict[ArtifactKey, ContractModel]:
    return {(K.TOOL_EXECUTION, item.tool_execution_id): item for item in rows}


def _facts(rows: tuple[FactRecord, ...]) -> dict[ArtifactKey, ContractModel]:
    return {(K.FACT, item.fact_id): item for item in rows}


def _selections(
    rows: tuple[AuthoritativeResultSelection, ...],
) -> dict[ArtifactKey, ContractModel]:
    return {
        (K.AUTHORITATIVE_RESULT_SELECTION, item.selection_id): item for item in rows
    }


def _message_records(message: NodeMessage) -> dict[ArtifactKey, ContractModel]:
    if isinstance(message, ContextMessage) and message.snapshot is not None:
        return {
            (K.CONTEXT_SNAPSHOT, message.snapshot.context_snapshot_id): message.snapshot
        }
    if isinstance(message, DiscoveryMessage):
        return {
            **{
                (K.QUESTION_SELECTION_RECORD, item.selection_id): item
                for item in message.selections
            },
            **_models(message.invocations),
        }
    if isinstance(message, GroundingMessage):
        return {
            **{(K.OBJECT, item.object_id): item for item in message.objects},
            **{
                (K.GROUNDING_RECORD, item.grounding_record_id): item
                for item in message.records
            },
            **_models((*message.invocations, *message.nested_invocations)),
            **_tools(message.tool_executions),
        }
    if isinstance(message, ObservationMessage):
        return {
            (K.OBSERVATION_BATCH, message.batch.observation_batch_id): message.batch,
            **_facts(message.facts),
            **_models((message.invocation,)),
            **_selections(() if message.authority is None else (message.authority,)),
        }
    if isinstance(message, ToolMessage):
        return {
            **_facts(message.facts),
            **_tools(message.executions),
            **_selections(message.authorities),
            **_models(message.nested_invocations),
        }
    if isinstance(message, DecisionMessage):
        return {
            (K.DECISION, message.draft.decision.decision_id): message.draft.decision,
            **_models(() if message.invocation is None else (message.invocation,)),
        }
    return {}


async def _verified_message_records(
    connection: AsyncConnection,
    scope: IdempotencyScope,
    eid: str,
    message: NodeMessage,
    records: dict[ArtifactKey, ContractModel],
) -> None:
    env = message.output_envelope()
    pins = {_key(pin.ref): pin.digest for pin in env.output_artifacts}
    child_records: dict[ArtifactKey, ContractModel] = {}
    if isinstance(message, ToolMessage):
        history = await load_parent_step_history(connection, scope, message.inputs)
        if history is not None:
            check_tool_history(message, history)
            for entry in history.entries:
                child_records.update(_facts(entry.facts))
                child_records.update(_tools((entry.execution,)))
                child_records.update(_models(entry.invocations))
                if entry.selection is not None:
                    child_records.update(_selections((entry.selection,)))
    for (kind, identity), record in records.items():
        key = (kind, identity)
        if (kind is not K.AUTHORITATIVE_RESULT_SELECTION or key in pins) and pins.get(
            key
        ) != record_digest(record):
            raise ArtifactReplayError("artifact_manifest_digest_mismatch")
        if key in child_records:
            # The child reader verifies the exact archived output and bytes digest.
            if child_records[key] != record:
                raise ArtifactReplayError("artifact_child_output_mismatch")
            continue
        persisted = await repo.read_output(
            connection,
            scope,
            eid,
            env.node_run_id,
            env.attempt_id,
            f"{kind.value}.{identity}",
            type(record),
        )
        if persisted is None:
            raise ArtifactReplayError("artifact_source_output_missing")
        if persisted != record:
            raise ArtifactReplayError("artifact_source_output_mismatch")


async def _archived_models(
    connection: AsyncConnection, scope: IdempotencyScope, eid: str, ids: set[str]
) -> dict[ArtifactKey, ContractModel]:
    """Find invocations appended to Execution before a later Attempt won."""
    if not ids:
        return {}
    owners = await connection.execute(
        text("""
            SELECT node_run_id, attempt_id, output_key
            FROM jimu_workflow.produced_outputs
            WHERE (project_id, client_id, execution_id)
                = (:project, :client, :execution)
                AND output_kind = 'model_invocation' AND output_key = ANY(:keys)
            LIMIT :maximum
        """),
        {
            "project": scope.project_id,
            "client": scope.client_id,
            "execution": eid,
            "keys": [f"model_invocation.{identity}" for identity in ids],
            "maximum": len(ids) + 1,
        },
    )
    archived: dict[ArtifactKey, ContractModel] = {}
    for nid, aid, key in owners:
        record = await repo.read_output(
            connection, scope, eid, nid, aid, key, ModelInvocation
        )
        output = await repo.read_output(
            connection, scope, eid, nid, aid, NODE_OUTPUT_KEY, _NODE_OUTPUT
        )
        if (
            record is None
            or output is None
            or record.model_invocation_id not in ids
            or (record.node_run_ref.record_id, record.attempt_ref.record_id)
            != (nid, aid)
            or key != f"model_invocation.{record.model_invocation_id}"
            or not any(
                pin.ref.record_kind is K.MODEL_INVOCATION
                and pin.ref.record_id == record.model_invocation_id
                and pin.digest == record_digest(record)
                for pin in output.output_artifacts
            )
        ):
            raise ArtifactReplayError("artifact_archived_model_mismatch")
        identity = K.MODEL_INVOCATION, record.model_invocation_id
        if identity in archived:
            raise ArtifactReplayError("artifact_archived_model_ambiguous")
        archived[identity] = record
    if len(archived) != len(ids):
        raise ArtifactReplayError("artifact_archived_model_missing")
    return archived


async def _check_decision_output(
    connection: AsyncConnection,
    scope: IdempotencyScope,
    final: ExecutionFinalization,
    message: DecisionMessage,
) -> None:
    env = message.output_envelope()
    output = await repo.read_output(
        connection,
        scope,
        final.execution_id,
        env.node_run_id,
        env.attempt_id,
        "worker.decision.final",
        DecisionOutput,
    )
    draft = await repo.read_decision_draft(
        connection,
        scope,
        final.execution_id,
        env.node_run_id,
        env.attempt_id,
        DecisionDraft,
    )
    if output is None or draft is None:
        raise ArtifactReplayError("artifact_decision_publication_missing")
    if (
        draft != message.draft
        or output.envelope != env
        or output.decision_ref != message.draft.decision.decision_ref
        or output.execution_coverage_ref != final.coverage.coverage_ref
        or output.mandatory_rule_coverage != final.coverage.mandatory_rule_coverage
    ):
        raise ArtifactReplayError("artifact_decision_publication_mismatch")


async def _check_branch_budget(
    connection: AsyncConnection, scope: IdempotencyScope, eid: str
) -> None:
    branches = await connection.scalar(
        text("""
            SELECT count(*) FROM (
                SELECT 1 FROM jimu_workflow.node_runs
                WHERE (project_id, client_id, execution_id) = (:project, :client, :execution)
                LIMIT :maximum
            ) AS bounded
        """),
        {
            "project": scope.project_id,
            "client": scope.client_id,
            "execution": eid,
            "maximum": _MAX_BRANCHES + 1,
        },
    )
    if branches > _MAX_BRANCHES:
        raise ArtifactReplayError("artifact_replay_history_work_limit")


async def load_historical_source(
    connection: AsyncConnection,
    scope: IdempotencyScope,
    source: ReplayExecutionSource,
) -> HistoricalSource:
    eid = source.execution_ref.record_id
    locked = await repo.get_execution(connection, scope, eid)
    if locked is None:
        raise ArtifactReplayError("artifact_source_execution_unavailable")
    execution = locked.execution
    try:
        source.validate_execution(execution)
    except ValueError as error:
        raise ArtifactReplayError("artifact_source_execution_pin_mismatch") from error
    if execution.status in (ExecutionStatus.INITIALIZED, ExecutionStatus.RUNNING):
        raise ArtifactReplayError("artifact_source_execution_unsettled")
    final = await repo.read_finalization(connection, scope, eid, ExecutionFinalization)
    if final is None:
        raise ArtifactReplayError("artifact_source_finalization_missing")
    if (final.coverage.release_id, final.coverage.bundle_digest) != (
        execution.pins.release_id,
        execution.pins.bundle_digest,
    ):
        raise ArtifactReplayError("artifact_finalization_pin_mismatch")
    execution_model_ids = {ref.record_id for ref in execution.model_invocation_refs}
    if len(execution_model_ids) + 1 > _MAX_NORMALIZED_ARTIFACTS:  # coverage is required
        raise ArtifactReplayError("artifact_replay_inventory_limit")
    await _check_branch_budget(connection, scope, eid)
    data = await load_records(connection, scope, locked)
    context = await read_context_snapshot(connection, scope, locked)
    records: dict[ArtifactKey, ContractModel] = {
        (K.EXECUTION_COVERAGE, final.coverage.coverage_id): final.coverage,
    }
    if context is not None:
        records[K.CONTEXT_SNAPSHOT, context.context_snapshot_id] = context
    coverage_scope = final.coverage.applicability_scope
    if isinstance(coverage_scope, EvaluatedCoverageScope):
        if (
            context is None
            or context.context_snapshot_id
            != coverage_scope.context_snapshot_ref.record_id
            or data.gate is None
            or data.gate.evaluation.evaluation_ref
            != coverage_scope.profile_eligibility_evaluation_ref
            or data.gate.evaluation.context_snapshot_ref
            != coverage_scope.context_snapshot_ref
        ):
            raise ArtifactReplayError(
                "artifact_eligibility_source_missing_or_mismatched"
            )
        eligibility = data.gate.evaluation
        records[
            K.PROFILE_ELIGIBILITY_EVALUATION, eligibility.evaluation_ref.record_id
        ] = eligibility
    elif data.gate is not None or coverage_scope.blocker_node_run_ref.record_id not in {
        node.node_run_id
        for branch, node in data.runs.items()
        if branch.node_id is NodeId.N01
    }:
        raise ArtifactReplayError("artifact_coverage_blocker_mismatch")

    checks = []
    for branch, message in data.messages.items():
        if tuple(map(_key, data.runs[branch].output_refs)) != tuple(
            _key(pin.ref) for pin in message.output_envelope().output_artifacts
        ):
            raise ArtifactReplayError("artifact_node_manifest_mismatch")
        expected = _message_records(message)
        checks.append((message, expected))
        for key, record in expected.items():
            if records.setdefault(key, record) != record:
                raise ArtifactReplayError("artifact_source_identity_conflict")
        if len(records) > _MAX_NORMALIZED_ARTIFACTS:
            raise ArtifactReplayError("artifact_replay_inventory_limit")

    selected_model_ids = {
        identity for kind, identity in records if kind is K.MODEL_INVOCATION
    }
    if selected_model_ids - execution_model_ids:
        raise ArtifactReplayError("artifact_source_model_invocations_mismatch")
    archived_ids = execution_model_ids - selected_model_ids
    if len(records) + len(archived_ids) > _MAX_NORMALIZED_ARTIFACTS:
        raise ArtifactReplayError("artifact_replay_inventory_limit")

    if {
        (item.profile_ref, item.object_id)
        for item in records.values()
        if isinstance(item, ObjectRecord)
    } != {
        (profile.profile_ref, ref.record_id)
        for profile in final.coverage.profiles
        for ref in profile.object_refs
    }:
        raise ArtifactReplayError("artifact_coverage_objects_mismatch")
    if {item for (kind, _), item in records.items() if kind is K.DECISION} != set(
        final.decisions
    ):
        raise ArtifactReplayError("artifact_finalization_decisions_mismatch")
    selections = {
        key: item
        for key, item in records.items()
        if isinstance(item, (ObservationBatchSelection, ToolStepSelection))
    }
    selected_facts: dict[str, FactRecord] = {}
    for selection in selections.values():
        target = records.get(_key(selection.selected_result_ref))
        if target is None:
            raise ArtifactReplayError("artifact_selected_result_missing")
        if isinstance(target, ObservationBatch) and isinstance(
            selection, ObservationBatchSelection
        ):
            facts = {row.primary_fact.fact_id for row in target.question_results}
            arrived_on_time = target.arrival_qualification == "on_time"
        elif isinstance(target, ToolExecution) and isinstance(
            selection, ToolStepSelection
        ):
            facts = {ref.record_id for ref in target.generated_fact_refs}
            arrived_on_time = (
                target.authority_observation is not None
                and target.authority_observation.arrival_qualification == "on_time"
            )
        else:
            raise ArtifactReplayError("artifact_selected_result_kind_mismatch")
        if (
            not arrived_on_time
            or target.object_ref is None
            or (
                target.attempt_ref.record_id,
                target.object_ref.record_id,
                target.profile_ref,
            )
            != (
                selection.selected_attempt_id,
                selection.object_id,
                selection.profile_ref,
            )
            or {ref.record_id for ref in selection.selected_fact_refs} != facts
        ):
            raise ArtifactReplayError("artifact_selected_result_mismatch")
        for ref in selection.selected_fact_refs:
            fact = records.get(_key(ref))
            if not isinstance(fact, FACT_TYPES):
                raise ArtifactReplayError("artifact_selected_fact_missing")
            if (fact.attempt_id, fact.object_id, fact.profile_ref) != (
                selection.selected_attempt_id,
                selection.object_id,
                selection.profile_ref,
            ):
                raise ArtifactReplayError("artifact_selected_fact_owner_mismatch")
            selected_facts[ref.record_id] = fact
    for decision in final.decisions:
        owner = decision.object_ref.record_id, decision.profile_ref
        for ref in decision.explanation.fact_refs:
            fact = selected_facts.get(ref.record_id)
            if fact is None:
                raise ArtifactReplayError("artifact_decision_fact_missing")
            if (fact.object_id, fact.profile_ref) != owner:
                raise ArtifactReplayError("artifact_decision_fact_owner_mismatch")
        for row in decision.rule_evaluations:
            proofs = row.optional_fact_proofs
            if isinstance(row.result, (EvaluatedRuleOutcome, BlockedRuleOutcome)):
                proofs += row.result.guard.fact_proofs
            for proof in proofs:
                selected = selections.get(_key(proof.authority_selection_ref))
                if (
                    selected is None
                    or proof.fact_ref not in selected.selected_fact_refs
                ):
                    raise ArtifactReplayError("artifact_decision_selection_missing")
                if (selected.object_id, selected.profile_ref) != owner:
                    raise ArtifactReplayError(
                        "artifact_decision_selection_owner_mismatch"
                    )

    for message, expected in checks:
        await _verified_message_records(connection, scope, eid, message, expected)
        if isinstance(message, DecisionMessage):
            await _check_decision_output(connection, scope, final, message)
    records.update(await _archived_models(connection, scope, eid, archived_ids))
    models = tuple(
        item for item in records.values() if isinstance(item, ModelInvocation)
    )
    tools = tuple(item for item in records.values() if isinstance(item, ToolExecution))

    ordered = sorted(records, key=lambda key: (key[0].value, key[1]))
    return HistoricalSource(
        execution=execution,
        finalization=final,
        artifacts=tuple(_pin(_ref(*key), records[key]) for key in ordered),
        facts=tuple(selected_facts.values()),
        models=models,
        tools=tools,
    )
