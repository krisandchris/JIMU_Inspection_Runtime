"""Read real persisted receipts from the signed PostgreSQL workflow scenarios."""

from contextlib import asynccontextmanager
from pathlib import Path

import pytest

from jimu_runtime.domain.runtime.intake import IdempotencyScope
from jimu_runtime.gateway.journal import DispatchJournal
from jimu_runtime.observability import queries
from jimu_runtime.observability.contracts import EventCursor
from jimu_runtime.observability.postgres import (
    ObservabilityCursorStale,
    PostgresObservability,
)
from jimu_runtime.tools.step_attempts import StepAttemptStore
from tests.contract import test_wp05_perception_loopback as perception_cases
from tests.contract import test_wp05_worker_inspection as worker_cases
from tests.fixtures.wp04 import http_support

loopback_only = http_support.loopback_only


@pytest.mark.parametrize(
    "scenario", ("retry_and_tools", "partial_observation", "cancelled_late")
)
def test_scoped_audit_projects_actual_postgres_rows(
    tmp_path: Path,
    loopback_only: set[int],
    monkeypatch: pytest.MonkeyPatch,
    scenario: str,
) -> None:
    module = perception_cases if scenario == "partial_observation" else worker_cases
    fixture = module.perception_fixture
    finished = []

    active = []
    previous_call_pages = []
    previous_tool_pages = []
    if scenario == "cancelled_late":
        append_result = DispatchJournal.append_result

        async def observe_before_late(
            journal, claim, attempt_id, dispatch_id, result, **kwargs
        ):
            invocation = getattr(result, "invocation", None)
            if invocation is not None and invocation.node_id == "N04":
                runtime = active[-1]
                page = await PostgresObservability(runtime.application).calls(
                    claim.scope,
                    claim.execution_id,
                    limit=1,
                )
                assert page is not None and page.next_cursor is not None
                previous_call_pages.append(page)
            return await append_result(
                journal, claim, attempt_id, dispatch_id, result, **kwargs
            )

        monkeypatch.setattr(DispatchJournal, "append_result", observe_before_late)
    elif scenario == "retry_and_tools":
        save_completion = StepAttemptStore._save_completion

        async def observe_before_completion(
            store, connection, handle, bound, completion
        ):
            claim = handle.claim
            page = await PostgresObservability(active[-1].application).tool_steps(
                claim.scope,
                claim.execution_id,
                limit=1,
            )
            if page is not None and page.next_cursor is not None:
                previous_tool_pages.append(page)
            return await save_completion(store, connection, handle, bound, completion)

        monkeypatch.setattr(
            StepAttemptStore, "_save_completion", observe_before_completion
        )

    @asynccontextmanager
    async def monitored(*args, **kwargs):
        async with fixture(*args, **kwargs) as runtime:
            active.append(runtime)
            yield runtime
            task, execution = runtime.accepted.task, runtime.accepted.execution
            scope, eid = task.idempotency_scope, execution.execution_id
            recorder = PostgresObservability(runtime.application)
            foreign = IdempotencyScope(
                project_id=scope.project_id, client_id="other.client"
            )
            other_project = IdempotencyScope(
                project_id="other.project", client_id=scope.client_id
            )
            assert await recorder.events(other_project, eid) is None
            assert await recorder.events(foreign, eid) is None
            assert await recorder.calls(foreign, eid) is None
            assert await recorder.tool_steps(foreign, eid) is None

            page = await recorder.events(scope, eid, limit=2)
            assert page is not None and page.next_cursor is not None
            assert page.execution.task_id == task.task_id
            assert page.execution.trace_id == execution.trace.trace_id
            high = page.through
            events = list(page.items)
            while page.next_cursor is not None:
                page = await recorder.events(
                    scope, eid, cursor=page.next_cursor, limit=2
                )
                assert page is not None and page.through == high
                events.extend(page.items)
            assert [event.number for event in events] == list(range(1, high + 1))
            assert any(event.attempt_id and event.node_run_id for event in events)
            capped = await recorder.events(
                scope, eid, cursor=EventCursor(through=high - 1), limit=100
            )
            assert capped is not None and capped.items[-1].number == high - 1
            assert capped.through == high - 1 and capped.execution_state_through == high

            calls = await recorder.calls(scope, eid, limit=1)
            assert calls is not None and calls.next_cursor is not None
            spans = list(calls.items)
            while calls.next_cursor is not None:
                calls = await recorder.calls(
                    scope, eid, cursor=calls.next_cursor, limit=1
                )
                assert calls is not None
                spans.extend(calls.items)
            assert len({span.dispatch_id for span in spans}) == len(spans)
            assert [(row.recorded_at, row.dispatch_id) for row in spans] == sorted(
                (row.recorded_at, row.dispatch_id) for row in spans
            )
            one = await recorder.calls(scope, eid, attempt_id=spans[0].attempt_id)
            assert one is not None and all(
                row.attempt_id == spans[0].attempt_id for row in one.items
            )
            output = await recorder.metrics(scope)
            assert (
                await PostgresObservability(runtime.application).metrics(scope)
                == output
            )
            assert b"jimu_retained_tasks" in output
            assert b"jimu_retained_accepted_tasks 1\n" in output
            assert b"jimu_retained_queue_wait_seconds_count 1\n" in output
            assert b'jimu_retained_admitted_dispatches{kind="model"}' in output
            assert b"jimu_retained_reported_tokens" not in output
            assert b'jimu_telemetry_available{signal="gpu"} 0' in output
            assert b"jimu_retained_tasks" not in await recorder.metrics(foreign)
            assert b"jimu_retained_admitted_dispatches" not in await recorder.metrics(
                foreign
            )
            for secret in (
                scope.project_id,
                scope.client_id,
                task.task_id,
                eid,
                execution.trace.trace_id,
            ):
                assert secret.encode() not in output
            assert all(
                span.attempt_id in {event.attempt_id for event in events}
                for span in spans
            )
            assert (await recorder.readiness(scope)).ready

            async def worker_unavailable(verified_scope):
                assert verified_scope == scope
                return "unavailable"

            unhealthy = await PostgresObservability(
                runtime.application, worker_claim_probe=worker_unavailable
            ).readiness(scope)
            assert unhealthy.database == unhealthy.workflow_schema == "ready"
            assert unhealthy.worker_claim == "unavailable" and not unhealthy.ready
            if scenario == "retry_and_tools":
                assert previous_tool_pages
                with pytest.raises(ObservabilityCursorStale):
                    await recorder.tool_steps(
                        scope, eid, cursor=previous_tool_pages[-1].next_cursor
                    )
                assert any(
                    span.parent_node_run_id == span.operation_node_run_id
                    and span.node_run_id != span.operation_node_run_id
                    for span in spans
                )
                assert b'jimu_retained_node_retries{node="N04"} 1\n' in output
                assert (
                    b'jimu_retained_attempt_results{node="N04",status="failed"} 1\n'
                    in output
                )
                assert b"jimu_retained_tool_duration_seconds_count 3\n" in output
                assert b"jimu_retained_task_duration_seconds_count 1\n" in output
                assert any(
                    receipt.error_class == "transport"
                    and receipt.boundary == "sent_unknown"
                    for span in spans
                    if span.kind == "model"
                    for receipt in span.receipts
                ), [
                    (item.status, item.boundary, item.error_class)
                    for span in spans
                    for item in span.receipts
                ]
                tools = await recorder.tool_steps(scope, eid, limit=2)
                assert tools is not None and len(tools.items) == 2 and tools.next_cursor
                tail = await recorder.tool_steps(scope, eid, cursor=tools.next_cursor)
                assert tail is not None and len(tail.items) == 1
                assert (
                    len({row.tool_execution_id for row in (*tools.items, *tail.items)})
                    == 3
                )
                assert all(
                    row.parent_attempt_id
                    and row.child_attempt_id
                    and row.outcome == "succeeded"
                    for row in (*tools.items, *tail.items)
                )
            elif scenario == "cancelled_late":
                assert previous_call_pages
                with pytest.raises(ObservabilityCursorStale):
                    await recorder.calls(
                        scope, eid, cursor=previous_call_pages[-1].next_cursor
                    )
                (late_event,) = tuple(
                    event for event in events if event.kind == "late_response"
                )
                (late_call,) = tuple(
                    span
                    for span in spans
                    if span.attempt_id == late_event.attempt_id
                    and any(receipt.late for receipt in span.receipts)
                )
                assert late_event.dispatch_id == late_call.dispatch_id
                await _insert_unadmitted_sibling(
                    runtime, scope, eid, late_call.dispatch_id
                )
                sibling_page = await recorder.calls(
                    scope, eid, attempt_id=late_call.attempt_id
                )
                assert sibling_page is not None
                assert {span.dispatch_id for span in sibling_page.items} == {
                    "fixture.no-send.sibling",
                    late_call.dispatch_id,
                }
                reopened = await recorder.events(scope, eid, limit=100)
                assert reopened is not None
                assert (
                    next(
                        event
                        for event in reopened.items
                        if event.kind == "late_response"
                    ).dispatch_id
                    == late_call.dispatch_id
                )
                assert any(
                    receipt.late
                    and receipt.boundary == "acknowledged"
                    and receipt.fence_qualified is not True
                    for span in spans
                    for receipt in span.receipts
                )
                assert b'jimu_retained_late_receipts{kind="model"} 1\n' in output
                await _assert_measurements_select_each_reported_field(runtime)
            else:
                assert any(
                    receipt.invalid_slots or receipt.missing_slots
                    for span in spans
                    for receipt in span.receipts
                )
                assert b'jimu_retained_observation_slots{status="invalid"}' in output
                assert b'jimu_retained_observation_slots{status="missing"}' in output
                assert b"jimu_retained_task_duration_seconds_count" not in output
            finished.append(True)

    monkeypatch.setattr(module, "perception_fixture", monitored)
    if scenario == "retry_and_tools":
        worker_cases.test_configured_worker_executes_signed_parking_scene(
            tmp_path, loopback_only, "transport_failure", monkeypatch
        )
    elif scenario == "cancelled_late":
        worker_cases.test_observation_retains_late_or_corrupt_attempt_disposition(
            tmp_path, loopback_only, "cancellation", monkeypatch
        )
    else:
        perception_cases.test_signed_perception_through_worker(
            tmp_path, loopback_only, "multiple_objects", monkeypatch
        )
    assert finished == [True]


