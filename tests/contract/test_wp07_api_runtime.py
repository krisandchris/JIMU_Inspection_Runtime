"""Exercise the local ASGI adapter over isolated PostgreSQL and the real Worker."""

import asyncio
import json
import secrets
from datetime import UTC, datetime
from pathlib import Path

import httpx
from sqlalchemy import text

from jimu_runtime.api.app import create_app
from jimu_runtime.api.auth import Action, Identity, Resource
from jimu_runtime.api.boundary import ApiLimits
from jimu_runtime.api.contracts import TracePage
from jimu_runtime.domain.runtime.authority import ActorRef
from jimu_runtime.domain.runtime.executions import ExecutionEvent, NodeId
from jimu_runtime.observability.contracts import TraceEvent
from jimu_runtime.workflow import repository
from jimu_runtime.workflow.intake import IntakeService
from jimu_runtime.workflow.worker import WorkerStep
from tests.fixtures.wp01c.runtime_authority.cases import documents
from tests.fixtures.wp03.intake.support import migrated_application
from tests.fixtures.wp03.worker.support import (
    LIMITS,
    SCHEDULING,
    accepted_worker,
    open_worker_fixture,
    worker_submission,
)

_ACTOR = ActorRef(
    actor_kind="service",
    actor_id="fixture.edge",
    identity_registry_id="fixture.registry",
)


_PRIVATE_OUTPUT = "internal-system-witness-only"


async def _append_private_system_event(engine, scope, execution_id):
    # Inject a typed historical SystemOutput witness into the isolated DB. The
    # synthetic Worker does not itself produce SystemAttempt output.
    _, source = documents("system")
    (record,) = source["execution_events"]
    arguments = {**scope.model_dump(), "execution_id": execution_id}
    async with engine.begin() as connection:
        owner = (
            await connection.execute(
                text("""
            SELECT a.node_run_id, a.attempt_id, e.next_event_number,
                   e.record -> 'trace' AS trace
            FROM jimu_workflow.executions e
            JOIN jimu_workflow.attempts a USING (project_id, client_id, execution_id)
            WHERE (e.project_id, e.client_id, e.execution_id)
                = (:project_id, :client_id, :execution_id)
            ORDER BY a.attempt_id LIMIT 1
        """),
                arguments,
            )
        ).one()
        payload = record["payload"]
        payload["attempt_ref"]["record_id"] = owner.attempt_id
        payload["output_name"] = _PRIVATE_OUTPUT
        payload["authority_output"]["execution_id"] = execution_id
        payload["authority_output"]["output"]["output_name"] = _PRIVATE_OUTPUT
        record.update(
            {
                "execution_id": execution_id,
                "execution_event_id": "fixture.private-witness",
                "trace": owner.trace,
                "event_number": owner.next_event_number,
                "recorded_at": datetime.now(UTC).isoformat(),
            }
        )
        event = ExecutionEvent.model_validate_json(json.dumps(record))
        await connection.execute(
            text("""
            INSERT INTO jimu_workflow.execution_events
                (project_id, client_id, execution_id, event_number, event_id, recorded_at,
                 node_run_id, attempt_id, record)
            VALUES (:project_id, :client_id, :execution_id, :event_number, :event_id,
                    :recorded_at, :node_run_id, :attempt_id, CAST(:body AS json))
        """),
            {
                **arguments,
                "event_number": event.event_number,
                "event_id": event.execution_event_id,
                "recorded_at": event.recorded_at,
                "node_run_id": owner.node_run_id,
                "attempt_id": owner.attempt_id,
                "body": event.stable_json(),
            },
        )
        await connection.execute(
            text("""
            UPDATE jimu_workflow.executions
            SET next_event_number = next_event_number + 1
            WHERE (project_id, client_id, execution_id)
                = (:project_id, :client_id, :execution_id)
        """),
            arguments,
        )
    return event


