"""Coordinated backup and restore for a provisioned, quiesced WP-07 test deployment."""

import json
import re
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path

from psycopg import Connection
from pydantic import ValidationError

from ops.recovery import postgres as pg
from ops.recovery.contracts import (
    BUNDLE,
    DUMP,
    IDENTITY,
    MANIFEST,
    MEDIA,
    RESOURCES,
    HostBinding,
    Manifest,
    RecoveryError,
)
from ops.recovery.filesystem import (
    MAX_MANIFEST,
    create_directory,
    file_witness,
    freeze_media,
    ledger,
    ledger_versions,
    media_names,
    new_private,
    private_path,
    publish_marker,
    read_manifest,
    read_private,
    require_clean_sqlite,
    stream_witness,
    sync_directory,
    trusted_ancestors,
    validate_package_names,
    verify_bundle_pins,
)
from ops.recovery.filesystem import (
    prepare_media_locks as prepare_media_locks,
)
from ops.recovery.postgres import Endpoint as Endpoint
from ops.recovery.postgres import NativeTools as NativeTools

RESTORED = "restore-verified.json"
PENDING = "restore-pending.json"


@dataclass(frozen=True, slots=True)
class SourceVolumes:
    bundle: Path
    resources: Path
    media_root: Path


def _digest(value: str) -> str:
    if not re.fullmatch(r"[0-9a-f]{64}", value):
        raise RecoveryError("trusted_sha256_required")
    return value


def _encoded(manifest: Manifest) -> bytes:
    return (
        json.dumps(
            manifest.model_dump(mode="json"), sort_keys=True, separators=(",", ":")
        )
        + "\n"
    ).encode("utf-8")


def _require_credentials_outside(endpoint: Endpoint, *directories: Path) -> None:
    password = private_path(endpoint.passfile)
    if any(password.is_relative_to(path.absolute()) for path in directories):
        raise RecoveryError("credentials_must_stay_outside_recovery_paths")


def _host_binding(
    path: Path,
    expected: str,
    maintenance_confirmed: bool,
    *excluded: Path,
) -> HostBinding:
    if maintenance_confirmed is not True:
        raise RecoveryError("exclusive_isolated_maintenance_required")
    host_path = private_path(path)
    trusted_ancestors(host_path.parent)
    if any(host_path.is_relative_to(item.absolute()) for item in excluded):
        raise RecoveryError("host_config_must_stay_outside_recovery_paths")
    raw = read_manifest(host_path)
    if sha256(raw).hexdigest() != _digest(expected):
        raise RecoveryError("trusted_host_binding_digest_mismatch")
    try:
        return HostBinding.model_validate_json(raw)
    except ValidationError:
        raise RecoveryError("trusted_host_binding_invalid") from None


def _check_inventory(versions: tuple[int, int, str, str], host: HostBinding) -> None:
    if versions[2:] != (host.inventory_id, host.inventory_sha256):
        raise RecoveryError("resource_ledger_does_not_match_trusted_inventory")


def _check_media_identity(
    root: Path, endpoint: Endpoint, *, source: bool = False
) -> None:
    with read_private(root / IDENTITY, parent_protected=source) as value:
        if value.read(128) != endpoint.comment.encode("ascii"):
            raise RecoveryError("media_database_identity_mismatch")