async def _insert_unadmitted_sibling(runtime, scope, eid, dispatch_id):
    # Extra intent with the same Attempt and timestamp can never establish an event link.
    async with runtime.application.begin() as connection:
        await connection.exec_driver_sql(
            """
            INSERT INTO jimu_workflow.dispatch_intents
              (project_id,client_id,execution_id,node_run_id,operation_node_run_id,operation_id,
               dispatch_id,attempt_id,job_id,fence,lease_owner,send_admitted,record,recorded_at)
            SELECT project_id,client_id,execution_id,node_run_id,operation_node_run_id,operation_id,
                   'fixture.no-send.sibling',attempt_id,job_id,fence,lease_owner,false,
                   jsonb_set(record::jsonb,'{dispatch_id}',to_jsonb('fixture.no-send.sibling'::text))::json,
                   recorded_at
            FROM jimu_workflow.dispatch_intents
            WHERE project_id=%s AND client_id=%s AND execution_id=%s AND dispatch_id=%s
        """,
            (scope.project_id, scope.client_id, eid, dispatch_id),
        )


async def _assert_measurements_select_each_reported_field(runtime):
    # Evaluate the production aggregation with deterministic explicit receipt
    # inputs for loss, measured zero, fallback, and execution-local identities.
    cases = """
      WITH receipts(execution_id,dispatch_id,late,boundary,started_at,completed_at,input_tokens,output_tokens) AS (
        VALUES
          ('one','shared',false,'sent_unknown','2026-01-01T00:00:00Z'::timestamptz,NULL::timestamptz,0::bigint,NULL::bigint),
          ('one','shared',true,'acknowledged','2026-01-01T00:00:00Z','2026-01-01T00:00:01Z',99,17),
          ('two','shared',false,'acknowledged','2026-01-01T00:00:00Z','2026-01-01T00:00:02Z',3,4),
          ('two','shared',true,'acknowledged','2026-01-01T00:00:00Z','2026-01-01T00:00:09Z',90,90),
          ('one','unmeasured',false,'not_dispatched',NULL,NULL,90,90)
      )
    """
    measures = """
      SELECT (SELECT sum(input_tokens) FROM input_reports),
             (SELECT count(*) FROM input_reports),
             (SELECT sum(output_tokens) FROM output_reports),
             (SELECT count(*) FROM output_reports),
             (SELECT sum(EXTRACT(epoch FROM completed_at - started_at)) FROM elapsed_results),
             (SELECT count(*) FROM elapsed_results)
    """
    async with runtime.application.connect() as connection:
        result = await connection.exec_driver_sql(
            cases + queries.MEASUREMENTS + measures
        )
        assert tuple(result.one()) == (3, 2, 21, 2, 3, 2)
