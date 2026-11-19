"""Artifact Replay opens real workflow and scoring records in isolated PostgreSQL."""

import asyncio
import json
import re
from collections import Counter
from contextlib import asynccontextmanager
from hashlib import sha256
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from examples.nonmotor_vehicle_illegal_parking.compilable.lifecycle import ACTOR
from examples.nonmotor_vehicle_illegal_parking.compilable.scene import LIMITS
from jimu_runtime.bundle.semantic_inputs import require_definition
from jimu_runtime.domain.definitions.tools import ToolDefinition
from jimu_runtime.domain.enums import ArtifactKind, DecisionResult, RuntimeRecordKind
from jimu_runtime.domain.runtime.coverage import CoverageStatus
from jimu_runtime.domain.runtime.executions import NodeId
from jimu_runtime.domain.runtime.replay import ExecutionRef, ReplayExecutionSource
from jimu_runtime.gateway.bridge_tools import ToolExecutionAdapter
from jimu_runtime.inspection.geometry import synthetic_cpu_implementations
from jimu_runtime.inspection.geometry.node import GeometryNodeAdapter
from jimu_runtime.replay import artifact_source
from jimu_runtime.replay.artifact import ArtifactReplayService
from jimu_runtime.replay.artifact_records import SCORING_PROTOCOL_PIN
from jimu_runtime.replay.artifact_source import ArtifactReplayError
from jimu_runtime.tools.executor import ToolExecutor
from jimu_runtime.tools.registry import ToolWorkerRegistry
from jimu_runtime.tools.step_attempts import StepAttemptStore
from jimu_runtime.workflow import repository
from jimu_runtime.workflow.engine import ExecutionFinalization
from jimu_runtime.workflow.postgres import apply_migrations, create_postgres_engine
from jimu_runtime.workflow.worker import WorkerStep
from jimu_runtime.workflow.worker_inspection import (
    VerifiedMediaWorkerHost,
    inspection_worker,
)
from jimu_runtime.workflow.worker_records import DecisionMessage, EligibilityMessage
from jimu_runtime.workflow.worker_source import SyntheticScenario
from tests.fixtures.wp03.intake.support import migrated_application
from tests.fixtures.wp03.jobs.support import inspect
from tests.fixtures.wp03.postgres.support import isolated_postgres
from tests.fixtures.wp03.worker.support import accepted_worker
from tests.fixtures.wp04 import http_support
from tests.fixtures.wp05 import perception as perception_support
from tests.fixtures.wp05.perception import perception_fixture

loopback_only = http_support.loopback_only


async def _history_snapshot(engine, scope, eid):
    async with engine.connect() as connection:
        execution = await repository.get_execution(connection, scope, eid)
        produced = (
            await connection.execute(
                text("""
                    SELECT node_run_id, attempt_id, output_key, output_digest
                    FROM jimu_workflow.produced_outputs ORDER BY 1, 2, 3
                """)
            )
        ).all()
        dispatches = tuple(
            (
                await connection.execute(
                    text("""
                    SELECT (SELECT count(*) FROM jimu_workflow.dispatch_intents),
                           (SELECT count(*) FROM jimu_workflow.dispatch_results)
                """)
                )
            ).one()
        )
    return execution, produced, dispatches, await inspect(engine, scope, eid)


