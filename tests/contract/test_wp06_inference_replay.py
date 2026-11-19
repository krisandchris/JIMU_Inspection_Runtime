"""Run same-pin replay through the normal Worker against isolated PostgreSQL."""

import asyncio
import json
from collections import Counter
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy import text

from examples.nonmotor_vehicle_illegal_parking.compilable.lifecycle import ACTOR, SCOPE
from examples.nonmotor_vehicle_illegal_parking.compilable.scene import LIMITS
from jimu_runtime.bundle.semantic_inputs import require_definition
from jimu_runtime.domain.definitions.tools import ToolDefinition
from jimu_runtime.domain.enums import ArtifactKind, ExecutionStatus, TaskStatus
from jimu_runtime.domain.runtime.replay import (
    ExecutionRef,
    InferenceReplayRequest,
    ReplayExecutionSource,
)
from jimu_runtime.gateway.bridge_tools import ToolExecutionAdapter
from jimu_runtime.inspection.geometry import synthetic_cpu_implementations
from jimu_runtime.inspection.geometry.node import GeometryNodeAdapter
from jimu_runtime.replay.inference import InferenceReplayError, InferenceReplayService
from jimu_runtime.tools.executor import ToolExecutor
from jimu_runtime.tools.registry import ToolWorkerRegistry
from jimu_runtime.tools.step_attempts import StepAttemptStore
from jimu_runtime.workflow import repository
from jimu_runtime.workflow.intake import (
    IntakeAuthorizationError,
    IntakeIdentity,
    IntakeService,
)
from jimu_runtime.workflow.mock_support import record_digest
from jimu_runtime.workflow.postgres import apply_migrations, create_postgres_engine
from jimu_runtime.workflow.worker import WorkerSourceError, WorkerStep
from jimu_runtime.workflow.worker_capabilities import read_historical_capabilities
from jimu_runtime.workflow.worker_inspection import (
    VerifiedMediaWorkerHost,
    inspection_worker,
)
from jimu_runtime.workflow.worker_state import load_records, read_context_snapshot
from tests.fixtures.wp03.postgres.support import isolated_postgres
from tests.fixtures.wp03.worker.support import (
    LIMITS as SYNTHETIC_LIMITS,
)
from tests.fixtures.wp03.worker.support import (
    SCHEDULING,
    FixtureWorkerHost,
    accepted_worker,
    open_worker_fixture,
    worker_submission,
)
from tests.fixtures.wp04 import http_support
from tests.fixtures.wp05.perception import perception_fixture

loopback_only = http_support.loopback_only
_REPLIES = {
    "profile_discovery-result": {"profile_hits": ["nonmotor_vehicle"]},
    "object_grounding-result": {"boxes": [{"xyxy": [40, 23, 52, 33]}]},
    "object_observation-result": {
        "vehicle_type": "electric_bicycle",
        "parking_state": "parked",
        "parking_position": "over_boundary",
    },
    "candidate_prediction-result": {"categories": []},
}


@dataclass
class ReplayHost:
    worker_host: VerifiedMediaWorkerHost | FixtureWorkerHost
    allowed_principal: object
    authorization_count: int = 0
    media_count: int = 0
    subject: str = "tester"
    media_barrier: asyncio.Barrier | None = None

    async def authorize_inference_replay(self, principal, scope, request, task):
        self.authorization_count += 1
        if principal is not self.allowed_principal or task.idempotency_scope != scope:
            raise IntakeAuthorizationError()
        return IntakeIdentity(
            issuer="https://fixture.invalid/replay", subject=self.subject
        )

    async def verified_original(self, scope, media_ref):
        self.media_count += 1
        if self.media_barrier is not None:
            await self.media_barrier.wait()
        return await self.worker_host.verified_original(scope, media_ref)


