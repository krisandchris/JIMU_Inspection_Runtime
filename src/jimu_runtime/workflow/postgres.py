"""Async PostgreSQL connections and transactional workflow migrations."""

from dataclasses import dataclass
from hashlib import sha256
from importlib.resources import files
from importlib.resources.abc import Traversable
from re import fullmatch
from uuid import UUID

from sqlalchemy import URL, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, create_async_engine

from jimu_runtime.workflow import migrations

WORKFLOW_SCHEMA = "jimu_workflow"
WORKFLOW_OWNER_ROLE = "jimu_workflow_owner"
WORKFLOW_APP_ROLE = "jimu_workflow_app"
# One transaction-scoped lock per database for the workflow migration history.
_MIGRATION_LOCK_KEY = 0x4A494D55574F524B
_SCRIPT_DELIMITER = "$jimu_migration$"


@dataclass(frozen=True, slots=True)
class MigrationTarget:
    """Identity supplied by the host that provisioned the migration database."""

    database: str
    owner: str
    instance_id: UUID

    @property
    def database_comment(self) -> str:
        return f"jimu-workflow-instance:{self.instance_id}"


class MigrationError(RuntimeError):
    """A normalized migration failure that avoids exposing connection details."""

    def __init__(self, code: str, version: int | None = None) -> None:
        self.code = code
        self.version = version
        super().__init__(code if version is None else f"{code}: {version:03d}")


@dataclass(frozen=True, slots=True)
class _Migration:
    version: int
    script_name: str
    script_sha256: str
    sql: str


def create_postgres_engine(url: URL) -> AsyncEngine:
    """Prepare a pooled PostgreSQL engine using a host-supplied psycopg URL."""
    if url.drivername != "postgresql+psycopg":
        raise ValueError("Expected the postgresql+psycopg dialect")
    return create_async_engine(
        url,
        pool_pre_ping=True,
        hide_parameters=True,
        connect_args={"connect_timeout": 5},
    )


def _read_catalogue(directory: Traversable) -> tuple[_Migration, ...]:
    result: list[_Migration] = []
    for path in sorted(directory.iterdir(), key=lambda item: item.name):
        if not path.name.endswith(".sql"):
            continue
        match = fullmatch(r"([0-9]{3})_[a-z][a-z0-9_]*\.sql", path.name)
        if match is None or not path.is_file():
            raise MigrationError("invalid_migration_name")
        version = int(match[1])
        if version != len(result) + 1:
            raise MigrationError("noncontiguous_migrations", version)
        data = path.read_bytes()
        try:
            sql = data.decode("utf-8")
        except UnicodeDecodeError:
            raise MigrationError("invalid_migration_encoding", version) from None
        if not sql.strip() or "\x00" in sql or _SCRIPT_DELIMITER in sql:
            raise MigrationError("invalid_migration_script", version)
        result.append(_Migration(version, path.name, sha256(data).hexdigest(), sql))
    if not result:
        raise MigrationError("migration_catalogue_empty")
    return tuple(result)


async def _assert_target(connection: AsyncConnection, target: MigrationTarget) -> None:
    result = await connection.execute(
        text("""
            SELECT pg_catalog.current_database() = :database
                AND current_user = :owner
                AND session_user = :owner
                AND role.rolname = :owner
                AND role.rolcanlogin
                AND NOT role.rolsuper
                AND NOT role.rolcreatedb
                AND NOT role.rolcreaterole
                AND NOT role.rolreplication
                AND NOT role.rolbypassrls
                AND app.rolcanlogin
                AND NOT app.rolsuper
                AND NOT app.rolcreatedb
                AND NOT app.rolcreaterole
                AND NOT app.rolreplication
                AND NOT app.rolbypassrls
                AND NOT pg_catalog.pg_has_role(app.oid, role.oid, 'MEMBER')
                AND pg_catalog.shobj_description(db.oid, 'pg_database') = :identity
            FROM pg_catalog.pg_database AS db
            JOIN pg_catalog.pg_roles AS role ON role.oid = db.datdba
            JOIN pg_catalog.pg_roles AS app ON app.rolname = :app
            WHERE db.datname = pg_catalog.current_database()
        """),
        {
            "database": target.database,
            "owner": target.owner,
            "app": WORKFLOW_APP_ROLE,
            "identity": target.database_comment,
        },
    )
    if result.scalar_one_or_none() is not True:
        raise MigrationError("unverified_migration_target")


