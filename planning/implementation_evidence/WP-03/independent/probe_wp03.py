"""Independent public-entry probes against one disposable PostgreSQL 16 database."""

import asyncio
import json
import os
import sys
from collections import Counter
from dataclasses import dataclass, replace
from hashlib import sha256
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

import rfc8785
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncEngine

from jimu_runtime.bundle.archive import read_archive
from jimu_runtime.bundle.compiler import (
    BundleCompileInput,
    ReferenceSlot,
    SourceIdentity,
)
from jimu_runtime.bundle.integrity import verify_bundle_files
from jimu_runtime.bundle.store import LocalBundleStore
from jimu_runtime.domain.enums import ArtifactKind, NodeRunStatus, RuntimeRecordKind
from jimu_runtime.domain.nodes.envelopes import FailedNodeOutput, NodeError
from jimu_runtime.domain.references import RuntimeRecordRef
from jimu_runtime.domain.runtime.executions import (
    NodeId,
    PinnedRuntimeInput,
    TraceContext,
)
from jimu_runtime.domain.runtime.facts import ProfileContentRef
from jimu_runtime.domain.runtime.intake import (
    AcceptedTask,
    IdempotencyScope,
    RegistryRecordRef,
)
from jimu_runtime.domain.runtime.invocations import ModelInvocation
from jimu_runtime.domain.runtime.media import MediaAsset, MediaRef
from jimu_runtime.workflow.engine import ExecutionFinalization
from jimu_runtime.workflow.intake import (
    IntakeAuthorizationError,
    IntakeIdentity,
    IntakeService,
)
from jimu_runtime.workflow.jobs import DurableJobs, ResponseEvidence
from jimu_runtime.workflow.mock_perception import SyntheticGroundingCase
from jimu_runtime.workflow.postgres import apply_migrations, create_postgres_engine
from jimu_runtime.workflow.scheduling import JobBudget, SchedulingLimits
from jimu_runtime.workflow.worker_source import SyntheticScenario, WorkerSource
from tests.fixtures.wp02.compiler.scenario import (
    DEPLOYMENT,
    LIMITS,
    approve_source_bytes,
    capability_snapshot,
)
from tests.fixtures.wp02.releases.support import GLOBAL, FixtureHost
from tests.fixtures.wp03.intake.support import FixtureIntakeHost, manager_for
from tests.fixtures.wp03.nodes.scenario import (
    RGB_IMAGE,
    parking_fixture,
    parking_source,
)
from tests.fixtures.wp03.pins.support import activate_scenario
from tests.fixtures.wp03.postgres.support import POSTGRES_IMAGE, isolated_postgres

VERSION = "1.0.0"
HANDSHAKE_SECONDS = 25
BUDGET = SchedulingLimits(
    schema_version=VERSION,
    version=VERSION,
    default=JobBudget(
        max_attempts=2,
        timeout_ms=15_000,
        retry_backoff_ms=(20,),
        retryable_error_classes=("transport",),
        retry_on_lease_loss=True,
    ),
    overrides=(),
)


def emit(label: str, **data: Any) -> None:
    print(json.dumps({"check": label, **data}, sort_keys=True), flush=True)


@dataclass
class ParkingIntake(FixtureIntakeHost):
    asset: MediaAsset | None = None

    async def authorize_submission(
        self, principal: object, original: Any
    ) -> IntakeIdentity:
        assert self.asset is not None
        if (
            principal is not self.release_host.task_client
            or original.idempotency_scope != self.scope
            or original.media_ref != self.asset.media_ref
            or self.release_host.resolve(principal, original) is None
        ):
            raise IntakeAuthorizationError()
        return self.restored_identity

    async def verified_original(
        self, scope: IdempotencyScope, media_ref: MediaRef
    ) -> MediaAsset:
        if self.asset is None or (scope, media_ref) != (
            self.scope,
            self.asset.media_ref,
        ):
            raise IntakeAuthorizationError()
        return self.asset


@dataclass(frozen=True)
class WorkerInputs:
    capability_host: FixtureIntakeHost
    source: WorkerSource

    async def capabilities(self, scope: IdempotencyScope) -> Any:
        return await self.capability_host.capabilities(scope)

    async def worker_source(
        self, scope: IdempotencyScope, task: AcceptedTask
    ) -> WorkerSource:
        assert (
            scope == task.idempotency_scope
            and task.media_ref == self.source.media.media_ref
        )
        return self.source


def child_reply(label: str, **data: Any) -> None:
    print(
        json.dumps({"label": label, "pid": os.getpid(), **data}, sort_keys=True),
        flush=True,
    )


