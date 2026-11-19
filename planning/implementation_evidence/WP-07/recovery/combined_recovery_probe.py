"""Local-only recovery drill over the installed WP06 wheel and signed WP05 scenario."""

import asyncio
import json
import sqlite3
import sys
from dataclasses import replace
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from audit import AuditScope, check_dispatches, check_tool_evidence
from cases import CASES, OUTSIDE, read_frame
from harness import ACTOR, SCOPE, _rebind_artifact
from lifecycle_probe import EvidenceAccessGrant, EvidenceLocator, lifecycle
from loopback import ParkingProvider, Reply
from ops.recovery import (
    Endpoint,
    NativeTools,
    SourceVolumes,
    backup,
    prepare_media_locks,
    restore,
)
from ops.recovery.contracts import (
    BUNDLE,
    MANIFEST,
    RESOURCES,
    FileWitness,
    RecoveryError,
)
from ops.recovery.filesystem import ledger
from ops.recovery.media_validation import verify_media
from psycopg import sql
from replay_probe import (
    ReplayHost,
    assert_inference,
    check_cpu,
    emit,
    history,
    total_runtime_rows,
    trace_artifact,
)
from run_cases import launch
from sqlalchemy import text
from tests.contract.test_wp07_recovery import _endpoint, _fence, _owner
from worker_entry import QUOTA

import jimu_runtime
from examples.nonmotor_vehicle_illegal_parking.compilable import scene
from jimu_runtime.bundle.compiler import SourceIdentity
from jimu_runtime.bundle.releases import ReleaseManager
from jimu_runtime.domain.enums import RuntimeRecordKind
from jimu_runtime.domain.policies import CPUResourceRequirements
from jimu_runtime.domain.runtime.evidence import EvidenceAsset
from jimu_runtime.domain.runtime.replay import (
    ExecutionRef,
    InferenceReplayRequest,
    ReplayExecutionSource,
    ReplayKind,
)
from jimu_runtime.gateway.journal import DispatchJournal
from jimu_runtime.inspection.media.types import MediaError
from jimu_runtime.replay.artifact import ArtifactReplayService
from jimu_runtime.replay.inference import InferenceReplayService
from jimu_runtime.workers.resource_inventory import (
    HostInventory,
    RemoteCapacity,
    RemoteQuota,
    ResourceScope,
)
from jimu_runtime.workers.resources import ResourceAdmission
from jimu_runtime.workflow import repository
from jimu_runtime.workflow.engine import ExecutionFinalization
from jimu_runtime.workflow.jobs import DurableJobs
from jimu_runtime.workflow.pins import ExecutionPinError, resolve_execution_pins
from jimu_runtime.workflow.postgres import apply_migrations, create_postgres_engine
from jimu_runtime.workflow.worker import WorkflowWorker
from tests.fixtures.wp03.postgres.support import POSTGRES_IMAGE, isolated_postgres


class Provider(ParkingProvider):
    delay_next_discovery = False

    def _reply(self, request):
        if self.delay_next_discovery and request.role == "discovery":
            self.delay_next_discovery = False
            return Reply(request, 0, None)
        return super()._reply(request)


class RestoredHost(ReplayHost):
    def manager(self):
        return ReleaseManager(
            self.ledger,
            authorize=self._authorize,
            resolve_task_scope=self._resolve,
            scope_precedence=("device", "region", "scene", "global"),
            allow_new_task=lambda _r, activation, scope, _at: activation.scope == SCOPE
            and scope.region_id == scene.REGION,
            clock=lambda: scene.AT,
        )


def restored_host(app, path, endpoint):
    host = RestoredHost(app, path, endpoint)
    host.ledger = type(host.ledger)(path)
    host.media = type(host.media)(path, app)
    return host


async def finish(host, execution_id, trace):
    process = await launch(host, execution_id, "wp07.original", trace, installed=True)
    try:
        for _ in range(18):
            if (await process.call("step"))["step"] == "finalized":
                break
        return ExecutionFinalization.model_validate_json(
            json.dumps((await process.call("read"))["final"])
        )
    finally:
        await process.close()


