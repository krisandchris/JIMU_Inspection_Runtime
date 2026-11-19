"""Upgrade a populated WP-03 database using the unprivileged application reader."""

import asyncio
from importlib.resources import files

from sqlalchemy import text

from jimu_runtime.workflow import migrations, repository
from jimu_runtime.workflow.postgres import (
    _apply_catalogue,
    _read_catalogue,
    apply_migrations,
    create_postgres_engine,
)
from jimu_runtime.workflow.worker_capabilities import read_historical_capabilities
from tests.fixtures.wp03.intake.support import submission
from tests.fixtures.wp03.postgres.support import isolated_postgres
from tests.fixtures.wp03.repository.support import accept


def test_migrations_preserve_existing_execution_and_pending_business_job() -> None:
    catalogue = _read_catalogue(files(migrations))
    with isolated_postgres() as postgres:

        async def run() -> None:
            owner = create_postgres_engine(postgres.migration_url)
            app = create_postgres_engine(postgres.application_url)
            try:
                assert await _apply_catalogue(
                    owner, postgres.target, catalogue[:2]
                ) == (
                    1,
                    2,
                )
                request = submission("before-wp04-upgrade")
                original, root, job = await accept(app, request)
                scope, execution = request.idempotency_scope, original.execution
                await app.dispose()
                assert await apply_migrations(owner, postgres.target) == tuple(
                    item.version for item in catalogue[2:]
                )
                assert await apply_migrations(owner, postgres.target) == ()
                async with app.connect() as conn:
                    reopened = await repository.get_execution(
                        conn, scope, execution.execution_id
                    )
                    assert reopened == original
                    assert (
                        await repository.get_node(
                            conn, scope, execution.execution_id, root.node_run_id
                        )
                        == root
                    )
                    assert (
                        await read_historical_capabilities(conn, scope, execution)
                        is None
                    )
                    rows = await conn.execute(
                        text(
                            "SELECT job_id, internal_tool_step FROM jimu_workflow.jobs "
                            "WHERE project_id = :project AND client_id = :client "
                            "AND execution_id = :execution"
                        ),
                        {
                            "project": scope.project_id,
                            "client": scope.client_id,
                            "execution": execution.execution_id,
                        },
                    )
                    assert rows.all() == [(job.job_id, False)]
            finally:
                await app.dispose()
                await owner.dispose()

        asyncio.run(run())
