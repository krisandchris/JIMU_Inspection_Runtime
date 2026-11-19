"""Own a disposable PostgreSQL 16 container and its test credentials."""

import re
import secrets
import subprocess
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from importlib.resources import files
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import uuid4

import psycopg
from psycopg import sql
from sqlalchemy import URL

from jimu_runtime.workflow import migrations
from jimu_runtime.workflow.postgres import (
    WORKFLOW_APP_ROLE,
    WORKFLOW_OWNER_ROLE,
    MigrationTarget,
)

POSTGRES_IMAGE = (
    "sha256:4b7183ac05f8ef417db21fd72d71047a4238340c261d3cc3ddb6d579ab5071ae"
)
_DOCKER = ("docker", "--host", "unix:///var/run/docker.sock")
_BOOTSTRAP_HOST = "127.0.0.1"


@dataclass(frozen=True, slots=True)
class IsolatedPostgres:
    target: MigrationTarget
    server_version: str
    application_url: URL = field(repr=False)
    migration_url: URL = field(repr=False)


def stage_fixture_migrations(directory: Path) -> None:
    """Stage the real bootstrap and isolated DDL for migration fault tests."""
    bootstrap = "001_workflow_schema.sql"
    (directory / bootstrap).write_bytes(
        files(migrations).joinpath(bootstrap).read_bytes()
    )
    fixture_script = Path(__file__).with_name("002_fixture_objects.sql")
    (directory / fixture_script.name).write_bytes(fixture_script.read_bytes())


def _docker(*args: str, timeout: float = 20) -> str:
    result = subprocess.run(
        [*_DOCKER, *args],
        capture_output=True,
        text=True,
        check=False,
        timeout=timeout,
    )
    if result.returncode != 0:
        raise RuntimeError(f"Isolated PostgreSQL docker {args[0]} failed")
    return result.stdout.strip()


def _bootstrap(
    port: int,
    admin_password: str,
    target: MigrationTarget,
    owner_password: str,
    app_password: str,
) -> str:
    deadline = time.monotonic() + 35
    while True:
        try:
            connection = psycopg.connect(
                host=_BOOTSTRAP_HOST,
                hostaddr=_BOOTSTRAP_HOST,
                port=port,
                user="postgres",
                password=admin_password,
                dbname="postgres",
                sslmode="disable",
                connect_timeout=2,
                autocommit=True,
            )
        except psycopg.OperationalError:
            if time.monotonic() >= deadline:
                raise RuntimeError("Isolated PostgreSQL never became ready") from None
            time.sleep(0.1)
        else:
            break
    try:
        with connection:
            for role, password in (
                (target.owner, owner_password),
                (WORKFLOW_APP_ROLE, app_password),
            ):
                connection.execute(
                    sql.SQL("CREATE ROLE {} LOGIN PASSWORD {}").format(
                        sql.Identifier(role), sql.Literal(password)
                    )
                )
            database = sql.Identifier(target.database)
            owner = sql.Identifier(target.owner)
            app = sql.Identifier(WORKFLOW_APP_ROLE)
            connection.execute(
                sql.SQL("CREATE DATABASE {} OWNER {}").format(database, owner)
            )
            connection.execute(
                sql.SQL("REVOKE ALL ON DATABASE {} FROM PUBLIC").format(database)
            )
            connection.execute(
                sql.SQL("GRANT CONNECT ON DATABASE {} TO {}").format(database, app)
            )
            connection.execute(
                sql.SQL("COMMENT ON DATABASE {} IS {}").format(
                    database, sql.Literal(target.database_comment)
                )
            )
            row = connection.execute("SHOW server_version").fetchone()
            assert row is not None
            return str(row[0])
    except psycopg.Error:
        raise RuntimeError("Isolated PostgreSQL bootstrap failed") from None


@contextmanager
def isolated_postgres() -> Iterator[IsolatedPostgres]:
    """Create an independent test database on a Docker-assigned loopback port."""
    instance_id = uuid4()
    target = MigrationTarget(
        f"jimu_wp03_{instance_id.hex}", WORKFLOW_OWNER_ROLE, instance_id
    )
    container_name = f"jimu-wp03-pg-{instance_id.hex}"
    admin_password, owner_password, app_password = (
        secrets.token_urlsafe(32) for _ in range(3)
    )
    with TemporaryDirectory(prefix="jimu-wp03-pg-") as directory:
        password_file = Path(directory) / "admin-password"
        password_file.write_text(admin_password, encoding="utf-8")
        password_file.chmod(0o600)
        container_id = None
        try:
            container_id = _docker(
                "run",
                "--pull=never",
                "--detach",
                "--name",
                container_name,
                "--label",
                f"jimu.wp03.instance={instance_id.hex}",
                "--publish",
                "127.0.0.1::5432",
                "--tmpfs",
                "/var/lib/postgresql/data:rw,noexec,nosuid,size=256m",
                "--mount",
                f"type=bind,source={password_file},target=/run/wp03-pg-password,readonly",
                "--env",
                "POSTGRES_PASSWORD_FILE=/run/wp03-pg-password",
                "--env",
                "POSTGRES_INITDB_ARGS=--auth-host=scram-sha-256",
                POSTGRES_IMAGE,
                "-c",
                "log_min_error_statement=panic",
            )
            port_output = _docker("port", container_id, "5432/tcp")
            match = re.fullmatch(r"127\.0\.0\.1:([0-9]+)", port_output)
            if match is None:
                raise RuntimeError("Isolated PostgreSQL must publish a loopback port")
            port = int(match[1])
            version = _bootstrap(
                port, admin_password, target, owner_password, app_password
            )
            base = URL.create(
                "postgresql+psycopg",
                host=_BOOTSTRAP_HOST,
                port=port,
                database=target.database,
                query={"hostaddr": _BOOTSTRAP_HOST, "sslmode": "disable"},
            )
            yield IsolatedPostgres(
                target,
                version,
                application_url=base.set(
                    username=WORKFLOW_APP_ROLE, password=app_password
                ),
                migration_url=base.set(username=target.owner, password=owner_password),
            )
        finally:
            if container_id is None:
                # A docker client timeout can occur after the daemon creates a
                # container. Only claim it if its unique creation label matches.
                owned = _docker(
                    "ps",
                    "--all",
                    "--quiet",
                    "--no-trunc",
                    "--filter",
                    f"name=^/{container_name}$",
                    "--filter",
                    f"label=jimu.wp03.instance={instance_id.hex}",
                )
                if re.fullmatch(r"[0-9a-f]{64}", owned):
                    container_id = owned
            if container_id is not None:
                _docker("rm", "--force", "--volumes", container_id)