async def geometry_locator(host, source, eid):
    async with host.db.connect() as con:
        rows = (
            await con.execute(
                text("""
                SELECT node_run_id, attempt_id, output_digest, record::text AS record
                FROM jimu_workflow.produced_outputs
                WHERE execution_id=:e AND output_kind='evidence'
                  AND record->>'evidence_type'='geometry'
                ORDER BY node_run_id, attempt_id
            """),
                {"e": eid},
            )
        ).all()
        row = rows[0]
        at = await con.scalar(text("SELECT clock_timestamp()"))
    asset = EvidenceAsset.model_validate_json(row.record)
    loc = EvidenceLocator(
        execution_id=eid,
        node_run_id=row.node_run_id,
        attempt_id=row.attempt_id,
        evidence_id=asset.evidence_id,
        output_digest=row.output_digest,
    )
    grant = EvidenceAccessGrant(
        scope=host.scope,
        actor=ACTOR,
        task_id=source.task_id,
        manage_retention=True,
    )
    return grant, loc, asset, at


async def placed_hold(host, source, eid):
    grant, loc, _, at = await geometry_locator(host, source, eid)
    service = lifecycle(host)
    assert (await service.read_evidence(grant, loc)).status == "available"
    hold = await service.place_hold(
        grant, loc, "wp07.recovery-hold", reason="review_case", at=at
    )
    assert (await service.dispose(grant, loc, at=at)).status == "review_hold"
    return grant, loc, hold


async def interrupted(host, provider, source, trace):
    replay = InferenceReplayService(
        host.db, host.manager(), host.ledger, host=host, bundle_limits=scene.LIMITS
    )
    run = await replay.submit(
        host.scope,
        InferenceReplayRequest(
            schema_version="1.0.0",
            kind=ReplayKind.INFERENCE,
            source=source,
            target_pins=source.pins,
        ),
        idempotency_key="wp07.interrupted",
        principal=host.principal,
    )
    eid = run.target_execution_ref.record_id
    process = await launch(host, eid, "wp07.interrupted", trace, installed=True)
    incoming = len(provider.requests)
    provider.delay_next_discovery = True

    async def advance_to_discovery():
        for _ in range(3):
            await process.call("step")
            if len(provider.requests) > incoming:
                break

    pending = asyncio.create_task(advance_to_discovery())
    try:
        async with asyncio.timeout(4):
            while len(provider.requests) == incoming:
                await asyncio.sleep(0.005)
        (request,) = provider.requests[incoming:]
        assert request.role == "discovery"
        assert provider.replies[-1].body is None
    finally:
        await process.close(crash=True)
        await asyncio.gather(pending, return_exceptions=True)
        provider.release_delayed.set()
    journal = DispatchJournal(host.db)
    async with host.db.connect() as connection:
        dispatch_id = await connection.scalar(
            text("""
            SELECT dispatch_id FROM jimu_workflow.dispatch_intents WHERE execution_id=:e
        """),
            {"e": eid},
        )
        attempt = (
            await connection.execute(
                text("""
                SELECT current_attempt_id, lease_owner, fence, status
                FROM jimu_workflow.jobs WHERE execution_id=:e AND status='leased'
            """),
                {"e": eid},
            )
        ).one()
    record = await journal.get_dispatch(host.scope, eid, dispatch_id)
    assert record is not None and record.send_admitted and not record.results
    assert tuple(attempt) == (
        record.attempt_id,
        record.lease_owner,
        record.fence,
        "leased",
    )
    assert record.intent.request_digest == request.request_digest
    emit(
        "process_death_at_unacknowledged_dispatch",
        execution=eid,
        dispatch=dispatch_id,
        attempt=record.attempt_id,
        owner=record.lease_owner,
        fence=record.fence,
        request_sha256=request.request_digest,
        observed_http=1,
        acknowledged=0,
    )
    return run, record


def ledger_rows(path):
    with sqlite3.connect(path.as_uri() + "?mode=ro", uri=True) as db:
        return db.execute("""
            SELECT lease_id, owner_id, owner_generation, state, dispatch_id
            FROM leases ORDER BY lease_id
        """).fetchall()


def short_fixture_retention():
    source = _LOAD_SOURCE()
    (old,) = (row for row in source.artifacts if row.artifact_id == "geometry-evidence")
    body = json.loads(old.content)
    body["version"] = "1.0.1"
    body["policy"]["retention_duration_seconds"] = 1
    new = replace(
        old,
        content=json.dumps(body, sort_keys=True).encode(),
        version="1.0.1",
        origin=SourceIdentity(
            artifact_id="independent.wp07.geometry-retention", version="1.0.1"
        ),
    )
    return _rebind_artifact(source, old, new)


