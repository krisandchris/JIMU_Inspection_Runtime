"""Exercise physical dispatch receipts against a disposable PostgreSQL instance."""

import asyncio
import json
import multiprocessing
from dataclasses import replace
from hashlib import sha256
from importlib.resources import files
from pathlib import Path
from urllib.parse import urlsplit

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from jimu_runtime.bundle.trust import TrustPin
from jimu_runtime.domain.enums import RuntimeRecordKind
from jimu_runtime.domain.runtime.executions import NodeId
from jimu_runtime.domain.runtime.facts import AttemptRef, NodeRunRef
from jimu_runtime.domain.runtime.invocations import (
    FailedInvocation,
    InvocationError,
    ModelInvocation,
    ReportedProviderIdentity,
    SucceededInvocation,
    UnavailableProviderIdentity,
)
from jimu_runtime.domain.runtime.tool_executions import ToolExecution
from jimu_runtime.domain.schema_types import ToolInputSchemaRef, ToolOutputSchemaRef
from jimu_runtime.gateway.journal import (
    DispatchConflict,
    DispatchIntent,
    DispatchJournal,
    DispatchLease,
    ModelDispatchResult,
    ModelDispatchTarget,
    ToolDispatchResult,
    ToolDispatchTarget,
    ToolImplementationObservation,
    UnselectedModelDispatchTarget,
    tool_receipt_digest,
)
from jimu_runtime.workflow import migrations, repository
from jimu_runtime.workflow.jobs import CompletionDisposition, DurableJobs
from jimu_runtime.workflow.postgres import (
    _apply_catalogue,
    _read_catalogue,
    apply_migrations,
    create_postgres_engine,
)
from tests.fixtures.wp03.jobs.support import (
    branch,
    database,
    fetch_node,
    inspect,
    outcome,
    seed,
    wait_for_db_time,
)
from tests.fixtures.wp03.postgres.support import isolated_postgres
from tests.fixtures.wp04 import http_support

loopback_only = http_support.loopback_only

_FIXTURE = Path(__file__).parents[1] / "fixtures/wp01c/invocations/contracts.json"
_ERROR = InvocationError(
    error_class="timeout", code="provider_timeout", diagnostic_refs=()
)
_BODY = b'{"result":"loopback"}'
_KEY = "joint-discovery"


def _invocation() -> ModelInvocation:
    payload = json.loads(_FIXTURE.read_bytes())["models"]["discovery"]
    return ModelInvocation.model_validate_json(json.dumps(payload))


def _intent(trace, dispatch_id, *, lease=True):
    invocation = _invocation()
    actual = invocation.actual_provider
    assert isinstance(actual, ReportedProviderIdentity)
    return DispatchIntent(
        dispatch_id=dispatch_id,
        operation_id=_KEY,
        trace_id=trace,
        request_digest=sha256(_BODY).hexdigest(),
        input_digest="1" * 64,
        response_contract_digest="2" * 64,
        resource_lease=(
            DispatchLease(
                lease_id=f"lease-{dispatch_id}",
                owner_id="host-worker",
                scope_id="quota-model",
            )
            if lease
            else None
        ),
        target=ModelDispatchTarget(
            node_id="N02",
            configured_provider=invocation.configured_provider,
            route_trace=invocation.route_trace,
            purposes=invocation.purposes,
            profile_refs=invocation.profile_refs,
            selected_provider_id=actual.provider_id,
            selected_provider_kind=actual.provider_kind,
            selected_deployment_id=actual.deployment_id,
            selected_deployment_version=actual.deployment_version,
            selected_model_id=actual.model_id,
            selected_model_version=actual.model_version,
            owner_ref=TrustPin(
                identity="fixture-owner", version="1.0.0", sha256="d" * 64
            ),
            endpoint_descriptor_ref=TrustPin(
                identity="loopback-model", version="1.0.0", sha256="a" * 64
            ),
        ),
    )


