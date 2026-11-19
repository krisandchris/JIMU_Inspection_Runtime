"""Verify a fenced PostgreSQL 16 database and run its native archive tools."""

import os
import re
import subprocess
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from tempfile import TemporaryFile
from typing import BinaryIO, TypedDict
from uuid import UUID, uuid4

import psycopg
from psycopg import sql

from jimu_runtime.workflow.postgres import (
    WORKFLOW_APP_ROLE,
    WORKFLOW_OWNER_ROLE,
    WORKFLOW_SCHEMA,
)
from ops.recovery.contracts import RecoveryError, RowWitness
from ops.recovery.filesystem import private_path, trusted_ancestors, trusted_executable
from ops.recovery.media_validation import verify_media as verify_media


class _ConnectionArgs(TypedDict):
    host: str
    hostaddr: str
    port: str
    dbname: str
    user: str
    passfile: str
    connect_timeout: str
    sslmode: str


@dataclass(frozen=True, slots=True)
class Endpoint:
    database: str
    port: int
    instance_id: UUID
    passfile: Path

    def __post_init__(self) -> None:
        if (
            not re.fullmatch(r"[a-z][a-z0-9_]{0,62}", self.database)
            or self.database
            in {
                "postgres",
                "template0",
                "template1",
            }
            or type(self.port) is not int
            or not 1 <= self.port <= 65535
        ):
            raise RecoveryError("invalid_isolated_database_endpoint")

    @property
    def comment(self) -> str:
        return f"jimu-workflow-instance:{self.instance_id}"

    def library_args(self) -> _ConnectionArgs:
        return {
            "host": "127.0.0.1",
            "hostaddr": "127.0.0.1",
            "port": str(self.port),
            "dbname": self.database,
            "user": WORKFLOW_OWNER_ROLE,
            "passfile": str(private_path(self.passfile)),
            "connect_timeout": "5",
            "sslmode": "disable",
        }

    def cli_args(self) -> list[str]:
        return [
            "--host",
            "127.0.0.1",
            "--port",
            str(self.port),
            "--username",
            WORKFLOW_OWNER_ROLE,
            "--dbname",
            self.database,
            "--no-password",
        ]