_LOAD_SOURCE = scene.load_source


async def dispose_geometry(host, source, eid):
    grant, loc, asset, at = await geometry_locator(host, source, eid)
    service = lifecycle(host)
    assert (await service.read_evidence(grant, loc)).status == "available"
    for _ in range(20):
        result = await service.dispose(grant, loc, at=at)
        if result.status == "disposed":
            break
        assert result.status == "retention_pending", result.status
        await asyncio.sleep(0.1)
        async with host.db.connect() as con:
            at = await con.scalar(text("SELECT clock_timestamp()"))
    assert result.status == "disposed" and result.completion.outcome == "removed"
    assert (await service.read_evidence(grant, loc)).status == "expired"
    assert not (host.root / "media" / asset.storage.object_key).exists()
    return grant, loc, asset


def assert_forgery_rejections(pg, manifest, held, disposed_eid, bundle_path):
    files = {
        name: FileWitness.model_validate(item)
        for name, item in manifest["files"].items()
    }
    names = tuple(manifest["postgres_tables"])
    with _owner(pg) as connection, ledger(bundle_path, freeze=False) as bundle:
        wrong = files.copy()
        del wrong[held.storage.object_key]
        for tamper, args, evidence in (
            ("SELECT 1", (), wrong),
            (
                "DELETE FROM jimu_workflow.evidence_disposition_completions WHERE execution_id=%s",
                (disposed_eid,),
                files,
            ),
            (
                """UPDATE jimu_workflow.evidence_disposition_intents
                SET record=jsonb_set(record::jsonb,'{sha256}',to_jsonb(repeat('f',64))) WHERE execution_id=%s""",
                (disposed_eid,),
                files,
            ),
            (
                """UPDATE jimu_workflow.evidence_disposition_intents
                SET record=jsonb_set(record::jsonb,'{references,0,locator,output_digest}',to_jsonb(repeat('f',64)))
                WHERE execution_id=%s""",
                (disposed_eid,),
                files,
            ),
        ):
            try:
                with connection.transaction():
                    connection.execute(tamper, args)
                    verify_media(connection, names, evidence, bundle)
            except RecoveryError:
                pass
            else:
                raise AssertionError(
                    "recovery accepted a corrupt disposition or missing held file"
                )
    emit(
        "negative_media_binding",
        missing_held=True,
        completion_missing=True,
        wrong_hash=True,
        wrong_alias=True,
    )