async def child() -> None:
    config = json.loads(await asyncio.to_thread(sys.stdin.readline))
    db = create_postgres_engine(make_url(config["url"]))
    scope = IdempotencyScope.model_validate(config["scope"])
    execution_id = config["execution_id"]
    owner = config["owner"]
    jobs = DurableJobs(db)
    claim = attempt = None
    worker = None
    mode = config.get("mode", "full")
    invocation_control = {"pause_after_mock": False}
    try:
        async with db.connect() as connection:
            backend = await connection.scalar(text("SELECT pg_backend_pid()"))
        child_reply("ready", backend=backend)
        while line := await asyncio.to_thread(sys.stdin.readline):
            action = json.loads(line)
            name = action["name"]
            if name == "claim":
                claim = await jobs.claim(
                    scope, owner, action["ms"], execution_id=execution_id
                )
                child_reply(name, fence=claim.fence if claim else None)
            elif name == "start":
                assert claim is not None
                attempt = await jobs.start(claim)
                child_reply(name, attempt=attempt.attempt_id if attempt else None)
            elif name == "heartbeat":
                assert claim is not None
                renewed = await jobs.heartbeat(claim, action["ms"])
                if renewed is not None:
                    claim = renewed
                child_reply(name, renewed=renewed is not None)
            elif name == "cancel":
                child_reply(
                    name, cancelled=await jobs.request_cancel(scope, execution_id)
                )
            elif name == "late":
                assert (
                    claim is not None
                    and attempt is not None
                    and attempt.started_at is not None
                )
                outcome = FailedNodeOutput(
                    schema_version=VERSION,
                    node_id=NodeId.N01,
                    node_run_id=claim.node_run_id,
                    attempt_id=attempt.attempt_id,
                    status=NodeRunStatus.FAILED,
                    trace=TraceContext.model_validate(config["trace"]),
                    output_artifacts=(),
                    started_at=attempt.started_at,
                    finished_at=attempt.started_at,
                    duration_ms=0,
                    error=NodeError(
                        code="probe_transport",
                        error_class="transport",
                        retryable=True,
                        message="Synthetic late result from the former owner",
                        validation_issues=(),
                    ),
                )
                response = ResponseEvidence(
                    schema_version=VERSION,
                    execution_id=execution_id,
                    attempt_id=attempt.attempt_id,
                    response_key="independent-late",
                    external_request_id="fixture-request-a",
                    response=PinnedRuntimeInput(
                        ref=RuntimeRecordRef(
                            record_kind=RuntimeRecordKind.EVIDENCE,
                            record_id="fixture-late-bytes",
                        ),
                        digest="f" * 64,
                    ),
                )
                result = await jobs.complete(claim, outcome, response=response)
                child_reply(name, disposition=result.value)
            elif name in {"worker", "poll"}:
                invocation_control["pause_after_mock"] = action.get(
                    "pause_after_mock", False
                )
                if worker is None:
                    from jimu_runtime.workflow.worker import WorkflowWorker
                    from jimu_runtime.workflow.worker_nodes import invoke

                    store = LocalBundleStore(Path(config["ledger"]))
                    authority = FixtureHost()
                    profile_refs = tuple(
                        ProfileContentRef.model_validate(profile)
                        for profile in config["profiles"]
                    )
                    grounding = ()
                    if mode == "no_target":
                        grounding = tuple(
                            SyntheticGroundingCase(profile_ref=profile, object_count=0)
                            for profile in profile_refs
                        )
                    elif mode == "partial":
                        grounding = (
                            SyntheticGroundingCase(
                                profile_ref=profile_refs[0], object_count=1
                            ),
                            SyntheticGroundingCase(
                                profile_ref=profile_refs[1],
                                object_count=0,
                                disposition="failed",
                            ),
                        )
                    elif mode == "full":
                        grounding = tuple(
                            SyntheticGroundingCase(
                                profile_ref=profile, object_count=2 if i == 0 else 1
                            )
                            for i, profile in enumerate(profile_refs)
                        )
                    scenario = SyntheticScenario(
                        discovery_hits=() if mode == "no_hits" else None,
                        grounding=grounding,
                    )
                    fields = config["source"]
                    host = WorkerInputs(
                        FixtureIntakeHost(authority),
                        WorkerSource(
                            media=MediaAsset.model_validate_json(
                                json.dumps(fields["media"])
                            ),
                            device=RegistryRecordRef.model_validate(fields["device"]),
                            camera=RegistryRecordRef.model_validate(fields["camera"]),
                            fixture_deployment_id=DEPLOYMENT,
                            scenario=scenario,
                        ),
                    )

                    async def controlled_mock(*args: Any) -> Any:
                        mocked = await asyncio.to_thread(invoke, *args)
                        if invocation_control["pause_after_mock"]:
                            invocations = [
                                item.record
                                for item in mocked.artifacts
                                if isinstance(item.record, ModelInvocation)
                            ]
                            assert invocations and all(
                                i.node_id == NodeId.N02.value for i in invocations
                            )
                            (attempt_id,) = {
                                i.attempt_ref.record_id for i in invocations
                            }
                            child_reply(
                                "mock_returned",
                                invocations=[
                                    i.model_invocation_id for i in invocations
                                ],
                                external_request_ids=[
                                    i.provider_request_id for i in invocations
                                ],
                                attempt=attempt_id,
                            )
                            await asyncio.Event().wait()
                        return mocked

                    worker = WorkflowWorker(
                        db,
                        authority.manager(store),
                        store,
                        host=host,
                        bundle_limits=LIMITS,
                        invoke_node=controlled_mock,
                    )
                if name == "poll":
                    poll = await worker.run_next(scope, owner=owner, lease_ms=5_000)
                    child_reply(
                        name,
                        step=poll.step.value,
                        execution_id=poll.execution_id,
                        issues=len(poll.issues),
                    )
                else:
                    result = await worker.run_once(
                        scope,
                        execution_id,
                        owner=owner,
                        lease_ms=action.get("ms", 5_000),
                    )
                    child_reply(name, result=str(result))
            elif name == "query":
                assert worker is not None
                final = await worker.read_finalization(scope, execution_id)
                other = scope.model_copy(update={"client_id": "independent-other"})
                assert await worker.read_finalization(other, execution_id) is None
                child_reply(
                    name, final=final.model_dump(mode="json") if final else None
                )
            else:
                raise ValueError("unrecognized probe operation")
    finally:
        await db.dispose()


@dataclass
class Process:
    process: asyncio.subprocess.Process
    pid: int
    backend: int

    async def send(self, name: str, **values: Any) -> None:
        assert self.process.stdin is not None
        self.process.stdin.write((json.dumps({"name": name, **values}) + "\n").encode())
        await self.process.stdin.drain()

    async def ask(self, name: str, **values: Any) -> dict[str, Any]:
        await self.send(name, **values)
        result = await reply(self.process)
        assert result["label"] == name and result["pid"] == self.pid
        return result

    async def kill(self) -> None:
        if self.process.returncode is None:
            self.process.kill()
            code = await asyncio.wait_for(self.process.wait(), 5)
            assert code == -9
            emit(
                "worker_process_killed",
                pid=self.pid,
                pg_backend_pid=self.backend,
                exit=code,
            )


