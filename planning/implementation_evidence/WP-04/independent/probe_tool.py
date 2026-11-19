"""Verify independent N05 Tool child attempts over PG and real CPU dispatch."""

import asyncio
import json
import sys
import traceback
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory

from probe_model import accept_independent, digest, prior
from sqlalchemy import text

from jimu_runtime.bundle.compiler import SourceIdentity
from jimu_runtime.bundle.trust import TrustPin
from jimu_runtime.domain.definitions.tools import ToolDefinition, ToolPlanDefinition
from jimu_runtime.domain.enums import ArtifactKind, NodeRunStatus, RuntimeRecordKind
from jimu_runtime.domain.nodes.envelopes import SucceededNodeOutput
from jimu_runtime.domain.nodes.tool_execution import (
    InvokedToolStep,
    ObjectImageView,
    ToolNodeInput,
    ToolNodeOutput,
    ToolPlanRunSummary,
)
from jimu_runtime.domain.runtime.authority import ActorRef
from jimu_runtime.domain.runtime.executions import NodeId
from jimu_runtime.domain.runtime.facts import ObjectRef, ToolExecutionRef
from jimu_runtime.gateway.bridge_tools import ToolExecutionAdapter
from jimu_runtime.gateway.journal import DispatchJournal
from jimu_runtime.tools.executor import ToolExecutor
from jimu_runtime.tools.registry import (
    IsolatedCPUWorker,
    ToolImplementation,
    ToolWorkerRegistry,
)
from jimu_runtime.tools.step_attempts import (
    StepAttemptStore,
    load_parent_step_history,
)
from jimu_runtime.workers.resource_inventory import HostInventory, ResourceScope
from jimu_runtime.workers.resources import ResourceAdmission
from jimu_runtime.workflow import repository
from jimu_runtime.workflow.mock_context import snapshot_ref
from jimu_runtime.workflow.mock_support import artifact
from jimu_runtime.workflow.postgres import apply_migrations, create_postgres_engine
from jimu_runtime.workflow.worker import (
    ClaimedNode,
    RetryableNodeFailure,
    WorkerStep,
    WorkflowWorker,
)
from jimu_runtime.workflow.worker_capabilities import ToolOwnershipPin
from jimu_runtime.workflow.worker_nodes import NodeResult, invocation_envelope, invoke
from jimu_runtime.workflow.worker_records import ToolMessage
from jimu_runtime.workflow.worker_source import WorkerSource
from tests.fixtures.wp02.compiler.scenario import (
    DEPLOYMENT,
    LIMITS,
    approve_source_bytes,
)
from tests.fixtures.wp03.nodes.scenario import parking_source
from tests.fixtures.wp03.postgres.support import isolated_postgres

PROCESS = """
import json, sys, time
from pathlib import Path

log = Path(sys.argv[1])
value = json.load(sys.stdin)["image"]
prior = log.read_text().splitlines() if log.exists() else []
with log.open("a", encoding="utf-8") as handle:
    handle.write(value + "\\n")
if value == "step-b" and value not in prior:
    if sys.argv[2] == "physical-timeout":
        time.sleep(25)
    else:
        json.dump(
            {"status": "failed", "diagnostics": [], "evidence_refs": [], "evidence": [],
             "output_states": [], "error": {"error_class": "transport", "code": "probe_retry",
                                            "diagnostic_refs": []}},
            sys.stdout,
        )
        sys.exit(0)
json.dump(
    {"status": "succeeded", "diagnostics": [], "evidence_refs": [], "evidence": [],
     "output_states": [{"output_name": "outside", "fact_status": "known", "evidence_refs": []}],
     "payload": {"kind": "bool", "value": True}},
    sys.stdout,
)
"""