async def _ready(engine, *, node_id=NodeId.N02):
    async def transition(connection, scope, locked, run, attempt, result, now):
        if run.node_id is NodeId.N01:
            await branch(connection, scope, locked, run, now, node_id, "dispatch")

    scope, execution_id = await seed(engine)
    jobs = DurableJobs(engine, on_terminal=transition)
    first = await jobs.claim(scope, "context-worker", 15_000, execution_id=execution_id)
    assert first is not None
    attempt = await jobs.start(first)
    assert attempt is not None
    run = await fetch_node(engine, scope, execution_id, first.node_run_id)
    assert (
        await jobs.complete(first, outcome(attempt, run))
        is CompletionDisposition.APPLIED
    )
    return jobs, scope, execution_id


async def _start(
    engine, jobs, scope, execution_id, owner, *, duration=15_000, node_id=NodeId.N02
):
    claim = await jobs.claim(scope, owner, duration, execution_id=execution_id)
    assert claim is not None
    attempt = await jobs.start(claim)
    assert attempt is not None
    run = await fetch_node(engine, scope, execution_id, claim.node_run_id)
    assert run.node_id is node_id
    return claim, attempt, run


def _worker_at_intent(url, channel):
    async def run():
        engine = create_postgres_engine(url)
        try:
            async with engine.connect() as connection:
                await connection.execute(text("SELECT 1"))
            channel.send("ready")
            claim, attempt_id, intent = channel.recv()
            assert await DispatchJournal(engine).begin(claim, attempt_id, intent)
            channel.send("intent-committed")
            await asyncio.Event().wait()
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_crash_after_intent_before_send_keeps_the_boundary_unresolved():
    async def run():
        async with database() as (engine, url):
            jobs, scope, execution_id = await _ready(engine)
            # Time only the journal operation under the real short lease. A
            # cold spawned interpreter may take longer than that lease to import.
            context = multiprocessing.get_context("spawn")
            receiver, sender = context.Pipe()
            process = context.Process(target=_worker_at_intent, args=(url, sender))
            process.start()
            sender.close()
            try:
                assert await asyncio.to_thread(receiver.poll, 20)
                assert receiver.recv() == "ready"
                first, attempt, node = await _start(
                    engine, jobs, scope, execution_id, "first-worker", duration=2500
                )
                intent = _intent(node.trace.trace_id, "first-dispatch")
                receiver.send((first, attempt.attempt_id, intent))
                assert await asyncio.to_thread(receiver.poll, 8)
                assert receiver.recv() == "intent-committed"
                process.kill()
                await asyncio.to_thread(process.join, 8)
                assert process.exitcode is not None and process.exitcode < 0
            finally:
                if process.is_alive():
                    process.kill()
                    await asyncio.to_thread(process.join, 8)
                receiver.close()
                process.close()
            journal = DispatchJournal(engine)
            (saved,) = await journal.get_operation(
                scope, execution_id, node.node_run_id, _KEY
            )
            assert saved.intent == intent and saved.results == ()
            assert saved.attempt_id == attempt.attempt_id and saved.fence == first.fence
            assert not await journal.begin(first, attempt.attempt_id, intent)
            await wait_for_db_time(engine, first.lease_until)
            assert await jobs.reconcile(scope, execution_id)
            await asyncio.sleep(0.025)
            second, retry, node = await _start(
                engine, jobs, scope, execution_id, "retry-worker"
            )
            assert not await journal.begin(
                first, attempt.attempt_id, _intent(node.trace.trace_id, "stale")
            )
            assert await journal.begin(
                second, retry.attempt_id, _intent(node.trace.trace_id, "retry-dispatch")
            )
            assert retry.attempt_id != attempt.attempt_id
            assert second.fence != first.fence

    asyncio.run(run())