async def reply(process: asyncio.subprocess.Process) -> dict[str, Any]:
    assert process.stdout is not None
    line = await asyncio.wait_for(process.stdout.readline(), HANDSHAKE_SECONDS)
    if not line:
        raise RuntimeError(
            f"worker exited without a result, exit={await process.wait()}"
        )
    return json.loads(line)


async def launch(config: dict[str, Any], owner: str) -> Process:
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        __file__,
        "child",
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
    )
    assert process.stdin is not None
    process.stdin.write((json.dumps({**config, "owner": owner}) + "\n").encode())
    await process.stdin.drain()
    observed = await reply(process)
    assert observed["label"] == "ready" and observed["pid"] == process.pid
    return Process(process, observed["pid"], observed["backend"])


@dataclass(frozen=True)
class ExecutionProbe:
    app: AsyncEngine
    execution_id: str
    config: dict[str, Any]

    async def read(self, sql: str) -> list[Any]:
        async with self.app.connect() as connection:
            rows = await connection.execute(text(sql), {"eid": self.execution_id})
            return list(rows)

    async def counts(self) -> tuple[int, int, int]:
        rows = await self.read("""
            SELECT
              (SELECT count(*) FROM jimu_workflow.decision_drafts WHERE execution_id=:eid),
              (SELECT count(*) FROM jimu_workflow.execution_finalizations WHERE execution_id=:eid),
              (SELECT count(*) FROM jimu_workflow.authority_outputs a JOIN jimu_workflow.node_runs n
                  USING (project_id, client_id, execution_id, node_run_id)
               WHERE a.execution_id=:eid AND n.node_id='N06')
        """)
        return tuple(rows[0])


def joint_parking_source() -> BundleCompileInput:
    """Add a second independently typed observation to both proven Profile definitions."""
    initial = parking_source(second_profile=True)
    secondary_question = "parking_confirmation"
    secondary_schema = "parking_confirmation"
    secondary_fact = "parking_confirmation"
    question_identity = (ArtifactKind.QUESTION, secondary_question, VERSION)
    schema_identity = (ArtifactKind.FACT_SCHEMA, secondary_schema, VERSION)
    rows = []
    for item in initial.artifacts:
        document = json.loads(item.content)
        if item.kind is ArtifactKind.PROFILE:
            document["question_refs"].append(None)
            item = replace(
                item,
                reference_slots=(
                    *item.reference_slots,
                    ReferenceSlot(
                        location=("question_refs", 1), target=question_identity
                    ),
                ),
            )
        elif item.kind is ArtifactKind.FACET and item.artifact_id == "parking":
            document["fact_schema_refs"].append(None)
            item = replace(
                item,
                reference_slots=(
                    *item.reference_slots,
                    ReferenceSlot(
                        location=("fact_schema_refs", 1), target=schema_identity
                    ),
                ),
            )
        elif item.kind is ArtifactKind.POLICY and item.artifact_id == "selection":
            document["policy"]["optional_question_refs"].append(None)
            item = replace(
                item,
                reference_slots=(
                    *item.reference_slots,
                    ReferenceSlot(
                        location=("policy", "optional_question_refs", 0),
                        target=question_identity,
                    ),
                ),
            )
        elif item.kind is ArtifactKind.QUESTION and item.artifact_id == "parking_state":
            extra = {
                **document,
                "question_id": secondary_question,
                "objective": "Independently confirm the synthetic parking state",
                "primary_fact_key": secondary_fact,
            }
            slots = tuple(
                replace(slot, target=schema_identity)
                if slot.location == ("primary_fact_schema_ref",)
                else slot
                for slot in item.reference_slots
            )
            rows.append(
                replace(
                    item,
                    artifact_id=secondary_question,
                    content=json.dumps(extra).encode(),
                    origin=SourceIdentity(
                        artifact_id=f"question:{secondary_question}", version=VERSION
                    ),
                    reference_slots=slots,
                )
            )
        elif item.kind is ArtifactKind.FACT_SCHEMA and item.artifact_id == "parked":
            extra = {
                **document,
                "schema_id": secondary_schema,
                "semantic_key": secondary_fact,
            }
            rows.append(
                replace(
                    item,
                    artifact_id=secondary_schema,
                    content=json.dumps(extra).encode(),
                    path=f"schemas/facts/definitions/{secondary_schema}.json",
                    origin=SourceIdentity(
                        artifact_id=f"fact_schema:{secondary_schema}", version=VERSION
                    ),
                )
            )
        rows.append(replace(item, content=json.dumps(document).encode()))
    return approve_source_bytes(replace(initial, artifacts=tuple(rows)))


