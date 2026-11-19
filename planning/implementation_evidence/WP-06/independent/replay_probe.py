"""Verify replay behavior against independent PostgreSQL and physical observations."""

import asyncio
import json
import os
import re
import sys
from dataclasses import asdict
from hashlib import sha256
from pathlib import Path
from tempfile import TemporaryDirectory
from time import monotonic
from types import SimpleNamespace

from audit import AuditScope, check_dispatches, check_tool_evidence
from cases import CASES, OUTSIDE, read_frame
from harness import AUTH, Harness
from lifecycle_probe import assert_lifecycle
from loopback import ParkingProvider
from run_cases import launch
from scoring_probe import assert_scoring
from sqlalchemy import text
from sqlalchemy.engine import make_url

import jimu_runtime
from examples.nonmotor_vehicle_illegal_parking.compilable import scene
from jimu_runtime.domain.enums import RuntimeRecordKind
from jimu_runtime.domain.runtime.intake import IdempotencyScope
from jimu_runtime.domain.runtime.replay import (
    ExecutionRef,
    InferenceReplayRequest,
    ReplayExecutionSource,
    ReplayKind,
    ReplayScoringEvidence,
)
from jimu_runtime.evaluation.history_projection import EvaluationHistoryError
from jimu_runtime.replay.artifact import ArtifactReplayService
from jimu_runtime.replay.artifact_records import ArtifactScoringEvidence
from jimu_runtime.replay.artifact_source import ArtifactReplayError
from jimu_runtime.replay.inference import InferenceReplayService
from jimu_runtime.workflow import repository
from jimu_runtime.workflow.engine import ExecutionFinalization
from jimu_runtime.workflow.intake import IntakeAuthorizationError
from jimu_runtime.workflow.postgres import apply_migrations, create_postgres_engine
from tests.fixtures.wp03.postgres.support import isolated_postgres

EXECUTION_TABLES = (
    "executions",
    "execution_events",
    "node_runs",
    "jobs",
    "attempts",
    "attempt_results",
    "produced_outputs",
    "authority_outputs",
    "decision_drafts",
    "execution_finalizations",
    "dispatch_operations",
    "dispatch_intents",
    "dispatch_results",
    "dispatch_media",
    "execution_capabilities",
    "tool_step_attempts",
    "tool_step_completions",
    "tool_step_parent_outputs",
    "observation_request_media",
)


def emit(check: str, **details: object) -> None:
    print(json.dumps({"check": check, **details}, sort_keys=True), flush=True)


async def history(engine, scope, eid, task) -> dict[str, tuple[int, str]]:
    identity = {"p": scope.project_id, "c": scope.client_id, "e": eid, "t": task}
    captured = {}
    async with engine.connect() as connection:
        for table in ("tasks", *EXECUTION_TABLES):
            key = "task_id=:t" if table == "tasks" else "execution_id=:e"
            statement = text(f"""
                SELECT row_to_json(r)::text FROM jimu_workflow.{table} AS r
                WHERE project_id=:p AND client_id=:c AND {key}
                ORDER BY row_to_json(r)::text
            """)
            rows = (await connection.execute(statement, identity)).scalars().all()
            captured[table] = (len(rows), sha256(json.dumps(rows).encode()).hexdigest())
    return captured


async def invoke(args, config, *, env=None, timeout=120):
    process = await asyncio.create_subprocess_exec(
        *args, env=env, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE
    )
    try:
        data, _ = await asyncio.wait_for(
            process.communicate(json.dumps(config, default=str).encode() + b"\n"),
            timeout,
        )
    finally:
        if process.returncode is None:
            process.kill()
            await process.wait()
    if process.returncode != 0:
        raise RuntimeError(
            f"isolated child failed: {args[-1]} (exit={process.returncode})"
        )
    return json.loads(data)


def check_cpu(raw: Path, output: Path, expected: int) -> None:
    actual = [
        {"pid": int(row["pid"]), "tool": row["tool"]}
        for line in raw.read_text().splitlines()
        if (
            row := re.match(
                r'(?P<pid>\d+) execve\("[^"]+", \["[^"]+"(?:\.\.\.)?, "-I", '
                r'"/proc/self/fd/\d+", "(?P<tool>[^"]+)"\], .* = 0$',
                line,
            )
        )
    ]
    assert len(actual) == expected, actual
    if expected:
        assert {row["tool"] for row in actual} == {
            "wheel_segmentation",
            "tactile_paving_segmentation",
            "wheel_boundary_geometry",
        }
    output.write_text(json.dumps(actual, indent=2) + "\n")


