"""Verify nested-model scope and fallback receipts on actual PG StepAttempts."""

import asyncio
import json
from importlib.resources import files
from pathlib import Path

import pytest
from sqlalchemy import text

from jimu_runtime.bundle.trust import TrustPin
from jimu_runtime.domain.definition_enums import ModelCapability
from jimu_runtime.domain.enums import NodeRunStatus, RuntimeRecordKind
from jimu_runtime.domain.runtime.executions import AttemptLink, NodeId
from jimu_runtime.domain.runtime.facts import AttemptRef, ModelInvocationRef, ObjectRef
from jimu_runtime.domain.runtime.invocations import (
    FailedInvocation,
    FallbackProviderRoute,
    InvocationError,
    ModelInvocation,
)
from jimu_runtime.domain.validation.transitions import NodeTransitionGuard
from jimu_runtime.gateway.journal import (
    DispatchConflict,
    DispatchIntent,
    DispatchJournal,
    DispatchLease,
    ModelDispatchResult,
    ModelDispatchTarget,
    NestedModelDispatchTarget,
    ToolDispatchResult,
)
from jimu_runtime.tools.step_attempts import StepAttempt, StepAttemptStore
from jimu_runtime.workflow import migrations, repository
from jimu_runtime.workflow.jobs import CompletionDisposition, DurableJobs
from jimu_runtime.workflow.postgres import (
    _apply_catalogue,
    _read_catalogue,
    apply_migrations,
    create_postgres_engine,
)
from tests.contract.test_wp04_dispatch_journal import (
    _intent as model_intent,
)
from tests.fixtures.wp01c.executions.support import attempt as legacy_attempt
from tests.fixtures.wp03.intake.support import migrated_application
from tests.fixtures.wp03.jobs.support import branch, seed
from tests.fixtures.wp03.postgres.support import isolated_postgres
from tests.unit.test_wp04_tool_step_attempts_postgres import (
    _ACTOR,
    _INPUT,
    _claim_parent,
    _node_output,
    _result,
    _setup,
)

_FIXTURE = Path(__file__).parents[1] / "fixtures/wp01c/invocations/contracts.json"
_OPERATION = "nested-model-step"
_ERROR = InvocationError(
    error_class="timeout", code="nested-provider-timeout", diagnostic_refs=()
)


def _template(name="discovery"):
    return ModelInvocation.model_validate_json(
        json.dumps(json.loads(_FIXTURE.read_bytes())["models"][name])
    )


def _intent(handle, step, dispatch_id, source_id=None):
    inv = _template()
    actual = inv.actual_provider
    obj = handle.inputs.object_record
    route = inv.route_trace
    if source_id is not None:
        prior = _template("fallback").route_trace
        assert isinstance(prior, FallbackProviderRoute)
        route = prior.model_copy(
            update={
                "trigger_invocation_ref": ModelInvocationRef(
                    record_kind=RuntimeRecordKind.MODEL_INVOCATION,
                    record_id=source_id,
                )
            }
        )
    pin = TrustPin(identity="fixture-provider", version="1.0.0", sha256="f" * 64)
    return DispatchIntent(
        dispatch_id=dispatch_id,
        operation_id=_OPERATION,
        trace_id=handle.node.trace.trace_id,
        request_digest="2" * 64,
        input_digest="3" * 64,
        response_contract_digest="4" * 64,
        resource_lease=DispatchLease(
            lease_id=dispatch_id, owner_id="fixture-worker", scope_id="fixture-remote"
        ),
        target=NestedModelDispatchTarget(
            node_id="N05",
            configured_provider=inv.configured_provider,
            purposes=(ModelCapability.VISION_AUXILIARY,),
            profile_refs=(obj.profile_ref,),
            object_ref=ObjectRef(
                record_kind=RuntimeRecordKind.OBJECT, record_id=obj.object_id
            ),
            route_trace=route,
            selected_provider_id=actual.provider_id,
            selected_provider_kind=actual.provider_kind
            if source_id is None
            else "local",
            selected_deployment_id=route.selected_deployment_id,
            selected_deployment_version=actual.deployment_version,
            selected_model_id=actual.model_id,
            selected_model_version=actual.model_version,
            endpoint_descriptor_ref=pin,
            owner_ref=pin,
            parent_node_run_id=handle.parent_claim.node_run_id,
            parent_attempt_id=handle.parent_attempt_id,
            tool_ref=step.tool_ref,
            tool_plan_ref=handle.inputs.tool_plan_ref,
            step_id=step.step_id,
            bound_input_digest=handle.node.input_digest,
        ),
    )


