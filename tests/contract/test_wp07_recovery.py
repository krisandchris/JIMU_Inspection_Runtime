"""Exercise native backup and restore to separate private PostgreSQL 16 instances."""

import asyncio
import json
import sqlite3
import subprocess
import sys
from dataclasses import replace
from fcntl import LOCK_EX, flock
from hashlib import sha256
from pathlib import Path
from shutil import copytree
from unittest.mock import patch
from uuid import uuid4

import psycopg
import pytest
from psycopg import sql

from jimu_runtime.bundle.store import LocalBundleStore
from jimu_runtime.domain.policies import CPUResourceRequirements
from jimu_runtime.domain.runtime.media import ImageDimensions, MediaRef
from jimu_runtime.inspection.media import LocalMediaStore
from jimu_runtime.workers.resource_inventory import HostInventory, ResourceScope
from jimu_runtime.workers.resources import ResourceAdmission
from jimu_runtime.workflow.postgres import (
    WORKFLOW_APP_ROLE,
    apply_migrations,
    create_postgres_engine,
)
from jimu_runtime.workflow.worker import WorkerStep, WorkflowWorker
from ops.recovery import (
    Endpoint,
    NativeTools,
    SourceVolumes,
    backup,
    prepare_media_locks,
    restore,
    verify_restore,
)
from ops.recovery.contracts import BUNDLE, MANIFEST, RESOURCES, RecoveryError
from ops.recovery.filesystem import (
    create_directory,
    file_witness,
    ledger,
    read_private,
    stream_witness,
)
from tests.fixtures.wp03.postgres.support import POSTGRES_IMAGE, isolated_postgres
from tests.fixtures.wp03.worker.support import (
    LIMITS,
    accepted_worker,
)

_CONFIG = sha256(b"approved isolated fixture configuration revision").hexdigest()
_IMAGE = Path(
    "examples/nonmotor_vehicle_illegal_parking/compilable/assets/synthetic-frame.png"
)


def _owner(pg):
    url = pg.migration_url
    return psycopg.connect(
        host="127.0.0.1",
        port=url.port,
        dbname=url.database,
        user=url.username,
        password=url.password,
        connect_timeout=5,
        autocommit=True,
    )


def _endpoint(pg, password_file, identity=None):
    url = pg.migration_url
    password_file.write_text(
        f"127.0.0.1:{url.port}:{url.database}:{url.username}:{url.password}\n",
        encoding="ascii",
    )
    password_file.chmod(0o600)
    return Endpoint(
        url.database, url.port, identity or pg.target.instance_id, password_file
    )


def _fence(pg, *, on):
    with _owner(pg) as owner:
        oc = (
            sql.SQL("REVOKE CONNECT ON DATABASE {} FROM {}")
            if on
            else sql.SQL("GRANT CONNECT ON DATABASE {} TO {}")
        )
        owner.execute(
            oc.format(
                sql.Identifier(pg.target.database), sql.Identifier(WORKFLOW_APP_ROLE)
            )
        )


async def _seed(pg, source: Path, media_root: Path):
    owner = create_postgres_engine(pg.migration_url)
    app = create_postgres_engine(pg.application_url)
    try:
        versions = await apply_migrations(owner, pg.target)
        assert 7 in versions
        worker, accepted, host = await accepted_worker(app, source)
        scope, eid = accepted.task.idempotency_scope, accepted.execution.execution_id
        steps = tuple(
            [
                await worker.run_once(scope, eid, owner="recovery-seed", lease_ms=2000)
                for _ in range(14)
            ]
        )
        assert WorkerStep.FINALIZED in steps
        finalization = await worker.read_finalization(scope, eid)
        assert finalization is not None and finalization.decisions
        raw = _IMAGE.read_bytes()
        media_ref = MediaRef(
            media_id="physical-recovery-original", sha256=sha256(raw).hexdigest()
        )
        media = LocalMediaStore(media_root, app)
        asset = await media.publish_original(
            scope,
            media_ref,
            mime_type="image/png",
            dimensions=ImageDimensions(width=64, height=48),
            content=raw,
        )
        return accepted, host, finalization, asset, raw
    finally:
        await owner.dispose()
        await app.dispose()