def backup(
    endpoint: Endpoint,
    tools: NativeTools,
    volumes: SourceVolumes,
    package: Path,
    *,
    host_config_sha256: str,
    host_config_path: Path,
    maintenance_confirmed: bool = False,
) -> str:
    """Write a new immutable-input package and return its externally pinned manifest SHA."""
    host = _host_binding(
        host_config_path,
        host_config_sha256,
        maintenance_confirmed,
        package,
        volumes.media_root / MEDIA,
    )
    _require_credentials_outside(endpoint, package, volumes.media_root / MEDIA)
    if (
        package.absolute().is_relative_to(volumes.media_root.absolute() / MEDIA)
        or volumes.bundle.absolute() == volumes.resources.absolute()
    ):
        raise RecoveryError("backup_sources_and_destination_must_be_distinct")
    bundle = private_path(volumes.bundle)
    resources = private_path(volumes.resources)
    for path in (
        bundle.parent,
        resources.parent,
        (volumes.media_root / MEDIA).absolute(),
    ):
        trusted_ancestors(path)
    with pg.database(endpoint) as source:
        server_version = pg.quiesced(source, endpoint)
        pg.isolated_schema(source, empty=False)
        dump_version = tools.version("pg_dump")
        # Writers acquire media locks before a database write. Keep that ordering
        # and use NOWAIT/zero timeout for the other stores.
        with (
            freeze_media(volumes.media_root),
            read_private(bundle) as bundle_bytes,
            read_private(resources) as resource_bytes,
            ledger(bundle, freeze=True) as bundle_db,
            ledger(resources, freeze=True) as resource_db,
            source.transaction(),
        ):
            source.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ")
            pg.quiesced(source, endpoint)
            names = pg.tables(source)
            pg.lock_tables(source, names)
            versions = ledger_versions(bundle_db, resource_db, quiesced=True)
            _check_inventory(versions, host)
            verify_bundle_pins(bundle_db, pg.bundle_pins(source))
            entries = media_names(volumes.media_root, source=True)
            _check_media_identity(volumes.media_root, endpoint, source=True)
            snapshot = source.execute("SELECT pg_export_snapshot()").fetchone()
            assert snapshot is not None
            output = create_directory(package)
            media = create_directory(output / MEDIA)
            witnesses = {
                name: file_witness(
                    volumes.media_root / name,
                    output / name,
                    parent_protected=name == IDENTITY,
                )
                for name in sorted(entries)
            }
            pg.verify_media(source, names, witnesses, bundle_db)
            witnesses[BUNDLE] = stream_witness(bundle_bytes, output / BUNDLE)
            witnesses[RESOURCES] = stream_witness(resource_bytes, output / RESOURCES)
            with new_private(output / DUMP) as dump:
                tools.run(
                    "pg_dump",
                    [
                        *endpoint.cli_args(),
                        "--format=custom",
                        "--no-owner",
                        "--no-tablespaces",
                        "--lock-wait-timeout=5000",
                        "--snapshot",
                        str(snapshot[0]),
                    ],
                    endpoint=endpoint,
                    output=dump,
                )
            witnesses[DUMP] = file_witness(output / DUMP)
            if witnesses[DUMP].size == 0:
                raise RecoveryError("postgres_dump_empty")
            manifest = Manifest(
                schema_version="1.0.0",
                logical_database_id=endpoint.instance_id,
                source_database=endpoint.database,
                postgres_version=server_version,
                dump_version=dump_version,
                host_config_sha256=host_config_sha256,
                bundle_version=versions[0],
                resource_version=versions[1],
                inventory_id=versions[2],
                inventory_sha256=versions[3],
                postgres_tables=pg.snapshot(source, names),
                files=witnesses,
            )
            pg.quiesced(source, endpoint)
            raw = _encoded(manifest)
            if len(raw) > MAX_MANIFEST:
                raise RecoveryError("manifest_size_invalid")
            require_clean_sqlite(bundle)
            require_clean_sqlite(resources)
            sync_directory(media)
            publish_marker(output / MANIFEST, raw)
            sync_directory(output)
            sync_directory(output.parent)
            return sha256(raw).hexdigest()


def _check_package(package: Path, expected_sha256: str) -> Manifest:
    raw = read_manifest(package / MANIFEST)
    if sha256(raw).hexdigest() != _digest(expected_sha256):
        raise RecoveryError("backup_manifest_digest_mismatch")
    try:
        manifest = Manifest.model_validate_json(raw)
    except ValidationError:
        raise RecoveryError("backup_manifest_invalid") from None
    if _encoded(manifest) != raw:
        raise RecoveryError("backup_manifest_noncanonical")
    expected = set(manifest.files)
    validate_package_names(expected)
    if {item.name for item in package.iterdir()} != {
        MANIFEST,
        MEDIA,
        BUNDLE,
        RESOURCES,
        DUMP,
    } or media_names(package) != {
        name for name in expected if name.startswith(f"{MEDIA}/")
    }:
        raise RecoveryError("backup_package_has_missing_or_extra_files")
    if (package / MEDIA / ".locks").exists():
        raise RecoveryError("backup_package_contains_live_lock_files")
    return manifest


def _restore_inputs(
    endpoint: Endpoint,
    package: Path,
    destination: Path,
    *,
    manifest_sha256: str,
    host_config_sha256: str,
    host_config_path: Path,
    maintenance_confirmed: bool,
) -> tuple[Path, Manifest, HostBinding]:
    package = private_path(package, directory=True)
    trusted_ancestors(package)
    _require_credentials_outside(endpoint, package, destination)
    if destination.absolute().is_relative_to(package):
        raise RecoveryError("restore_destination_must_be_outside_package")
    manifest = _check_package(package, manifest_sha256)
    if (
        manifest.logical_database_id != endpoint.instance_id
        or manifest.host_config_sha256 != _digest(host_config_sha256)
    ):
        raise RecoveryError("restore_database_or_host_configuration_mismatch")
    host = _host_binding(
        host_config_path,
        host_config_sha256,
        maintenance_confirmed,
        package,
        destination,
    )
    _check_media_identity(package, endpoint)
    return package, manifest, host


def _pending(
    endpoint: Endpoint, manifest_sha256: str, host_config_sha256: str
) -> bytes:
    return (
        json.dumps(
            {
                "schema_version": "1.0.0",
                "database": endpoint.database,
                "port": endpoint.port,
                "logical_database_id": str(endpoint.instance_id),
                "manifest_sha256": manifest_sha256,
                "host_config_sha256": host_config_sha256,
            },
            sort_keys=True,
        )
        + "\n"
    ).encode("utf-8")