def _failure(handle, intent, *, acknowledged):
    inv = _template()
    target = intent.target
    invocation = inv.model_copy(
        update={
            "model_invocation_id": intent.dispatch_id,
            "execution_id": handle.node.execution_id,
            "node_id": "N05",
            "node_run_ref": handle.attempt.owner.node_run_ref,
            "attempt_ref": handle.node.attempt_history[0].attempt_ref,
            "trace_id": handle.node.trace.trace_id,
            "purposes": target.purposes,
            "profile_refs": target.profile_refs,
            "object_ref": target.object_ref,
            "request_digest": intent.request_digest,
            "outcome": FailedInvocation(status="failed", error=_ERROR),
            "started_at": handle.attempt.started_at,
            "completed_at": handle.attempt.started_at,
        }
    )
    if acknowledged:
        return ModelDispatchResult(
            boundary="acknowledged",
            acknowledgement="http_headers",
            response_status=408,
            error=_ERROR,
            provider_request_id=inv.provider_request_id,
            actual_provider=inv.actual_provider,
            invocation=invocation,
        )
    actual = _template("registry_verified").actual_provider
    invocation = invocation.model_copy(
        update={"actual_provider": actual, "provider_request_id": None}
    )
    invocation = ModelInvocation.model_validate_json(
        invocation.model_dump_json(exclude_none=True)
    )
    return ModelDispatchResult(
        boundary="sent_unknown",
        error=_ERROR,
        actual_provider=actual,
        invocation=invocation,
    )