async def _verify(pg, target, accepted, host, expected, asset, raw):
    app = create_postgres_engine(pg.application_url)
    try:
        from jimu_runtime.bundle.store import LocalBundleStore
        from tests.fixtures.wp03.worker.support import manager_for

        store = LocalBundleStore(target)
        manager = manager_for(store, host.release_host)
        assert manager.get_release(accepted.task.release_id) is not None
        worker = WorkflowWorker(app, manager, store, host=host, bundle_limits=LIMITS)
        final = await worker.read_finalization(
            accepted.task.idempotency_scope,
            accepted.execution.execution_id,
        )
        assert final == expected
        verified = await LocalMediaStore(target, app).read_original(
            accepted.task.idempotency_scope, asset.media_ref
        )
        assert verified.asset == asset and verified.content == raw
    finally:
        await app.dispose()


@pytest.mark.parametrize("interrupted", [False, True])
def test_recovery_round_trip_preserves_decision_original_release_and_lease(
    tmp_path, interrupted
):
    with isolated_postgres() as original, isolated_postgres() as fresh:
        source = tmp_path / "source"
        source.mkdir(mode=0o700)
        media_root = source / "media"
        accepted, host, final, asset, raw = asyncio.run(
            _seed(original, source, media_root)
        )
        inventory = HostInventory(
            "recovery.host",
            lease_ttl_ms=5000,
            cpu_slots=1,
            scopes=(ResourceScope("recovery.cpu", "cpu", 1),),
        )
        resources_path = source / RESOURCES
        resources = ResourceAdmission.provision(resources_path, inventory)
        lease = resources.reserve(
            CPUResourceRequirements(
                policy_kind="resource_requirements",
                resource_class="cpu",
                max_concurrency=1,
                timeout_ms=1000,
                input_byte_limit=2048,
                output_byte_limit=2048,
            ),
            "recovery.cpu",
            owner_id="recovery.suspended",
        )
        binding_path = tmp_path / "host-binding.json"
        binding_bytes = json.dumps(
            {
                "schema_version": "1.0.0",
                "deployment_config_sha256": _CONFIG,
                "inventory_id": inventory.inventory_id,
                "inventory_sha256": inventory.fingerprint(),
            },
            sort_keys=True,
        ).encode()
        binding_path.write_bytes(binding_bytes)
        binding_path.chmod(0o600)
        config_digest = sha256(binding_bytes).hexdigest()
        common = {"host_config_path": binding_path, "maintenance_confirmed": True}
        views = SourceVolumes(source / "ledger" / BUNDLE, resources_path, media_root)
        package, target = tmp_path / "package", tmp_path / "restored"
        dump = NativeTools(image=POSTGRES_IMAGE)
        source_endpoint = _endpoint(original, tmp_path / "source.pgpass")
        with pytest.raises(RecoveryError, match="postgres_writers_not_quiesced"):
            backup(
                source_endpoint,
                dump,
                views,
                package,
                host_config_sha256=config_digest,
                **common,
            )
        assert not package.exists()
        _fence(original, on=True)
        shards = media_root / "media-v1" / ".locks"
        before = set(shards.iterdir())
        with pytest.raises(RecoveryError, match="media_lock_shards_not_provisioned"):
            backup(
                source_endpoint,
                dump,
                views,
                package,
                host_config_sha256=config_digest,
                **common,
            )
        assert set(shards.iterdir()) == before
        prepare_media_locks(media_root)
        with _owner(original):
            with pytest.raises(RecoveryError, match="postgres_writers_not_quiesced"):
                backup(
                    source_endpoint,
                    dump,
                    views,
                    package,
                    host_config_sha256=config_digest,
                    **common,
                )
        with sqlite3.connect(resources_path) as occupied:
            occupied.execute("BEGIN IMMEDIATE")
            occupied.execute("UPDATE inventory SET inventory_id='staged'")
            with pytest.raises(RecoveryError, match="sqlite_recovery_required"):
                backup(
                    source_endpoint,
                    dump,
                    views,
                    package,
                    host_config_sha256=config_digest,
                    **common,
                )
            occupied.rollback()
        shard = media_root / "media-v1" / ".locks" / "00"
        with shard.open("r+b") as held:
            flock(held.fileno(), LOCK_EX)
            with pytest.raises(RecoveryError, match="media_writer_not_quiesced"):
                backup(
                    source_endpoint,
                    dump,
                    views,
                    package,
                    host_config_sha256=config_digest,
                    **common,
                )
        assert not package.exists()
        if not interrupted:
            unrelated = source / "unrelated"
            LocalBundleStore(unrelated)
            wrong_bundle = replace(views, bundle=unrelated / BUNDLE)
            with pytest.raises(
                RecoveryError, match="postgres_release_or_bundle_missing_from_ledger"
            ):
                backup(
                    source_endpoint,
                    dump,
                    wrong_bundle,
                    package,
                    host_config_sha256=config_digest,
                    **common,
                )
            wrong_path = source / "unrelated-resource.sqlite3"
            ResourceAdmission.provision(
                wrong_path, replace(inventory, inventory_id="other.host")
            )
            with pytest.raises(
                RecoveryError, match="resource_ledger_does_not_match_trusted_inventory"
            ):
                backup(
                    source_endpoint,
                    dump,
                    replace(views, resources=wrong_path),
                    package,
                    host_config_sha256=config_digest,
                    **common,
                )
            for install, cleanup, error in [
                (
                    "GRANT DELETE ON jimu_workflow.execution_finalizations TO PUBLIC",
                    "REVOKE DELETE ON jimu_workflow.execution_finalizations FROM PUBLIC",
                    "postgres_runtime_permissions_or_owner_mismatch",
                ),
                (
                    "CREATE FUNCTION jimu_workflow.extra() RETURNS bool LANGUAGE SQL SECURITY DEFINER AS 'SELECT true'",
                    "DROP FUNCTION jimu_workflow.extra()",
                    "postgres_runtime_permissions_or_owner_mismatch",
                ),
                (
                    'CREATE COLLATION public.extra FROM "C"',
                    "DROP COLLATION public.extra",
                    "unsupported_source_database_objects",
                ),
            ]:
                with _owner(original) as owner:
                    owner.execute(install)
                with pytest.raises(RecoveryError, match=error):
                    backup(
                        source_endpoint,
                        dump,
                        views,
                        package,
                        host_config_sha256=config_digest,
                        **common,
                    )
                with _owner(original) as owner:
                    owner.execute(cleanup)
            with sqlite3.connect(resources_path) as connection:
                connection.execute(
                    "UPDATE leases SET pid=999999, process_start='999' WHERE lease_id=?",
                    (lease.lease_id,),
                )
            with pytest.raises(RecoveryError, match="worker_process_tree_not_reaped"):
                backup(
                    source_endpoint,
                    dump,
                    views,
                    package,
                    host_config_sha256=config_digest,
                    **common,
                )
            with sqlite3.connect(resources_path) as connection:
                connection.execute(
                    "UPDATE leases SET pid=NULL, process_start=NULL WHERE lease_id=?",
                    (lease.lease_id,),
                )
            with patch("ops.recovery.MAX_MANIFEST", 50):
                oversized = tmp_path / "oversized"
                with pytest.raises(RecoveryError, match="manifest_size_invalid"):
                    backup(
                        source_endpoint,
                        dump,
                        views,
                        oversized,
                        host_config_sha256=config_digest,
                        **common,
                    )
                assert not (oversized / MANIFEST).exists()
        manifest_sha = backup(
            source_endpoint,
            dump,
            views,
            package,
            host_config_sha256=config_digest,
            **common,
        )
        observed = json.loads((package / MANIFEST).read_bytes())
        assert observed["source_database"] == original.target.database
        assert observed["postgres_tables"]["execution_finalizations"]["rows"] == 1
        assert observed["postgres_tables"]["original_media"]["rows"] == 1
        with _owner(fresh) as owner:
            owner.execute(
                sql.SQL("COMMENT ON DATABASE {} IS {}").format(
                    sql.Identifier(fresh.target.database),
                    sql.Literal(original.target.database_comment),
                )
            )
        _fence(fresh, on=True)
        target_endpoint = _endpoint(
            fresh, tmp_path / "target.pgpass", original.target.instance_id
        )
        with pytest.raises(RecoveryError, match="backup_manifest_digest_mismatch"):
            restore(
                target_endpoint,
                dump,
                package,
                target,
                manifest_sha256="0" * 64,
                host_config_sha256=config_digest,
                **common,
            )
        with pytest.raises(RecoveryError, match="configuration_mismatch"):
            restore(
                target_endpoint,
                dump,
                package,
                target,
                manifest_sha256=manifest_sha,
                host_config_sha256="0" * 64,
                **common,
            )
        existing = tmp_path / "existing"
        existing.mkdir(mode=0o700)
        with pytest.raises(RecoveryError, match="output_directory_must_be_new"):
            restore(
                target_endpoint,
                dump,
                package,
                existing,
                manifest_sha256=manifest_sha,
                host_config_sha256=config_digest,
                **common,
            )
        tampered = copytree(package, tmp_path / "tampered")
        with (tampered / RESOURCES).open("ab") as ledger:
            ledger.write(b"changed")
        rejected = tmp_path / "rejected-tampered"
        with pytest.raises(RecoveryError, match="backup_file_digest_mismatch"):
            restore(
                target_endpoint,
                dump,
                tampered,
                rejected,
                manifest_sha256=manifest_sha,
                host_config_sha256=config_digest,
                **common,
            )
        assert not (rejected / "restore-verified.json").exists()
        with _owner(fresh) as owner:
            assert owner.execute(
                "SELECT to_regnamespace('jimu_workflow') IS NULL"
            ).fetchone() == (True,)
        forged = copytree(package, tmp_path / "traversal")
        malformed = json.loads((forged / MANIFEST).read_bytes())
        malformed["files"]["../outside"] = {"sha256": "0" * 64, "size": 0}
        manifest_bytes = (
            json.dumps(malformed, separators=(",", ":"), sort_keys=True) + "\n"
        ).encode()
        (forged / MANIFEST).write_bytes(manifest_bytes)
        with pytest.raises(RecoveryError, match="manifest_contains_unsupported_paths"):
            restore(
                target_endpoint,
                dump,
                forged,
                tmp_path / "rejected-traversal",
                manifest_sha256=sha256(manifest_bytes).hexdigest(),
                host_config_sha256=config_digest,
                **common,
            )
        args = (target_endpoint, dump, package, target)
        proofs = {
            "manifest_sha256": manifest_sha,
            "host_config_sha256": config_digest,
            **common,
        }
        if interrupted:
            # Stop publication after the native PostgreSQL restore has committed.
            with patch(
                "ops.recovery._verify_and_publish",
                side_effect=RecoveryError("fixture_interruption"),
            ):
                with pytest.raises(RecoveryError, match="fixture_interruption"):
                    restore(*args, **proofs)
            assert not (target / "restore-verified.json").exists()
            assert (target / "restore-pending.json").exists()
            witness = verify_restore(*args, **proofs)
        else:
            witness = restore(*args, **proofs)
        assert witness.logical_database_id == original.target.instance_id
        assert (target / "restore-verified.json").exists()
        assert (target / asset.storage.object_key).stat().st_mode & 0o777 == 0o400
        assert (target / "media-v1" / ".database").stat().st_mode & 0o777 == 0o600
        with sqlite3.connect(target / RESOURCES) as restored:
            assert restored.execute(
                "SELECT lease_id, owner_id, owner_generation FROM leases",
            ).fetchall() == [(lease.lease_id, lease.owner_id, lease.owner_generation)]
        ResourceAdmission(target / RESOURCES, inventory)
        with pytest.raises(RecoveryError, match="target_database_not_empty"):
            restore(
                target_endpoint,
                dump,
                package,
                tmp_path / "again",
                manifest_sha256=manifest_sha,
                host_config_sha256=config_digest,
                **common,
            )
        _fence(fresh, on=False)
        asyncio.run(_verify(fresh, target, accepted, host, final, asset, raw))