async def start_execution(
    app: AsyncEngine,
    work: Path,
    mode: str,
) -> ExecutionProbe:
    work.mkdir()
    variant = {
        "second_profile": mode in {"full", "partial"},
        "excluded": mode == "excluded",
    }
    preview = work / "fixture-inputs"
    preview.mkdir()
    parking = parking_fixture(preview, **variant)
    assert sha256(RGB_IMAGE).hexdigest() == parking.scope.media.media_ref.sha256
    assert len(RGB_IMAGE) == parking.scope.media.byte_size
    store = LocalBundleStore(work / "ledger")
    manager = manager_for(store, parking.host)
    activate_scenario(
        work / "admission",
        store,
        manager,
        parking.host,
        joint_parking_source() if mode == "full" else parking_source(**variant),
        capability_snapshot(),
        number=1,
    )
    submission = type(parking.submission).model_validate(
        {
            **parking.submission.model_dump(),
            "request_id": f"independent-{mode}",
            "idempotency_key": f"independent-{mode}",
        }
    )
    scope = submission.idempotency_scope
    host = ParkingIntake(parking.host, scope=scope, asset=parking.scope.media)
    intake = IntakeService(
        app,
        manager,
        store,
        host=host,
        bundle_limits=LIMITS,
        scheduling_limits=BUDGET,
        max_submission_bytes=65_536,
    )
    first, duplicate = await asyncio.gather(
        intake.submit(submission, principal=parking.host.task_client),
        intake.submit(submission, principal=parking.host.task_client),
    )
    assert first == duplicate
    assert first.execution.pins.release_id == parking.scope.task.release_id
    assert first.execution.pins.bundle_digest == first.task.bundle_digest
    pinned_release = manager.get_release(first.task.release_id)
    assert pinned_release is not None and pinned_release.admission_receipt is not None
    verified = verify_bundle_files(
        read_archive(
            store.read_archive(pinned_release.admission_receipt.receipt_id), LIMITS
        ),
        max_file_bytes=LIMITS.max_file_bytes,
        max_json_depth=LIMITS.max_json_depth,
    )
    assert verified.manifest.integrity.root_digest == first.task.bundle_digest
    profiles = [
        item.content_ref.model_dump(mode="json")
        for item in verified.manifest.artifacts
        if item.content_ref.artifact_kind is ArtifactKind.PROFILE
    ]
    context = parking.context_input()
    config = {
        "url": app.url.render_as_string(hide_password=False),
        "scope": scope.model_dump(mode="json"),
        "trace": first.execution.trace.model_dump(mode="json"),
        "release_id": first.task.release_id,
        "bundle_digest": first.task.bundle_digest,
        "execution_id": first.execution.execution_id,
        "mode": mode,
        "ledger": str(work / "ledger"),
        "profiles": profiles,
        "source": {
            "media": parking.scope.media.model_dump(mode="json"),
            "device": context.device_ref.model_dump(mode="json"),
            "camera": context.camera_ref.model_dump(mode="json"),
        },
    }
    wrong_scope = scope.model_copy(update={"client_id": "other-clients"})
    jobs = DurableJobs(app)
    assert (
        await jobs.claim(
            wrong_scope, "wrong-scope", 900, execution_id=first.execution.execution_id
        )
        is None
    )
    assert not await jobs.request_cancel(wrong_scope, first.execution.execution_id)
    emit(
        "admission_and_duplicate_intake",
        mode=mode,
        execution_id=first.execution.execution_id,
        bundle_digest=first.task.bundle_digest,
    )
    return ExecutionProbe(app, first.execution.execution_id, config)


async def permission_probe(app: AsyncEngine) -> None:
    statements = (
        "CREATE TABLE jimu_workflow.independent_denied (x integer)",
        "UPDATE jimu_workflow.schema_migrations SET script_sha256=script_sha256",
        "UPDATE jimu_workflow.execution_events SET event_number=event_number",
        "DELETE FROM jimu_workflow.attempts WHERE false",
        "TRUNCATE jimu_workflow.produced_outputs",
    )
    for statement in statements:
        async with app.begin() as connection:
            try:
                await connection.execute(text(statement))
            except DBAPIError as error:
                assert error.orig.sqlstate == "42501"
                emit(
                    "app_permission_rejected",
                    sql=statement,
                    sqlstate=error.orig.sqlstate,
                )
            else:
                raise AssertionError(f"unexpected runtime role permission: {statement}")


async def wait_db_time(probe: ExecutionProbe, expression: str) -> None:
    async with asyncio.timeout(8):
        while True:
            rows = await probe.read(
                f"SELECT {expression} FROM jimu_workflow.jobs WHERE execution_id=:eid"
            )
            if any(row[0] for row in rows):
                return
            await asyncio.sleep(0.015)


async def lease_probe(probe: ExecutionProbe, first: Process, second: Process) -> None:
    assert first.pid != second.pid and first.backend != second.backend
    assert (await first.ask("claim", ms=900))["fence"] == 1
    assert (await first.ask("start"))["attempt"] is not None
    [(before,)] = await probe.read(
        "SELECT lease_until FROM jimu_workflow.jobs WHERE execution_id=:eid"
    )
    assert (await first.ask("heartbeat", ms=1800))["renewed"]
    [(after,)] = await probe.read(
        "SELECT lease_until FROM jimu_workflow.jobs WHERE execution_id=:eid"
    )
    assert after > before
    assert (await second.ask("claim", ms=900))["fence"] is None
    async with probe.app.begin() as blocker:
        await blocker.execute(
            text("""
                SELECT t.task_id FROM jimu_workflow.tasks t
                JOIN jimu_workflow.executions e USING (project_id, client_id, task_id)
                WHERE e.execution_id=:eid FOR UPDATE OF t
            """),
            {"eid": probe.execution_id},
        )
        waiting = asyncio.create_task(first.ask("heartbeat", ms=900))
        async with asyncio.timeout(5):
            while not await blocker.scalar(
                text("""
                    SELECT wait_event_type='Lock'
                    FROM pg_catalog.pg_stat_activity WHERE pid=:pid
                """),
                {"pid": first.backend},
            ):
                await asyncio.sleep(0.015)
        assert not await blocker.scalar(
            text("SELECT pg_catalog.clock_timestamp() >= :deadline"),
            {"deadline": after},
        )
        await wait_db_time(probe, "pg_catalog.clock_timestamp() >= lease_until")
    assert not (await waiting)["renewed"]
    assert await probe.counts() == (0, 0, 0)
    emit(
        "heartbeat_and_expired_fence",
        before=before.isoformat(),
        after=after.isoformat(),
        contender_backend=second.backend,
    )
    for _ in range(20):
        takeover_step = await second.ask("worker")
        [(fence,)] = await probe.read(
            "SELECT fence FROM jimu_workflow.jobs WHERE execution_id=:eid AND fence>1"
        ) or [(None,)]
        if fence is not None:
            break
        await asyncio.sleep(0.03)
    assert fence == 2
    late = await first.ask("late")
    repeat = await first.ask("late")
    assert (
        late["disposition"] == "late_recorded" and repeat["disposition"] == "duplicate"
    )
    rows = await probe.read("""
        SELECT a.attempt_number, a.fence, a.lease_owner, r.status, l.external_request_id
        FROM jimu_workflow.attempts a JOIN jimu_workflow.attempt_results r
          USING (project_id, client_id, execution_id, node_run_id, attempt_id)
        LEFT JOIN jimu_workflow.late_responses l
          USING (project_id, client_id, execution_id, node_run_id, attempt_id)
        WHERE a.execution_id=:eid ORDER BY a.attempt_number
    """)
    expected = [
        (1, 1, "probe-a", "superseded", "fixture-request-a"),
        (2, 2, "probe-b", "succeeded", None),
    ]
    observed = [tuple(row) for row in rows]
    if observed != expected:
        diagnostics = await probe.read("""
            SELECT output_key, record::text FROM jimu_workflow.produced_outputs
            WHERE execution_id=:eid AND output_key='node.output'
        """)
        raise AssertionError(
            (takeover_step, observed, [tuple(row) for row in diagnostics])
        )
    emit(
        "takeover_and_late_audit",
        rows=[list(row) for row in rows],
        a_pid=first.pid,
        b_pid=second.pid,
    )