@pytest.mark.parametrize(
    ("acknowledged", "on_time"), [(True, True), (False, True), (False, False)]
)
def test_nested_model_fallback_requires_an_original_qualified_receipt(
    tmp_path, acknowledged, on_time
):
    async def run():
        async with migrated_application() as engine:
            scope, eid, opened, obj, snapshot, profile, plan = await _setup(
                engine, tmp_path
            )
            (step,) = plan.steps
            jobs = DurableJobs(engine)
            store = StepAttemptStore(engine, actor=_ACTOR)
            journal = DispatchJournal(engine)
            first_parent, inputs, started = await _claim_parent(
                engine, jobs, scope, eid, opened, obj, snapshot, profile, "first-parent"
            )
            first = await store.begin(
                first_parent, inputs, step, _INPUT, timeout_ms=3000
            )
            assert isinstance(first, StepAttempt)
            intent = _intent(first, step, "original")
            if not acknowledged:
                evidence = _template(
                    "registry_verified"
                ).actual_provider.version_evidence
                intent = intent.model_copy(
                    update={
                        "target": intent.target.model_copy(
                            update={"version_evidence": evidence}
                        ),
                    }
                )

            async def first_fence(connection):
                return await store.dispatch_live(connection, first)

            ordinary = intent.model_copy(
                update={
                    "target": ModelDispatchTarget.model_validate(
                        intent.target.model_dump(
                            exclude={
                                "parent_node_run_id",
                                "parent_attempt_id",
                                "tool_ref",
                                "tool_plan_ref",
                                "step_id",
                                "bound_input_digest",
                                "kind",
                            }
                        )
                    )
                }
            )
            with pytest.raises(DispatchConflict, match="ordinary model"):
                await journal.begin(first.claim, first.attempt.attempt_id, ordinary)
            foreign = intent.model_copy(
                update={
                    "target": intent.target.model_copy(
                        update={"bound_input_digest": "0" * 64}
                    ),
                }
            )
            assert not await journal.begin(
                first.claim, first.attempt.attempt_id, foreign, live_guard=first_fence
            )
            assert await journal.begin(
                first.claim, first.attempt.attempt_id, intent, live_guard=first_fence
            )
            assert await journal.is_dispatch_current(
                first.claim, first.attempt.attempt_id, intent
            )
            with pytest.raises(DispatchConflict, match="target types"):
                await journal.append_result(
                    first.claim,
                    first.attempt.attempt_id,
                    intent.dispatch_id,
                    ToolDispatchResult(
                        boundary="acknowledged", acknowledgement="tool_process"
                    ),
                )

            receipt = _failure(first, intent, acknowledged=acknowledged)
            if on_time:
                assert await journal.append_result(
                    first.claim, first.attempt.attempt_id, intent.dispatch_id, receipt
                )
            completed = await store.complete(
                first,
                _result(opened, first, step, succeeded=False).model_copy(
                    update={"invocations": (receipt.invocation,)}
                ),
            )
            assert completed is not None
            if not on_time:
                assert await journal.append_result(
                    first.claim, first.attempt.attempt_id, intent.dispatch_id, receipt
                )
            first_record = await journal.get_dispatch(scope, eid, intent.dispatch_id)
            assert first_record is not None
            assert first_record.results[0].fence_qualified is on_time
            assert not first_record.results[0].late
            assert (
                await jobs.complete(
                    first_parent,
                    _node_output(
                        inputs, (completed,), started_at=started, failed=True
                    ).envelope,
                )
                is CompletionDisposition.RETRY_SCHEDULED
            )
            await asyncio.sleep(0.04)

            second_parent, inputs, _ = await _claim_parent(
                engine, jobs, scope, eid, opened, obj, snapshot, profile, "next-parent"
            )
            second = await store.begin(
                second_parent, inputs, step, _INPUT, timeout_ms=3000
            )
            assert isinstance(second, StepAttempt)
            assert second.node.node_run_id != first.node.node_run_id
            assert second.node.input_digest == first.node.input_digest
            next_intent = _intent(
                second, step, "fallback", source_id=intent.dispatch_id
            )

            async def next_fence(connection):
                return await store.dispatch_live(connection, second)

            assert not await journal.get_tool_step_operation(
                scope, eid, second_parent.node_run_id, _OPERATION
            )
            (prior,) = await journal.get_nested_model_operation(
                scope, eid, second_parent.node_run_id, _OPERATION
            )
            assert prior.attempt_id == first.attempt.attempt_id
            usage = await journal.usage(
                scope,
                eid,
                second.claim.node_run_id,
                parent_node_run_id=second_parent.node_run_id,
                tool_step_id=step.step_id,
                operation_id=_OPERATION,
            )
            assert (usage.node_sends, usage.operation_sends, usage.tool_step_sends) == (
                1,
                1,
                1,
            )
            incorrect_source = _intent(
                second, step, "different-source", source_id="no-such-receipt"
            )
            with pytest.raises(DispatchConflict, match="qualified source receipt"):
                await journal.begin(
                    second.claim,
                    second.attempt.attempt_id,
                    incorrect_source,
                    live_guard=next_fence,
                )
            if not on_time:
                with pytest.raises(DispatchConflict, match="qualified source receipt"):
                    await journal.begin(
                        second.claim,
                        second.attempt.attempt_id,
                        next_intent,
                        live_guard=next_fence,
                    )
                assert (
                    await journal.get_dispatch(scope, eid, next_intent.dispatch_id)
                    is None
                )
            else:
                assert await journal.begin(
                    second.claim,
                    second.attempt.attempt_id,
                    next_intent,
                    live_guard=next_fence,
                )
                duplicate = next_intent.model_copy(update={"dispatch_id": "duplicate"})
                with pytest.raises(DispatchConflict, match="already has a send"):
                    await journal.begin(
                        second.claim,
                        second.attempt.attempt_id,
                        duplicate,
                        live_guard=next_fence,
                    )

    asyncio.run(run())