def two_steps():
    source = parking_source()
    rows = []
    tool_id = "independent-geometry-b"
    schema_id = "independent-outside-b"
    fact_key = "parking.outside_secondary"
    for item in source.artifacts:
        if item.kind is ArtifactKind.TOOL_PLAN:
            doc = json.loads(item.content)
            (first,) = doc["steps"]
            doc["steps"].append(
                {
                    **first,
                    "step_id": "second-geometry",
                    "fact_output_mappings": [
                        {"output_name": "outside", "target_fact_key": fact_key}
                    ],
                }
            )
            slots = tuple(
                replace(
                    slot,
                    location=("steps", 1, *slot.location[2:]),
                    target=(ArtifactKind.TOOL, tool_id, "1.0.0")
                    if slot.location == ("steps", 0, "tool_ref")
                    else slot.target,
                )
                for slot in item.reference_slots
                if slot.location[:2] == ("steps", 0)
            )
            item = replace(
                item,
                content=json.dumps(doc).encode(),
                reference_slots=(*item.reference_slots, *slots),
            )
        elif item.kind is ArtifactKind.FACT_SCHEMA and item.artifact_id == "outside":
            doc = json.loads(item.content)
            doc.update(schema_id=schema_id, semantic_key=fact_key)
            rows.append(
                replace(
                    item,
                    artifact_id=schema_id,
                    path=f"schemas/facts/definitions/{schema_id}.json",
                    origin=SourceIdentity(
                        artifact_id=f"fact_schema:{schema_id}", version="1.0.0"
                    ),
                    content=json.dumps(doc).encode(),
                )
            )
        elif item.kind is ArtifactKind.TOOL:
            doc = json.loads(item.content)
            doc["tool_id"] = tool_id
            doc["fact_outputs"][0]["fact_key"] = fact_key
            rows.append(
                replace(
                    item,
                    artifact_id=tool_id,
                    origin=SourceIdentity(
                        artifact_id=f"tool:{tool_id}", version="1.0.0"
                    ),
                    content=json.dumps(doc).encode(),
                    reference_slots=tuple(
                        replace(
                            slot, target=(ArtifactKind.FACT_SCHEMA, schema_id, "1.0.0")
                        )
                        if slot.location == ("fact_outputs", 0, "fact_schema_ref")
                        else slot
                        for slot in item.reference_slots
                    ),
                )
            )
        elif item.kind is ArtifactKind.POLICY and item.artifact_id == "evidence":
            doc = json.loads(item.content)
            doc["policy"]["original_media_required"] = False
            item = replace(item, content=json.dumps(doc).encode())
        elif item.kind is ArtifactKind.POLICY and item.artifact_id == "execution":
            doc = json.loads(item.content)
            doc["policy"].update(
                timeout_ms=900,
                input_byte_limit=4096,
                max_attempts=2,
                retryable_error_classes=["timeout", "transport"],
                retry_backoff_ms=[20],
            )
            item = replace(item, content=json.dumps(doc).encode())
        rows.append(item)
    return approve_source_bytes(replace(source, artifacts=tuple(rows)))


def tool_reference(item):
    return ToolExecutionRef(
        record_kind=RuntimeRecordKind.TOOL_EXECUTION, record_id=item.tool_execution_id
    )