async def _wire_send(port, body):
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    try:
        writer.write(
            b"POST /mock HTTP/1.1\r\nHost: 127.0.0.1\r\n"
            + f"Content-Length: {len(body)}\r\n\r\n".encode()
            + body
        )
        await writer.drain()
        headers, reply = (await reader.read()).split(b"\r\n\r\n", 1)
        assert headers.startswith(b"HTTP/1.1 200 ")
        return reply
    finally:
        writer.close()
        await writer.wait_closed()


def _model_outcome(intent, attempt, node, reply, request_id):
    template = _invocation()
    actual = template.actual_provider
    assert isinstance(actual, ReportedProviderIdentity)
    actual = actual.model_copy(update={"version_evidence": request_id})
    digest = sha256(reply).hexdigest()
    invocation = template.model_copy(
        update={
            "model_invocation_id": intent.dispatch_id,
            "execution_id": node.execution_id,
            "node_run_ref": NodeRunRef(
                record_kind=RuntimeRecordKind.NODE_RUN, record_id=node.node_run_id
            ),
            "attempt_ref": AttemptRef(
                record_kind=RuntimeRecordKind.ATTEMPT, record_id=attempt.attempt_id
            ),
            "trace_id": intent.trace_id,
            "started_at": attempt.started_at,
            "completed_at": attempt.started_at,
            "request_digest": intent.request_digest,
            "actual_provider": actual,
            "provider_request_id": request_id,
            "outcome": SucceededInvocation(status="succeeded", response_digest=digest),
        }
    )
    return ModelDispatchResult(
        boundary="acknowledged",
        acknowledgement="http_headers",
        response_status=200,
        observed_response_bytes=len(reply),
        response_digest=digest,
        provider_request_id=request_id,
        actual_provider=actual,
        invocation=invocation,
    )