def test_recovery_rejects_unsafe_paths_and_bounds_writes(tmp_path):
    shared = tmp_path / "shared"
    shared.mkdir(mode=0o700)
    shared.chmod(0o777)
    try:
        with pytest.raises(RecoveryError, match="unsafe_path_ancestor"):
            create_directory(shared / "unsafe")
        binary = shared / "pg_dump"
        binary.write_text("#!/bin/sh\nexit 0\n")
        binary.chmod(0o700)
        with pytest.raises(RecoveryError, match="unsafe_path_ancestor"):
            NativeTools(directory=shared).version("pg_dump")
    finally:
        shared.chmod(0o700)
    source = tmp_path / "asset"
    source.write_bytes(b"unexpectedly larger")
    source.chmod(0o600)
    output = tmp_path / "output"
    with pytest.raises(RecoveryError, match="backup_file_digest_mismatch"):
        file_witness(source, output, expected_size=1)
    assert not output.exists()


def test_sqlite_read_snapshot_survives_copy_without_writer_commit(tmp_path):
    source = tmp_path / "source.sqlite3"
    with sqlite3.connect(source) as db:
        db.execute("CREATE TABLE observed(value INTEGER)")
        db.execute("INSERT INTO observed VALUES(1)")
    source.chmod(0o600)
    copy = tmp_path / "copied.sqlite3"
    with read_private(source) as original, ledger(source, freeze=True):
        stream_witness(original, copy)
        # POSIX ownership is per process. A separate SQLite writer must still
        # fail to acquire the commit lock after the byte copy completes.
        writer = subprocess.run(
            [
                sys.executable,
                "-c",
                (
                    "import sqlite3,sys; db=sqlite3.connect(sys.argv[1],timeout=0); "
                    "db.execute('UPDATE observed SET value=2'); db.commit()"
                ),
                str(source),
            ],
            capture_output=True,
            check=False,
        )
        assert writer.returncode != 0 and b"database is locked" in writer.stderr
    assert file_witness(source) == file_witness(copy)


