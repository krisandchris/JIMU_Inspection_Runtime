"""Provision only the explicitly named local deployment database and roles."""

import asyncio
import os
from pathlib import Path
from uuid import UUID

import psycopg
from psycopg import sql
from sqlalchemy import URL

from jimu_runtime.workflow.postgres import (
    WORKFLOW_APP_ROLE,
    WORKFLOW_OWNER_ROLE,
    MigrationTarget,
    apply_migrations,
    create_postgres_engine,
)


def read_secret(name: str) -> str:
    with Path("/run/secrets", name).open("r", encoding="ascii") as stream:
        value = stream.read(514).removesuffix("\n")
    if (
        len(value) not in range(32, 513)
        or not value.isascii()
        or any(not char.isalnum() and char not in "_-" for char in value)
    ):
        raise ValueError("deployment secret is absent or malformed")
    return value


def database_target() -> MigrationTarget:
    return MigrationTarget(
        "jimu_local", WORKFLOW_OWNER_ROLE, UUID(os.environ["JIMU_DB_INSTANCE"])
    )


def app_url(target: MigrationTarget, *, owner: bool = False) -> URL:
    return URL.create(
        "postgresql+psycopg",
        username=target.owner if owner else WORKFLOW_APP_ROLE,
        password=read_secret("owner_password" if owner else "app_password"),
        host="database",
        port=5432,
        database=target.database,
        query={"sslmode": "disable"},
    )


_SAFE_ROLE = """
    r.rolcanlogin AND NOT (r.rolsuper OR r.rolcreaterole OR r.rolcreatedb
          OR r.rolreplication OR r.rolbypassrls)
    AND NOT EXISTS (SELECT 1 FROM pg_catalog.pg_auth_members m WHERE m.member = r.oid)
"""


def _create_roles(
    conn: psycopg.Connection, target: MigrationTarget, pending: str
) -> None:
    with conn.transaction():
        for role, filename, marker in (
            (target.owner, "owner_password", pending),
            (WORKFLOW_APP_ROLE, "app_password", target.database_comment),
        ):
            secret = read_secret(filename)
            verifier = conn.pgconn.encrypt_password(
                secret.encode("ascii"), role.encode("ascii"), b"scram-sha-256"
            ).decode("ascii")
            conn.execute(
                sql.SQL("CREATE ROLE {} LOGIN PASSWORD {}").format(
                    sql.Identifier(role), sql.Literal(verifier)
                )
            )
            conn.execute(
                sql.SQL("COMMENT ON ROLE {} IS {}").format(
                    sql.Identifier(role), sql.Literal(marker)
                )
            )


def _finish_database(conn: psycopg.Connection, target: MigrationTarget) -> None:
    database = sql.Identifier(target.database)
    with conn.transaction():
        conn.execute(sql.SQL("REVOKE ALL ON DATABASE {} FROM PUBLIC").format(database))
        conn.execute(
            sql.SQL("GRANT CONNECT ON DATABASE {} TO {}").format(
                database, sql.Identifier(WORKFLOW_APP_ROLE)
            )
        )
        conn.execute(
            sql.SQL("COMMENT ON DATABASE {} IS {}").format(
                database, sql.Literal(target.database_comment)
            )
        )
        conn.execute(
            sql.SQL("COMMENT ON ROLE {} IS {}").format(
                sql.Identifier(target.owner), sql.Literal(target.database_comment)
            )
        )


def provision(target: MigrationTarget) -> None:
    """Resume only bootstrap operations with an exact, committed ownership witness."""
    pending = target.database_comment + ":bootstrapping"
    with psycopg.connect(
        host="database",
        user="postgres",
        dbname="postgres",
        password=read_secret("admin_password"),
        sslmode="disable",
        connect_timeout=5,
        autocommit=True,
    ) as conn:
        conn.execute("SELECT pg_catalog.pg_advisory_lock(57071212643524725)")
        current = conn.execute(
            """SELECT pg_catalog.shobj_description(oid, 'pg_database'),
                      pg_catalog.pg_get_userbyid(datdba)
               FROM pg_catalog.pg_database WHERE datname = %s""",
            (target.database,),
        ).fetchone()
        roles = {
            role: (marker, safe)
            for role, marker, safe in conn.execute(
                sql.SQL("""
                    SELECT r.rolname, pg_catalog.shobj_description(r.oid, 'pg_authid'), {safe}
                    FROM pg_catalog.pg_roles r WHERE r.rolname IN (%s, %s)
                """).format(safe=sql.SQL(_SAFE_ROLE)),
                (target.owner, WORKFLOW_APP_ROLE),
            )
        }
        if not roles and current is None:
            _create_roles(conn, target, pending)
        elif roles == {
            target.owner: (target.database_comment, True),
            WORKFLOW_APP_ROLE: (target.database_comment, True),
        } and current == (target.database_comment, target.owner):
            allowed = conn.execute(
                "SELECT pg_catalog.has_database_privilege(%s, %s, 'CONNECT'), "
                "pg_catalog.has_database_privilege(%s, %s, 'CREATE,TEMPORARY')",
                (
                    WORKFLOW_APP_ROLE,
                    target.database,
                    WORKFLOW_APP_ROLE,
                    target.database,
                ),
            ).fetchone()
            if allowed != (True, False):
                raise ValueError(
                    "existing database application privileges have drifted"
                )
            return
        elif roles != {
            target.owner: (pending, True),
            WORKFLOW_APP_ROLE: (target.database_comment, True),
        } or current not in (None, (None, target.owner)):
            raise ValueError(
                "database or roles do not match the deployment bootstrap pins"
            )

        # CREATE DATABASE must run outside a transaction. The pending owner marker
        # is committed first and atomically replaced with the database comment.
        if current is None:
            conn.execute(
                sql.SQL("CREATE DATABASE {} OWNER {}").format(
                    sql.Identifier(target.database), sql.Identifier(target.owner)
                )
            )
        _finish_database(conn, target)


async def migrate() -> None:
    target = database_target()
    await asyncio.to_thread(provision, target)
    engine = create_postgres_engine(app_url(target, owner=True))
    try:
        await apply_migrations(engine, target)
    finally:
        await engine.dispose()


def main() -> None:
    try:
        asyncio.run(migrate())
    except Exception:
        raise SystemExit("Local database bootstrap or migration failed") from None


if __name__ == "__main__":
    main()