def test_loopback_two_physical_sends_cannot_overwrite_original_or_authoritative_attempt(
    loopback_only,
):
    async def run():
        async with database() as (engine, _):
            jobs, scope, execution_id = await _ready(engine)
            journal = DispatchJournal(engine)
            first, original, node = await _start(
                engine, jobs, scope, execution_id, "original-worker"
            )
            one = _intent(node.trace.trace_id, "dispatch-1")
            racing = await asyncio.gather(
                journal.begin(first, original.attempt_id, one),
                journal.begin(first, original.attempt_id, one),
            )
            assert sorted(racing) == [False, True]
            with pytest.raises(DispatchConflict, match="already has a send"):
                await journal.begin(
                    first,
                    original.attempt_id,
                    _intent(node.trace.trace_id, "duplicate-dispatch"),
                )
            first_received = asyncio.Event()
            let_first_reply = asyncio.Event()
            calls = []

            async def serve(reader, writer):
                try:
                    await reader.readuntil(b"\r\n\r\n")
                    calls.append(await reader.readexactly(len(_BODY)))
                    number = len(calls)
                    if number == 1:
                        first_received.set()
                        await let_first_reply.wait()
                    reply = f'{{"physical":{number}}}'.encode()
                    writer.write(
                        b"HTTP/1.1 200 OK\r\n"
                        + f"Content-Length: {len(reply)}\r\nConnection: close\r\n\r\n".encode()
                        + reply
                    )
                    await writer.drain()
                finally:
                    writer.close()
                    await writer.wait_closed()

            async with (
                http_support.http_peer(loopback_only, serve) as url,
                asyncio.timeout(10),
                asyncio.TaskGroup() as tasks,
            ):
                port = urlsplit(url).port
                assert port is not None
                first_read = tasks.create_task(_wire_send(port, _BODY))
                await first_received.wait()
                stored = await journal.get_operation(
                    scope, execution_id, node.node_run_id, _KEY
                )
                assert len(stored) == 1 and stored[0].results == ()
                prior = _invocation()
                endpoint = prior.actual_provider
                assert endpoint is not None
                actual = UnavailableProviderIdentity(
                    provider_id=endpoint.provider_id,
                    provider_kind=endpoint.provider_kind,
                    deployment_id=endpoint.deployment_id,
                    model_id=endpoint.model_id,
                    version_resolution="unavailable",
                    version_unavailable_reason="No complete response received",
                )
                failed = _model_outcome(one, original, node, b"", "unused").invocation
                assert failed is not None
                failed = ModelInvocation.model_validate_json(
                    failed.model_copy(
                        update={
                            "actual_provider": actual,
                            "provider_request_id": None,
                            "outcome": FailedInvocation(status="failed", error=_ERROR),
                        }
                    ).model_dump_json(exclude_none=True)
                )
                assert await journal.append_result(
                    first,
                    original.attempt_id,
                    one.dispatch_id,
                    ModelDispatchResult(
                        boundary="sent_unknown",
                        error=_ERROR,
                        actual_provider=actual,
                        invocation=failed,
                    ),
                )
                assert (
                    await jobs.complete(
                        first, outcome(original, node, result="timeout")
                    )
                    is CompletionDisposition.RETRY_SCHEDULED
                )
                await asyncio.sleep(0.025)
                second, retry, node = await _start(
                    engine, jobs, scope, execution_id, "retry-worker"
                )
                two = _intent(node.trace.trace_id, "dispatch-2")
                with pytest.raises(DispatchConflict, match="different immutable pins"):
                    await journal.begin(
                        second,
                        retry.attempt_id,
                        two.model_copy(update={"input_digest": "3" * 64}),
                    )
                assert await journal.begin(second, retry.attempt_id, two)
                new_reply = await _wire_send(port, _BODY)
                new_result = _model_outcome(two, retry, node, new_reply, "loopback-2")
                assert await journal.append_result(
                    second, retry.attempt_id, two.dispatch_id, new_result
                )
                assert (
                    await jobs.complete(second, outcome(retry, node))
                    is CompletionDisposition.APPLIED
                )
                let_first_reply.set()
                old_reply = await first_read
                old_result = _model_outcome(
                    one, original, node, old_reply, "loopback-1"
                )
                with pytest.raises(DispatchConflict, match="original dispatch Attempt"):
                    await journal.append_result(
                        second,
                        original.attempt_id,
                        one.dispatch_id,
                        old_result,
                        late=True,
                    )
                with pytest.raises(DispatchConflict, match="original dispatch Attempt"):
                    await journal.append_result(
                        replace(first, owner="forged"),
                        original.attempt_id,
                        one.dispatch_id,
                        old_result,
                        late=True,
                    )
                assert await journal.append_result(
                    first, original.attempt_id, one.dispatch_id, old_result
                )
                assert not await journal.append_result(
                    first, original.attempt_id, one.dispatch_id, old_result, late=True
                )
            saved = await journal.get_operation(
                scope, execution_id, node.node_run_id, _KEY
            )
            assert [call for call in calls] == [_BODY, _BODY]
            assert [
                (record.intent.dispatch_id, record.attempt_id) for record in saved
            ] == [
                (one.dispatch_id, original.attempt_id),
                (two.dispatch_id, retry.attempt_id),
            ]
            assert [item.late for item in saved[0].results] == [False, True]
            assert saved[0].results[0].result.invocation == failed
            assert saved[0].results[1].result == old_result
            assert saved[1].results[0].result == new_result
            usage = await journal.usage(
                scope,
                execution_id,
                node.node_run_id,
                provider_id="provider",
                deployment_id="remote-deployment",
            )
            assert (usage.node_sends, usage.deployment_sends) == (2, 2)
            assert old_result.response_digest != new_result.response_digest
            assert _BODY.decode() not in "".join(
                item.result.stable_json() for rec in saved for item in rec.results
            )
            snapshots = await inspect(engine, scope, execution_id)
            assert (node.node_run_id, retry.attempt_id) in snapshots["authority"]
            assert (node.node_run_id, original.attempt_id) not in snapshots["authority"]
            page = await journal.get_operation(
                scope, execution_id, node.node_run_id, _KEY, limit=1
            )
            assert page == (saved[0],)
            assert await journal.get_operation(
                scope,
                execution_id,
                node.node_run_id,
                _KEY,
                after_dispatch_id=page[0].intent.dispatch_id,
            ) == (saved[1],)

    asyncio.run(run())


