"""Versioned evidence for a deterministic, historical artifact score."""

from collections import Counter
from typing import Literal

from jimu_runtime.domain.base import ContractModel
from jimu_runtime.domain.definition_support import NonNegativeBudget
from jimu_runtime.domain.enums import DecisionResult, FactStatus, RuntimeRecordKind
from jimu_runtime.domain.primitives import Digest, RecordId
from jimu_runtime.domain.references import RuntimeRecordRef
from jimu_runtime.domain.runtime.coverage import CoverageStatus
from jimu_runtime.domain.runtime.decisions import RecordList
from jimu_runtime.domain.runtime.executions import ExecutionPins, PinnedRuntimeInput
from jimu_runtime.domain.runtime.facts import EvidenceRef, FactRecord
from jimu_runtime.domain.runtime.intake import IdempotencyScope
from jimu_runtime.domain.runtime.replay import ScoringProtocolPin
from jimu_runtime.workflow.engine import ExecutionFinalization
from jimu_runtime.workflow.mock_support import record_digest


class HistoricalScoreProtocol(ContractModel):
    schema_version: Literal["1.0.0"] = "1.0.0"
    protocol_id: Literal["historical_outcome_counts"] = "historical_outcome_counts"
    version: Literal["1.0.0"] = "1.0.0"
    decision_population: Literal["finalized_decisions"] = "finalized_decisions"
    fact_population: Literal["distinct_selected_facts"] = "distinct_selected_facts"
    source_population: Literal["normalized_workflow"] = "normalized_workflow"
    result_kind: Literal["outcome_counts"] = "outcome_counts"


SCORE_PROTOCOL = HistoricalScoreProtocol()
SCORING_PROTOCOL_PIN = ScoringProtocolPin(
    protocol_id=SCORE_PROTOCOL.protocol_id,
    version=SCORE_PROTOCOL.version,
    digest=record_digest(SCORE_PROTOCOL),
)


class DecisionCount(ContractModel):
    result: DecisionResult
    count: NonNegativeBudget


class FactCount(ContractModel):
    status: FactStatus
    count: NonNegativeBudget


class HistoricalScore(ContractModel):
    coverage_status: CoverageStatus
    summary_result: DecisionResult
    object_count: NonNegativeBudget
    decision_counts: RecordList[DecisionCount]
    selected_fact_counts: RecordList[FactCount]


class JournalWitness(ContractModel):
    dispatch_id: RecordId
    intent_digest: Digest
    on_time_result_digest: Digest
    send_admitted: bool
    fence_qualified: bool | None


class HistoricalWitnessSnapshot(ContractModel):
    """Canonical read-only projection of the records used as audit witnesses."""

    schema_version: Literal["1.0.0"] = "1.0.0"
    scope: IdempotencyScope
    source_execution_id: RecordId
    source_execution_digest: Digest
    finalization_digest: Digest
    historical_dispatches: RecordList[JournalWitness]

    def pin(self) -> PinnedRuntimeInput:
        identity = record_digest(
            WitnessIdentity(scope=self.scope, execution_id=self.source_execution_id)
        )
        return PinnedRuntimeInput(
            ref=RuntimeRecordRef(
                record_kind=RuntimeRecordKind.EVIDENCE,
                record_id=f"artifact-witness.{identity}",
            ),
            digest=record_digest(self),
        )


class WitnessIdentity(ContractModel):
    scope: IdempotencyScope
    execution_id: RecordId


class ArtifactScoringEvidence(ContractModel):
    """Small inline evidence in the scoped replay store's EvidenceRef namespace."""

    schema_version: Literal["1.0.0"]
    evidence_ref: EvidenceRef
    replay_run_id: RecordId
    scope: IdempotencyScope
    source_execution_id: RecordId
    execution_pins: ExecutionPins
    source_execution_digest: Digest
    finalization_digest: Digest
    request_digest: Digest
    scoring_protocol: HistoricalScoreProtocol
    historical_dispatches: RecordList[JournalWitness]
    score: HistoricalScore
    model_calls: Literal[0]
    tool_calls: Literal[0]

    def witness_snapshot(self) -> HistoricalWitnessSnapshot:
        return HistoricalWitnessSnapshot(
            scope=self.scope,
            source_execution_id=self.source_execution_id,
            source_execution_digest=self.source_execution_digest,
            finalization_digest=self.finalization_digest,
            historical_dispatches=self.historical_dispatches,
        )


def score_history(
    finalization: ExecutionFinalization, facts: tuple[FactRecord, ...]
) -> HistoricalScore:
    decisions = Counter(decision.result for decision in finalization.decisions)
    statuses = Counter(fact.status for fact in facts)
    coverage = finalization.coverage
    return HistoricalScore(
        coverage_status=coverage.coverage_status,
        summary_result=coverage.summary_result,
        object_count=coverage.object_count,
        decision_counts=tuple(
            DecisionCount(result=result, count=decisions[result])
            for result in DecisionResult
        ),
        selected_fact_counts=tuple(
            FactCount(status=status, count=statuses[status]) for status in FactStatus
        ),
    )