async def seed(db, root, endpoint, raw):
    return await invoke(
        [
            "strace",
            "-f",
            "-qq",
            "-e",
            "trace=execve",
            "-o",
            str(raw),
            os.environ["WP06_LEGACY_PYTHON"],
            str(Path(__file__).with_name("legacy_seed.py")),
        ],
        {
            "migration": db.migration_url.render_as_string(hide_password=False),
            "application": db.application_url.render_as_string(hide_password=False),
            "target": asdict(db.target),
            "directory": str(root),
            "endpoint": endpoint,
        },
        env={**os.environ, "PYTHONPATH": os.environ["WP06_LEGACY_SOURCE"]},
    )


async def assert_artifact(app, scope, source, output) -> None:
    tick = monotonic()
    service = ArtifactReplayService(app)
    request = await service.prepare_request(scope, source)
    a = await service.run(scope, "audit-sequential", request)
    b, retry = await asyncio.gather(
        service.run(scope, "audit-concurrent", request),
        service.run(scope, "audit-concurrent", request),
    )
    assert a.replay_run_id != b.replay_run_id and b == retry
    assert await service.run(scope, "audit-sequential", request) == a
    await app.dispose()
    reopened = ArtifactReplayService(app)
    runs = [a, b]
    for run in runs:
        assert await reopened.get_run(scope, run.replay_run_id) == run
        evidence = await reopened.read_scoring_evidence(scope, run.scoring_evidence)
        assert evidence is not None
        assert (
            sha256(evidence.stable_json().encode()).hexdigest()
            == run.scoring_evidence.digest
        )
        assert evidence.source_execution_id == source.execution_ref.record_id
        assert evidence.score.object_count == 1
        assert evidence.score.summary_result.value == "violation"
        assert sum(row.count for row in evidence.score.decision_counts) == 1
        assert evidence.model_calls == evidence.tool_calls == 0
        assert len(evidence.historical_dispatches) == 7
        wrong = scope.model_copy(update={"client_id": "independent.foreign"})
        assert await reopened.get_run(wrong, run.replay_run_id) is None
        try:
            await reopened.read_scoring_evidence(
                scope, run.scoring_evidence.model_copy(update={"digest": "0" * 64})
            )
        except ArtifactReplayError as error:
            assert str(error) == "artifact_scoring_evidence_pin_mismatch"
        else:
            raise AssertionError("artifact score was read with an invalid digest")
        (output / f"{run.replay_run_id}.score.json").write_text(
            evidence.stable_json() + "\n"
        )
    emit(
        "artifact_replay_scoring",
        runs=2,
        concurrent_same_key=True,
        historical_dispatches=7,
        elapsed_ms=int((monotonic() - tick) * 1000),
    )


async def total_runtime_rows(app):
    async with app.connect() as con:
        return tuple(
            (
                await con.execute(
                    text("""
            SELECT
              (SELECT count(*) FROM jimu_workflow.tasks),
              (SELECT count(*) FROM jimu_workflow.executions),
              (SELECT count(*) FROM jimu_workflow.jobs),
              (SELECT count(*) FROM jimu_workflow.dispatch_intents)
        """)
                )
            ).one()
        )


async def trace_artifact(app, scope, source, saved, provider, output, raw):
    incoming = len(provider.requests)
    global_rows = await total_runtime_rows(app)
    result = await invoke(
        [
            "strace",
            "-f",
            "-qq",
            "-e",
            "trace=execve",
            "-o",
            str(raw),
            sys.executable,
            str(Path(__file__)),
            "--artifact",
        ],
        {
            "database": app.url.render_as_string(hide_password=False),
            "scope": scope.model_dump(mode="json"),
            "source": source.model_dump(mode="json"),
            "output": str(output),
        },
    )
    check_cpu(raw, output / "artifact.cpu.json", 0)
    assert (
        sum(
            "execve(" in item and item.endswith(" = 0")
            for item in raw.read_text().splitlines()
        )
        == 1
    )
    assert len(provider.requests) == incoming
    assert await total_runtime_rows(app) == global_rows
    async with app.connect() as con:
        assert (
            await con.scalar(
                text("SELECT count(*) FROM jimu_workflow.artifact_replay_runs")
            )
            == 2
        )
    assert (
        await history(app, scope, source.execution_ref.record_id, source.task_id)
        == saved
    )
    details = {k: v for k, v in result.items() if k != "check"}
    emit(
        "artifact_replay", **details, physical_http=0, physical_cpu=0, source_equal=True
    )


async def artifact_child() -> None:
    config = json.loads(sys.stdin.readline())
    assert Path(jimu_runtime.__file__).resolve().is_relative_to(Path(sys.prefix))
    app = create_postgres_engine(make_url(config["database"]))
    try:
        await assert_artifact(
            app,
            IdempotencyScope.model_validate_json(json.dumps(config["scope"])),
            ReplayExecutionSource.model_validate_json(json.dumps(config["source"])),
            Path(config["output"]),
        )
    finally:
        await app.dispose()