def test_upgrade_preserves_pre_006_receipt_with_unknown_fence_qualification():
    async def run(fixture):
        owner = create_postgres_engine(fixture.migration_url)
        app = create_postgres_engine(fixture.application_url)
        try:
            catalogue = _read_catalogue(files(migrations))
            assert await _apply_catalogue(owner, fixture.target, catalogue[:5]) == (
                1,
                2,
                3,
                4,
                5,
            )
            scope, eid = await seed(app)
            result = ModelDispatchResult(boundary="sent_unknown", error=_ERROR)
            async with app.begin() as conn:
                now = await conn.scalar(text("SELECT clock_timestamp()"))
                execution = await repository.get_execution(conn, scope, eid)
                assert now is not None and execution is not None
                (root,) = await repository.list_nodes(conn, scope, eid)
                await branch(conn, scope, execution, root, now, NodeId.N02, "dispatch")
                node = await repository.get_node(conn, scope, eid, "node-dispatch")
                assert node is not None
                attempt = legacy_attempt(
                    attempt_id="legacy-attempt",
                    execution_id=eid,
                    owner={
                        "owner_kind": "node",
                        "node_run_ref": {
                            "record_kind": "node_run",
                            "record_id": node.node_run_id,
                        },
                    },
                    status="running",
                    created_at=now.isoformat(),
                    started_at=now.isoformat(),
                    ended_at=None,
                    reason=None,
                )
                intent = model_intent(node.trace.trace_id, "legacy-dispatch")
                params = {
                    "project": scope.project_id,
                    "client": scope.client_id,
                    "execution": eid,
                    "node": node.node_run_id,
                    "operation": intent.operation_id,
                    "dispatch": intent.dispatch_id,
                    "attempt": attempt.attempt_id,
                    "job": "job-dispatch",
                    "fence": 1,
                    "owner": "legacy-worker",
                    "attempt_record": attempt.stable_json(),
                    "started": now,
                    "input": intent.input_digest,
                    "contract": intent.response_contract_digest,
                    "target": "f" * 64,
                    "intent": intent.stable_json(),
                    "receipt": result.stable_json(),
                }
                # Freeze the pre-006 scheduler's write shape. New schedulers may
                # only run after the suffix migrations have been applied.
                await conn.execute(
                    text("""
                    INSERT INTO jimu_workflow.attempts
                        (project_id, client_id, execution_id, node_run_id, attempt_id,
                            attempt_number, job_id, fence, lease_owner, record)
                    VALUES (:project, :client, :execution, :node, :attempt,
                        1, :job, :fence, :owner, CAST(:attempt_record AS json))
                """),
                    params,
                )
                leased = await conn.scalar(
                    text("""
                    UPDATE jimu_workflow.jobs
                    SET status = 'leased', fence = :fence, lease_owner = :owner,
                        lease_until = :started + interval '15 seconds', attempt_count = 1,
                        current_attempt_id = :attempt,
                        attempt_deadline = :started + timeout_ms * interval '1 millisecond'
                    WHERE (project_id, client_id, execution_id, node_run_id, job_id)
                        = (:project, :client, :execution, :node, :job)
                    RETURNING job_id
                """),
                    params,
                )
                assert leased == params["job"]
                await repository.transition_node(
                    conn,
                    scope,
                    eid,
                    node,
                    node.model_copy(
                        update={
                            "status": NodeRunStatus.RUNNING,
                            "started_at": now,
                            "attempt_history": (
                                AttemptLink(
                                    attempt_ref=AttemptRef(
                                        record_kind=RuntimeRecordKind.ATTEMPT,
                                        record_id=attempt.attempt_id,
                                    ),
                                    attempt_number=attempt.attempt_number,
                                ),
                            ),
                        }
                    ),
                    now=now,
                    guard=NodeTransitionGuard.FIRST_ATTEMPT_STARTED,
                    appended_attempt=attempt,
                )
                await conn.execute(
                    text("""
                    INSERT INTO jimu_workflow.dispatch_operations
                        (project_id, client_id, execution_id, node_run_id, operation_id,
                            input_digest, response_contract_digest, target_digest)
                    VALUES (:project, :client, :execution, :node, :operation, :input, :contract, :target)
                """),
                    params,
                )
                await conn.execute(
                    text("""
                    INSERT INTO jimu_workflow.dispatch_intents
                        (project_id, client_id, execution_id, node_run_id, operation_node_run_id,
                            operation_id, dispatch_id, attempt_id, job_id, fence, lease_owner,
                            send_admitted, record)
                    VALUES (:project, :client, :execution, :node, :node, :operation, :dispatch,
                        :attempt, :job, :fence, :owner, true, CAST(:intent AS json))
                """),
                    params,
                )
                before = await conn.scalar(
                    text("""
                    INSERT INTO jimu_workflow.dispatch_results
                        (project_id, client_id, execution_id, dispatch_id, late, record)
                    VALUES (:project, :client, :execution, :dispatch, false, CAST(:receipt AS json))
                    RETURNING recorded_at
                """),
                    params,
                )
            assert await apply_migrations(owner, fixture.target) == tuple(
                m.version for m in catalogue[5:]
            )
            assert await apply_migrations(owner, fixture.target) == ()
            saved = await DispatchJournal(app).get_dispatch(
                scope, eid, intent.dispatch_id
            )
            assert saved is not None and saved.intent == intent
            (receipt,) = saved.results
            assert receipt.result == result and receipt.recorded_at == before
            assert receipt.fence_qualified is None
        finally:
            await app.dispose()
            await owner.dispose()

    with isolated_postgres() as fixture:
        asyncio.run(run(fixture))
