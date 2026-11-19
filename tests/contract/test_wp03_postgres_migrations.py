import asyncio
from importlib.resources import files
from pathlib import Path

from sqlalchemy import text

from jimu_runtime.workflow import migrations
from jimu_runtime.workflow.postgres import (
    _MIGRATION_LOCK_KEY,
    WORKFLOW_APP_ROLE,
    _apply_catalogue,
    _read_catalogue,
    apply_migrations,
    create_postgres_engine,
)
from tests.fixtures.wp03.postgres.support import (
    isolated_postgres,
    stage_fixture_migrations,
)


def test_packaged_migrations_are_idempotent_across_connections() -> None:
    with isolated_postgres() as database:

        async def run() -> None:
            owner = create_postgres_engine(database.migration_url)
            app = create_postgres_engine(database.application_url)
            try:
                assert database.server_version.startswith("16.")
                catalogue = _read_catalogue(files(migrations))
                assert await apply_migrations(owner, database.target) == tuple(
                    item.version for item in catalogue
                )
                await owner.dispose()
                assert await apply_migrations(owner, database.target) == ()
                async with owner.connect() as connection:
                    result = await connection.execute(
                        text("""
                            SELECT version, script_name, script_sha256
                            FROM jimu_workflow.schema_migrations ORDER BY version
                        """)
                    )
                    assert result.all() == [
                        (item.version, item.script_name, item.script_sha256)
                        for item in catalogue
                    ]
                async with app.connect() as connection:
                    result = await connection.execute(
                        text("""
                        SELECT current_user, current_database(), rol.rolsuper,
                            has_schema_privilege(current_user, 'jimu_workflow', 'CREATE'),
                            has_schema_privilege(current_user, 'jimu_workflow', 'USAGE'),
                            pg_has_role(current_user, :owner, 'MEMBER')
                        FROM pg_catalog.pg_roles AS rol
                        WHERE rol.rolname = current_user
                    """),
                        {"owner": database.target.owner},
                    )
                    assert result.one() == (
                        WORKFLOW_APP_ROLE,
                        database.target.database,
                        False,
                        False,
                        True,
                        False,
                    )
            finally:
                await app.dispose()
                await owner.dispose()

        asyncio.run(run())


def test_concurrent_first_migrations_apply_exactly_one_suffix(tmp_path: Path) -> None:
    stage_fixture_migrations(tmp_path)
    catalogue = _read_catalogue(tmp_path)
    with isolated_postgres() as database:

        async def run() -> None:
            first = create_postgres_engine(database.migration_url)
            second = create_postgres_engine(database.migration_url)
            try:
                async with asyncio.TaskGroup() as concurrent:
                    async with first.begin() as blocker:
                        await blocker.execute(
                            text("SELECT pg_advisory_xact_lock(:key)"),
                            {"key": _MIGRATION_LOCK_KEY},
                        )
                        left = concurrent.create_task(
                            _apply_catalogue(first, database.target, catalogue)
                        )
                        right = concurrent.create_task(
                            _apply_catalogue(second, database.target, catalogue)
                        )
                        async with asyncio.timeout(3):
                            while True:
                                waiting = await blocker.execute(
                                    text("""
                                        SELECT count(*) FROM pg_catalog.pg_locks
                                        WHERE locktype = 'advisory' AND NOT granted
                                    """)
                                )
                                if waiting.scalar_one() == 2:
                                    break
                                await asyncio.sleep(0.01)
                assert sorted((left.result(), right.result())) == [(), (1, 2)]
                async with first.connect() as connection:
                    result = await connection.execute(
                        text("""
                        SELECT version FROM jimu_workflow.schema_migrations ORDER BY version
                    """)
                    )
                    assert result.scalars().all() == [1, 2]
            finally:
                await first.dispose()
                await second.dispose()

        asyncio.run(run())
