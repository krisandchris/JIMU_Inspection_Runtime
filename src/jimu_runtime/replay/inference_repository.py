"""Scoped, append-only Inference Replay lineage."""

from dataclasses import dataclass

from sqlalchemy import text
from sqlalchemy.engine import RowMapping
from sqlalchemy.ext.asyncio import AsyncConnection

from jimu_runtime.bundle.integrity import sha256_digest
from jimu_runtime.domain.runtime.intake import IdempotencyScope
from jimu_runtime.domain.runtime.replay import InferenceReplayRun
from jimu_runtime.workflow.intake import IntakeIdentity

_RUN_COLUMNS = """
    request_digest, issuer, subject, replay_run_id, source_execution_id,
    target_execution_id, capture_execution_id, capture_snapshot_digest, task_id, record_digest, record::text AS record
"""


@dataclass(frozen=True, slots=True)
class StoredInferenceReplay:
    run: InferenceReplayRun
    request_digest: str
    identity: IntakeIdentity
    capture_execution_id: str
    capture_snapshot_digest: str


def _decode(row: RowMapping) -> StoredInferenceReplay:
    body: str = row["record"]
    if sha256_digest(body.encode("utf-8")) != row["record_digest"]:
        raise ValueError("Inference Replay run digest mismatch")
    run = InferenceReplayRun.model_validate_json(body)
    if (
        run.stable_json() != body
        or sha256_digest(run.request.stable_json().encode("utf-8"))
        != row["request_digest"]
        or run.replay_run_id != row["replay_run_id"]
        or run.request.source.task_id != row["task_id"]
        or run.request.source.execution_ref.record_id != row["source_execution_id"]
        or run.target_execution_ref.record_id != row["target_execution_id"]
        or run.request.source.pins != run.request.target_pins
    ):
        raise ValueError("Inference Replay run index mismatch")
    return StoredInferenceReplay(
        run,
        row["request_digest"],
        IntakeIdentity(issuer=row["issuer"], subject=row["subject"]),
        row["capture_execution_id"],
        row["capture_snapshot_digest"],
    )


async def read_inference_replay_by_key(
    connection: AsyncConnection, scope: IdempotencyScope, key: bytes
) -> StoredInferenceReplay | None:
    rows = await connection.execute(
        text(f"""
            SELECT {_RUN_COLUMNS}
            FROM jimu_workflow.inference_replay_runs
            WHERE project_id = :project_id AND client_id = :client_id
                AND idempotency_key = :key
        """),
        {**scope.model_dump(), "key": key},
    )
    row = rows.mappings().one_or_none()
    return _decode(row) if row is not None else None


async def read_inference_replay_for_target(
    connection: AsyncConnection, scope: IdempotencyScope, execution_id: str
) -> StoredInferenceReplay | None:
    rows = await connection.execute(
        text(f"""
            SELECT {_RUN_COLUMNS}
            FROM jimu_workflow.inference_replay_runs
            WHERE project_id = :project_id AND client_id = :client_id
                AND target_execution_id = :execution_id
        """),
        {**scope.model_dump(), "execution_id": execution_id},
    )
    row = rows.mappings().one_or_none()
    return _decode(row) if row is not None else None


async def insert_inference_replay(
    connection: AsyncConnection,
    scope: IdempotencyScope,
    key: bytes,
    replay: StoredInferenceReplay,
) -> None:
    run = replay.run
    body = run.stable_json()
    await connection.execute(
        text("""
            INSERT INTO jimu_workflow.inference_replay_runs
                (project_id, client_id, idempotency_key, request_digest, issuer, subject,
                    replay_run_id, source_execution_id, target_execution_id, capture_execution_id,
                    capture_snapshot_digest, task_id,
                    record_digest, record)
            VALUES (:project_id, :client_id, :key, :request_digest, :issuer, :subject,
                :replay_run_id, :source, :target, :capture, :capture_digest, :task_id, :digest, CAST(:record AS json))
        """),
        {
            **scope.model_dump(),
            "key": key,
            "request_digest": replay.request_digest,
            "issuer": replay.identity.issuer,
            "subject": replay.identity.subject,
            "replay_run_id": run.replay_run_id,
            "source": run.request.source.execution_ref.record_id,
            "target": run.target_execution_ref.record_id,
            "capture": replay.capture_execution_id,
            "capture_digest": replay.capture_snapshot_digest,
            "task_id": run.request.source.task_id,
            "digest": sha256_digest(body.encode("utf-8")),
            "record": body,
        },
    )