def test_real_intake_primary_task_cancellation_result_and_trace(tmp_path: Path) -> None:
    async def run() -> None:
        async with migrated_application() as engine:
            worker, original, host = await accepted_worker(engine, tmp_path)
            store, manager, _ = open_worker_fixture(tmp_path, host)
            intake = IntakeService(
                engine,
                manager,
                store,
                host=host,
                bundle_limits=LIMITS,
                scheduling_limits=SCHEDULING,
                max_submission_bytes=64 * 1024,
            )
            submitted = worker_submission("api-second-task")
            scope = submitted.idempotency_scope
            token = secrets.token_urlsafe(32)
            authorized: list[Resource] = []
            second_scope = scope.model_copy(
                update={"client_id": "allowed.other.client"}
            )
            grant = {
                Action.ACCESS_SCOPE,
                Action.TASK_SUBMIT,
                Action.TASK_READ,
                Action.TASK_CANCEL,
                Action.TASK_RESULT,
                Action.EXECUTION_READ,
                Action.EXECUTION_TRACE,
            }

            class Authority:
                async def authenticate(self, credential):
                    if credential == token:
                        return Identity(host.release_host.task_client, _ACTOR)
                    return None

                async def allows(self, identity, resource):
                    authorized.append(resource)
                    return (
                        identity.actor == _ACTOR
                        and resource.scope in (scope, second_scope)
                        and resource.action in grant
                    )

            app = create_app(engine, intake, authority=Authority())
            schema = app.openapi()
            task_input = schema["paths"]["/api/v1/inference/tasks"]["post"][
                "requestBody"
            ]
            assert task_input["content"]["application/json"]["schema"] == {
                "$ref": "#/components/schemas/JimuInput_TaskSubmission"
            }
            assert (
                schema["components"]["schemas"]["JimuInput_TaskSubmission"][
                    "additionalProperties"
                ]
                is False
            )
            assert (
                len(
                    app.openapi()["paths"]["/api/v1/inference/tasks"]["post"][
                        "parameters"
                    ]
                )
                == 2
            )
            assert (
                "/api/v1/executions/{execution_id}/artifact-replay"
                not in schema["paths"]
            )

            transport = httpx.ASGITransport(app=app)
            headers = {
                "Authorization": "Bearer " + token,
                "X-Jimu-Project": scope.project_id,
                "X-Jimu-Client": scope.client_id,
                "Content-Type": "application/json",
            }
            async with httpx.AsyncClient(
                transport=transport,
                base_url="http://127.0.0.1/api/v1/",
                headers=headers,
            ) as client:
                unauthorized = await client.get(
                    "inference/tasks/" + original.task.task_id,
                    headers={"Authorization": "Bearer " + secrets.token_urlsafe(32)},
                )
                assert unauthorized.status_code == 401
                assert unauthorized.headers["www-authenticate"] == "Bearer"
                assert not authorized

                endpoint = "inference/tasks"
                raw = submitted.stable_json()
                response, retry = await asyncio.gather(
                    client.post(endpoint, content=raw),
                    client.post(endpoint, content=raw),
                )
                assert response.status_code == retry.status_code == 202, response.text
                assert response.json() == retry.json()
                accepted = response.json()
                task_id = accepted["task"]["task_id"]
                execution_id = accepted["execution"]["execution_id"]
                assert "task_status" not in accepted
                assert "status" not in accepted["execution"]
                assert "cancel_requested_at" not in accepted

                resource = f"inference/tasks/{task_id}"
                for header in ("X-Jimu-Client", "X-Jimu-Project"):
                    blocked = await client.get(resource, headers={header: "another"})
                    assert blocked.status_code == 403
                assert (
                    await client.get(
                        resource, headers={"X-Jimu-Client": second_scope.client_id}
                    )
                ).status_code == 404
                live = (await client.get(resource)).json()
                assert live["task"] == accepted["task"]
                assert live["task_status"] == "queued"
                assert live["execution"]["status"] == "initialized"
                assert (await client.get(resource + "/result")).json()[
                    "code"
                ] == "result_pending"
                assert (await client.get(f"executions/{execution_id}")).json()[
                    "task_id"
                ] == task_id
                assert (
                    await client.get(f"executions/{execution_id}/trace?limit=101")
                ).status_code == 422

                trace_url = f"executions/{execution_id}/trace"
                snapshot_start = (
                    await client.get(trace_url, params={"limit": 1})
                ).json()
                initial_through = snapshot_start["through_event_number"]
                assert snapshot_start["next_after_event_number"] == 1
                assert initial_through > 1
                grant.remove(Action.TASK_RESULT)
                assert (await client.get(resource + "/result")).status_code == 404
                grant.add(Action.TASK_RESULT)
                for offset in range(20):
                    step = await worker.run_once(
                        scope,
                        execution_id,
                        owner=f"api-fixture-{offset}",
                        lease_ms=10000,
                    )
                    if step is WorkerStep.FINALIZED:
                        break
                assert step is WorkerStep.FINALIZED
                result = await client.get(resource + "/result")
                assert result.status_code == 200, result.text
                assert result.json()["execution_id"] == execution_id
                assert result.json()["decisions"]
                grant.remove(Action.TASK_READ)
                assert (await client.get(resource)).status_code == 404
                replayed_acceptance = await client.post(endpoint, content=raw)
                assert replayed_acceptance.status_code == 202
                assert replayed_acceptance.json() == accepted
                grant.add(Action.TASK_READ)
                assert (await client.post(resource + "/cancel")).status_code == 409

                witness = await _append_private_system_event(
                    engine, scope, execution_id
                )
                old_page = await client.get(
                    trace_url,
                    params={
                        "after_event_number": 1,
                        "through_event_number": initial_through,
                        "limit": 100,
                    },
                )
                assert old_page.status_code == 200, old_page.text
                assert old_page.json()["through_event_number"] == initial_through
                assert [row["number"] for row in old_page.json()["events"]] == list(
                    range(2, initial_through + 1)
                )
                assert old_page.json()["next_after_event_number"] is None

                public_events: list[TraceEvent] = []
                trace_id = accepted["task"]["trace"]["trace_id"]
                after, through = 0, None
                while True:
                    params = {"after_event_number": after, "limit": 2}
                    if through is not None:
                        params["through_event_number"] = through
                    page = await client.get(trace_url, params=params)
                    assert page.status_code == 200, page.text
                    assert _PRIVATE_OUTPUT not in page.text
                    response_page = TracePage.model_validate_json(page.content)
                    assert response_page.execution_id == execution_id
                    assert response_page.trace_id == trace_id
                    through = response_page.through_event_number
                    assert through == witness.event_number
                    public_events.extend(response_page.events)
                    cursor = response_page.next_after_event_number
                    if cursor is None:
                        break
                    assert cursor > after
                    after = cursor

                async with engine.connect() as connection:
                    recorded = await repository.list_events(
                        connection, scope, execution_id
                    )
                assert [event.number for event in public_events] == [
                    record.event_number for record in recorded
                ]
                assert public_events[-1].kind == "system_output"
                assert public_events[-1].attempt_id is not None
                assert public_events[-1].node_id is not None
                assert public_events[-1].status is None
                assert {
                    event.node_id
                    for event in public_events
                    if event.kind == "node_transition" and event.status == "succeeded"
                } == set(NodeId)
                for cursor in (
                    {"through_event_number": witness.event_number + 1},
                    {"after_event_number": 2, "through_event_number": 1},
                ):
                    invalid = await client.get(trace_url, params=cursor)
                    assert (
                        invalid.status_code == 422
                        and invalid.json()["code"] == "invalid_trace_cursor"
                    )
                grant.remove(Action.EXECUTION_TRACE)
                assert (await client.get(trace_url)).status_code == 404
                grant.add(Action.EXECUTION_TRACE)

                capped = len(
                    TracePage(
                        execution_id=execution_id,
                        trace_id=trace_id,
                        through_event_number=witness.event_number,
                        events=(public_events[0],),
                        next_after_event_number=1,
                    )
                    .model_dump_json()
                    .encode("utf-8")
                )
                tight = create_app(
                    engine,
                    intake,
                    authority=Authority(),
                    limits=ApiLimits(trace_bytes=max(512, capped)),
                )
                async with httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=tight),
                    base_url="http://127.0.0.1/api/v1/",
                    headers=headers,
                ) as limited:
                    page = await limited.get(
                        f"executions/{execution_id}/trace", params={"limit": 100}
                    )
                    assert page.status_code == 200, page.text
                    assert len(page.content) == capped
                    assert page.json()["next_after_event_number"] == 1
                    assert page.json()["through_event_number"] == witness.event_number

                original_uri = f"inference/tasks/{original.task.task_id}"
                assert (await client.post(original_uri + "/cancel")).json()[
                    "execution_id"
                ] == original.execution.execution_id
                assert (
                    await worker.run_once(
                        scope,
                        original.execution.execution_id,
                        owner="api-cancel",
                        lease_ms=10000,
                    )
                    is WorkerStep.CANCELLED
                )
                cancelled = (await client.get(original_uri)).json()
                assert cancelled["task_status"] == "cancelled"
                assert (await client.get(original_uri + "/result")).json()[
                    "code"
                ] == "result_unavailable"

    asyncio.run(run())