def test_rejection_is_atomic_and_journal_rows_are_append_only():
    async def run():
        async with database() as (engine, _):
            jobs, scope, execution_id = await _ready(engine)
            claim, attempt, node = await _start(
                engine, jobs, scope, execution_id, "current-worker"
            )
            journal = DispatchJournal(engine)
            denied = _intent(node.trace.trace_id, "dispatch-rejected", lease=False)
            rejection = ModelDispatchResult(boundary="not_dispatched", error=_ERROR)
            with pytest.raises(ValueError, match="admitted resource lease"):
                await journal.begin(claim, attempt.attempt_id, denied)
            assert await journal.reject(claim, attempt.attempt_id, denied, rejection)
            assert (
                await journal.usage(scope, execution_id, node.node_run_id)
            ).node_sends == 0
            assert not await journal.reject(
                claim, attempt.attempt_id, denied, rejection
            )
            reply = _model_outcome(denied, attempt, node, b"observed", "request-id")
            with pytest.raises(DispatchConflict, match="known undispatched"):
                await journal.append_result(
                    claim, attempt.attempt_id, denied.dispatch_id, reply, late=True
                )
            for statement in (
                "UPDATE jimu_workflow.dispatch_results SET late = true",
                "DELETE FROM jimu_workflow.dispatch_intents",
                "TRUNCATE jimu_workflow.dispatch_operations",
            ):
                with pytest.raises(DBAPIError) as caught:
                    async with engine.begin() as connection:
                        await connection.execute(text(statement))
                assert caught.value.orig.sqlstate == "42501"

    asyncio.run(run())


def test_003_is_additive_after_the_original_migration_prefix():
    async def run(fixture):
        owner = create_postgres_engine(fixture.migration_url)
        try:
            catalogue = _read_catalogue(files(migrations))
            assert await _apply_catalogue(owner, fixture.target, catalogue[:2]) == (
                1,
                2,
            )
            async with owner.connect() as conn:
                before = (
                    await conn.execute(
                        text(
                            "SELECT * FROM jimu_workflow.schema_migrations ORDER BY version"
                        )
                    )
                ).all()
            assert await apply_migrations(owner, fixture.target) == tuple(
                entry.version for entry in catalogue[2:]
            )
            async with owner.connect() as conn:
                after = (
                    await conn.execute(
                        text(
                            "SELECT * FROM jimu_workflow.schema_migrations ORDER BY version"
                        )
                    )
                ).all()
                assert after[:2] == before
                assert after[2].script_name == "003_dispatch_journal.sql"
            assert await apply_migrations(owner, fixture.target) == ()
        finally:
            await owner.dispose()

    with isolated_postgres() as fixture:
        asyncio.run(run(fixture))