async def _applied_count(
    connection: AsyncConnection, catalogue: tuple[_Migration, ...]
) -> int:
    result = await connection.execute(
        text("""
            SELECT pg_catalog.pg_get_userbyid(namespace.nspowner) = current_user,
                relation.relkind = 'r'
                    AND pg_catalog.pg_get_userbyid(relation.relowner) = current_user
            FROM pg_catalog.pg_namespace AS namespace
            LEFT JOIN pg_catalog.pg_class AS relation
                ON relation.relnamespace = namespace.oid
                AND relation.relname = 'schema_migrations'
            WHERE namespace.nspname = 'jimu_workflow'
        """)
    )
    ownership = result.one_or_none()
    if ownership is None:
        return 0
    schema_owned, history_owned = ownership
    if history_owned is None:
        raise MigrationError("migration_history_missing")
    if not schema_owned or not history_owned:
        raise MigrationError("invalid_migration_history_owner")
    history = await connection.execute(
        text("""
            SELECT version, script_name, script_sha256
            FROM jimu_workflow.schema_migrations
            ORDER BY version
            LIMIT :limit
        """),
        {"limit": len(catalogue) + 1},
    )
    rows = history.all()
    if not rows:
        raise MigrationError("migration_history_empty")
    for expected_version, row in enumerate(rows, 1):
        version, script_name, digest = row
        if version > len(catalogue):
            raise MigrationError("unknown_applied_version", version)
        if version != expected_version:
            raise MigrationError("migration_history_gap", version)
        expected = catalogue[version - 1]
        if (script_name, digest) != (expected.script_name, expected.script_sha256):
            raise MigrationError("migration_digest_mismatch", version)
    return len(rows)


async def _apply_catalogue(
    engine: AsyncEngine, target: MigrationTarget, catalogue: tuple[_Migration, ...]
) -> tuple[int, ...]:
    version = None
    try:
        async with engine.connect() as connection:
            await connection.execution_options(isolation_level="READ COMMITTED")
            async with connection.begin():
                await connection.exec_driver_sql("SET LOCAL search_path TO pg_catalog")
                await connection.exec_driver_sql("SET LOCAL lock_timeout TO '5s'")
                await connection.exec_driver_sql("SET LOCAL statement_timeout TO '15s'")
                await _assert_target(connection, target)
                await connection.execute(
                    text("SELECT pg_catalog.pg_advisory_xact_lock(:lock_key)"),
                    {"lock_key": _MIGRATION_LOCK_KEY},
                )
                applied = await _applied_count(connection, catalogue)
                for migration in catalogue[applied:]:
                    version = migration.version
                    # PostgreSQL forbids transaction control in this block because
                    # the caller already started the owning transaction.
                    await connection.exec_driver_sql(
                        f"DO {_SCRIPT_DELIMITER} BEGIN\n"
                        f"{migration.sql}\nEND; {_SCRIPT_DELIMITER}",
                        execution_options={"no_parameters": True},
                    )
                    await connection.execute(
                        text("""
                            INSERT INTO jimu_workflow.schema_migrations
                                (version, script_name, script_sha256)
                            VALUES (:version, :script_name, :script_sha256)
                        """),
                        {
                            "version": version,
                            "script_name": migration.script_name,
                            "script_sha256": migration.script_sha256,
                        },
                    )
                return tuple(migration.version for migration in catalogue[applied:])
    except DBAPIError as error:
        state = getattr(error.orig, "sqlstate", None)
        if state in {"55P03", "57014"}:
            raise MigrationError("migration_timeout", version) from None
        raise MigrationError("migration_database_failure", version) from None


async def apply_migrations(
    engine: AsyncEngine, target: MigrationTarget
) -> tuple[int, ...]:
    """Apply the packaged migration suffix atomically and return its version numbers."""
    return await _apply_catalogue(engine, target, _read_catalogue(files(migrations)))