def test_replay_enqueues_and_executes_fresh_attempts_under_the_original_release(
    tmp_path: Path, loopback_only: set[int], monkeypatch: pytest.MonkeyPatch
) -> None:
    async def run() -> None:
        calls: Counter[str] = Counter()

        async def peer(
            reader: asyncio.StreamReader, writer: asyncio.StreamWriter
        ) -> None:
            request = await http_support.read_request(reader)
            body = json.loads(request.body)
            schema = body["response_format"]["json_schema"]["schema"]
            name = schema["$id"].split(":")[-2]
            calls[name] += 1
            raw = http_support.chat_body(
                model=body["model"], content=json.dumps(_REPLIES[name])
            )
            writer.write(
                http_support.response(
                    raw,
                    headers=(
                        (b"X-Request-ID", f"replay-fixture-{calls.total()}".encode()),
                        (b"X-Jimu-Deployment-Version", b"1.0.0"),
                        (b"X-Jimu-Model-Version", b"1.0.0"),
                    ),
                )
            )
            await writer.drain()

        async with http_support.http_peer(loopback_only, peer) as url:
            async with perception_fixture(tmp_path, url, with_media=True) as fixture:
                media = fixture.media_store
                assert media is not None
                task, source = fixture.accepted.task, fixture.accepted.execution
                scope, source_id = task.idempotency_scope, source.execution_id
                app = fixture.application
                catalogue = fixture.scene.execution.catalogue
                definition_tools = tuple(
                    require_definition(catalogue, item.ref, ToolDefinition)
                    for item in catalogue.of_kind(ArtifactKind.TOOL)
                )
                tools = GeometryNodeAdapter(
                    media,
                    ToolExecutionAdapter(
                        ToolExecutor(
                            ToolWorkerRegistry(
                                synthetic_cpu_implementations(
                                    fixture.resources,
                                    "cpu",
                                    "inspection-tools",
                                    definition_tools,
                                    fixture.tool_proofs,
                                    synthetic_image_digests=frozenset(
                                        {task.media_ref.sha256}
                                    ),
                                )
                            )
                        ),
                        StepAttemptStore(app, actor=ACTOR),
                        fixture.journal,
                        fixture.host.capabilities,
                    ),
                )
                capture_reads = 0

                async def capture_registry(scope, task):
                    nonlocal capture_reads
                    capture_reads += 1
                    assert capture_reads == 1
                    verified = await fixture.host.worker_source(scope, task)
                    return verified.device, verified.camera

                host = VerifiedMediaWorkerHost(
                    media, fixture.host.capabilities, capture_registry
                )

                def open_worker():
                    return inspection_worker(
                        app,
                        fixture.releases,
                        fixture.store,
                        host=host,
                        bundle_limits=LIMITS,
                        media=media,
                        gateway=fixture.gateway,
                        execute_tools=tools.execute,
                        model_resource_scope_id="model",
                    )

                async def finish(eid):
                    steps = []
                    for index in range(20):
                        step = await open_worker().run_once(
                            scope, eid, owner=f"fixture-worker-{index}", lease_ms=10000
                        )
                        steps.append(step)
                        if step is WorkerStep.FINALIZED:
                            return await open_worker().read_finalization(scope, eid)
                    raise AssertionError(steps)

                async def history(eid):
                    async with app.connect() as connection:
                        locked = await repository.get_execution(connection, scope, eid)
                        assert locked is not None
                        events = await repository.list_events(connection, scope, eid)
                        historical = await read_historical_capabilities(
                            connection, scope, locked.execution
                        )
                        snapshot = await read_context_snapshot(
                            connection, scope, locked
                        )
                        ids = {}
                        for table, id_column in (
                            ("attempts", "attempt_id"),
                            ("jobs", "job_id"),
                            ("dispatch_intents", "dispatch_id"),
                            ("tool_step_attempts", "child_attempt_id"),
                        ):
                            rows = await connection.execute(
                                text(
                                    f"SELECT {id_column} FROM jimu_workflow.{table} "
                                    "WHERE project_id = :project_id AND client_id = :client_id "
                                    "AND execution_id = :eid"
                                ),
                                {**scope.model_dump(), "eid": eid},
                            )
                            ids[table] = frozenset(rows.scalars())
                    final = await open_worker().read_finalization(scope, eid)
                    return locked, events, historical, snapshot, ids, final

                principal = object()
                replay_host = ReplayHost(host, principal)

                def open_replay(engine=app):
                    return InferenceReplayService(
                        engine,
                        fixture.releases,
                        fixture.store,
                        host=replay_host,
                        bundle_limits=LIMITS,
                    )

                request = InferenceReplayRequest(
                    schema_version="1.0.0",
                    kind="inference",
                    source=ReplayExecutionSource(
                        execution_ref=ExecutionRef(
                            record_kind="execution", record_id=source_id
                        ),
                        task_id=task.task_id,
                        pins=source.pins,
                    ),
                    target_pins=source.pins,
                )

                async def submit(
                    key="fixture-replay", accepted_request=request, **kwargs
                ):
                    return await open_replay(kwargs.pop("engine", app)).submit(
                        kwargs.pop("scope", scope),
                        accepted_request,
                        idempotency_key=key,
                        principal=kwargs.pop("principal", principal),
                    )

                with pytest.raises(
                    InferenceReplayError, match="source_context_pending"
                ):
                    await submit()
                assert replay_host.media_count == 0
                assert (
                    await open_worker().run_once(
                        scope, source_id, owner="source-context", lease_ms=10000
                    )
                    is WorkerStep.APPLIED
                )
                in_progress = await history(source_id)
                assert in_progress[0].execution.status is ExecutionStatus.RUNNING
                assert in_progress[3] is not None
                async with app.begin() as connection:
                    assert await repository.lock_execution(connection, scope, source_id)
                    active_replay = await asyncio.wait_for(submit("active-source"), 10)
                    active_id = active_replay.target_execution_ref.record_id
                    assert await asyncio.wait_for(
                        open_worker().request_cancel(scope, active_id), 10
                    )
                    assert (
                        await asyncio.wait_for(
                            open_worker().run_once(
                                scope, active_id, owner="cancel-active", lease_ms=10000
                            ),
                            10,
                        )
                        is WorkerStep.CANCELLED
                    )
                assert await history(source_id) == in_progress

                # Hold the actual Gateway dispatch transaction after it has
                # acquired the source Execution lock, before any HTTP send.
                journal_begin = fixture.journal.begin
                gateway_locked, continue_dispatch = asyncio.Event(), asyncio.Event()

                async def begin_with_witness(
                    claim, attempt_id, intent, *, live_guard=None, budget_guard=None
                ):
                    async def witness(connection):
                        if claim.execution_id == source_id:
                            gateway_locked.set()
                            await continue_dispatch.wait()
                        return await budget_guard(connection) if budget_guard else None

                    return await journal_begin(
                        claim,
                        attempt_id,
                        intent,
                        live_guard=live_guard,
                        budget_guard=witness,
                    )

                with monkeypatch.context() as dispatch:
                    dispatch.setattr(fixture.journal, "begin", begin_with_witness)
                    source_step = asyncio.create_task(
                        open_worker().run_once(
                            scope, source_id, owner="source-dispatch", lease_ms=10000
                        )
                    )
                    try:
                        await asyncio.wait_for(gateway_locked.wait(), 5)
                        during_dispatch = await asyncio.wait_for(
                            submit("during-dispatch"), 5
                        )
                        assert not calls
                    finally:
                        continue_dispatch.set()
                        assert (
                            await asyncio.wait_for(source_step, 10)
                            is WorkerStep.APPLIED
                        )
                during_id = during_dispatch.target_execution_ref.record_id
                assert calls == {"profile_discovery-result": 1}
                assert await open_worker().request_cancel(scope, during_id)
                assert (
                    await open_worker().run_once(
                        scope, during_id, owner="cancel-during-dispatch", lease_ms=10000
                    )
                    is WorkerStep.CANCELLED
                )

                final_source = await finish(source_id)
                assert final_source is not None
                assert {row.result.value for row in final_source.decisions} == {
                    "violation"
                }
                original_history = await history(source_id)
                assert original_history[0].task_status is TaskStatus.COMPLETED
                before = calls.copy()
                assert before == dict.fromkeys(_REPLIES, 1)
                active = fixture.releases.active_activation(SCOPE)
                assert active is not None
                assert active.release_ref.record_id != source.pins.release_id

                async def enqueue_interrupted(*args, **kwargs):
                    raise RuntimeError("fixture aborts the replay acceptance")

                with monkeypatch.context() as fault:
                    fault.setattr(repository, "ready_root_job", enqueue_interrupted)
                    with pytest.raises(
                        RuntimeError, match="aborts the replay acceptance"
                    ):
                        await submit()
                async with app.connect() as connection:
                    assert (
                        await connection.scalar(
                            text("SELECT count(*) FROM jimu_workflow.executions")
                        )
                        == 3
                    )
                    assert (
                        await connection.scalar(
                            text(
                                "SELECT count(*) FROM jimu_workflow.inference_replay_runs"
                            )
                        )
                        == 2
                    )
                strict_engine = app.execution_options(isolation_level="SERIALIZABLE")
                replay_host.media_barrier = asyncio.Barrier(2)
                enqueue = repository.ready_root_job

                async def hold_until_contended(connection, *args, **kwargs):
                    assert await connection.get_isolation_level() == "READ COMMITTED"
                    async with app.connect() as observed:
                        async with asyncio.timeout(8):
                            while not await observed.scalar(
                                text("""
                                SELECT EXISTS (
                                    SELECT 1 FROM pg_catalog.pg_locks
                                    WHERE locktype = 'advisory' AND NOT granted
                                      AND database = (
                                          SELECT oid FROM pg_catalog.pg_database
                                          WHERE datname = pg_catalog.current_database()
                                      )
                                )
                            """)
                            ):
                                await asyncio.sleep(0.01)
                    await enqueue(connection, *args, **kwargs)

                with monkeypatch.context() as race:
                    race.setattr(repository, "ready_root_job", hold_until_contended)
                    run_one, run_two = await asyncio.wait_for(
                        asyncio.gather(
                            submit(engine=strict_engine), submit(engine=strict_engine)
                        ),
                        20,
                    )
                replay_host.media_barrier = None
                assert run_one == run_two
                eid = run_one.target_execution_ref.record_id
                assert eid != source_id
                assert calls == before
                rejected = object()
                with pytest.raises(IntakeAuthorizationError) as existing_denial:
                    await submit(principal=rejected)
                unknown = request.model_copy(
                    update={
                        "source": request.source.model_copy(
                            update={
                                "execution_ref": ExecutionRef(
                                    record_kind="execution",
                                    record_id="execution.absent",
                                )
                            }
                        )
                    }
                )
                with pytest.raises(IntakeAuthorizationError) as missing_denial:
                    await submit(accepted_request=unknown, principal=rejected)
                assert existing_denial.value.code == missing_denial.value.code
                with pytest.raises(
                    IntakeAuthorizationError, match=existing_denial.value.code
                ):
                    await submit(scope=scope.model_copy(update={"client_id": "other"}))
                with pytest.raises(InferenceReplayError, match="source_pin_mismatch"):
                    await submit(
                        "bad-source",
                        request.model_copy(
                            update={
                                "source": request.source.model_copy(
                                    update={
                                        "pins": source.pins.model_copy(
                                            update={"runtime_version": "9.9.9"}
                                        )
                                    }
                                )
                            }
                        ),
                    )
                with pytest.raises(
                    InferenceReplayError, match="target_pins_unsupported"
                ):
                    await submit(
                        "bad-pins",
                        request.model_copy(
                            update={
                                "target_pins": source.pins.model_copy(
                                    update={"runtime_version": "9.9.9"}
                                )
                            }
                        ),
                    )
                assert await submit() == run_one
                assert replay_host.authorization_count == 10
                replay_host.subject = "other-authorized-principal"
                with pytest.raises(InferenceReplayError, match="key_conflict"):
                    await submit()
                replay_host.subject = "tester"
                queued, _, _, _, queued_ids, _ = await history(eid)
                assert queued.is_inference_replay
                assert queued.task == task
                assert queued.task_status is TaskStatus.COMPLETED
                assert queued.execution.status is ExecutionStatus.INITIALIZED
                assert queued_ids["attempts"] == frozenset()
                assert len(queued_ids["jobs"]) == 1

                final_target = await finish(eid)
                assert final_target is not None
                assert {row.result.value for row in final_target.decisions} == {
                    "violation"
                }
                assert calls == dict.fromkeys(_REPLIES, 2)
                assert await history(source_id) == original_history
                replayed, events, capabilities, snapshot, ids, _ = await history(eid)
                assert replayed.execution.status is ExecutionStatus.COMPLETED
                assert replayed.task_status is TaskStatus.COMPLETED
                assert replayed.execution.pins == source.pins
                assert capabilities == original_history[2]
                assert snapshot is not None
                assert original_history[3] is not None
                assert snapshot.execution_id == eid
                assert snapshot.device_ref == original_history[3].device_ref
                assert snapshot.camera_ref == original_history[3].camera_ref
                assert capture_reads == 1
                for table, source_ids in original_history[4].items():
                    assert source_ids and len(ids[table]) == len(source_ids)
                    assert ids[table].isdisjoint(source_ids)
                assert len(ids["tool_step_attempts"]) == 3
                assert events
                assert all(
                    row.payload.event_kind != "task_transition" for row in events
                )
                replay_as_source = request.model_copy(
                    update={
                        "source": request.source.model_copy(
                            update={"execution_ref": run_one.target_execution_ref}
                        )
                    }
                )
                with pytest.raises(InferenceReplayError, match="key_conflict"):
                    await submit(accepted_request=replay_as_source)

                cancel_run = await submit("cancel-target")
                cancelled_id = cancel_run.target_execution_ref.record_id
                assert await open_worker().request_cancel(scope, cancelled_id)
                assert (
                    await open_worker().run_once(
                        scope, cancelled_id, owner="cancel-fixture", lease_ms=10000
                    )
                    is WorkerStep.CANCELLED
                )
                cancelled, _, _, _, cancelled_ids, final = await history(cancelled_id)
                assert cancelled.execution.status is ExecutionStatus.CANCELLED
                assert cancelled.task_status is TaskStatus.COMPLETED
                assert cancelled_ids["attempts"] == frozenset()
                assert final is None
                assert calls == dict.fromkeys(_REPLIES, 2)
                assert await history(source_id) == original_history
                assert (await history(eid))[0] == replayed

                # The cancelled replay never reached N01. Its own replay still
                # inherits the immutable registry revisions selected upstream.
                cancelled_as_source = request.model_copy(
                    update={
                        "source": request.source.model_copy(
                            update={"execution_ref": cancel_run.target_execution_ref}
                        )
                    }
                )
                chained = await submit("chained-target", cancelled_as_source)
                chained_id = chained.target_execution_ref.record_id
                assert (
                    await open_worker().run_once(
                        scope, chained_id, owner="chained-context", lease_ms=10000
                    )
                    is WorkerStep.APPLIED
                )
                chained_snapshot = (await history(chained_id))[3]
                assert chained_snapshot is not None
                assert chained_snapshot.execution_id == chained_id
                assert chained_snapshot.device_ref == snapshot.device_ref
                assert chained_snapshot.camera_ref == snapshot.camera_ref
                assert capture_reads == 1
                assert calls == dict.fromkeys(_REPLIES, 2)
                current_routing = fixture.gateway._current

                async def revoked(scope):
                    current = await current_routing(scope)
                    return current.model_copy(
                        update={
                            "ownership": tuple(
                                row.model_copy(update={"revoked_at": datetime.now(UTC)})
                                for row in current.ownership
                            )
                        }
                    )

                with monkeypatch.context() as revoked_host:
                    revoked_host.setattr(fixture.gateway, "_current", revoked)
                    await finish(chained_id)
                async with app.connect() as connection:
                    locked = await repository.get_execution(
                        connection, scope, chained_id
                    )
                    assert locked is not None
                    state = await load_records(connection, scope, locked)
                    discovery = next(
                        value
                        for key, value in state.messages.items()
                        if key.node_id.value == "N02"
                    )
                    assert any(
                        failure.error.code == "model.ownership_unavailable"
                        for failure in discovery.group_failures
                    )
                assert calls == dict.fromkeys(_REPLIES, 2)
                assert await history(source_id) == original_history

    asyncio.run(run())