def ids(refs: list[dict[str, Any]]) -> set[str]:
    return {ref["record_id"] for ref in refs}


async def history(probe: ExecutionProbe) -> tuple[Any, ...]:
    rows = await probe.read("""
        SELECT event_number, event_id, record::text AS body
        FROM jimu_workflow.execution_events WHERE execution_id=:eid ORDER BY event_number
    """)
    assert [row.event_number for row in rows] == list(range(1, len(rows) + 1))
    return tuple(tuple(row) for row in rows)


async def tick_to_terminal(
    probe: ExecutionProbe, process: Process
) -> ExecutionFinalization:
    for _ in range(60):
        await process.ask("worker")
        [(state,)] = await probe.read(
            "SELECT status FROM jimu_workflow.executions WHERE execution_id=:eid"
        )
        if state in {"completed", "partial_failed", "failed", "cancelled"}:
            result = await process.ask("query")
            assert result["final"] is not None
            return ExecutionFinalization.model_validate_json(
                json.dumps(result["final"])
            )
    raise AssertionError("workflow exceeded 60 bounded steps")


async def verified_messages(probe: ExecutionProbe) -> dict[str, list[dict[str, Any]]]:
    rows = await probe.read("""
        SELECT n.node_id, n.status, a.attempt_id, p.record
        FROM jimu_workflow.authority_outputs a
        JOIN jimu_workflow.produced_outputs p USING (project_id, client_id, execution_id, node_run_id, attempt_id)
        JOIN jimu_workflow.node_runs n USING (project_id, client_id, execution_id, node_run_id)
        JOIN jimu_workflow.attempt_results r USING (project_id, client_id, execution_id, node_run_id, attempt_id)
        WHERE a.execution_id=:eid AND p.output_key='worker.message'
            AND n.status='succeeded' AND r.status='succeeded'
        ORDER BY n.node_id, n.branch_key
    """)
    result: dict[str, list[dict[str, Any]]] = {}
    for node_id, _, attempt_id, message in rows:
        inputs = message["draft"]["inputs"] if node_id == "N06" else message["inputs"]
        envelope = inputs["envelope"]
        assert (
            envelope["execution_id"],
            envelope["node_id"],
            envelope["attempt_id"],
        ) == (probe.execution_id, node_id, attempt_id)
        assert envelope["trace"] == probe.config["trace"]
        result.setdefault(node_id, []).append(message)
    [(release_id, bundle_digest, pin)] = await probe.read("""
        SELECT release_id, bundle_digest, execution_policy_ref FROM jimu_workflow.executions
        WHERE execution_id=:eid
    """)
    for stage in result.values():
        for message in stage:
            envelope = (message.get("draft") or message)["inputs"]["envelope"]
            assert (
                envelope["release_id"],
                envelope["bundle_digest"],
                envelope["node_config_ref"],
            ) == (
                release_id,
                bundle_digest,
                pin,
            )
    return result


async def persisted_artifacts(probe: ExecutionProbe) -> int:
    rows = await probe.read("""
        SELECT a.node_run_id, a.attempt_id, env.record, data.output_key, data.record
        FROM jimu_workflow.authority_outputs a
        JOIN jimu_workflow.produced_outputs env
          USING (project_id,client_id,execution_id,node_run_id,attempt_id,output_key,output_digest)
        JOIN jimu_workflow.produced_outputs data
          USING (project_id,client_id,execution_id,node_run_id,attempt_id)
        WHERE a.execution_id=:eid
    """)
    artifacts = {(node, attempt, key): data for node, attempt, _, key, data in rows}
    envelopes = {(node, attempt): env for node, attempt, env, _, _ in rows}
    count = 0
    for (node, attempt), envelope in envelopes.items():
        for pin in envelope["output_artifacts"]:
            ref = pin["ref"]
            key = f"{ref['record_kind']}.{ref['record_id']}"
            assert (
                sha256(rfc8785.dumps(artifacts[node, attempt, key])).hexdigest()
                == pin["digest"]
            )
            count += 1
    assert count > 0
    return count


