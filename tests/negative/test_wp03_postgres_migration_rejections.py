import asyncio
from dataclasses import replace
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from jimu_runtime.workflow.postgres import (
    MigrationError,
    _apply_catalogue,
    _read_catalogue,
    apply_migrations,
    create_postgres_engine,
)
from tests.fixtures.wp03.postgres.support import (
    isolated_postgres,
    stage_fixture_migrations,
)


def test_unverified_database_or_current_role_cannot_migrate() -> None:
    with isolated_postgres() as database:

        async def run() -> None:
            owner = create_postgres_engine(database.migration_url)
            app = create_postgres_engine(database.application_url)
            try:
                for target in (
                    replace(database.target, instance_id=uuid4()),
                    replace(database.target, database="postgres"),
                ):
                    with pytest.raises(
                        MigrationError, match="unverified_migration_target"
                    ):
                        await apply_migrations(owner, target)
                with pytest.raises(MigrationError, match="unverified_migration_target"):
                    await apply_migrations(app, database.target)
                async with owner.connect() as connection:
                    result = await connection.execute(
                        text("SELECT to_regnamespace('jimu_workflow')")
                    )
                    assert result.scalar_one() is None
            finally:
                await app.dispose()
                await owner.dispose()

        asyncio.run(run())


def test_dml_and_ddl_privilege_boundary_for_application(tmp_path: Path) -> None:
    stage_fixture_migrations(tmp_path)
    with isolated_postgres() as database:

        async def run() -> None:
            owner = create_postgres_engine(database.migration_url)
            app = create_postgres_engine(database.application_url)
            try:
                assert await _apply_catalogue(
                    owner, database.target, _read_catalogue(tmp_path)
                ) == (1, 2)
                async with app.begin() as connection:
                    await connection.execute(
                        text("INSERT INTO jimu_workflow.fixture_history VALUES (1)")
                    )
                async with app.begin() as connection:
                    with pytest.raises(DBAPIError) as rejected:
                        await connection.execute(
                            text(
                                "INSERT INTO jimu_workflow.fixture_history VALUES (-1)"
                            )
                        )
                    assert rejected.value.orig.sqlstate == "P0001"
                for statement in (
                    "CREATE TABLE jimu_workflow.app_created (id integer)",
                    "CREATE SCHEMA app_created",
                    "CREATE TEMP TABLE app_created (id integer)",
                    "UPDATE jimu_workflow.schema_migrations SET script_name = 'changed'",
                    "INSERT INTO jimu_workflow.schema_migrations VALUES (3, 'fake', 'fake')",
                    "DELETE FROM jimu_workflow.schema_migrations",
                    "TRUNCATE jimu_workflow.schema_migrations",
                    "UPDATE jimu_workflow.fixture_history SET id = 2",
                    "DELETE FROM jimu_workflow.fixture_history",
                    "TRUNCATE jimu_workflow.fixture_history",
                ):
                    with pytest.raises(DBAPIError) as rejected:
                        async with app.begin() as connection:
                            await connection.execute(text(statement))
                    assert rejected.value.orig.sqlstate == "42501"
                async with app.connect() as connection:
                    result = await connection.execute(
                        text("SELECT id FROM jimu_workflow.fixture_history")
                    )
                    assert result.scalars().all() == [1]
            finally:
                await app.dispose()
                await owner.dispose()

        asyncio.run(run())


@pytest.mark.parametrize("fault", ["SELECT 1 / 0;", "COMMIT;"])
def test_failed_suffix_rolls_back_ddl_and_history_and_retries(
    tmp_path: Path, fault: str
) -> None:
    stage_fixture_migrations(tmp_path)
    initial = _read_catalogue(tmp_path)[:1]
    fault_path = tmp_path / "003_fault.sql"
    fault_path.write_text(
        "CREATE TABLE jimu_workflow.retry_object (id integer);\n" + fault,
        encoding="utf-8",
    )
    with isolated_postgres() as database:

        async def run() -> None:
            owner = create_postgres_engine(database.migration_url)
            try:
                assert await _apply_catalogue(owner, database.target, initial) == (1,)
                before = _read_catalogue(tmp_path)
                with pytest.raises(MigrationError) as failure:
                    await _apply_catalogue(owner, database.target, before)
                assert failure.value.code == "migration_database_failure"
                assert failure.value.version == 3
                await owner.dispose()
                async with owner.connect() as connection:
                    result = await connection.execute(
                        text("""
                        SELECT version FROM jimu_workflow.schema_migrations ORDER BY version
                    """)
                    )
                    assert result.scalars().all() == [1]
                    result = await connection.execute(
                        text("""
                        SELECT to_regclass('jimu_workflow.fixture_history'),
                            to_regclass('jimu_workflow.retry_object')
                    """)
                    )
                    assert result.one() == (None, None)
                fault_path.write_text(
                    "CREATE TABLE jimu_workflow.retry_object (id integer);",
                    encoding="utf-8",
                )
                fixed = _read_catalogue(tmp_path)
                assert await _apply_catalogue(owner, database.target, fixed) == (2, 3)
                assert await _apply_catalogue(owner, database.target, fixed) == ()
            finally:
                await owner.dispose()

        asyncio.run(run())


def test_history_rejects_mutation_downgrade_and_gaps(tmp_path: Path) -> None:
    stage_fixture_migrations(tmp_path)
    catalogue = _read_catalogue(tmp_path)
    with isolated_postgres() as database:

        async def run() -> None:
            owner = create_postgres_engine(database.migration_url)
            try:
                assert await _apply_catalogue(owner, database.target, catalogue) == (
                    1,
                    2,
                )
                with pytest.raises(MigrationError, match="unknown_applied_version"):
                    await _apply_catalogue(owner, database.target, catalogue[:1])
                changed = replace(catalogue[1], script_sha256="f" * 64)
                with pytest.raises(MigrationError, match="migration_digest_mismatch"):
                    await _apply_catalogue(
                        owner, database.target, (catalogue[0], changed)
                    )
                async with owner.begin() as connection:
                    await connection.execute(
                        text(
                            "DELETE FROM jimu_workflow.schema_migrations WHERE version = 1"
                        )
                    )
                with pytest.raises(MigrationError, match="migration_history_gap"):
                    await _apply_catalogue(owner, database.target, catalogue)
            finally:
                await owner.dispose()

        asyncio.run(run())