def test_concurrent_distinct_operations_recheck_node_and_deployment_send_budgets():
    async def run():
        async with database() as (engine, _):
            jobs, scope, execution_id = await _ready(engine)
            claim, attempt, node = await _start(
                engine, jobs, scope, execution_id, "worker"
            )
            journal = DispatchJournal(engine)
            assert await journal.is_current(claim, attempt.attempt_id)
            entered = asyncio.Event()
            commit_first = asyncio.Event()
            refusal = ModelDispatchResult(
                boundary="not_dispatched",
                error=InvocationError(
                    error_class="resource_exhausted",
                    code="node_budget_exhausted",
                    diagnostic_refs=(),
                ),
            )
            trace = node.trace.trace_id
            first = _intent(trace, "budget-1").model_copy(
                update={"operation_id": "op-1"}
            )
            second = _intent(trace, "budget-2").model_copy(
                update={"operation_id": "op-2"}
            )
            third = _intent(trace, "budget-3").model_copy(
                update={"operation_id": "op-3"}
            )

            async def usage(connection, intent):
                return await journal.usage(
                    scope,
                    execution_id,
                    node.node_run_id,
                    provider_id="provider",
                    deployment_id="remote-deployment",
                    operation_id=intent.operation_id,
                    connection=connection,
                )

            async def hold_first(connection):
                assert (await usage(connection, first)).node_sends == 0
                entered.set()
                await commit_first.wait()

            async def check_limit(connection):
                limits = await usage(connection, second)
                assert limits.operation_sends == 0
                assert limits.deployment_sends == limits.node_sends
                return refusal if limits.node_sends >= 1 else None

            async with asyncio.timeout(8), asyncio.TaskGroup() as tasks:
                pending_first = tasks.create_task(
                    journal.begin(
                        claim, attempt.attempt_id, first, budget_guard=hold_first
                    )
                )
                await entered.wait()
                pending_second = tasks.create_task(
                    DispatchJournal(engine).begin(
                        claim,
                        attempt.attempt_id,
                        second,
                        budget_guard=check_limit,
                    )
                )
                commit_first.set()
            assert pending_first.result()
            assert not pending_second.result()
            denied = await journal.get_dispatch(scope, execution_id, second.dispatch_id)
            assert denied is not None and not denied.send_admitted
            assert denied.results[0].result == refusal
            assert (await usage(None, first)).node_sends == 1
            assert await journal.append_result(
                claim, attempt.attempt_id, first.dispatch_id, refusal
            )
            assert (await usage(None, first)).node_sends == 0
            assert await journal.begin(
                claim, attempt.attempt_id, third, budget_guard=check_limit
            )
            assert await journal.append_result(
                claim,
                attempt.attempt_id,
                third.dispatch_id,
                ModelDispatchResult(boundary="sent_unknown", error=_ERROR),
            )
            assert (await usage(None, third)).node_sends == 1
            assert (
                await jobs.complete(claim, outcome(attempt, node))
                is CompletionDisposition.APPLIED
            )
            assert not await journal.is_current(claim, attempt.attempt_id)

    asyncio.run(run())


def test_unselected_model_is_a_rejection_without_inventing_a_provider_route():
    async def run():
        async with database() as (engine, _):
            jobs, scope, execution_id = await _ready(engine)
            claim, attempt, node = await _start(
                engine, jobs, scope, execution_id, "worker"
            )
            journal = DispatchJournal(engine)
            selected = _intent(node.trace.trace_id, "selected")
            target = selected.target
            assert isinstance(target, ModelDispatchTarget)
            pending = selected.model_copy(
                update={
                    "dispatch_id": "unselected",
                    "target": UnselectedModelDispatchTarget(
                        node_id=target.node_id,
                        configured_provider=target.configured_provider,
                        purposes=target.purposes,
                        profile_refs=target.profile_refs,
                    ),
                },
            )
            with pytest.raises(ValueError, match="requires a selected model route"):
                await journal.begin(claim, attempt.attempt_id, pending)
            assert await journal.reject(
                claim,
                attempt.attempt_id,
                pending,
                ModelDispatchResult(boundary="not_dispatched", error=_ERROR),
            )
            assert await journal.begin(claim, attempt.attempt_id, selected)
            saved = await journal.get_operation(
                scope, execution_id, node.node_run_id, _KEY
            )
            assert len(saved) == 2
            assert saved[0].send_admitted and not saved[1].send_admitted
            refused = saved[1].results[0].result
            assert isinstance(refused, ModelDispatchResult)
            assert refused.invocation is None and refused.actual_provider is None

    asyncio.run(run())