def _sqlite_check(
    output: Path,
    manifest: Manifest,
    host: HostBinding,
) -> None:
    with (
        ledger(output / BUNDLE, freeze=False) as bundle,
        ledger(output / RESOURCES, freeze=False) as resources,
    ):
        observed = ledger_versions(bundle, resources, quiesced=False)
    _check_inventory(observed, host)
    if observed != (
        manifest.bundle_version,
        manifest.resource_version,
        manifest.inventory_id,
        manifest.inventory_sha256,
    ):
        raise RecoveryError("restored_sqlite_metadata_mismatch")


def _verify_and_publish(
    endpoint: Endpoint,
    target: Connection,
    output: Path,
    manifest: Manifest,
    *,
    server_version: str,
    restore_version: str,
    pending: bytes,
) -> None:
    if read_manifest(output / PENDING) != pending:
        raise RecoveryError("restore_pending_binding_mismatch")
    _check_media_identity(output, endpoint)
    for name, witness in manifest.files.items():
        if file_witness(output / name, expected_size=witness.size) != witness:
            raise RecoveryError("restored_files_changed")
    with target.transaction():
        pg.quiesced(target, endpoint)
        pg.isolated_schema(target, empty=False)
        names = pg.tables(target)
        pg.lock_tables(target, names)
        if pg.snapshot(target, names) != manifest.postgres_tables:
            raise RecoveryError("restored_postgres_records_differ")
        with ledger(output / BUNDLE, freeze=False) as bundle:
            pg.verify_media(target, names, manifest.files, bundle)
            verify_bundle_pins(bundle, pg.bundle_pins(target))
        sync_directory(output / MEDIA / ".locks")
        sync_directory(output / MEDIA)
        binding = json.loads(pending)
        binding.update(
            {"postgres_version": server_version, "restore_version": restore_version}
        )
        publish_marker(
            output / RESTORED, (json.dumps(binding, sort_keys=True) + "\n").encode()
        )
        sync_directory(output)
        sync_directory(output.parent)


def restore(
    endpoint: Endpoint,
    tools: NativeTools,
    package: Path,
    destination: Path,
    *,
    manifest_sha256: str,
    host_config_sha256: str,
    host_config_path: Path,
    maintenance_confirmed: bool = False,
) -> Manifest:
    """Restore to externally provisioned empty PG16 and a new quarantined file root."""
    package, manifest, host = _restore_inputs(
        endpoint,
        package,
        destination,
        manifest_sha256=manifest_sha256,
        host_config_sha256=host_config_sha256,
        host_config_path=host_config_path,
        maintenance_confirmed=maintenance_confirmed,
    )
    with pg.database(endpoint) as target:
        target_version = pg.quiesced(target, endpoint)
        pg.isolated_schema(target, empty=True)
        restore_version = tools.version("pg_restore")
        output = create_directory(destination)
        media = create_directory(output / MEDIA)
        create_directory(media / ".locks")
        prepare_media_locks(output)
        for name, witness in sorted(manifest.files.items()):
            restored = file_witness(
                package / name,
                output / name,
                readonly=name.startswith(f"{MEDIA}/") and name != IDENTITY,
                expected_size=witness.size,
            )
            if restored != witness:
                raise RecoveryError("backup_file_digest_mismatch")
        _sqlite_check(output, manifest, host)
        pg.quiesced(target, endpoint)
        pg.isolated_schema(target, empty=True)
        pending = _pending(endpoint, manifest_sha256, host_config_sha256)
        publish_marker(output / PENDING, pending)
        sync_directory(media)
        sync_directory(output)
        sync_directory(output.parent)
        with read_private(output / DUMP) as dump:
            tools.run(
                "pg_restore",
                [
                    *endpoint.cli_args(),
                    "--single-transaction",
                    "--exit-on-error",
                    "--no-owner",
                    "--no-tablespaces",
                ],
                endpoint=endpoint,
                source=dump,
            )
        _verify_and_publish(
            endpoint,
            target,
            output,
            manifest,
            server_version=target_version,
            restore_version=restore_version,
            pending=pending,
        )
        return manifest


def verify_restore(
    endpoint: Endpoint,
    tools: NativeTools,
    package: Path,
    destination: Path,
    *,
    manifest_sha256: str,
    host_config_sha256: str,
    host_config_path: Path,
    maintenance_confirmed: bool = False,
) -> Manifest:
    """Reverify a quarantined restore after an uncertain commit without replaying SQL."""
    _, manifest, host = _restore_inputs(
        endpoint,
        package,
        destination,
        manifest_sha256=manifest_sha256,
        host_config_sha256=host_config_sha256,
        host_config_path=host_config_path,
        maintenance_confirmed=maintenance_confirmed,
    )
    output = private_path(destination, directory=True)
    trusted_ancestors(output)
    pending = _pending(endpoint, manifest_sha256, host_config_sha256)
    if read_manifest(output / PENDING) != pending or (output / RESTORED).exists():
        raise RecoveryError("restore_pending_binding_mismatch")
    _sqlite_check(output, manifest, host)
    with pg.database(endpoint) as target:
        version = pg.quiesced(target, endpoint)
        _verify_and_publish(
            endpoint,
            target,
            output,
            manifest,
            server_version=version,
            restore_version=tools.version("pg_restore"),
            pending=pending,
        )
    return manifest
