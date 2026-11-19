"""Run WP-07 recovery inside a provisioned isolated deployment."""

import argparse
import json
from pathlib import Path
from uuid import UUID

from ops.recovery import (
    Endpoint,
    NativeTools,
    SourceVolumes,
    backup,
    prepare_media_locks,
    restore,
    verify_restore,
)
from ops.recovery.contracts import RecoveryError


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    command = parser.add_subparsers(dest="command", required=True)
    provision = command.add_parser("prepare-media-locks")
    provision.add_argument("--media-root", required=True, type=Path)
    backups = command.add_parser("backup")
    restores = command.add_parser("restore")
    verifies = command.add_parser("verify")
    for action in (backups, restores, verifies):
        action.add_argument("--database", required=True)
        action.add_argument("--port", required=True, type=int)
        action.add_argument("--logical-id", required=True, type=UUID)
        action.add_argument("--pgpass", required=True, type=Path)
        action.add_argument("--host-config", required=True, type=Path)
        action.add_argument("--host-config-sha256", required=True)
        action.add_argument(
            "--confirm-isolated-maintenance", required=True, action="store_true"
        )
        action.add_argument("--package", required=True, type=Path)
        binaries = action.add_mutually_exclusive_group(required=True)
        binaries.add_argument("--pg-bin", type=Path)
        binaries.add_argument("--postgres-image")
    backups.add_argument("--bundle-db", required=True, type=Path)
    backups.add_argument("--resource-db", required=True, type=Path)
    backups.add_argument("--media-root", required=True, type=Path)
    for action in (restores, verifies):
        action.add_argument("--destination", required=True, type=Path)
        action.add_argument("--manifest-sha256", required=True)
    args = parser.parse_args()
    try:
        if args.command == "prepare-media-locks":
            prepare_media_locks(args.media_root)
        else:
            endpoint = Endpoint(args.database, args.port, args.logical_id, args.pgpass)
            native = NativeTools(args.pg_bin, args.postgres_image)
            if args.command == "backup":
                digest = backup(
                    endpoint,
                    native,
                    SourceVolumes(args.bundle_db, args.resource_db, args.media_root),
                    args.package,
                    host_config_sha256=args.host_config_sha256,
                    host_config_path=args.host_config,
                    maintenance_confirmed=args.confirm_isolated_maintenance,
                )
                print(json.dumps({"manifest_sha256": digest}, sort_keys=True))
            else:
                apply = restore if args.command == "restore" else verify_restore
                manifest = apply(
                    endpoint,
                    native,
                    args.package,
                    args.destination,
                    manifest_sha256=args.manifest_sha256,
                    host_config_sha256=args.host_config_sha256,
                    host_config_path=args.host_config,
                    maintenance_confirmed=args.confirm_isolated_maintenance,
                )
                print(
                    json.dumps(
                        {"logical_database_id": str(manifest.logical_database_id)}
                    )
                )
        return 0
    except (RecoveryError, OSError) as error:
        code = (
            str(error)
            if isinstance(error, RecoveryError)
            else "filesystem_operation_failed"
        )
        parser.exit(1, f"{code}\n")


if __name__ == "__main__":
    raise SystemExit(main())
