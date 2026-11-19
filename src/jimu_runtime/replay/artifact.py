"""Scoped PostgreSQL Artifact Replay service for finalized workflow evidence."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from hashlib import sha256
from uuid import uuid4

from pydantic import TypeAdapter
from sqlalchemy import text
from sqlalchemy.engine import RowMapping
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from jimu_runtime.domain.definition_support import ShortText
from jimu_runtime.domain.enums import RuntimeRecordKind
from jimu_runtime.domain.primitives import RecordId
from jimu_runtime.domain.runtime.executions import PinnedRuntimeInput
from jimu_runtime.domain.runtime.facts import EvidenceRef
from jimu_runtime.domain.runtime.intake import IdempotencyScope
from jimu_runtime.domain.runtime.replay import (
    ArtifactReplayRequest,
    ArtifactReplayRun,
    ReplayExecutionSource,
    ReplayKind,
    ReplayScoringEvidence,
)
from jimu_runtime.replay.artifact_journal import read_journal_witnesses
from jimu_runtime.replay.artifact_records import (
    SCORE_PROTOCOL,
    SCORING_PROTOCOL_PIN,
    ArtifactScoringEvidence,
    HistoricalWitnessSnapshot,
    score_history,
)
from jimu_runtime.replay.artifact_source import (
    ArtifactReplayError,
    HistoricalSource,
    load_historical_source,
)
from jimu_runtime.workflow.mock_support import record_digest
from jimu_runtime.workflow.repository import RepositoryError

_KEY = TypeAdapter[str](ShortText)
_ID = TypeAdapter[str](RecordId)
_SCOPE = "project_id = :project_id AND client_id = :client_id"
_TABLE = "jimu_workflow.artifact_replay_runs"
_COLUMNS = """
    replay_run_id, evidence_id, source_execution_id, request_digest, run_digest,
    evidence_digest, run_record::text, evidence_record::text