async def relation_probe(probe: ExecutionProbe, final: ExecutionFinalization) -> None:
    messages = await verified_messages(probe)
    artifact_count = await persisted_artifacts(probe)
    assert {node_id: len(records) for node_id, records in messages.items()} == {
        "N01": 1,
        "N02": 1,
        "N03": 1,
        "N04": 3,
        "N05": 3,
        "N06": 3,
    }
    [(gate,)] = await probe.read("""
        SELECT record FROM jimu_workflow.engine_outputs
        WHERE execution_id=:eid AND output_key='worker.eligibility'
    """)
    (context,) = messages["N01"]
    (discovery,) = messages["N02"]
    (grounding,) = messages["N03"]
    snapshot = context["snapshot"]
    evaluation = gate["evaluation"]
    assert (
        evaluation["context_snapshot_ref"]["record_id"]
        == snapshot["context_snapshot_id"]
    )
    assert (
        discovery["inputs"]["profile_eligibility_evaluation_ref"]["record_id"]
        == evaluation["evaluation_id"]
    )
    assert discovery["inputs"]["eligible_profiles"] == evaluation["eligible_profiles"]
    assert len(discovery["output"]["profile_hits"]) == 2
    assert [row["hit"] for row in grounding["inputs"]["profiles"]] == discovery[
        "output"
    ]["profile_hits"]
    assert {invocation["node_id"] for invocation in discovery["invocations"]} == {"N02"}
    grounding_by_id = {row["grounding_record_id"]: row for row in grounding["records"]}
    assert len(grounding_by_id) == 2
    assert len({i["model_invocation_id"] for i in grounding["invocations"]}) == 2
    objects_by_id = {row["object_id"]: row for row in grounding["objects"]}
    assert sorted(
        Counter(
            row["profile_ref"]["artifact_id"] for row in objects_by_id.values()
        ).values()
    ) == [1, 2]
    hit_by_profile = {
        row["profile_ref"]["artifact_id"]: row
        for row in discovery["output"]["profile_hits"]
    }
    for object_id, obj in objects_by_id.items():
        record = grounding_by_id[obj["grounding_record_ref"]["record_id"]]
        assert object_id in ids(record["object_refs"])
        hit = hit_by_profile[obj["profile_ref"]["artifact_id"]]
        assert obj["effective_question_refs"] == hit["effective_question_refs"]
        assert obj["question_selection_ref"] == hit["question_selection_ref"]
        assert obj["source_media_ref"] == probe.config["source"]["media"]["media_ref"]
    observations = {
        row["batch"]["object_ref"]["record_id"]: row for row in messages["N04"]
    }
    tools = {
        row["inputs"]["object_record"]["object_id"]: row for row in messages["N05"]
    }
    decisions = {row.object_ref.record_id: row for row in final.decisions}
    assert set(objects_by_id) == set(observations) == set(tools) == set(decisions)
    assert (
        len({row["invocation"]["model_invocation_id"] for row in observations.values()})
        == 3
    )
    total_observation_facts = total_tool_facts = total_tools = 0
    for oid, obj in objects_by_id.items():
        obs = observations[oid]
        tool = tools[oid]
        decision = decisions[oid]
        assert obs["inputs"]["object_record"] == obj == tool["inputs"]["object_record"]
        batch = obs["batch"]
        invocation = obs["invocation"]
        assert batch["effective_question_refs"] == obj["effective_question_refs"]
        assert [row["question_ref"] for row in batch["question_results"]] == obj[
            "effective_question_refs"
        ]
        assert len(batch["question_results"]) == 2
        assert (
            batch["model_invocation_ref"]["record_id"]
            == invocation["model_invocation_id"]
        )
        assert (
            invocation["contract_snapshot"]["effective_question_refs"]
            == obj["effective_question_refs"]
        )
        assert decision.execution_coverage_ref == final.coverage.coverage_ref
        assert decision.profile_ref.artifact_id == obj["profile_ref"]["artifact_id"]
        assert decision.rule_evaluations and decision.explanation.evaluated_rules
        observed_facts = [row["primary_fact"] for row in batch["question_results"]]
        total_observation_facts += len(observed_facts)
        assert all(
            fact["source"]["model_invocation_ref"] == batch["model_invocation_ref"]
            for fact in observed_facts
        )
        assert ids(tool["inputs"]["validated_observation_fact_refs"]) <= {
            fact["fact_id"] for fact in observed_facts
        }
        tool_execs = {row["tool_execution_id"]: row for row in tool["executions"]}
        total_tools += len(tool_execs)
        total_tool_facts += len(tool["facts"])
        assert tool_execs and tool["facts"]
        for fact in tool["facts"]:
            source = fact["source"]
            tool_exec = tool_execs[source["tool_execution_ref"]["record_id"]]
            assert fact["fact_id"] in ids(tool_exec["generated_fact_refs"])
            assert tool_exec["object_ref"]["record_id"] == oid
            assert (
                tool_exec["invocation_origin"]["tool_plan_ref"]
                == tool["inputs"]["tool_plan_ref"]
            )
        for evaluation in decision.rule_evaluations:
            assert {ref.record_id for ref in evaluation.fact_refs} <= {
                fact["fact_id"] for fact in (*observed_facts, *tool["facts"])
            }
            assert evaluation.fact_refs and evaluation.evidence_refs
    for row in messages["N06"]:
        assert row["draft"]["decision"]["decision_id"] in {
            decision.decision_id for decision in final.decisions
        }
        candidate = row["draft"]["candidate"]
        if candidate is not None:
            assert candidate["advisory_only"] and not candidate["categories"]
    assert final.coverage.mandatory_rule_coverage.scan == "all_mandatory"
    assert (
        final.coverage.object_count == 3
        and final.coverage.coverage_status.value == "complete"
    )
    assert (
        final.coverage.summary_result.value == "violation"
        and len(final.coverage.matches) >= 3
    )
    emit(
        "six_nodes_exact_relationships",
        profiles=len(grounding_by_id),
        objects=len(objects_by_id),
        joint_requests=len(observations),
        question_facts=total_observation_facts,
        tool_executions=total_tools,
        tool_facts=total_tool_facts,
        decisions=len(decisions),
        mandatory_matched=len(final.coverage.matches),
        verified_artifact_digests=artifact_count,
    )