def test_scoped_reopen_idempotency_and_unchanged_history(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def run() -> None:
        async with migrated_application() as engine:
            worker, accepted, _ = await accepted_worker(engine, tmp_path)
            scope, execution = accepted.task.idempotency_scope, accepted.execution
            eid = execution.execution_id
            source = ReplayExecutionSource(
                execution_ref=ExecutionRef(
                    record_kind=RuntimeRecordKind.EXECUTION, record_id=eid
                ),
                task_id=accepted.task.task_id,
                pins=execution.pins,
            )
            service = ArtifactReplayService(engine)
            with pytest.raises(
                ArtifactReplayError, match="artifact_source_execution_unsettled"
            ):
                await service.prepare_request(scope, source)
            for _ in range(7):
                step = await worker.run_once(
                    scope, eid, owner="artifact.fixture", lease_ms=1000
                )
            assert step is WorkerStep.FINALIZED
            baseline = await _history_snapshot(engine, scope, eid)

            request = await service.prepare_request(scope, source)
            assert request.scoring_protocol == SCORING_PROTOCOL_PIN
            kinds = {item.ref.record_kind for item in request.source_artifacts}
            assert {
                RuntimeRecordKind.EXECUTION_COVERAGE,
                RuntimeRecordKind.DECISION,
                RuntimeRecordKind.FACT,
                RuntimeRecordKind.AUTHORITATIVE_RESULT_SELECTION,
                RuntimeRecordKind.QUESTION_SELECTION_RECORD,
            } <= kinds
            stricter_engine = engine.execution_options(
                isolation_level="REPEATABLE READ"
            )
            # Both service calls use separate connections from the same stricter pool.
            service = ArtifactReplayService(stricter_engine)
            left, right = await asyncio.gather(
                service.run(scope, "fixture-key", request),
                service.run(scope, "fixture-key", request),
            )
            assert left == right
            await engine.dispose()
            reopened = ArtifactReplayService(engine)
            assert await reopened.get_run(scope, left.replay_run_id) == left
            evidence = await reopened.read_scoring_evidence(
                scope, left.scoring_evidence
            )
            assert evidence is not None
            assert evidence.model_calls == evidence.tool_calls == 0
            assert evidence.score.summary_result is DecisionResult.VIOLATION
            assert evidence.score.object_count == 1
            assert {row.result: row.count for row in evidence.score.decision_counts}[
                DecisionResult.VIOLATION
            ] == 1
            assert sum(row.count for row in evidence.score.selected_fact_counts) > 0

            inactive_scope = scope.model_copy(update={"client_id": "different-client"})
            assert await reopened.get_run(inactive_scope, left.replay_run_id) is None
            assert (
                await reopened.read_scoring_evidence(
                    inactive_scope, left.scoring_evidence
                )
                is None
            )
            with pytest.raises(
                ArtifactReplayError, match="artifact_source_execution_unavailable"
            ):
                await reopened.run(inactive_scope, "fixture-key", request)

            with pytest.raises(
                ArtifactReplayError, match="artifact_scoring_evidence_pin_mismatch"
            ):
                await reopened.read_scoring_evidence(
                    scope, left.scoring_evidence.model_copy(update={"digest": "0" * 64})
                )
            missing = request.model_copy(
                update={"source_artifacts": request.source_artifacts[1:]}
            )
            with pytest.raises(
                ArtifactReplayError, match="artifact_replay_input_missing"
            ):
                await reopened.run(scope, "missing", missing)
            with pytest.raises(
                ArtifactReplayError, match="artifact_replay_idempotency_conflict"
            ):
                await reopened.run(scope, "fixture-key", missing)

            first, *rest = request.source_artifacts
            tampered = request.model_copy(
                update={
                    "source_artifacts": (
                        first.model_copy(update={"digest": "0" * 64}),
                        *rest,
                    )
                }
            )
            with pytest.raises(
                ArtifactReplayError, match="artifact_replay_input_digest_mismatch"
            ):
                await reopened.run(scope, "tampered", tampered)
            wrong_protocol = request.model_copy(
                update={
                    "scoring_protocol": SCORING_PROTOCOL_PIN.model_copy(
                        update={"digest": "0" * 64}
                    )
                }
            )
            with pytest.raises(
                ArtifactReplayError, match="artifact_scoring_protocol_unavailable"
            ):
                await reopened.run(scope, "wrong-protocol", wrong_protocol)
            wrong_source = request.model_copy(
                update={
                    "source": source.model_copy(
                        update={
                            "pins": source.pins.model_copy(
                                update={"input_digest": "0" * 64}
                            )
                        }
                    )
                }
            )
            with pytest.raises(
                ArtifactReplayError, match="artifact_source_execution_pin_mismatch"
            ):
                await reopened.run(scope, "wrong-source", wrong_source)

            async def unexpected_artifact_read(*args, **kwargs):
                pytest.fail("over-budget replay began reading individual artifacts")

            with monkeypatch.context() as scoped:
                scoped.setattr(artifact_source, "_MAX_NORMALIZED_ARTIFACTS", 8)
                scoped.setattr(
                    artifact_source,
                    "_verified_message_records",
                    unexpected_artifact_read,
                )
                with pytest.raises(
                    ArtifactReplayError, match="artifact_replay_inventory_limit"
                ):
                    await reopened.prepare_request(scope, source)
                with pytest.raises(
                    ArtifactReplayError, match="artifact_replay_inventory_limit"
                ):
                    await reopened.run(scope, "oversized", request)

                scoped.setattr(artifact_source, "_MAX_BRANCHES", 1)
                scoped.setattr(
                    artifact_source, "load_records", unexpected_artifact_read
                )
                with pytest.raises(
                    ArtifactReplayError, match="artifact_replay_history_work_limit"
                ):
                    await reopened.prepare_request(scope, source)

            assert await _history_snapshot(engine, scope, eid) == baseline
            async with engine.connect() as connection:
                assert (
                    await connection.scalar(
                        text("SELECT count(*) FROM jimu_workflow.artifact_replay_runs")
                    )
                    == 1
                )

    asyncio.run(run())


@pytest.mark.parametrize("retry_observation", [False, True])
def test_replay_matches_configured_model_and_tool_receipts(
    tmp_path: Path,
    loopback_only: set[int],
    retry_observation: bool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def peer(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        received = await http_support.read_request(reader)
        request = json.loads(received.body)
        schema = request["response_format"]["json_schema"]["schema"]
        response_kind = schema["$id"].split(":")[-2]
        calls[response_kind] += 1
        reply = {
            "profile_discovery-result": {"profile_hits": ["nonmotor_vehicle"]},
            "object_grounding-result": {"boxes": [{"xyxy": [40, 23, 52, 33]}]},
            "object_observation-result": {
                "vehicle_type": "electric_bicycle",
                "parking_state": "parked",
                "parking_position": "over_boundary",
            },
            "candidate_prediction-result": {"categories": []},
        }[response_kind]
        if (
            retry_observation
            and response_kind == "object_observation-result"
            and calls[response_kind] == 1
        ):
            reply = {**reply, "object_id": "invalid.provider.object"}
        response = http_support.chat_body(
            model=request["model"], content=json.dumps(reply)
        )
        writer.write(
            http_support.response(
                response,
                headers=(
                    (b"X-Request-ID", f"artifact-{sum(calls.values())}".encode()),
                    (b"X-Jimu-Deployment-Version", b"1.0.0"),
                    (b"X-Jimu-Model-Version", b"1.0.0"),
                ),
            )
        )
        await writer.drain()

    calls: Counter[str] = Counter()
    database_owners = []

    @asynccontextmanager
    async def application_with_owner():
        with isolated_postgres() as database:
            owner = create_postgres_engine(database.migration_url)
            app = create_postgres_engine(database.application_url)
            database_owners.append(owner)
            try:
                await apply_migrations(owner, database.target)
                yield app
            finally:
                await app.dispose()
                await owner.dispose()

    monkeypatch.setattr(
        perception_support, "migrated_application", application_with_owner
    )

    async def run() -> None:
        async with http_support.http_peer(loopback_only, peer) as url:
            async with perception_fixture(tmp_path, url, with_media=True) as fixture:
                media = fixture.media_store
                assert media is not None
                accepted = fixture.accepted
                scope, execution = accepted.task.idempotency_scope, accepted.execution
                eid = execution.execution_id
                catalogue = fixture.scene.execution.catalogue
                definitions = tuple(
                    require_definition(catalogue, item.ref, ToolDefinition)
                    for item in catalogue.of_kind(ArtifactKind.TOOL)
                )
                registry = ToolWorkerRegistry(
                    synthetic_cpu_implementations(
                        fixture.resources,
                        "cpu",
                        "inspection-tools",
                        definitions,
                        fixture.tool_proofs,
                        synthetic_image_digests=frozenset(
                            {accepted.task.media_ref.sha256}
                        ),
                    )
                )
                tools = GeometryNodeAdapter(
                    media,
                    ToolExecutionAdapter(
                        ToolExecutor(registry),
                        StepAttemptStore(fixture.application, actor=ACTOR),
                        fixture.journal,
                        fixture.host.capabilities,
                    ),
                )

                async def capture_registry(request_scope, task):
                    source = await fixture.host.worker_source(request_scope, task)
                    return source.device, source.camera

                worker = inspection_worker(
                    fixture.application,
                    fixture.releases,
                    fixture.store,
                    host=VerifiedMediaWorkerHost(
                        media, fixture.host.capabilities, capture_registry
                    ),
                    bundle_limits=LIMITS,
                    media=media,
                    gateway=fixture.gateway,
                    execute_tools=tools.execute,
                    model_resource_scope_id="model",
                )
                statuses = []
                for index in range(20):
                    step = await worker.run_once(
                        scope, eid, owner=f"artifact-{index}", lease_ms=10000
                    )
                    statuses.append(step)
                    if step is WorkerStep.FINALIZED:
                        break
                    if step in {WorkerStep.IDLE, WorkerStep.RETRY_SCHEDULED}:
                        await asyncio.sleep(0.025)
                assert statuses[-1] is WorkerStep.FINALIZED, statuses
                assert sum(calls.values()) == 4 + retry_observation
                dispatched = calls.copy()
                baseline = await _history_snapshot(fixture.application, scope, eid)
                historical_dispatch_count = 7 + retry_observation
                assert baseline[2] == (historical_dispatch_count,) * 2

                service = ArtifactReplayService(fixture.application)
                source = ReplayExecutionSource(
                    execution_ref=ExecutionRef(
                        record_kind=RuntimeRecordKind.EXECUTION, record_id=eid
                    ),
                    task_id=accepted.task.task_id,
                    pins=execution.pins,
                )
                tool_replay_calls = 0

                async def reject_unexpected_tool(*args, **kwargs):
                    nonlocal tool_replay_calls
                    tool_replay_calls += 1
                    pytest.fail("Artifact Replay invoked a local Tool")

                monkeypatch.setattr(ToolExecutor, "execute", reject_unexpected_tool)
                request = await service.prepare_request(scope, source)
                replay = await service.run(scope, "configured", request)
                assert tool_replay_calls == 0
                scored = await service.read_scoring_evidence(
                    scope, replay.scoring_evidence
                )
                assert scored is not None
                assert len(scored.historical_dispatches) == historical_dispatch_count
                assert scored.model_calls == scored.tool_calls == 0
                assert scored.score.summary_result is DecisionResult.VIOLATION
                assert calls == dispatched
                assert (
                    await _history_snapshot(fixture.application, scope, eid) == baseline
                )
                if retry_observation:
                    (owner,) = database_owners
                    dispatch_id = next(
                        row.dispatch_id
                        for row in scored.historical_dispatches
                        if row.send_admitted
                    )
                    async with owner.begin() as connection:
                        await connection.execute(
                            text("""
                                UPDATE jimu_workflow.dispatch_intents SET send_admitted = false
                                WHERE execution_id = :eid AND dispatch_id = :dispatch
                            """),
                            {"eid": eid, "dispatch": dispatch_id},
                        )
                    with pytest.raises(
                        ArtifactReplayError,
                        match="artifact_replay_input_digest_mismatch",
                    ):
                        await service.run(scope, "changed-witness", request)
                    assert await service.run(scope, "configured", request) == replay
                    assert (
                        await service.read_scoring_evidence(
                            scope, replay.scoring_evidence
                        )
                        == scored
                    )
                    changed = await service.prepare_request(scope, source)
                    assert (
                        changed.source_artifacts[:-1] == request.source_artifacts[:-1]
                    )
                    assert (
                        changed.source_artifacts[-1].ref
                        == request.source_artifacts[-1].ref
                    )
                    assert (
                        changed.source_artifacts[-1].digest
                        != request.source_artifacts[-1].digest
                    )
                    assert tool_replay_calls == 0
                    assert calls == dispatched

                    async with owner.begin() as connection:
                        moved = await connection.scalar(
                            text("""
                                UPDATE jimu_workflow.produced_outputs AS p
                                SET attempt_id = a.attempt_id
                                FROM jimu_workflow.authority_outputs AS a
                                WHERE (p.project_id, p.client_id, p.execution_id, p.node_run_id)
                                    = (a.project_id, a.client_id, a.execution_id, a.node_run_id)
                                    AND p.execution_id = :eid
                                    AND p.attempt_id <> a.attempt_id
                                    AND p.output_kind = 'model_invocation'
                                RETURNING p.output_key
                            """),
                            {"eid": eid},
                        )
                        assert moved is not None
                    with pytest.raises(
                        ArtifactReplayError,
                        match="artifact_(?:history_output_owner|archived_model)_mismatch",
                    ):
                        await service.prepare_request(scope, source)

    asyncio.run(run())


def test_replay_detects_durable_fact_and_report_corruption(tmp_path: Path) -> None:
    async def run() -> None:
        with isolated_postgres() as database:
            owner = create_postgres_engine(database.migration_url)
            app = create_postgres_engine(database.application_url)
            try:
                await apply_migrations(owner, database.target)
                worker, accepted, _ = await accepted_worker(app, tmp_path)
                scope = accepted.task.idempotency_scope
                eid = accepted.execution.execution_id
                for _ in range(7):
                    step = await worker.run_once(
                        scope, eid, owner="artifact-corruption", lease_ms=1000
                    )
                assert step is WorkerStep.FINALIZED
                service = ArtifactReplayService(app)
                request = await service.prepare_request(
                    scope,
                    ReplayExecutionSource(
                        execution_ref=ExecutionRef(
                            record_kind=RuntimeRecordKind.EXECUTION, record_id=eid
                        ),
                        task_id=accepted.task.task_id,
                        pins=accepted.execution.pins,
                    ),
                )
                replay = await service.run(scope, "before-fault", request)
                with pytest.raises(DBAPIError) as denied:
                    async with app.begin() as connection:
                        await connection.execute(
                            text(
                                "UPDATE jimu_workflow.artifact_replay_runs SET evidence_record = '{}'::json"
                            )
                        )
                assert denied.value.orig.sqlstate == "42501"

                # Corrupt only the isolated owner's historical record, retaining its raw digest.
                async with owner.begin() as connection:
                    await connection.execute(
                        text("""
                            UPDATE jimu_workflow.produced_outputs
                            SET record = '{}'::json WHERE output_kind = 'fact'
                        """)
                    )
                with pytest.raises(
                    ArtifactReplayError, match="artifact_history_output_digest_mismatch"
                ):
                    await service.run(scope, "after-fault", request)

                # The accepted run and evidence re-open even if the old input is no
                # longer available, preserving the original review/scoring history.
                assert await service.run(scope, "before-fault", request) == replay
                assert (
                    await service.read_scoring_evidence(scope, replay.scoring_evidence)
                    is not None
                )
                async with owner.begin() as connection:
                    await connection.execute(
                        text("""
                            UPDATE jimu_workflow.artifact_replay_runs
                            SET evidence_record = jsonb_set(evidence_record::jsonb,
                                '{score,object_count}', '99'::jsonb)::json
                        """)
                    )
                with pytest.raises(
                    ArtifactReplayError, match="artifact_replay_record_corrupt"
                ):
                    await service.read_scoring_evidence(scope, replay.scoring_evidence)
                async with app.connect() as connection:
                    assert (
                        await connection.scalar(
                            text(
                                "SELECT count(*) FROM jimu_workflow.artifact_replay_runs"
                            )
                        )
                        == 1
                    )
            finally:
                await app.dispose()
                await owner.dispose()

    asyncio.run(run())


def test_replay_keeps_empty_coverage_separate_from_negative_decisions(
    tmp_path: Path,
) -> None:
    async def run() -> None:
        async with migrated_application() as engine:
            worker, accepted, host = await accepted_worker(engine, tmp_path)
            scope = accepted.task.idempotency_scope
            eid = accepted.execution.execution_id
            host.scenario = SyntheticScenario(discovery_hits=())
            for _ in range(7):
                step = await worker.run_once(
                    scope, eid, owner="artifact-no-hits", lease_ms=1000
                )
                if step is WorkerStep.FINALIZED:
                    break
            assert step is WorkerStep.FINALIZED

            service = ArtifactReplayService(engine)
            request = await service.prepare_request(
                scope,
                ReplayExecutionSource(
                    execution_ref=ExecutionRef(
                        record_kind=RuntimeRecordKind.EXECUTION, record_id=eid
                    ),
                    task_id=accepted.task.task_id,
                    pins=accepted.execution.pins,
                ),
            )
            replay = await service.run(scope, "no-target", request)
            evidence = await service.read_scoring_evidence(
                scope, replay.scoring_evidence
            )
            assert evidence is not None
            assert evidence.score.coverage_status is CoverageStatus.NONE
            assert evidence.score.object_count == 0
            assert all(row.count == 0 for row in evidence.score.decision_counts)
            assert all(row.count == 0 for row in evidence.score.selected_fact_counts)

    asyncio.run(run())


def _substitute_ids(record, substitutions):
    pattern = re.compile(
        "|".join(re.escape(json.dumps(value)) for value in substitutions)
    )
    changed = pattern.sub(
        lambda found: json.dumps(substitutions[json.loads(found.group())]),
        record.stable_json(),
    )
    return type(record).model_validate_json(changed)


def test_replay_rejects_crossed_object_profile_and_context_links(
    tmp_path: Path,
) -> None:
    async def run() -> None:
        with isolated_postgres() as database:
            owner = create_postgres_engine(database.migration_url)
            app = create_postgres_engine(database.application_url)
            try:
                await apply_migrations(owner, database.target)
                worker, accepted, _ = await accepted_worker(
                    app, tmp_path, second_profile=True
                )
                scope = accepted.task.idempotency_scope
                eid = accepted.execution.execution_id
                for _ in range(12):
                    step = await worker.run_once(
                        scope, eid, owner="graph-links", lease_ms=1000
                    )
                    if step is WorkerStep.FINALIZED:
                        break
                assert step is WorkerStep.FINALIZED
                service = ArtifactReplayService(app)
                source = ReplayExecutionSource(
                    execution_ref=ExecutionRef(
                        record_kind=RuntimeRecordKind.EXECUTION, record_id=eid
                    ),
                    task_id=accepted.task.task_id,
                    pins=accepted.execution.pins,
                )
                await service.prepare_request(scope, source)

                async with app.connect() as connection:
                    final = await repository.read_finalization(
                        connection, scope, eid, ExecutionFinalization
                    )
                    assert final is not None
                    first, second = final.decisions
                    message = await repository.read_output(
                        connection,
                        scope,
                        eid,
                        first.node_run_ref.record_id,
                        first.attempt_ref.record_id,
                        "worker.message",
                        DecisionMessage,
                    )
                    assert message is not None
                    context_node = next(
                        row
                        for row in await repository.list_nodes(connection, scope, eid)
                        if row.node_id is NodeId.N01
                    )
                    gate = await repository.read_engine_output(
                        connection,
                        scope,
                        eid,
                        context_node.node_run_id,
                        "worker.eligibility",
                        EligibilityMessage,
                    )
                    assert gate is not None

                async def store_finalization(record):
                    body = record.stable_json()
                    async with owner.begin() as connection:
                        await connection.execute(
                            text("""
                                UPDATE jimu_workflow.execution_finalizations
                                SET record = CAST(:body AS json), finalization_digest = :digest
                                WHERE execution_id = :eid
                            """),
                            {
                                "eid": eid,
                                "body": body,
                                "digest": sha256(body.encode()).hexdigest(),
                            },
                        )

                async def store_eligibility(record):
                    body = record.stable_json()
                    async with owner.begin() as connection:
                        await connection.execute(
                            text("""
                                UPDATE jimu_workflow.engine_outputs
                                SET record = CAST(:body AS json), output_digest = :digest
                                WHERE execution_id = :eid AND output_key = 'worker.eligibility'
                            """),
                            {
                                "eid": eid,
                                "body": body,
                                "digest": sha256(body.encode()).hexdigest(),
                            },
                        )

                context_id = gate.evaluation.context_snapshot_ref.record_id
                context_substitutions = {context_id: "different.context.snapshot"}
                await store_eligibility(_substitute_ids(gate, context_substitutions))
                await store_finalization(_substitute_ids(final, context_substitutions))
                with pytest.raises(
                    ArtifactReplayError,
                    match="artifact_eligibility_source_missing_or_mismatched",
                ):
                    await service.prepare_request(scope, source)
                await store_eligibility(gate)

                first_object, second_object = (
                    first.object_ref.record_id,
                    second.object_ref.record_id,
                )
                assert first.profile_ref != second.profile_ref
                await store_finalization(
                    _substitute_ids(
                        final,
                        {
                            first_object: second_object,
                            second_object: first_object,
                        },
                    )
                )
                with pytest.raises(
                    ArtifactReplayError, match="artifact_coverage_objects_mismatch"
                ):
                    await service.prepare_request(scope, source)

                # Both Decisions and all selected Facts remain in the Execution.
                # Change the first Decision to cite an existing second Object's Fact.
                first_fact = first.explanation.fact_refs[0].record_id
                second_fact = second.explanation.fact_refs[0].record_id
                fact_substitutions = {first_fact: second_fact}
                await store_finalization(_substitute_ids(final, fact_substitutions))
                wrong_decision = _substitute_ids(first, fact_substitutions)
                altered = message.model_copy(
                    update={
                        "draft": message.draft.model_copy(
                            update={"decision": wrong_decision}
                        )
                    }
                )
                body = altered.stable_json()
                async with owner.begin() as connection:
                    await connection.execute(
                        text("""
                            UPDATE jimu_workflow.produced_outputs
                            SET record = CAST(:body AS json), output_digest = :digest
                            WHERE execution_id = :eid AND node_run_id = :nid
                                AND output_key = 'worker.message'
                        """),
                        {
                            "eid": eid,
                            "nid": first.node_run_ref.record_id,
                            "body": body,
                            "digest": sha256(body.encode()).hexdigest(),
                        },
                    )
                with pytest.raises(
                    ArtifactReplayError, match="artifact_decision_fact_owner_mismatch"
                ):
                    await service.prepare_request(scope, source)
            finally:
                await app.dispose()
                await owner.dispose()

    asyncio.run(run())