@dataclass(frozen=True, slots=True)
class NativeTools:
    directory: Path | None = None
    image: str | None = None

    def __post_init__(self) -> None:
        if (self.directory is None) == (self.image is None):
            raise RecoveryError("exactly_one_postgres_tool_source_required")
        if self.image is not None and not re.fullmatch(
            r"sha256:[a-f0-9]{64}", self.image
        ):
            raise RecoveryError("postgres_image_digest_required")

    def _docker(self) -> list[str]:
        return [
            str(trusted_executable(Path("/usr/bin/docker"))),
            "--host",
            "unix:///var/run/docker.sock",
        ]

    def _prove_docker_network(self, docker: list[str]) -> None:
        boot = Path("/proc/sys/kernel/random/boot_id").read_text().strip()
        namespace = os.readlink("/proc/self/ns/net")
        result = subprocess.run(
            [
                *docker,
                "run",
                "--rm",
                "--pull=never",
                "--network=host",
                str(self.image),
                "sh",
                "-c",
                "cat /proc/sys/kernel/random/boot_id; readlink /proc/self/ns/net",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=15,
            check=False,
            env={"PATH": os.defpath, "LC_ALL": "C"},
        )
        if result.returncode or result.stdout.decode(
            "ascii", errors="replace"
        ).splitlines() != [boot, namespace]:
            raise RecoveryError("docker_postgres_network_identity_unverified")

    def run(
        self,
        executable: str,
        arguments: list[str],
        *,
        endpoint: Endpoint | None = None,
        source: BinaryIO | None = None,
        output: BinaryIO | None = None,
    ) -> bytes:
        if executable not in {"pg_dump", "pg_restore"}:
            raise RecoveryError("unsupported_postgres_command")
        env = {
            "PATH": os.defpath,
            "LC_ALL": "C",
            "PGSSLMODE": "disable",
            "PGCONNECT_TIMEOUT": "5",
            "PGAPPNAME": f"jimu-recovery-{uuid4().hex}",
        }
        docker: list[str] | None = None
        name = f"jimu-recovery-{uuid4().hex}"
        credentials: list[str] = []
        try:
            if endpoint is not None:
                password = private_path(endpoint.passfile)
                trusted_ancestors(password.parent)
                if self.directory is not None:
                    env["PGPASSFILE"] = str(password)
                else:
                    if any(character in str(password) for character in ",\r\n"):
                        raise RecoveryError("unsafe_pgpass_mount_path")
                    credentials = [
                        "--mount",
                        f"type=bind,source={password},target=/run/pgpass,readonly",
                        "--env",
                        "PGPASSFILE=/run/pgpass",
                    ]
            if self.directory is not None:
                command = [str(trusted_executable(self.directory / executable))]
            else:
                docker = self._docker()
                if endpoint is not None:
                    self._prove_docker_network(docker)
                command = [
                    *docker,
                    "run",
                    "--rm",
                    "--pull=never",
                    "--network=host" if endpoint else "--network=none",
                    "--name",
                    name,
                    "--label",
                    f"jimu.recovery.run={name}",
                    "--user",
                    f"{os.geteuid()}:{os.getegid()}",
                    "--env",
                    "PGCONNECT_TIMEOUT=5",
                    "--env",
                    "PGSSLMODE=disable",
                    "--env",
                    "PGAPPNAME",
                    *credentials,
                    *([] if source is None else ["-i"]),
                    str(self.image),
                    executable,
                ]
            with TemporaryFile() as stderr:
                result = subprocess.run(
                    [*command, *arguments],
                    stdin=source if source is not None else subprocess.DEVNULL,
                    stdout=output if output is not None else subprocess.PIPE,
                    stderr=stderr,
                    env=env,
                    timeout=300,
                    check=False,
                )
        except OSError:
            raise RecoveryError(f"{executable}_unavailable") from None
        except subprocess.TimeoutExpired:
            if docker is not None:
                self._terminate(docker, name)
            # Termination does not settle whether a sent COMMIT was accepted.
            code = (
                "target_restore_commit_unknown"
                if executable == "pg_restore" and endpoint
                else f"{executable}_timeout"
            )
            raise RecoveryError(code) from None
        if result.returncode:
            if executable == "pg_restore" and endpoint is not None:
                raise RecoveryError("target_restore_commit_unknown")
            raise RecoveryError(f"{executable}_failed_exit_{result.returncode}")
        return result.stdout or b""

    @staticmethod
    def _terminate(docker: list[str], name: str) -> None:
        try:
            result = subprocess.run(
                [
                    *docker,
                    "ps",
                    "-aq",
                    "--no-trunc",
                    "--filter",
                    f"name=^/{name}$",
                    "--filter",
                    f"label=jimu.recovery.run={name}",
                ],
                capture_output=True,
                timeout=10,
                check=False,
            )
            owned = result.stdout.decode("ascii", errors="replace").strip()
            if result.returncode == 0 and re.fullmatch(r"[a-f0-9]{64}", owned):
                subprocess.run(
                    [*docker, "rm", "-f", owned],
                    capture_output=True,
                    timeout=10,
                    check=False,
                )
        except (OSError, subprocess.TimeoutExpired):
            # The caller retains the unknown outcome and must not reopen CONNECT.
            pass

    def version(self, executable: str) -> str:
        raw = self.run(executable, ["--version"])
        text = raw.decode("ascii", errors="replace").strip()
        if not re.match(rf"^{executable} \(PostgreSQL\) 16(?:\.|\s|$)", text):
            raise RecoveryError("postgres_16_tools_required")
        return text


def quiesced(connection: psycopg.Connection, endpoint: Endpoint) -> str:
    connection.execute("SELECT pg_catalog.pg_stat_clear_snapshot()")
    row = connection.execute(
        """
        SELECT current_database(), current_user, session_user, pg_get_userbyid(d.datdba),
               shobj_description(d.oid, 'pg_database'), current_setting('server_version'),
               current_setting('server_version_num')::int,
               owner.rolcanlogin AND NOT (owner.rolsuper OR owner.rolcreatedb OR owner.rolcreaterole
                   OR owner.rolreplication OR owner.rolbypassrls),
               app.rolcanlogin AND NOT (app.rolsuper OR app.rolcreatedb OR app.rolcreaterole
                   OR app.rolreplication OR app.rolbypassrls)
                   AND NOT pg_has_role(app.oid, owner.oid, 'MEMBER')
                   AND NOT has_database_privilege(app.oid, d.oid, 'CREATE,TEMPORARY')
                   AND NOT has_schema_privilege(app.oid, 'public', 'CREATE')
                   AND NOT EXISTS (
                       SELECT 1 FROM pg_catalog.pg_auth_members m
                       WHERE m.member IN (app.oid, owner.oid)
                   )
        FROM pg_catalog.pg_database d
        JOIN pg_catalog.pg_roles owner ON owner.oid = d.datdba
        JOIN pg_catalog.pg_roles app ON app.rolname = %s
        WHERE d.datname = current_database()
        """,
        (WORKFLOW_APP_ROLE,),
    ).fetchone()
    if (
        row is None
        or row[:5]
        != (
            endpoint.database,
            WORKFLOW_OWNER_ROLE,
            WORKFLOW_OWNER_ROLE,
            WORKFLOW_OWNER_ROLE,
            endpoint.comment,
        )
        or not 160000 <= row[6] < 170000
        or row[7:] != (True, True)
    ):
        raise RecoveryError("unverified_pg16_database_owner_or_identity")
    connected_or_enabled = connection.execute(
        """
        SELECT EXISTS (
            SELECT 1 FROM pg_catalog.pg_stat_activity
            WHERE datname=current_database() AND pid <> pg_backend_pid()
        ) OR EXISTS (
            SELECT 1 FROM pg_catalog.pg_roles
            WHERE rolcanlogin AND NOT rolsuper AND rolname <> current_user
                AND has_database_privilege(oid, current_database(), 'CONNECT')
        ) OR EXISTS (
            SELECT 1 FROM pg_catalog.pg_prepared_xacts WHERE database=current_database()
        )
        """
    ).fetchone()
    if connected_or_enabled != (False,):
        raise RecoveryError("postgres_writers_not_quiesced")
    return str(row[5])


@contextmanager
def database(endpoint: Endpoint) -> Iterator[psycopg.Connection]:
    try:
        with psycopg.connect(**endpoint.library_args(), autocommit=True) as connection:
            connection.execute("SET search_path TO pg_catalog")
            connection.execute("SET statement_timeout TO '300s'")
            connection.execute("SET lock_timeout TO '5s'")
            connection.execute("SET TIME ZONE 'UTC'")
            connection.execute("SET DateStyle TO 'ISO, YMD'")
            connection.execute("SET bytea_output TO 'hex'")
            yield connection
    except psycopg.Error:
        raise RecoveryError("postgres_query_failed") from None


def isolated_schema(connection: psycopg.Connection, *, empty: bool) -> None:
    found = {
        row[0]
        for row in connection.execute("""
            SELECT nspname FROM pg_catalog.pg_namespace
            WHERE nspname <> 'public' AND nspname <> 'information_schema'
                AND nspname !~ '^pg_'
        """)
    }
    owned_extras = connection.execute("""
        SELECT EXISTS (
            SELECT 1
            FROM pg_catalog.pg_shdepend d
            CROSS JOIN LATERAL pg_catalog.pg_identify_object(d.classid, d.objid, d.objsubid) obj
            WHERE d.dbid = (SELECT oid FROM pg_catalog.pg_database WHERE datname=current_database())
              AND d.deptype = 'o'
              AND NOT (obj.type = 'schema' AND obj.identity IN ('public', 'jimu_workflow'))
              AND d.classid <> 'pg_default_acl'::regclass
              AND obj.schema IS DISTINCT FROM 'jimu_workflow'
        ) OR EXISTS (
            SELECT 1 FROM pg_catalog.pg_depend d
            WHERE d.refclassid = 'pg_namespace'::regclass
              AND d.refobjid = 'public'::regnamespace
        )
    """).fetchone()
    defaults = connection.execute("""
        SELECT a.defaclrole = (SELECT oid FROM pg_catalog.pg_roles WHERE rolname=current_user)
            AND a.defaclnamespace = 0 AND a.defaclobjtype = 'f'
            AND EXISTS (SELECT 1 FROM aclexplode(a.defaclacl) g
                        WHERE g.grantee = a.defaclrole AND g.privilege_type='EXECUTE')
            AND NOT EXISTS (SELECT 1 FROM aclexplode(a.defaclacl) g
                            WHERE g.grantee <> a.defaclrole OR g.grantor <> a.defaclrole
                               OR g.privilege_type <> 'EXECUTE')
        FROM pg_catalog.pg_default_acl a
    """).fetchall()
    if (
        found != (set() if empty else {WORKFLOW_SCHEMA})
        or owned_extras != (False,)
        or defaults != ([] if empty else [(True,)])
        or connection.execute("""
        SELECT EXISTS (
            SELECT 1 FROM pg_catalog.pg_class WHERE relnamespace='public'::regnamespace
        ) OR EXISTS (
            SELECT 1 FROM pg_catalog.pg_proc WHERE pronamespace='public'::regnamespace
        ) OR EXISTS (
            SELECT 1 FROM pg_catalog.pg_type WHERE typnamespace='public'::regnamespace
        ) OR EXISTS (
            SELECT 1 FROM pg_catalog.pg_extension WHERE extname <> 'plpgsql'
        ) OR EXISTS (SELECT 1 FROM pg_catalog.pg_largeobject_metadata)
    """).fetchone()
        != (False,)
    ):
        raise RecoveryError(
            "target_database_not_empty"
            if empty
            else "unsupported_source_database_objects"
        )


def tables(connection: psycopg.Connection) -> tuple[str, ...]:
    rows = connection.execute(
        """
        SELECT c.relname, pg_get_userbyid(c.relowner)
        FROM pg_catalog.pg_class c
        JOIN pg_catalog.pg_namespace n ON n.oid=c.relnamespace
        WHERE n.nspname=%s AND c.relkind IN ('r','p','m','f')
        ORDER BY c.relname COLLATE "C"
    """,
        (WORKFLOW_SCHEMA,),
    ).fetchall()
    if any(owner != WORKFLOW_OWNER_ROLE for _, owner in rows):
        raise RecoveryError("postgres_table_owner_mismatch")
    names = tuple(str(name) for name, _ in rows)
    if not {
        "schema_migrations",
        "tasks",
        "executions",
        "jobs",
        "execution_finalizations",
        "dispatch_intents",
        "original_media",
        "dispatch_media",
    } <= set(names):
        raise RecoveryError("expected_workflow_tables_missing")
    authorize_runtime(connection, names)
    return names


_UPDATES = {
    "intake_intents": {"status", "failure_code", "task_id", "execution_id"},
    "tasks": {"status"},
    "executions": {"state", "status", "next_event_number", "cancel_requested_at"},
    "node_runs": {"state", "status"},
    "jobs": {
        "status",
        "fence",
        "lease_owner",
        "lease_until",
        "ready_at",
        "attempt_count",
        "current_attempt_id",
        "attempt_deadline",
        "observation_wait_node_id",
    },
}


def authorize_runtime(connection: psycopg.Connection, names: tuple[str, ...]) -> None:
    scalar = connection.execute(
        """
        SELECT has_schema_privilege(%s,'jimu_workflow','USAGE')
           AND NOT has_schema_privilege(%s,'jimu_workflow','CREATE')
           AND pg_get_userbyid(n.nspowner) = current_user
           AND NOT EXISTS (
               SELECT 1 FROM pg_catalog.pg_auth_members m
               WHERE m.member IN (SELECT oid FROM pg_catalog.pg_roles WHERE rolname IN (%s,current_user))
           )
           AND NOT EXISTS (
               SELECT 1 FROM pg_catalog.pg_proc p
               WHERE p.pronamespace = n.oid AND (p.proowner <> n.nspowner OR p.prosecdef)
           )
           AND NOT EXISTS (
               SELECT 1 FROM pg_catalog.pg_shdepend d
               CROSS JOIN LATERAL pg_catalog.pg_identify_object(d.classid,d.objid,d.objsubid) obj
               WHERE d.dbid = (SELECT oid FROM pg_catalog.pg_database WHERE datname=current_database())
                 AND d.deptype='o' AND obj.schema='jimu_workflow' AND d.refobjid <> n.nspowner
           )
        FROM pg_catalog.pg_namespace n WHERE n.nspname='jimu_workflow'
    """,
        (WORKFLOW_APP_ROLE,) * 3,
    ).fetchone()
    if scalar != (True,):
        raise RecoveryError("postgres_runtime_permissions_or_owner_mismatch")
    roles = connection.execute(
        """
        SELECT c.relname,
               has_table_privilege(%s,c.oid,'SELECT'),
               has_table_privilege(%s,c.oid,'INSERT'),
               has_table_privilege(%s,c.oid,'UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER'),
               has_table_privilege(%s,c.oid,'SELECT WITH GRANT OPTION, INSERT WITH GRANT OPTION'),
               a.attname,
               has_column_privilege(%s,c.oid,a.attnum,'UPDATE'),
               has_column_privilege(%s,c.oid,a.attnum,'REFERENCES,UPDATE WITH GRANT OPTION,
                   SELECT WITH GRANT OPTION,INSERT WITH GRANT OPTION'),
               has_column_privilege(%s,c.oid,a.attnum,'SELECT,INSERT')
        FROM pg_catalog.pg_class c
        JOIN pg_catalog.pg_attribute a ON a.attrelid=c.oid AND a.attnum>0 AND NOT a.attisdropped
        WHERE c.relnamespace='jimu_workflow'::regnamespace AND c.relname = ANY(%s)
    """,
        (*((WORKFLOW_APP_ROLE,) * 7), list(names)),
    )
    for (
        name,
        select,
        insert,
        forbidden,
        grant,
        column,
        update,
        column_forbidden,
        column_access,
    ) in roles:
        allowed = name != "schema_migrations"
        if (
            (select, insert, column_access) != (allowed, allowed, allowed)
            or forbidden
            or grant
            or column_forbidden
            or update != (column in _UPDATES.get(name, set()))
        ):
            raise RecoveryError("postgres_runtime_permissions_or_owner_mismatch")


def bundle_pins(connection: psycopg.Connection) -> tuple[tuple[str, str], ...]:
    rows = connection.execute("""
        SELECT release_id, bundle_digest FROM jimu_workflow.tasks
        UNION SELECT release_id, bundle_digest FROM jimu_workflow.executions
    """)
    return tuple((str(release), str(digest)) for release, digest in rows)


def lock_tables(connection: psycopg.Connection, names: tuple[str, ...]) -> None:
    connection.execute(
        sql.SQL("LOCK TABLE {} IN SHARE MODE NOWAIT").format(
            sql.SQL(", ").join(sql.Identifier(WORKFLOW_SCHEMA, name) for name in names)
        )
    )


def snapshot(
    connection: psycopg.Connection, names: tuple[str, ...]
) -> dict[str, RowWitness]:
    result = {}
    for name in names:
        digest, count = sha256(), 0
        with connection.cursor(name="recovery_witness") as cursor:
            cursor.execute(
                sql.SQL("""
                    SELECT encode(sha256(convert_to(to_jsonb(t)::text, 'UTF8')), 'hex') COLLATE "C" AS row_digest
                    FROM {} t ORDER BY row_digest
                """).format(sql.Identifier(WORKFLOW_SCHEMA, name))
            )
            for (value,) in cursor:
                digest.update(bytes.fromhex(value))
                count += 1
        result[name] = RowWitness(rows=count, sha256=digest.hexdigest())
    return result
