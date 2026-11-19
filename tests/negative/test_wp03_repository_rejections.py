"""Database ownership and fail-closed scoped relationships."""

from asyncio import run

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, IntegrityError

from jimu_runtime.domain.enums import TaskStatus
from jimu_runtime.workflow import repository as repo
from tests.fixtures.wp02.releases.support import submission
from tests.fixtures.wp03.repository.support import NOW, accept, database


def test_each_scoped_request_key_and_original_principal_are_independent() -> None:
    async def scenario() -> None:
        async with database() as engine:
            first = submission("first")
            second = submission("second")
            scope = first.idempotency_scope
            async with engine.begin() as connection:
                for request in (first, second):
                    await repo.reserve_intent(
                        connection,
                        scope,
                        request,
                        issuer="registry",
                        subject="user",
                    )
            crossed = first.model_copy(
                update={"idempotency_key": second.idempotency_key}
            )
            wrong_request = first.model_copy(update={"request_id": "new"})
            wrong_key = first.model_copy(update={"idempotency_key": "new"})
            for request in (crossed, wrong_request, wrong_key):
                async with engine.begin() as connection:
                    with pytest.raises(
                        repo.IntentConflict, match="intake_idempotency_conflict"
                    ):
                        await repo.reserve_intent(
                            connection,
                            scope,
                            request,
                            issuer="registry",
                            subject="user",
                        )
            async with engine.begin() as connection:
                with pytest.raises(
                    repo.IntentConflict, match="intake_principal_mismatch"
                ):
                    await repo.reserve_intent(
                        connection,
                        scope,
                        first,
                        issuer="registry",
                        subject="other-user",
                    )
                other = scope.model_copy(update={"client_id": "other-client"})
                new_scope = first.model_copy(update={"idempotency_scope": other})
                reserved = await repo.reserve_intent(
                    connection,
                    other,
                    new_scope,
                    issuer="registry",
                    subject="other-user",
                )
                assert reserved.submission == new_scope
                assert await repo.list_pending_request_ids(
                    connection,
                    scope,
                    after_request_id=None,
                    limit=1,
                ) == ("first",)
                assert await repo.list_pending_request_ids(
                    connection,
                    scope,
                    after_request_id="first",
                    limit=2,
                ) == ("second",)
                remaining = await repo.get_intent(connection, scope, "second")
                assert remaining is not None
                await repo.fail_intent(
                    connection, scope, remaining, failure_code="fixture_failure"
                )
                assert (
                    await repo.list_pending_request_ids(
                        connection,
                        scope,
                        after_request_id="first",
                        limit=2,
                    )
                    == ()
                )
                with pytest.raises(
                    repo.RepositoryError, match="invalid_pending_intake_limit"
                ):
                    await repo.list_pending_request_ids(
                        connection,
                        scope,
                        after_request_id=None,
                        limit=1026,
                    )

    run(scenario())


def test_application_cannot_rewrite_history_or_cross_execution() -> None:
    async def scenario() -> None:
        async with database() as engine:
            first = submission("first")
            scope = first.idempotency_scope
            one, node_one, _ = await accept(engine, first)
            _, node_two, _ = await accept(engine, submission("second"))
            execution = one.execution
            async with engine.connect() as connection:
                assert (
                    await connection.execute(text("SELECT current_user"))
                ).scalar_one() == "jimu_workflow_app"

            forbidden = (
                """CREATE TABLE jimu_workflow.invalid (id integer)""",
                """INSERT INTO jimu_workflow.schema_migrations (version, script_name, script_sha256)
                    VALUES (9, 'mock', repeat('a', 64))""",
                """UPDATE jimu_workflow.execution_events SET record = record""",
                """DELETE FROM jimu_workflow.execution_events""",
                """TRUNCATE jimu_workflow.execution_events""",
                """UPDATE jimu_workflow.attempts SET record = record""",
                """UPDATE jimu_workflow.attempt_results SET record = record""",
                """UPDATE jimu_workflow.executions SET scheduling_limits = scheduling_limits""",
                """UPDATE jimu_workflow.node_runs SET record = record""",
            )
            for statement in forbidden:
                with pytest.raises(DBAPIError) as error:
                    async with engine.begin() as connection:
                        await connection.execute(text(statement))
                assert error.value.orig.sqlstate == "42501"

            with pytest.raises(IntegrityError) as error:
                async with engine.begin() as connection:
                    await repo.link_successor(
                        connection,
                        scope,
                        execution.execution_id,
                        node_one.node_run_id,
                        node_two.node_run_id,
                    )
            assert error.value.orig.sqlstate == "23503"

            async with engine.begin() as connection:
                with pytest.raises(ValueError, match="illegal Task transition"):
                    await repo.transition_task(
                        connection,
                        scope,
                        execution.execution_id,
                        execution.task_id,
                        TaskStatus.QUEUED,
                        TaskStatus.COMPLETED,
                        now=NOW,
                        trace=execution.trace,
                    )
                assert (
                    await repo.get_execution(connection, scope, execution.execution_id)
                    == one
                )
                assert (
                    len(
                        await repo.list_events(
                            connection, scope, execution.execution_id
                        )
                    )
                    == 3
                )

    run(scenario())