def test_cancelled_tool_late_receipt_retains_normalized_execution_and_original_owner():
    async def run():
        async with database() as (engine, _):
            scope_node = NodeId.N03
            jobs, scope, execution_id = await _ready(engine, node_id=scope_node)
            claim, attempt, node = await _start(
                engine,
                jobs,
                scope,
                execution_id,
                "tool-worker",
                node_id=scope_node,
            )
            journal = DispatchJournal(engine)
            data = json.loads(_FIXTURE.read_bytes())
            template = ToolExecution.model_validate_json(
                json.dumps(data["tools"]["grounding"])
            )
            input_schema = ToolInputSchemaRef.model_validate(
                data["tool"]["definition"]["input_schema_ref"]
            )
            output_schema = ToolOutputSchemaRef.model_validate(
                data["tool"]["definition"]["output_schema_ref"]
            )
            proof = TrustPin(identity="cpu-test", version="1.0.0", sha256="a" * 64)
            execution = template.model_copy(
                update={
                    "tool_execution_id": "cpu-dispatch",
                    "execution_id": execution_id,
                    "node_run_ref": NodeRunRef(
                        record_kind=RuntimeRecordKind.NODE_RUN,
                        record_id=node.node_run_id,
                    ),
                    "attempt_ref": AttemptRef(
                        record_kind=RuntimeRecordKind.ATTEMPT,
                        record_id=attempt.attempt_id,
                    ),
                    "trace_id": node.trace.trace_id,
                    "started_at": attempt.started_at,
                    "completed_at": attempt.started_at,
                }
            )
            intent = DispatchIntent(
                dispatch_id=execution.tool_execution_id,
                operation_id="run-cpu-tool",
                trace_id=node.trace.trace_id,
                request_digest=sha256(b"cpu-test").hexdigest(),
                input_digest="6" * 64,
                response_contract_digest=output_schema.digest,
                resource_lease=DispatchLease(
                    lease_id="cpu-lease", owner_id="tool-worker", scope_id="cpu"
                ),
                target=ToolDispatchTarget(
                    tool_ref=execution.tool_ref,
                    invocation_origin=execution.invocation_origin,
                    profile_ref=execution.profile_ref,
                    capability_id="cpu-inspection",
                    contract_version="1.0.0",
                    implementation_version="1.0.0",
                    implementation_proof_ref=proof,
                    input_schema_ref=input_schema,
                    output_schema_ref=output_schema,
                    input_refs=execution.input_refs,
                ),
            )
            assert await journal.begin(claim, attempt.attempt_id, intent)
            assert await jobs.request_cancel(scope, execution_id)
            assert await jobs.reconcile(scope, execution_id)
            response = await asyncio.to_thread(
                execution.attempt_result.result.model_dump_json
            )
            wire = response.encode()
            receipt = ToolDispatchResult(
                boundary="acknowledged",
                acknowledgement="tool_process",
                observed_response_bytes=len(wire),
                response_digest=sha256(wire).hexdigest(),
                implementation=ToolImplementationObservation(
                    implementation_version="1.0.0", proof_ref=proof
                ),
                execution=execution,
                tool_execution_digest=tool_receipt_digest(execution),
            )
            foreign = replace(
                claim, scope=scope.model_copy(update={"project_id": "outside"})
            )
            with pytest.raises(DispatchConflict, match="original dispatch intent"):
                await journal.append_result(
                    foreign, attempt.attempt_id, intent.dispatch_id, receipt
                )
            assert await journal.append_result(
                claim, attempt.attempt_id, intent.dispatch_id, receipt
            )
            saved = await journal.get_dispatch(scope, execution_id, intent.dispatch_id)
            assert saved is not None and saved.results[0].late
            assert saved.results[0].result == receipt
            assert (node.node_run_id, attempt.attempt_id) not in (
                await inspect(engine, scope, execution_id)
            )["authority"]

    asyncio.run(run())