def test_replay_rejects_unfrozen_terminal_and_tampered_sibling_capture(
    tmp_path: Path,
) -> None:
    async def run() -> None:
        with isolated_postgres() as database:
            owner = create_postgres_engine(database.migration_url)
            app = create_postgres_engine(database.application_url)
            try:
                await apply_migrations(owner, database.target)
                worker, accepted, host = await accepted_worker(app, tmp_path)
                store, releases, _ = open_worker_fixture(tmp_path, host)
                scope = accepted.task.idempotency_scope
                principal = object()
                replay_host = ReplayHost(host, principal)
                service = InferenceReplayService(
                    app,
                    releases,
                    store,
                    host=replay_host,
                    bundle_limits=SYNTHETIC_LIMITS,
                )

                def inference_request(intake):
                    execution = intake.execution
                    return InferenceReplayRequest(
                        schema_version="1.0.0",
                        kind="inference",
                        source=ReplayExecutionSource(
                            execution_ref=ExecutionRef(
                                record_kind="execution",
                                record_id=execution.execution_id,
                            ),
                            task_id=intake.task.task_id,
                            pins=execution.pins,
                        ),
                        target_pins=execution.pins,
                    )

                async def submit(key, request):
                    return await service.submit(
                        scope, request, idempotency_key=key, principal=principal
                    )

                async def next_step(eid):
                    return await worker.run_once(
                        scope, eid, owner="replay-review", lease_ms=10000
                    )

                cancelled = accepted.execution.execution_id
                assert await worker.request_cancel(scope, cancelled)
                assert await next_step(cancelled) is WorkerStep.CANCELLED
                async with app.connect() as connection:
                    assert await read_historical_capabilities(
                        connection, scope, accepted.execution
                    )
                with pytest.raises(
                    InferenceReplayError, match="source_context_unavailable"
                ):
                    await submit("never-frozen", inference_request(accepted))
                assert replay_host.media_count == 0
                async with app.connect() as connection:
                    assert (
                        await connection.scalar(
                            text(
                                "SELECT count(*) FROM jimu_workflow.inference_replay_runs"
                            )
                        )
                        == 0
                    )

                intake = IntakeService(
                    app,
                    releases,
                    store,
                    host=host,
                    bundle_limits=SYNTHETIC_LIMITS,
                    scheduling_limits=SCHEDULING,
                    max_submission_bytes=65536,
                )
                source = await intake.submit(
                    worker_submission("snapshot-lineage"),
                    principal=host.release_host.task_client,
                )
                assert (
                    await next_step(source.execution.execution_id) is WorkerStep.APPLIED
                )
                request = inference_request(source)
                first = await submit("first-sibling", request)
                sibling_id = first.target_execution_ref.record_id
                assert await next_step(sibling_id) is WorkerStep.APPLIED
                second = await submit("second-sibling", request)
                target_id = second.target_execution_ref.record_id
                async with app.connect() as connection:
                    sibling_execution = await repository.get_execution(
                        connection, scope, sibling_id
                    )
                    assert sibling_execution is not None
                    context = await read_context_snapshot(
                        connection, scope, sibling_execution
                    )
                    assert context is not None

                # The isolated owner injects a valid FK and a matching digest that
                # point to a same-Task, same-pins sibling rather than the source.
                async with owner.begin() as connection:
                    changed = await connection.scalar(
                        text("""
                            UPDATE jimu_workflow.inference_replay_runs
                            SET capture_execution_id = :sibling, capture_snapshot_digest = :digest
                            WHERE project_id = :project_id AND client_id = :client_id
                              AND target_execution_id = :target
                            RETURNING target_execution_id
                        """),
                        {
                            **scope.model_dump(),
                            "sibling": sibling_id,
                            "digest": record_digest(context),
                            "target": target_id,
                        },
                    )
                    assert changed == target_id
                with pytest.raises(
                    WorkerSourceError, match="worker_source_invalid"
                ) as invalid:
                    await next_step(target_id)
                assert (
                    str(invalid.value.__cause__)
                    == "inference replay capture is outside the source lineage"
                )
                with pytest.raises(ValueError, match="outside the source lineage"):
                    await submit("second-sibling", request)
                async with app.connect() as connection:
                    target = await repository.get_execution(
                        connection, scope, target_id
                    )
                    assert (
                        target is not None
                        and target.execution.status is ExecutionStatus.INITIALIZED
                    )
            finally:
                await app.dispose()
                await owner.dispose()

    asyncio.run(run())