async def crash_after_mock(probe: ExecutionProbe, process: Process) -> dict[str, Any]:
    await process.send("worker", pause_after_mock=True, ms=450)
    orphan = await reply(process.process)
    assert orphan["label"] == "mock_returned" and orphan["pid"] == process.pid
    [(recorded_attempt, terminal, authority)] = await probe.read("""
        SELECT a.attempt_id, r.status, o.output_key
        FROM jimu_workflow.attempts a JOIN jimu_workflow.node_runs n
          USING (project_id,client_id,execution_id,node_run_id)
        LEFT JOIN jimu_workflow.attempt_results r
          USING (project_id,client_id,execution_id,node_run_id,attempt_id)
        LEFT JOIN jimu_workflow.authority_outputs o
          USING (project_id,client_id,execution_id,node_run_id,attempt_id)
        WHERE a.execution_id=:eid AND n.node_id='N02'
    """)
    assert (recorded_attempt, terminal, authority) == (orphan["attempt"], None, None)
    await process.kill()
    await wait_db_time(probe, "pg_catalog.clock_timestamp() >= lease_until")
    emit("mock_returned_before_worker_crash", worker_marker=orphan)
    return orphan


def activate_replacement(probe: ExecutionProbe) -> None:
    ledger = Path(probe.config["ledger"])
    store = LocalBundleStore(ledger)
    host = FixtureHost()
    manager = manager_for(store, host)
    previous = manager.active_activation(GLOBAL)
    assert previous is not None
    initial = joint_parking_source()
    source = replace(
        initial, metadata=replace(initial.metadata, bundle_version="1.0.2")
    )
    activate_scenario(
        ledger.parent / "replacement",
        store,
        manager,
        host,
        source,
        capability_snapshot(),
        number=2,
        expected=previous.activation_id,
    )
    active = manager.active_activation(GLOBAL)
    assert active is not None
    assert active.release_ref.record_id != probe.config["release_id"]
    assert active.bundle_ref.bundle_digest != probe.config["bundle_digest"]
    emit(
        "activated_replacement_before_restart",
        active_release=active.release_ref.record_id,
        pinned_release=probe.config["release_id"],
        active_digest=active.bundle_ref.bundle_digest,
        pinned_digest=probe.config["bundle_digest"],
    )


async def crash_publish(
    probe: ExecutionProbe, process: Process, orphan: dict[str, Any]
) -> None:
    for _ in range(40):
        result = await process.ask("worker")
        if result["result"] == "WorkerStep.IDLE":
            await asyncio.sleep(0.025)
        drafts, finalizations, authority = await probe.counts()
        if drafts:
            break
    assert (drafts, finalizations, authority) == (1, 0, 0)
    [[node_status, attempt_status, terminal_attempt_status]] = [
        list(row)
        for row in await probe.read("""
            SELECT n.status, a.record->>'status', r.status
            FROM jimu_workflow.decision_drafts d
            JOIN jimu_workflow.node_runs n USING (project_id, client_id, execution_id, node_run_id)
            JOIN jimu_workflow.attempts a USING (project_id, client_id, execution_id, node_run_id, attempt_id)
            LEFT JOIN jimu_workflow.attempt_results r USING (project_id, client_id, execution_id, node_run_id, attempt_id)
            WHERE d.execution_id=:eid
        """)
    ]
    assert (node_status, attempt_status, terminal_attempt_status) == (
        "running",
        "running",
        None,
    )
    emit(
        "n06_draft_before_authority",
        draft_count=drafts,
        n06_authority=authority,
        finalization_count=finalizations,
        node=node_status,
        attempt=attempt_status,
        terminal_attempt=terminal_attempt_status,
    )
    for expected_count in (2, 3):
        assert (await process.ask("worker"))["result"] == "WorkerStep.STAGED"
        assert await probe.counts() == (expected_count, 0, 0)
    before = await history(probe)
    emit("all_n06_drafts_before_authority", drafts=3, finalizations=0, n06_authority=0)
    await process.kill()
    await asyncio.to_thread(activate_replacement, probe)

    restart = await launch(probe.config, "probe-restart")
    try:
        scope_poll = await restart.ask("poll")
        assert (
            scope_poll["step"],
            scope_poll["execution_id"],
            scope_poll["issues"],
        ) == ("finalized", probe.execution_id, 0)
        final = await tick_to_terminal(probe, restart)
        assert (final.coverage.release_id, final.coverage.bundle_digest) == (
            probe.config["release_id"],
            probe.config["bundle_digest"],
        )
        assert await probe.counts() == (3, 1, 3)
        after = await history(probe)
        assert after[: len(before)] == before
        await relation_probe(probe, final)
        rows = await probe.read("""
            SELECT a.attempt_id, a.attempt_number, r.status, o.output_key,
                   m.record->'invocations' AS invocations
            FROM jimu_workflow.attempts a JOIN jimu_workflow.node_runs n
              USING (project_id,client_id,execution_id,node_run_id)
            JOIN jimu_workflow.attempt_results r
              USING (project_id,client_id,execution_id,node_run_id,attempt_id)
            LEFT JOIN jimu_workflow.authority_outputs o
              USING (project_id,client_id,execution_id,node_run_id,attempt_id)
            LEFT JOIN jimu_workflow.produced_outputs m
              ON (a.project_id,a.client_id,a.execution_id,a.node_run_id,a.attempt_id)
                 = (m.project_id,m.client_id,m.execution_id,m.node_run_id,m.attempt_id)
              AND m.output_key='worker.message'
            WHERE a.execution_id=:eid AND n.node_id='N02' ORDER BY a.attempt_number
        """)
        assert len(rows) == 2
        assert tuple(rows[0]) == (orphan["attempt"], 1, "superseded", None, None)
        assert tuple(rows[1][1:4]) == (2, "succeeded", "node.output")
        accepted_invocations = {
            item["model_invocation_id"] for item in rows[1].invocations
        }
        assert accepted_invocations and accepted_invocations.isdisjoint(
            orphan["invocations"]
        )
        emit(
            "ambiguous_mock_call_retried",
            first_attempt=orphan["attempt"],
            second_attempt=rows[1].attempt_id,
            first_invocations=orphan["invocations"],
            second_invocations=sorted(accepted_invocations),
            authoritative_attempts=1,
        )
        emit(
            "restarted_and_published",
            drafts=3,
            finalizations=1,
            authority=3,
            preserved_history_prefix=len(before),
            events=len(after),
        )
    finally:
        await restart.kill()

    verifier = await launch(probe.config, "probe-verified")
    try:
        repeated = await tick_to_terminal(probe, verifier)
        assert repeated == final
        assert await probe.counts() == (3, 1, 3) and await history(probe) == after
        emit("post_commit_restart_no_duplicate", events=len(after))
    finally:
        await verifier.kill()