def test_nested_tool_history_is_pinned_across_independent_node_runs():
    async def run():
        async with database() as (engine, _):
            tool_node_id = NodeId.N05
            jobs, scope, execution_id = await _ready(engine, node_id=tool_node_id)
            parent, parent_attempt, parent_node = await _start(
                engine,
                jobs,
                scope,
                execution_id,
                "parent-worker",
                node_id=tool_node_id,
            )
            async with engine.begin() as connection:
                locked = await repository.lock_execution(
                    connection, scope, execution_id
                )
                assert locked is not None
                now = await connection.scalar(text("SELECT clock_timestamp()"))
                for child in ("child-1", "child-2"):
                    await branch(
                        connection, scope, locked, parent_node, now, tool_node_id, child
                    )
            first, first_attempt, first_node = await _start(
                engine,
                jobs,
                scope,
                execution_id,
                "child-worker-1",
                node_id=tool_node_id,
            )
            second, second_attempt, second_node = await _start(
                engine,
                jobs,
                scope,
                execution_id,
                "child-worker-2",
                node_id=tool_node_id,
            )
            journal = DispatchJournal(engine)
            data = json.loads(_FIXTURE.read_bytes())["tools"]["step"]
            tool = ToolExecution.model_validate_json(json.dumps(data))
            proof = TrustPin(identity="cpu-test", version="1.0.0", sha256="a" * 64)

            def intent(dispatch_id, trace_id):
                return DispatchIntent(
                    dispatch_id=dispatch_id,
                    operation_id="parent-tool-step",
                    trace_id=trace_id,
                    request_digest="c" * 64,
                    input_digest="d" * 64,
                    response_contract_digest="e" * 64,
                    resource_lease=DispatchLease(
                        lease_id=f"lease-{dispatch_id}",
                        owner_id="cpu-worker",
                        scope_id="cpu",
                    ),
                    target=ToolDispatchTarget(
                        tool_ref=tool.tool_ref,
                        invocation_origin=tool.invocation_origin,
                        profile_ref=tool.profile_ref,
                        object_ref=tool.object_ref,
                        capability_id="cpu",
                        contract_version="1.0.0",
                        implementation_version="1.0.0",
                        implementation_proof_ref=proof,
                        parent_node_run_id=parent_node.node_run_id,
                        parent_attempt_id=parent_attempt.attempt_id,
                    ),
                )

            async def fixture_live_guard(connection):
                return True

            one = intent("child-dispatch-1", first_node.trace.trace_id)
            two = intent("child-dispatch-2", second_node.trace.trace_id)
            with pytest.raises(ValueError, match="live parent guard"):
                await journal.begin(first, first_attempt.attempt_id, one)
            assert await journal.begin(
                first, first_attempt.attempt_id, one, live_guard=fixture_live_guard
            )
            with pytest.raises(DispatchConflict, match="immutable pins"):
                await journal.begin(
                    second,
                    second_attempt.attempt_id,
                    two.model_copy(update={"input_digest": "f" * 64}),
                    live_guard=fixture_live_guard,
                )
            assert await journal.begin(
                second, second_attempt.attempt_id, two, live_guard=fixture_live_guard
            )
            results = await journal.get_tool_step_operation(
                scope,
                execution_id,
                parent_node.node_run_id,
                one.operation_id,
            )
            assert [(item.node_run_id, item.attempt_id) for item in results] == [
                (first_node.node_run_id, first_attempt.attempt_id),
                (second_node.node_run_id, second_attempt.attempt_id),
            ]
            assert (
                await jobs.complete(
                    parent,
                    outcome(parent_attempt, parent_node, result="timeout"),
                )
                is CompletionDisposition.RETRY_SCHEDULED
            )
            assert await journal.is_current(first, first_attempt.attempt_id)
            assert await journal.append_result(
                first,
                first_attempt.attempt_id,
                one.dispatch_id,
                ToolDispatchResult(
                    boundary="acknowledged", acknowledgement="tool_process"
                ),
                late=False,
            )
            retained = await journal.get_dispatch(scope, execution_id, one.dispatch_id)
            assert retained is not None and retained.results[0].late

    asyncio.run(run())
