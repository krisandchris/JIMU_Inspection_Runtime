import asyncio

import pytest

from jimu_runtime.domain.runtime.intake import IdempotencyScope
from jimu_runtime.observability.postgres import (
    ObservabilityUnavailable,
    PostgresObservability,
)
from jimu_runtime.workflow.postgres import apply_migrations, create_postgres_engine
from tests.fixtures.wp03.postgres.support import isolated_postgres


def test_readiness_requires_actual_projected_column_privileges():
    async def run():
        with isolated_postgres() as isolated:
            owner = create_postgres_engine(isolated.migration_url)
            application = create_postgres_engine(isolated.application_url)
            try:
                await apply_migrations(owner, isolated.target)
                scope = IdempotencyScope(
                    project_id="fixture.project", client_id="fixture.client"
                )
                reader = PostgresObservability(application)
                assert (await reader.readiness(scope)).ready
                async with owner.begin() as connection:
                    await connection.exec_driver_sql(
                        "REVOKE SELECT ON jimu_workflow.dispatch_results FROM jimu_workflow_app"
                    )
                    await connection.exec_driver_sql(
                        "GRANT SELECT (project_id,client_id) ON jimu_workflow.dispatch_results TO jimu_workflow_app"
                    )
                status = await reader.readiness(scope)
                assert (
                    status.database == "ready"
                    and status.workflow_schema == "unavailable"
                )
                assert not status.ready
                with pytest.raises(ObservabilityUnavailable):
                    await reader.metrics(scope)
            finally:
                await application.dispose()
                await owner.dispose()

    asyncio.run(run())
