"""Compare historical normalized producers with the scoped dispatch receipts."""

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from jimu_runtime.domain.runtime.intake import IdempotencyScope
from jimu_runtime.domain.runtime.invocations import ModelInvocation
from jimu_runtime.domain.runtime.tool_executions import ToolExecution
from jimu_runtime.gateway.journal_records import (
    RESULT_ADAPTER,
    DispatchIntent,
    ModelDispatchResult,
    ToolDispatchResult,
    ToolDispatchTarget,
)
from jimu_runtime.replay.artifact_records import JournalWitness
from jimu_runtime.replay.artifact_source import ArtifactReplayError, HistoricalSource
from jimu_runtime.workflow.mock_support import record_digest


def _match_tool(receipt: ToolDispatchResult, execution: ToolExecution) -> bool:
    original = receipt.execution
    if original is None:
        return False
    provisional, selected = (
        original.authority_observation,
        execution.authority_observation,
    )
    if provisional is None or selected is None:
        return original == execution
    return (
        provisional.model_copy(
            update={"arrival_qualification": selected.arrival_qualification}
        )
        == selected
        and original.model_copy(update={"authority_observation": selected}) == execution
    )


async def read_journal_witnesses(
    connection: AsyncConnection, scope: IdempotencyScope, history: HistoricalSource
) -> tuple[JournalWitness, ...]:
    producers: dict[str, ModelInvocation | ToolExecution] = {
        **{item.model_invocation_id: item for item in history.models},
        **{item.tool_execution_id: item for item in history.tools},
    }
    if len(producers) != len(history.models) + len(history.tools):
        raise ArtifactReplayError("artifact_dispatch_identity_conflict")
    if not producers:
        return ()
    rows = await connection.execute(
        text("""
            SELECT i.dispatch_id, i.node_run_id, i.attempt_id,
                i.record::text AS intent_body, i.send_admitted,
                r.record::text AS result_body, r.fence_qualified
            FROM jimu_workflow.dispatch_intents AS i
            LEFT JOIN jimu_workflow.dispatch_results AS r
                ON (r.project_id, r.client_id, r.execution_id, r.dispatch_id)
                    = (i.project_id, i.client_id, i.execution_id, i.dispatch_id)
                AND NOT r.late
            WHERE (i.project_id, i.client_id, i.execution_id)
                = (:project, :client, :execution)
                AND i.dispatch_id = ANY(:ids)
            ORDER BY i.dispatch_id COLLATE "C"
        """),
        {
            "project": scope.project_id,
            "client": scope.client_id,
            "execution": history.execution.execution_id,
            "ids": list(producers),
        },
    )
    witnesses = []
    for row in rows:
        producer = producers.pop(row.dispatch_id)
        if row.result_body is None:
            raise ArtifactReplayError("artifact_dispatch_receipt_missing")
        try:
            intent = DispatchIntent.model_validate_json(row.intent_body)
            result = RESULT_ADAPTER.validate_json(row.result_body)
        except ValueError as error:
            raise ArtifactReplayError("artifact_dispatch_receipt_corrupt") from error
        if (
            intent.dispatch_id != row.dispatch_id
            or intent.stable_json() != row.intent_body
            or result.stable_json() != row.result_body
            or (row.node_run_id, row.attempt_id)
            != (producer.node_run_ref.record_id, producer.attempt_ref.record_id)
        ):
            raise ArtifactReplayError("artifact_dispatch_receipt_mismatch")
        target = intent.target
        if isinstance(producer, ModelInvocation):
            if (
                not isinstance(result, ModelDispatchResult)
                or result.invocation != producer
                or intent.request_digest != producer.request_digest
                or isinstance(target, ToolDispatchTarget)
                or (target.configured_provider, target.object_ref, target.profile_refs)
                != (
                    producer.configured_provider,
                    producer.object_ref,
                    producer.profile_refs,
                )
            ):
                raise ArtifactReplayError("artifact_model_journal_mismatch")
        elif (
            not isinstance(result, ToolDispatchResult)
            or not isinstance(target, ToolDispatchTarget)
            or (
                target.tool_ref,
                target.invocation_origin,
                target.profile_ref,
                target.object_ref,
            )
            != (
                producer.tool_ref,
                producer.invocation_origin,
                producer.profile_ref,
                producer.object_ref,
            )
            or not _match_tool(result, producer)
        ):
            raise ArtifactReplayError("artifact_tool_journal_mismatch")
        witnesses.append(
            JournalWitness(
                dispatch_id=row.dispatch_id,
                intent_digest=record_digest(intent),
                on_time_result_digest=record_digest(result),
                send_admitted=row.send_admitted,
                fence_qualified=row.fence_qualified,
            )
        )
    # The legacy offline adapters created explicitly namespaced fixture records
    # before the dispatch journal existed. They have no physical receipt.
    if any(not identity.startswith("synthetic.") for identity in producers):
        raise ArtifactReplayError("artifact_dispatch_intent_missing")
    return tuple(witnesses)