class ReplayHost(Harness):
    async def authorize_inference_replay(self, principal, scope, request, source_task):
        if (
            principal is not self.principal
            or scope != self.scope
            or request.source.task_id != source_task.task_id
        ):
            raise IntakeAuthorizationError()
        return AUTH


async def assert_inference(host, source, saved, provider, output, raw):
    app = host.db
    scope = host.scope
    old = source.execution_ref.record_id
    new_release = host.release(1)
    assert new_release.bundle_digest != source.pins.bundle_digest
    host.revision = "1.1.0"
    service = InferenceReplayService(
        app, host.manager(), host.ledger, host=host, bundle_limits=scene.LIMITS
    )
    request = InferenceReplayRequest(
        schema_version="1.0.0",
        kind=ReplayKind.INFERENCE,
        source=source,
        target_pins=source.pins,
    )

    async def submit():
        return await service.submit(
            scope,
            request,
            idempotency_key="independent.inference",
            principal=host.principal,
        )

    start_calls = len(provider.requests)
    global_rows = await total_runtime_rows(app)
    tick = monotonic()
    created, duplicate = await asyncio.gather(submit(), submit())
    assert created == duplicate == await submit()
    assert len(provider.requests) == start_calls
    foreign = scope.model_copy(update={"client_id": "independent.foreign"})
    try:
        await service.submit(
            foreign, request, idempotency_key="wrong-scope", principal=host.principal
        )
    except IntakeAuthorizationError:
        pass
    else:
        raise AssertionError("foreign client obtained the historical Task")
    assert await total_runtime_rows(app) == (
        global_rows[0],
        global_rows[1] + 1,
        global_rows[2] + 1,
        global_rows[3],
    )
    target = created.target_execution_ref.record_id
    assert target != old
    async with app.connect() as connection:
        assert (
            await connection.scalar(
                text("SELECT count(*) FROM jimu_workflow.inference_replay_runs")
            )
            == 1
        )
        locked = await repository.get_execution(connection, scope, target)
        created.validate_target_execution(locked.execution)
        assert locked.is_inference_replay and locked.task.task_id == source.task_id
        (parent,) = (
            await connection.execute(
                text("""
                SELECT source_execution_id, capture_execution_id, task_id, capture_snapshot_digest
                FROM jimu_workflow.inference_replay_runs
            """)
            )
        ).all()
        assert parent.source_execution_id == parent.capture_execution_id == old
        assert (
            parent.task_id == source.task_id
            and len(parent.capture_snapshot_digest) == 64
        )
    process = await launch(host, target, "wp06.inference", raw, installed=True)
    try:
        for _ in range(18):
            if (await process.call("step"))["step"] == "finalized":
                break
        final = ExecutionFinalization.model_validate_json(
            json.dumps((await process.call("read"))["final"])
        )
    finally:
        await process.close()
    assert final.coverage.summary_result.value == "violation"
    assert final.coverage.bundle_digest == source.pins.bundle_digest
    assert final.execution_id == target
    check_cpu(raw, output / "target.cpu.json", 3)
    segment = SimpleNamespace(
        requests=provider.requests[start_calls:],
        replies=provider.replies[start_calls:],
        errors=provider.errors,
    )
    async with app.connect() as con:
        counts = await check_dispatches(
            con, AuditScope(scope, target), segment, host.media
        )
        assert counts == dict.fromkeys(
            ("discovery", "grounding", "observation", "candidate"), 1
        )
        (decision,) = final.decisions
        target_media = await host.media.verified_task_original(scope, locked.task)
        original = read_frame(scene.DIRECTORY, "base")
        assert target_media.content == original.content
        assets, _ = await check_tool_evidence(
            con,
            AuditScope(scope, target),
            host.media,
            original,
            {decision.object_ref.record_id: OUTSIDE},
            {},
        )
        assert len(assets) == 3
        contexts = (
            (
                await con.execute(
                    text("""
                SELECT record->'snapshot' FROM jimu_workflow.produced_outputs
                WHERE execution_id=:e AND output_kind='worker_message' AND record->>'kind'='context'
            """),
                    {"e": target},
                )
            )
            .scalars()
            .one()
        )
        assert (
            contexts["device_ref"]["version"]
            == contexts["camera_ref"]["version"]
            == "1.0.0"
        )
        target_attempts = set(
            (
                await con.execute(
                    text(
                        "SELECT attempt_id FROM jimu_workflow.attempts WHERE execution_id=:e"
                    ),
                    {"e": target},
                )
            ).scalars()
        )
        source_attempts = set(
            (
                await con.execute(
                    text(
                        "SELECT attempt_id FROM jimu_workflow.attempts WHERE execution_id=:e"
                    ),
                    {"e": old},
                )
            ).scalars()
        )
        assert target_attempts and not target_attempts & source_attempts
    assert await history(app, scope, old, source.task_id) == saved
    emit(
        "inference_replay",
        target_execution=target,
        http=counts,
        cpu=3,
        source_equal=True,
        elapsed_ms=int((monotonic() - tick) * 1000),
        current_bundle=new_release.bundle_digest,
        pinned_bundle=source.pins.bundle_digest,
    )
    return target