async def coverage_probe(app: AsyncEngine, work: Path, mode: str) -> None:
    probe = await start_execution(app, work, mode)
    process = await launch(probe.config, f"probe-{mode}")
    try:
        final = await tick_to_terminal(probe, process)
        cov = final.coverage
        expected = {
            "excluded": ("not_evaluated", "not_applicable", "not_applicable", 0),
            "no_hits": ("no_hits", "none", "uncertain", 0),
            "no_target": ("hits", "none", "uncertain", 0),
            "partial": ("hits", "partial", "violation", 1),
        }
        observed = (
            cov.discovery_outcome.value,
            cov.coverage_status.value,
            cov.summary_result.value,
            cov.object_count,
        )
        assert observed == expected[mode]
        rows = await probe.read("""
            SELECT n.node_id, n.status, count(a.attempt_id)
            FROM jimu_workflow.node_runs n LEFT JOIN jimu_workflow.attempts a
              USING (project_id, client_id, execution_id, node_run_id)
            WHERE n.execution_id=:eid GROUP BY n.node_id, n.status ORDER BY n.node_id
        """)
        assert all(
            count == 0 for _, status, count in rows if status in {"blocked", "skipped"}
        )
        assert len(final.decisions) == cov.object_count
        if mode == "excluded":
            assert not cov.discovery_model_invocation_refs and not cov.hit_profile_refs
            assert set(await verified_messages(probe)) == {"N01"}
            assert {
                o.outcome.disposition for o in cov.mandatory_rule_coverage.obligations
            } >= {
                "excluded",
                "not_evaluated",
            }
        if mode == "no_hits":
            assert cov.discovery_model_invocation_refs and not cov.hit_profile_refs
        if mode == "no_target":
            assert {p.grounding_outcome.value for p in cov.profiles} == {"no_target"}
        if mode == "partial":
            assert {p.grounding_outcome.value for p in cov.profiles} == {
                "objects",
                "failed",
            }
            assert len(cov.matches) >= 1
        emit(
            "coverage",
            mode=mode,
            observed=observed,
            node_attempts=[list(row) for row in rows],
        )
    finally:
        await process.kill()


async def cancel_probe(app: AsyncEngine, work: Path) -> None:
    probe = await start_execution(app, work, "cancel_before_start")
    original = await launch(probe.config, "probe-cancel-intent")
    try:
        assert (await original.ask("claim", ms=2_000))["fence"] == 1
        assert (await original.ask("cancel"))["cancelled"]
        [(state, intent)] = await probe.read("""
            SELECT status, cancel_requested_at IS NOT NULL
            FROM jimu_workflow.executions WHERE execution_id=:eid
        """)
        assert (state, intent) == ("initialized", True)
    finally:
        await original.kill()
    reopened = await launch(probe.config, "probe-cancel-restart")
    try:
        poll = await reopened.ask("poll")
        assert (poll["step"], poll["execution_id"], poll["issues"]) == (
            "cancelled",
            probe.execution_id,
            0,
        )
        [(task_state, state, node, jobs, attempts)] = await probe.read("""
            SELECT t.status, e.status, n.status, j.status,
                   (SELECT count(*) FROM jimu_workflow.attempts WHERE execution_id=:eid)
            FROM jimu_workflow.executions e JOIN jimu_workflow.tasks t
              USING (project_id,client_id,task_id)
            JOIN jimu_workflow.node_runs n USING (project_id,client_id,execution_id)
            JOIN jimu_workflow.jobs j USING (project_id,client_id,execution_id,node_run_id)
            WHERE e.execution_id=:eid
        """)
        assert (task_state, state, node, jobs, attempts) == (
            "cancelled",
            "cancelled",
            "cancelled",
            "done",
            0,
        )
        assert await probe.counts() == (0, 0, 0)
        emit(
            "prestart_cancel_survives_process_kill",
            task=task_state,
            execution=state,
            node=node,
            job=jobs,
            attempts=attempts,
        )
    finally:
        await reopened.kill()


async def independent() -> None:
    with (
        isolated_postgres() as database,
        TemporaryDirectory(prefix="jimu-wp03-independent-") as dir_name,
    ):
        owner = create_postgres_engine(database.migration_url)
        app = create_postgres_engine(database.application_url)
        work = Path(dir_name)
        try:
            assert database.server_version.startswith("16.")
            first = await apply_migrations(owner, database.target)
            again = await apply_migrations(owner, database.target)
            assert first == (1, 2) and again == ()
            emit(
                "postgres",
                version=database.server_version,
                image_id=POSTGRES_IMAGE,
                first=first,
                repeat=again,
            )
            await permission_probe(app)
            full = await start_execution(app, work / "full", "full")
            if sys.argv[1:] == ["--intake-only"]:
                return
            proc_a = await launch(full.config, "probe-a")
            proc_b = await launch(full.config, "probe-b")
            try:
                await lease_probe(full, proc_a, proc_b)
                await proc_a.kill()
                orphan = await crash_after_mock(full, proc_b)
                replacement = await launch(full.config, "probe-between-crashes")
                try:
                    await crash_publish(full, replacement, orphan)
                finally:
                    await replacement.kill()
            finally:
                await proc_a.kill()
                await proc_b.kill()
            for mode in ("excluded", "no_hits", "no_target", "partial"):
                await coverage_probe(app, work / mode, mode)
            await cancel_probe(app, work / "cancelled")
        finally:
            await app.dispose()
            await owner.dispose()


if __name__ == "__main__":
    asyncio.run(child() if sys.argv[1:] == ["child"] else independent())