def test_native_tool_version_is_secretless_and_wrong_docker_network_denies_launch(
    tmp_path,
):
    tools = NativeTools(image=POSTGRES_IMAGE)
    with patch(
        "ops.recovery.postgres.subprocess.run",
        return_value=subprocess.CompletedProcess(
            [],
            0,
            stdout=b"pg_dump (PostgreSQL) 16.14\n",
        ),
    ) as run:
        assert tools.version("pg_dump").startswith("pg_dump (PostgreSQL) 16.")
        values = run.call_args
        assert "--mount" not in values.args[0]
        assert "PGPASSFILE" not in values.kwargs["env"]
        assert "--network=none" in values.args[0]

    passfile = tmp_path / "credentials"
    passfile.touch(mode=0o600)
    endpoint = Endpoint("jimu_owned", 5439, uuid4(), passfile)
    with patch(
        "ops.recovery.postgres.subprocess.run",
        return_value=subprocess.CompletedProcess(
            [],
            0,
            stdout=b"unrelated-boot\nunrelated-network\n",
        ),
    ) as run:
        with pytest.raises(
            RecoveryError, match="docker_postgres_network_identity_unverified"
        ):
            tools.run("pg_restore", [], endpoint=endpoint)
        assert run.call_count == 1
        assert "--mount" not in run.call_args.args[0]


def test_restore_timeout_cleanup_leaves_commit_unknown(tmp_path):
    tools = NativeTools(image=POSTGRES_IMAGE)
    passfile = tmp_path / "credentials"
    passfile.touch(mode=0o600)
    endpoint = Endpoint("jimu_owned", 5439, uuid4(), passfile)
    owned = "a" * 64
    with (
        patch.object(NativeTools, "_prove_docker_network"),
        patch(
            "ops.recovery.postgres.subprocess.run",
            side_effect=[
                subprocess.TimeoutExpired(["docker"], 300),
                subprocess.CompletedProcess([], 0, stdout=(owned + "\n").encode()),
                subprocess.CompletedProcess([], 0, stdout=b""),
            ],
        ) as operations,
    ):
        with pytest.raises(RecoveryError, match="target_restore_commit_unknown"):
            tools.run("pg_restore", [], endpoint=endpoint)
        assert operations.call_count == 3
        assert any(
            "label=jimu.recovery.run=" in item
            for item in operations.call_args_list[1].args[0]
        )
        assert operations.call_args_list[2].args[0][-3:] == ["rm", "-f", owned]