async def main(output: Path) -> None:
    assert Path(jimu_runtime.__file__).resolve().is_relative_to(Path(sys.prefix))
    frame = read_frame(scene.DIRECTORY, "base")
    deployment = (
        scene.capability_snapshot(scene.load_source()).model_deployments[0].identity
    )
    obs = ParkingProvider(
        CASES["known_violation"],
        frame,
        model_id="independent-parking-vision",
        deployment_version=deployment.deployment_version,
        model_version=deployment.model_version,
    )
    with isolated_postgres() as pg, TemporaryDirectory(prefix="wp06-audit-") as temp:
        owner, app = (
            create_postgres_engine(pg.migration_url),
            create_postgres_engine(pg.application_url),
        )
        root = Path(temp)
        try:
            async with obs.serving():
                accepted = await seed(
                    pg, root / "source", obs.url, root / "source.exec.raw"
                )
                emit("legacy_wp05_source", migrations=list(range(1, 9)), **accepted)
                check_cpu(root / "source.exec.raw", output / "source.cpu.json", 3)
                host = ReplayHost(app, root / "source", obs.url)
                assert host.scope.model_dump(mode="json") == accepted["scope"]
                eid = accepted["execution"]
                async with app.connect() as connection:
                    locked = await repository.get_execution(connection, host.scope, eid)
                    assert locked is not None and not locked.is_inference_replay
                    assert locked.execution.pins.bundle_digest == accepted["bundle"]
                    assert await check_dispatches(
                        connection, AuditScope(host.scope, eid), obs, host.media
                    ) == dict.fromkeys(
                        ("discovery", "grounding", "observation", "candidate"), 1
                    )
                source = ReplayExecutionSource(
                    execution_ref=ExecutionRef(
                        record_kind=RuntimeRecordKind.EXECUTION, record_id=eid
                    ),
                    task_id=accepted["task"],
                    pins=locked.execution.pins,
                )
                saved = await history(app, host.scope, eid, source.task_id)
                assert await apply_migrations(owner, pg.target) == (9, 10, 11)
                assert await apply_migrations(owner, pg.target) == ()
                assert await history(app, host.scope, eid, source.task_id) == saved
                emit(
                    "database_upgrade",
                    applied=[9, 10, 11],
                    historical_row_sets=len(saved),
                )
                await trace_artifact(
                    app,
                    host.scope,
                    source,
                    saved,
                    obs,
                    output,
                    root / "artifact.exec.raw",
                )
                await assert_inference(
                    host, source, saved, obs, output, root / "target.exec.raw"
                )
                physical_http_before_optional_phases = len(obs.requests)
                exporter, dataset, export_request = await assert_scoring(
                    host, eid, output
                )
                await assert_lifecycle(host, source, output)
                try:
                    await exporter.export(dataset, export_request)
                except EvaluationHistoryError as rejected:
                    assert rejected.code == "evaluation_evidence_expired"
                else:
                    raise AssertionError(
                        "historical exporter ignored the expired required evidence"
                    )
                replay = ArtifactReplayService(app)
                for item in output.glob("artifact-replay.*.score.json"):
                    stored = ArtifactScoringEvidence.model_validate_json(
                        item.read_bytes()
                    )
                    assert (
                        await replay.read_scoring_evidence(
                            host.scope,
                            ReplayScoringEvidence(
                                ref=stored.evidence_ref,
                                digest=sha256(
                                    stored.stable_json().encode()
                                ).hexdigest(),
                            ),
                        )
                        == stored
                    )
                assert len(obs.requests) == physical_http_before_optional_phases
                emit(
                    "after_disposition",
                    new_export="reports_expired_required_evidence",
                    earlier_artifact_scores="readable",
                    physical_http=0,
                )
                assert await history(app, host.scope, eid, source.task_id) == saved
        finally:
            await app.dispose()
            await owner.dispose()


if __name__ == "__main__":
    if sys.argv[1] == "--artifact":
        asyncio.run(artifact_child())
    else:
        asyncio.run(main(Path(sys.argv[1]).resolve()))