async def run(app, root, failure_mode):
    script = root / "independent-cpu-tool.py"
    script.write_text(PROCESS)
    proof = TrustPin(
        identity="independent-tool-proof",
        version="1.0.0",
        sha256=digest(script.read_bytes()),
    )

    async def tool_ownership(_scope, resolved):
        return tuple(
            ToolOwnershipPin(
                tool_ref=pin.tool_ref,
                implementation_version=pin.implementation_version,
                implementation_proof_ref=proof,
            )
            for pin in resolved.pins.tool_pins
        )

    budget = prior.BUDGET.model_copy(
        update={
            "default": prior.BUDGET.default.model_copy(
                update={"retryable_error_classes": ("transport", "timeout")}
            ),
        }
    )
    accepted, preview, intake_host, manager, store = await accept_independent(
        app,
        root / "step-intake",
        "step-intake",
        source=two_steps(),
        tool_ownership=tool_ownership,
        budget=budget,
    )
    ctx = preview.context_input()
    host = prior.WorkerInputs(
        intake_host,
        WorkerSource(
            media=preview.scope.media,
            device=ctx.device_ref,
            camera=ctx.camera_ref,
            fixture_deployment_id=DEPLOYMENT,
        ),
    )
    scope, execution = accepted.task.idempotency_scope, accepted.execution
    admissions = root / "step-capacity.db"
    calls = root / "physical-calls.txt"
    inventory = HostInventory(
        "independent-tool", 1500, 1, (ResourceScope("tool", "cpu", 1),)
    )
    ResourceAdmission.provision(admissions, inventory)
    initial_children = {}
    histories = []
    errors = []
    step_store = StepAttemptStore(
        app,
        actor=ActorRef(
            actor_kind="service",
            actor_id="independent-tool",
            identity_registry_id="independent-registry",
        ),
    )

    async def real_steps(claimed):
        if claimed.node.node_id is not NodeId.N05:
            return await asyncio.to_thread(
                invoke,
                claimed.opened,
                claimed.previous,
                claimed.branch,
                claimed.node,
                claimed.attempt,
            )
        obj = claimed.previous.object_record(claimed.branch)
        profile = next(
            item.definition
            for item in claimed.opened.profiles
            if item.rules.profile_ref == obj.profile_ref
        )
        inputs = ToolNodeInput(
            schema_version="1.0.0",
            envelope=invocation_envelope(
                claimed.opened, claimed.branch, claimed.node, claimed.attempt
            ),
            object_record=obj,
            image_view=ObjectImageView(roi=obj.bbox, transforms=()),
            context_snapshot_ref=snapshot_ref(claimed.previous.snapshot()),
            tool_plan_ref=profile.tool_plan_ref,
            validated_observation_fact_refs=(),
        )
        plan = claimed.opened.scope.resolve(inputs.tool_plan_ref, ToolPlanDefinition)
        pinned = claimed.opened.resolved.pins.tool_pins
        entries = {}
        journal = DispatchJournal(app)
        for pin in pinned:
            tool = claimed.opened.scope.resolve(pin.tool_ref, ToolDefinition)
            requirement = tool.implementation_requirement
            key = (
                requirement.capability_id,
                requirement.contract_version,
                pin.implementation_version,
            )
            cpu = IsolatedCPUWorker(
                ResourceAdmission(admissions, inventory),
                (sys.executable, "-I", str(script), str(calls), failure_mode),
                "tool",
                "independent-tool",
                pin.implementation_version,
                proof,
                script,
            )
            entries[key] = ToolImplementation(
                *key,
                proof,
                tool.provider_kind,
                tool.input_schema_ref,
                tool.output_schema_ref,
                "cpu",
                cpu,
            )
        tool_adapter = ToolExecutionAdapter(
            ToolExecutor(ToolWorkerRegistry(tuple(entries.values()))),
            step_store,
            journal,
            intake_host.capabilities,
        )
        results = []
        for index, step in enumerate(plan.steps):
            value = "step-a" if index == 0 else "step-b"
            completion = await tool_adapter.execute_step(
                claimed,
                inputs,
                step.step_id,
                {"image": value},
                peers=tuple(item.result.execution for item in results),
            )
            assert completion.selection is not None, completion
            if index == 0:
                if claimed.attempt.attempt_number == 1:
                    initial_children[value] = completion
                else:
                    assert completion == initial_children[value]
            results.append(completion)
        async with app.connect() as connection:
            history = await load_parent_step_history(connection, scope, inputs)
        assert history is not None
        children = tuple(item.result.execution for item in results)
        facts = tuple(fact for item in results for fact in item.result.facts)
        selected = tuple(
            item.selection for item in results if item.selection is not None
        )
        evidence = tuple(item for result in results for item in result.result.evidence)
        assert len(children) == len(selected) == len(facts) == 2
        artifacts = (
            *(
                artifact(RuntimeRecordKind.TOOL_EXECUTION, item.tool_execution_id, item)
                for item in children
            ),
            *(artifact(RuntimeRecordKind.FACT, fact.fact_id, fact) for fact in facts),
            *(
                artifact(RuntimeRecordKind.EVIDENCE, item.evidence_id, item)
                for item in evidence
            ),
        )
        started = claimed.attempt.started_at
        assert started is not None
        finished = datetime.now(UTC)
        envelope = SucceededNodeOutput(
            schema_version="1.0.0",
            node_id=NodeId.N05,
            node_run_id=claimed.node.node_run_id,
            attempt_id=claimed.attempt.attempt_id,
            trace=claimed.node.trace,
            output_artifacts=tuple(item.pin for item in artifacts),
            started_at=started,
            finished_at=finished,
            duration_ms=(finished - started) // timedelta(milliseconds=1),
            status=NodeRunStatus.SUCCEEDED,
        )
        output = ToolNodeOutput(
            schema_version="1.0.0",
            envelope=envelope,
            summary=ToolPlanRunSummary(
                tool_plan_ref=inputs.tool_plan_ref,
                object_ref=ObjectRef(
                    record_kind=RuntimeRecordKind.OBJECT, record_id=obj.object_id
                ),
                status="succeeded",
                validation_issues=(),
                steps=tuple(
                    InvokedToolStep(
                        disposition="invoked",
                        step_id=step.step_id,
                        tool_execution_ref=tool_reference(item),
                    )
                    for step, item in zip(plan.steps, children, strict=True)
                ),
            ),
            tool_fact_refs=tuple(
                ref for item in children for ref in item.generated_fact_refs
            ),
            evidence_refs=tuple(ref for item in children for ref in item.evidence_refs),
        )
        output.check_steps(inputs, profile, plan, children, facts, history=history)
        try:
            output.check_steps(inputs, profile, plan, children, facts)
        except ValueError:
            pass
        else:
            raise AssertionError(
                "cross-attempt summary accepted without original child witnesses"
            )
        histories.append(history)
        prior.emit(
            "validated_before_parent_commit",
            failure_mode=failure_mode,
            physical_calls=calls.read_text().splitlines(),
            selected_child_attempts=[item.selected_attempt_id for item in selected],
            original_parent_attempts=[
                item.original_parent_attempt.attempt_id for item in history.entries
            ],
        )
        message = ToolMessage(
            schema_version="1.0.0",
            inputs=inputs,
            output=output,
            executions=children,
            facts=facts,
            authorities=selected,
            evidence=evidence,
        )
        return NodeResult(message, (*artifacts, *message.authority_artifacts))

    async def adapter(claimed: ClaimedNode) -> NodeResult:
        try:
            return await real_steps(claimed)
        except RetryableNodeFailure:
            raise
        except Exception as error:
            errors.append(error)
            traceback.print_exc()
            raise

    async def tick(owner: str):
        worker = WorkflowWorker(
            app,
            manager,
            store,
            host=host,
            bundle_limits=LIMITS,
            invoke_claimed_node=adapter,
        )
        return await worker.run_once(
            scope, execution.execution_id, owner=owner, lease_ms=12000
        )

    for _ in range(8):
        action = await tick("original-tool-worker")
        if errors:
            async with app.connect() as connection:
                evidence = (
                    await connection.execute(
                        text("""
                    SELECT a.step_id, z.status,
                        r.record->>'boundary' AS boundary,
                        r.record->'error'->>'error_class' AS error_class
                    FROM jimu_workflow.tool_step_attempts a
                    LEFT JOIN jimu_workflow.attempt_results z
                       ON (z.execution_id, z.attempt_id) = (a.execution_id, a.child_attempt_id)
                    LEFT JOIN jimu_workflow.dispatch_intents i
                       ON (i.execution_id, i.attempt_id) = (a.execution_id, a.child_attempt_id)
                    LEFT JOIN jimu_workflow.dispatch_results r
                       ON (r.execution_id, r.dispatch_id) = (i.execution_id, i.dispatch_id)
                    WHERE a.execution_id=:eid
                    ORDER BY a.step_id
                """),
                        {"eid": execution.execution_id},
                    )
                ).all()
            prior.emit(
                "tool_preflight_failure",
                parent_worker_step=action.value,
                member_result=[tuple(row) for row in evidence],
                physical_calls=calls.read_text().splitlines(),
            )
            raise errors[0]
        if action is WorkerStep.RETRY_SCHEDULED:
            break
    else:
        raise AssertionError(
            "the parent did not report a scheduled retry after B failed"
        )
    assert calls.read_text().splitlines() == ["step-a", "step-b"]
    await asyncio.sleep(0.08)
    assert await tick("replacement-tool-worker") is WorkerStep.APPLIED
    assert calls.read_text().splitlines() == ["step-a", "step-b", "step-b"]
    assert not errors and len(histories) == 1
    history = histories[0]
    original, later = sorted(
        history.entries, key=lambda item: item.original_parent_attempt.attempt_number
    )
    assert original.original_parent_attempt.status.value == "failed"
    assert (
        original.child_attempt.status.value
        == later.child_attempt.status.value
        == "succeeded"
    )
    assert (
        original.original_parent_attempt.attempt_id
        != later.original_parent_attempt.attempt_id
    )
    async with app.connect() as connection:
        parent = await repository.get_attempt(
            connection,
            scope,
            execution.execution_id,
            history.parent_node.node_run_id,
            later.original_parent_attempt.attempt_id,
        )
        rows = (
            await connection.execute(
                text("""
            SELECT step_id, attempt_status, child_attempt_id
            FROM jimu_workflow.tool_step_completions WHERE execution_id = :eid
            ORDER BY step_id, attempt_status
        """),
                {"eid": execution.execution_id},
            )
        ).all()
    assert parent is not None and parent.status.value == "succeeded"
    assert sum(row.attempt_status == "succeeded" for row in rows) == 2
    async with app.connect() as connection:
        dispatches = (
            await connection.execute(
                text("""
            SELECT a.step_id, z.status, i.dispatch_id, i.operation_id, r.record->>'boundary' AS boundary,
                   r.record->'error'->>'error_class' AS error_class
            FROM jimu_workflow.tool_step_attempts a
            JOIN jimu_workflow.attempt_results z
               ON (z.execution_id, z.attempt_id) = (a.execution_id, a.child_attempt_id)
            JOIN jimu_workflow.dispatch_intents i
               ON (i.execution_id, i.attempt_id) = (a.execution_id, a.child_attempt_id)
            JOIN jimu_workflow.dispatch_results r
               ON (r.execution_id, r.dispatch_id) = (i.execution_id, i.dispatch_id)
            WHERE a.execution_id=:eid ORDER BY a.step_id, z.status
        """),
                {"eid": execution.execution_id},
            )
        ).all()
    assert len({row.dispatch_id for row in dispatches}) == len(dispatches) == 3
    a_dispatches = [row for row in dispatches if row.step_id == "wheel_boundary"]
    b_dispatches = [row for row in dispatches if row.step_id == "second-geometry"]
    assert len(a_dispatches) == 1 and len(b_dispatches) == 2
    assert (
        b_dispatches[0].operation_id
        == b_dispatches[1].operation_id
        != a_dispatches[0].operation_id
    )
    rejected = next(row for row in dispatches if row.error_class is not None)
    assert rejected.step_id == "second-geometry"
    if failure_mode == "physical-timeout":
        assert rejected.error_class == "timeout" and rejected.status == "timed_out"
    else:
        assert rejected.error_class == "transport" and rejected.status == "failed"
        assert len(rows) == 3
    prior.emit(
        "n05_reuses_original_a_and_only_retries_b",
        physical_calls=calls.read_text().splitlines(),
        failure_mode=failure_mode,
        completion_statuses=[tuple(row) for row in rows],
        dispatches=[tuple(row) for row in dispatches],
        old_parent=original.original_parent_attempt.attempt_id,
        original_a=original.child_attempt.attempt_id,
        successful_b=later.child_attempt.attempt_id,
    )
    for _ in range(8):
        if await tick("post-tool-restarted-worker") is WorkerStep.FINALIZED:
            break
    else:
        raise AssertionError("N06 and final Coverage were not published")
    reader = WorkflowWorker(app, manager, store, host=host, bundle_limits=LIMITS)
    final = await reader.read_finalization(scope, execution.execution_id)
    async with app.connect() as connection:
        final_execution = await repository.get_execution(
            connection, scope, execution.execution_id
        )
        published = (
            await connection.execute(
                text("""
            SELECT n.node_id, n.status, count(a.node_run_id) AS authorities
            FROM jimu_workflow.node_runs n
            LEFT JOIN jimu_workflow.authority_outputs a
              USING (project_id, client_id, execution_id, node_run_id)
            WHERE n.execution_id=:eid AND n.node_id='N06'
            GROUP BY n.node_id, n.status
        """),
                {"eid": execution.execution_id},
            )
        ).all()
    assert (
        final_execution is not None
        and final_execution.execution.status.value == "completed"
    )
    assert final is not None and len(final.decisions) == 1
    assert final.coverage.coverage_status.value == "complete"
    assert [tuple(row) for row in published] == [("N06", "succeeded", 1)]
    assert calls.read_text().splitlines() == ["step-a", "step-b", "step-b"]
    prior.emit(
        "n06_finalized_with_child_step_authority",
        failure_mode=failure_mode,
        coverage=final.coverage.coverage_status.value,
        decision_id=final.decisions[0].decision_id,
        decision=final.decisions[0].result.value,
        execution_status=final_execution.execution.status.value,
    )


async def main() -> None:
    with (
        isolated_postgres() as database,
        TemporaryDirectory(prefix="wp04-tools-") as root,
    ):
        owner = create_postgres_engine(database.migration_url)
        app = create_postgres_engine(database.application_url)
        try:
            await apply_migrations(owner, database.target)
            await run(
                app,
                Path(root),
                sys.argv[1] if len(sys.argv) > 1 else "retryable-response",
            )
        finally:
            await app.dispose()
            await owner.dispose()


if __name__ == "__main__":
    asyncio.run(main())