"""


def _scope(scope: IdempotencyScope) -> dict[str, str]:
    return {"project_id": scope.project_id, "client_id": scope.client_id}


def _read_row(
    scope: IdempotencyScope, row: RowMapping
) -> tuple[ArtifactReplayRun, ArtifactScoringEvidence]:
    try:
        run = ArtifactReplayRun.model_validate_json(row["run_record"])
        evidence = ArtifactScoringEvidence.model_validate_json(row["evidence_record"])
    except ValueError as error:
        raise ArtifactReplayError("artifact_replay_record_corrupt") from error
    source = run.request.source
    if (
        run.stable_json() != row["run_record"]
        or evidence.stable_json() != row["evidence_record"]
        or record_digest(run) != row["run_digest"]
        or record_digest(evidence) != row["evidence_digest"]
        or run.scoring_evidence.digest != row["evidence_digest"]
        or run.scoring_evidence.ref != evidence.evidence_ref
        or evidence.evidence_ref.record_id != row["evidence_id"]
        or record_digest(run.request) != row["request_digest"]
        or evidence.request_digest != row["request_digest"]
        or run.replay_run_id != row["replay_run_id"]
        or evidence.replay_run_id != run.replay_run_id
        or evidence.source_execution_id != row["source_execution_id"]
        or source.execution_ref.record_id != row["source_execution_id"]
        or source.pins != evidence.execution_pins
        or evidence.scoring_protocol.protocol_id
        != run.request.scoring_protocol.protocol_id
        or evidence.scoring_protocol.version != run.request.scoring_protocol.version
        or record_digest(evidence.scoring_protocol)
        != run.request.scoring_protocol.digest
        or evidence.scope != scope
        or any(
            pin != evidence.witness_snapshot().pin()
            for pin in run.request.source_artifacts
            if pin.ref.record_kind is RuntimeRecordKind.EVIDENCE
            and pin.ref.record_id.startswith("artifact-witness.")
        )
    ):
        raise ArtifactReplayError("artifact_replay_record_corrupt")
    return run, evidence


async def _read_key(
    connection: AsyncConnection, scope: IdempotencyScope, key: bytes
) -> RowMapping | None:
    return (
        (
            await connection.execute(
                text(f"""
                SELECT {_COLUMNS} FROM {_TABLE}
                WHERE {_SCOPE} AND idempotency_key = :key
            """),
                {**_scope(scope), "key": key},
            )
        )
        .mappings()
        .one_or_none()
    )


def _match_request(
    scope: IdempotencyScope, row: RowMapping, request: ArtifactReplayRequest
) -> ArtifactReplayRun:
    run, _ = _read_row(scope, row)
    if run.request != request:
        raise ArtifactReplayError("artifact_replay_idempotency_conflict")
    return run


async def _load_source(
    connection: AsyncConnection, scope: IdempotencyScope, source: ReplayExecutionSource
) -> HistoricalSource:
    try:
        return await load_historical_source(connection, scope, source)
    except ArtifactReplayError:
        raise
    except RepositoryError as error:
        raise ArtifactReplayError(f"artifact_history_{error.code}") from error
    except ValueError as error:
        raise ArtifactReplayError("artifact_history_inconsistent") from error


async def _witness_snapshot(
    connection: AsyncConnection, scope: IdempotencyScope, history: HistoricalSource
) -> HistoricalWitnessSnapshot:
    return HistoricalWitnessSnapshot(
        scope=scope,
        source_execution_id=history.execution.execution_id,
        source_execution_digest=record_digest(history.execution),
        finalization_digest=record_digest(history.finalization),
        historical_dispatches=await read_journal_witnesses(connection, scope, history),
    )


@asynccontextmanager
async def _replay_transaction(engine: AsyncEngine) -> AsyncIterator[AsyncConnection]:
    async with engine.connect() as connection:
        # The key is read after waiting for its lock, from a fresh statement snapshot.
        await connection.execution_options(isolation_level="READ COMMITTED")
        async with connection.begin():
            yield connection


class ArtifactReplayService:
    """Replay within a trusted caller's Scope and persist the score atomically."""

    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine

    async def prepare_request(
        self, scope: IdempotencyScope, source: ReplayExecutionSource
    ) -> ArtifactReplayRequest:
        """Discover the exact normalized artifact pins required by this protocol."""
        scope = IdempotencyScope.model_validate(scope)
        source = ReplayExecutionSource.model_validate(source)
        async with self._engine.connect() as connection:
            history = await _load_source(connection, scope, source)
            witness = await _witness_snapshot(connection, scope, history)
        return ArtifactReplayRequest(
            schema_version="1.0.0",
            kind=ReplayKind.ARTIFACT,
            source=source,
            scoring_protocol=SCORING_PROTOCOL_PIN,
            source_artifacts=(*history.artifacts, witness.pin()),
        )

    async def get_run(
        self, scope: IdempotencyScope, replay_run_id: str
    ) -> ArtifactReplayRun | None:
        scope = IdempotencyScope.model_validate(scope)
        async with self._engine.connect() as connection:
            row = (
                (
                    await connection.execute(
                        text(f"""
                        SELECT {_COLUMNS} FROM {_TABLE}
                        WHERE {_SCOPE} AND replay_run_id = :identity
                    """),
                        {
                            **_scope(scope),
                            "identity": _ID.validate_python(replay_run_id),
                        },
                    )
                )
                .mappings()
                .one_or_none()
            )
        return None if row is None else _read_row(scope, row)[0]

    async def read_scoring_evidence(
        self, scope: IdempotencyScope, pin: ReplayScoringEvidence
    ) -> ArtifactScoringEvidence | None:
        scope = IdempotencyScope.model_validate(scope)
        pin = ReplayScoringEvidence.model_validate(pin)
        async with self._engine.connect() as connection:
            row = (
                (
                    await connection.execute(
                        text(f"""
                        SELECT {_COLUMNS} FROM {_TABLE}
                        WHERE {_SCOPE} AND evidence_id = :identity
                    """),
                        {**_scope(scope), "identity": pin.ref.record_id},
                    )
                )
                .mappings()
                .one_or_none()
            )
        if row is None:
            return None
        run, evidence = _read_row(scope, row)
        if run.scoring_evidence != pin:
            raise ArtifactReplayError("artifact_scoring_evidence_pin_mismatch")
        return evidence

    async def run(
        self,
        scope: IdempotencyScope,
        idempotency_key: str,
        request: ArtifactReplayRequest,
    ) -> ArtifactReplayRun:
        scope = IdempotencyScope.model_validate(scope)
        key = _KEY.validate_python(idempotency_key).encode("utf-8")
        if b"\x00" in key:
            raise ArtifactReplayError("artifact_replay_invalid_idempotency_key")
        request = ArtifactReplayRequest.model_validate(request)
        if request.scoring_protocol != SCORING_PROTOCOL_PIN:
            raise ArtifactReplayError("artifact_scoring_protocol_unavailable")
        async with _replay_transaction(self._engine) as connection:
            # Serialize a scoped key before reading history to avoid duplicate scoring.
            lock = sha256(
                f"artifact-replay\x00{scope.stable_json()}\x00".encode() + key
            )
            await connection.execute(
                text("SELECT pg_catalog.pg_advisory_xact_lock(:lock)"),
                {"lock": int.from_bytes(lock.digest()[:8], signed=True)},
            )
            previous = await _read_key(connection, scope, key)
            if previous is not None:
                return _match_request(scope, previous, request)
            history = await _load_source(connection, scope, request.source)
            witness = await _witness_snapshot(connection, scope, history)
            self._verify_inventory(request, (*history.artifacts, witness.pin()))
            evidence = ArtifactScoringEvidence(
                schema_version="1.0.0",
                evidence_ref=EvidenceRef(
                    record_kind=RuntimeRecordKind.EVIDENCE,
                    record_id=f"artifact-score.{uuid4().hex}",
                ),
                replay_run_id=f"artifact-replay.{uuid4().hex}",
                scope=scope,
                source_execution_id=history.execution.execution_id,
                execution_pins=history.execution.pins,
                source_execution_digest=witness.source_execution_digest,
                finalization_digest=witness.finalization_digest,
                request_digest=record_digest(request),
                scoring_protocol=SCORE_PROTOCOL,
                historical_dispatches=witness.historical_dispatches,
                score=score_history(history.finalization, history.facts),
                model_calls=0,
                tool_calls=0,
            )
            run = ArtifactReplayRun(
                schema_version="1.0.0",
                kind=ReplayKind.ARTIFACT,
                replay_run_id=evidence.replay_run_id,
                request=request,
                scoring_evidence=ReplayScoringEvidence(
                    ref=evidence.evidence_ref, digest=record_digest(evidence)
                ),
                recorded_at=datetime.now(UTC),
            )
            await connection.execute(
                text(f"""
                    INSERT INTO {_TABLE}
                        (project_id, client_id, replay_run_id, evidence_id,
                         source_execution_id, idempotency_key, request_digest,
                         run_digest, evidence_digest, run_record, evidence_record)
                    VALUES
                        (:project_id, :client_id, :run_id, :evidence_id,
                         :source_id, :key, :request, :run_digest, :evidence_digest,
                         CAST(:run AS json), CAST(:evidence AS json))
                """),
                {
                    **_scope(scope),
                    "run_id": run.replay_run_id,
                    "evidence_id": evidence.evidence_ref.record_id,
                    "source_id": evidence.source_execution_id,
                    "key": key,
                    "request": evidence.request_digest,
                    "run_digest": record_digest(run),
                    "evidence_digest": run.scoring_evidence.digest,
                    "run": run.stable_json(),
                    "evidence": evidence.stable_json(),
                },
            )
        return run

    @staticmethod
    def _verify_inventory(
        request: ArtifactReplayRequest, artifacts: tuple[PinnedRuntimeInput, ...]
    ) -> None:
        expected = {
            (pin.ref.record_kind, pin.ref.record_id): pin.digest for pin in artifacts
        }
        offered = {
            (pin.ref.record_kind, pin.ref.record_id): pin.digest
            for pin in request.source_artifacts
        }
        if offered.keys() - expected.keys():
            raise ArtifactReplayError("artifact_replay_input_unavailable")
        if expected.keys() - offered.keys():
            raise ArtifactReplayError("artifact_replay_input_missing")
        if offered != expected:
            raise ArtifactReplayError("artifact_replay_input_digest_mismatch")