async def drill(source_pg, fresh, root, output, provider, frame):
    source_dir = root / "source"
    source_dir.mkdir(mode=0o700)
    target = root / "restored"
    package = root / "package"
    owner = create_postgres_engine(source_pg.migration_url)
    app = create_postgres_engine(source_pg.application_url)
    restored_app = create_postgres_engine(fresh.application_url)
    try:
        assert await apply_migrations(owner, source_pg.target) == tuple(range(1, 12))
        assert await apply_migrations(owner, source_pg.target) == ()
        host = ReplayHost(app, source_dir, provider.url)
        with patch.object(scene, "load_source", short_fixture_retention):
            release = host.release(0)
        accepted = await host.accept(host.request("wp07-source", frame), frame)
        eid = accepted.execution.execution_id
        orphan_id = "wp07.request.orphan"
        orphan_body = b"isolated unreferenced request"
        orphan, _ = await host.media.stage_observation_request(
            host.scope, eid, orphan_id, orphan_body
        )
        source = ReplayExecutionSource(
            execution_ref=ExecutionRef(
                record_kind=RuntimeRecordKind.EXECUTION, record_id=eid
            ),
            task_id=accepted.task.task_id,
            pins=accepted.execution.pins,
        )
        final = await finish(host, eid, root / "source.exec.raw")
        assert final.coverage.summary_result.value == "violation"
        assert await host.media.cleanup_unreferenced_observation_request(
            host.scope, eid, orphan_id
        )
        assert not (source_dir / "media" / orphan.object_key).exists()
        check_cpu(root / "source.exec.raw", output / "source.cpu.json", 3)
        old_release = host.manager().get_release(accepted.task.release_id)
        assert old_release.bundle_ref.bundle_digest == release.bundle_digest
        async with app.connect() as con:
            assert await check_dispatches(
                con, AuditScope(host.scope, eid), provider, host.media
            ) == {
                "discovery": 1,
                "grounding": 1,
                "observation": 1,
                "candidate": 1,
            }
            sidecars, _ = await check_tool_evidence(
                con,
                AuditScope(host.scope, eid),
                host.media,
                frame,
                {final.decisions[0].object_ref.record_id: OUTSIDE},
                {},
            )
        assert len(sidecars) == 3
        saved = await history(app, host.scope, eid, source.task_id)
        await trace_artifact(
            app, host.scope, source, saved, provider, output, root / "artifact.exec.raw"
        )
        replay = ArtifactReplayService(app)
        replay_request = await replay.prepare_request(host.scope, source)
        replay_run = await replay.run(host.scope, "audit-sequential", replay_request)
        replay_score = await replay.read_scoring_evidence(
            host.scope, replay_run.scoring_evidence
        )
        assert replay_score is not None
        inference_eid = await assert_inference(
            host, source, saved, provider, output, root / "target.exec.raw"
        )
        inference_replay = ArtifactReplayService(app)
        inference_source = source.model_copy(
            update={
                "execution_ref": ExecutionRef(
                    record_kind=RuntimeRecordKind.EXECUTION,
                    record_id=inference_eid,
                )
            }
        )
        inference_request = await inference_replay.prepare_request(
            host.scope, inference_source
        )
        disposed_score = await inference_replay.run(
            host.scope, "wp07.disposed-score", inference_request
        )
        expired_grant, expired_loc, expired_asset = await dispose_geometry(
            host, source, inference_eid
        )
        grant, loc, hold = await placed_hold(host, source, eid)
        _, _, held_asset, _ = await geometry_locator(host, source, eid)
        run, pending = await interrupted(
            host, provider, source, root / "pending.exec.raw"
        )
        pending_eid = run.target_execution_ref.record_id
        check_cpu(root / "pending.exec.raw", output / "pending.cpu.json", 0)
        original = await host.media.verified_task_original(host.scope, accepted.task)
        inventory = HostInventory(
            "wp05-independently-bound-host",
            3500,
            1,
            (
                ResourceScope("cpu", "cpu", 1),
                ResourceScope("model", "remote", 1, pool_or_quota_id=QUOTA),
            ),
            (RemoteQuota(QUOTA, 1, RemoteCapacity.LOCAL_TRANSPORT),),
        )
        resource_path = source_dir / "capacity.db"
        rows = ledger_rows(resource_path)
        (transport_lease,) = (
            row[0] for row in rows if row[-1] == pending.intent.dispatch_id
        )
        admission = ResourceAdmission(resource_path, inventory)
        async with asyncio.timeout(5):
            while transport_lease not in admission.reconcile():
                await asyncio.sleep(0.1)
        emit(
            "physical_transport_reaped",
            dispatch=pending.intent.dispatch_id,
            lease=transport_lease,
        )
        reserved = admission.reserve(
            CPUResourceRequirements(
                policy_kind="resource_requirements",
                resource_class="cpu",
                max_concurrency=1,
                timeout_ms=1000,
                input_byte_limit=2048,
                output_byte_limit=2048,
            ),
            "cpu",
            owner_id="wp07.frozen-owner",
        )
        rows = ledger_rows(resource_path)
        assert [row[:3] for row in rows] == [
            (reserved.lease_id, reserved.owner_id, reserved.owner_generation)
        ]
        saved_pending = await history(app, host.scope, pending_eid, source.task_id)
        db_counts = await total_runtime_rows(app)
        prepare_media_locks(source_dir / "media")
        config_path = root / "host-binding.json"
        config_bytes = json.dumps(
            {
                "schema_version": "1.0.0",
                "deployment_config_sha256": sha256(
                    b"isolated signed WP05 and WP06 replay host"
                ).hexdigest(),
                "inventory_id": inventory.inventory_id,
                "inventory_sha256": inventory.fingerprint(),
            },
            sort_keys=True,
        ).encode()
        config_path.write_bytes(config_bytes)
        config_path.chmod(0o600)
        config_digest = sha256(config_bytes).hexdigest()
        proof = dict(
            host_config_path=config_path,
            host_config_sha256=config_digest,
            maintenance_confirmed=True,
        )
        source_endpoint = _endpoint(source_pg, root / "source.pgpass")
        target_endpoint: Endpoint = _endpoint(
            fresh, root / "target.pgpass", source_pg.target.instance_id
        )
        tools = NativeTools(image=POSTGRES_IMAGE)
        await app.dispose()
        await owner.dispose()
        _fence(source_pg, on=True)
        with _owner(fresh) as connection:
            connection.execute(
                sql.SQL("COMMENT ON DATABASE {} IS {}").format(
                    sql.Identifier(fresh.target.database),
                    sql.Literal(source_pg.target.database_comment),
                )
            )
        _fence(fresh, on=True)
        physical_before = len(provider.requests)
        manifest_sha = backup(
            source_endpoint,
            tools,
            SourceVolumes(
                source_dir / "releases" / BUNDLE, resource_path, source_dir / "media"
            ),
            package,
            **proof,
        )
        manifest_raw = (package / MANIFEST).read_bytes()
        assert sha256(manifest_raw).hexdigest() == manifest_sha
        assert_forgery_rejections(
            source_pg,
            json.loads(manifest_raw),
            held_asset,
            inference_eid,
            package / BUNDLE,
        )
        witness = restore(
            target_endpoint,
            tools,
            package,
            target,
            manifest_sha256=manifest_sha,
            **proof,
        )
        assert witness.logical_database_id == source_pg.target.instance_id
        emit(
            "physical_restore",
            manifest_sha256=manifest_sha,
            host_binding_sha256=config_digest,
            witness_sha256=sha256(
                (target / "restore-verified.json").read_bytes()
            ).hexdigest(),
            files=json.loads(manifest_raw)["files"],
            logical_database=str(witness.logical_database_id),
        )
        spawned = []

        def observe_exec(event, _args):
            if event in {"subprocess.Popen", "os.posix_spawn", "os.exec", "os.system"}:
                spawned.append(event)

        sys.addaudithook(observe_exec)
        assert ledger_rows(target / RESOURCES) == rows
        ResourceAdmission(target / RESOURCES, inventory)
        _fence(fresh, on=False)
        reopen = restored_host(restored_app, target, provider.url)
        wrong = host.scope.model_copy(update={"client_id": "wp07.wrong-client"})
        reader = WorkflowWorker(
            restored_app,
            reopen.manager(),
            reopen.ledger,
            host=reopen,
            bundle_limits=scene.LIMITS,
        )
        assert await reader.read_finalization(host.scope, eid) == final
        assert await reader.read_finalization(wrong, eid) is None
        manager = reopen.manager()
        assert manager.get_release(accepted.task.release_id) == old_release
        activation = manager.active_activation(SCOPE)
        assert activation is not None
        assert activation.release_ref.record_id != old_release.release_id
        pin_args = dict(
            host=reopen.snapshot, validation_at=datetime.now(UTC), limits=scene.LIMITS
        )
        assert (
            resolve_execution_pins(
                manager, reopen.ledger, accepted.task, scope=host.scope, **pin_args
            ).pins
            == source.pins
        )
        try:
            resolve_execution_pins(
                manager, reopen.ledger, accepted.task, scope=wrong, **pin_args
            )
        except ExecutionPinError:
            pass
        else:
            raise AssertionError("foreign scope opened the historical execution pin")
        assert (
            await reopen.media.verified_task_original(host.scope, accepted.task)
            == original
        )
        assert await history(restored_app, host.scope, eid, source.task_id) == saved
        async with restored_app.connect() as con:
            locked = await repository.get_execution(con, host.scope, eid)
            assert locked is not None and locked.execution.pins == source.pins
            assert await repository.get_execution(con, wrong, eid) is None
            restored_sidecars, _ = await check_tool_evidence(
                con,
                AuditScope(host.scope, eid),
                reopen.media,
                frame,
                {final.decisions[0].object_ref.record_id: OUTSIDE},
                {},
            )
            (active_hold,) = (
                await con.scalars(
                    text("""
                SELECT hold_id FROM jimu_workflow.evidence_retention_holds WHERE execution_id=:e
            """),
                    {"e": eid},
                )
            ).all()
            assert active_hold == hold.hold_id
        assert restored_sidecars == sidecars
        media = lifecycle(reopen)
        original_evidence = await media.read_evidence(grant, loc)
        assert original_evidence.status == "available"
        assert (
            await media.read_evidence(expired_grant, expired_loc)
        ).status == "expired"
        assert not (target / expired_asset.storage.object_key).exists()
        try:
            await reopen.media.read_observation_request(
                host.scope,
                eid,
                orphan_id,
                orphan,
                sha256(orphan_body).hexdigest(),
                len(orphan_body),
            )
        except MediaError as error:
            assert str(error) == "observation_request_media_unavailable"
        else:
            raise AssertionError("reclaimed WP05 request was resurrected")
        assert (
            await media.read_evidence(grant.model_copy(update={"scope": wrong}), loc)
        ).status == "denied"
        restored_replay = ArtifactReplayService(restored_app)
        assert (
            await restored_replay.get_run(host.scope, replay_run.replay_run_id)
            == replay_run
        )
        assert await restored_replay.get_run(wrong, replay_run.replay_run_id) is None
        assert (
            await restored_replay.read_scoring_evidence(
                host.scope, replay_run.scoring_evidence
            )
            == replay_score
        )
        assert (
            await restored_replay.read_scoring_evidence(
                host.scope, disposed_score.scoring_evidence
            )
            is not None
        )
        journal = DispatchJournal(restored_app)
        assert (
            await journal.get_dispatch(
                host.scope, pending_eid, pending.intent.dispatch_id
            )
            == pending
        )
        assert (
            await journal.get_dispatch(wrong, pending_eid, pending.intent.dispatch_id)
            is None
        )
        assert (
            await history(restored_app, host.scope, pending_eid, source.task_id)
            == saved_pending
        )
        assert await total_runtime_rows(restored_app) == db_counts
        jobs = DurableJobs(restored_app)
        # Never run a scheduler tick for the ambiguous send. Reconcile a scoped
        # cancellation before a worker can pick up any potentially retryable job.
        await jobs.request_cancel(host.scope, pending_eid)
        await jobs.reconcile(host.scope, pending_eid)
        async with restored_app.connect() as con:
            locked = await repository.get_execution(con, host.scope, pending_eid)
            assert locked is not None and locked.execution.status.value == "cancelled"
        assert (
            await journal.get_dispatch(
                host.scope, pending_eid, pending.intent.dispatch_id
            )
            == pending
        )
        assert (await total_runtime_rows(restored_app))[-1] == db_counts[-1]
        assert len(provider.requests) == physical_before
        assert spawned == []
        emit(
            "reopened_and_reconciled",
            source_execution=eid,
            pending_execution=pending_eid,
            pending_status="cancelled",
            old_release=accepted.task.release_id,
            old_bundle=source.pins.bundle_digest,
            original_sha256=original.asset.media_ref.sha256,
            decision_sha256=sha256(final.stable_json().encode()).hexdigest(),
            scoring_sha256=replay_run.scoring_evidence.digest,
            hold=hold.hold_id,
            scope_negatives=6,
            owner=pending.lease_owner,
            fence=pending.fence,
            physical_http_delta=0,
            physical_cpu_delta=0,
            restored_disposed_evidence="expired",
            wp05_request_reclaim="preserved",
            post_restore_dispatch_delta=0,
        )
    finally:
        await owner.dispose()
        await app.dispose()
        await restored_app.dispose()


async def main(output):
    assert Path(jimu_runtime.__file__).resolve().is_relative_to(Path(sys.prefix))
    frame = read_frame(scene.DIRECTORY, "base")
    model = scene.capability_snapshot(scene.load_source()).model_deployments[0].identity
    provider = Provider(
        CASES["known_violation"],
        frame,
        model_id="independent-parking-vision",
        deployment_version=model.deployment_version,
        model_version=model.model_version,
    )
    with TemporaryDirectory(prefix="wp07-combined-runtime-") as path:
        with isolated_postgres() as source, isolated_postgres() as fresh:
            async with provider.serving():
                await drill(source, fresh, Path(path), output, provider, frame)


if __name__ == "__main__":
    asyncio.run(main(Path(sys.argv[1]).resolve()))
