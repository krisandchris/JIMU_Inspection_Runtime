"""Exercise N05 child history against a fresh isolated PostgreSQL database."""

import asyncio
from copy import deepcopy
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from json import loads
from pathlib import Path
from typing import Literal

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from jimu_runtime.bundle.integrity import sha256_digest
from jimu_runtime.domain.definition_enums import EvidenceType, FactScope, ProducerKind
from jimu_runtime.domain.definitions.capabilities import ProfileDefinition
from jimu_runtime.domain.definitions.tools import (
    ToolDefinition,
    ToolPlanDefinition,
    ToolStep,
)
from jimu_runtime.domain.enums import (
    ArtifactKind,
    AttemptStatus,
    FactStatus,
    NodeRunStatus,
)
from jimu_runtime.domain.fact_schemas.canonicalization import canonical_bytes
from jimu_runtime.domain.nodes.envelopes import (
    FailedNodeOutput,
    NodeError,
    SucceededNodeOutput,
)
from jimu_runtime.domain.nodes.observation import ObjectImageView
from jimu_runtime.domain.nodes.tool_execution import (
    InvokedToolStep,
    ToolNodeInput,
    ToolNodeOutput,
    ToolPlanRunSummary,
)
from jimu_runtime.domain.producers import ToolStepProducer
from jimu_runtime.domain.runtime.authority import ActorRef
from jimu_runtime.domain.runtime.executions import NodeId
from jimu_runtime.domain.runtime.facts import (
    EvidenceRef,
    FactRef,
    KnownFact,
    ObjectRef,
    PolicyContentRef,
    ToolExecutionRef,
    ToolFactSource,
)
from jimu_runtime.domain.runtime.intake import ContextSnapshot, IdempotencyScope
from jimu_runtime.domain.runtime.perception import ObjectRecord
from jimu_runtime.domain.runtime.tool_executions import (
    FailedToolAttempt,
    FailedToolResult,
    KnownToolOutputState,
    ReturnedToolAttempt,
    SucceededToolResult,
    ToolArtifactRecordRef,
    ToolArtifactRef,
    ToolAuthorityObservation,
    ToolError,
    ToolExecution,
    ToolPlanStepOrigin,
)
from jimu_runtime.domain.validation.authority_inputs import EmittedOutput
from jimu_runtime.domain.values import BooleanValue
from jimu_runtime.tools.executor import _tool_input_refs
from jimu_runtime.tools.step_attempts import (
    StepAttempt,
    StepAttemptBudgetExhausted,
    StepAttemptStore,
    StepCompletion,
    StepResult,
    load_child_nodes,
    load_parent_step_history,
)
from jimu_runtime.workflow import repository as repo
from jimu_runtime.workflow.engine import BranchKey
from jimu_runtime.workflow.jobs import CompletionDisposition, DurableJobs, JobClaim
from jimu_runtime.workflow.mock_context import snapshot_ref
from jimu_runtime.workflow.mock_facts import _evidence, producer_catalogue
from jimu_runtime.workflow.mock_support import VERSION, K, artifact, synthetic_issue
from jimu_runtime.workflow.worker import WorkerStep
from jimu_runtime.workflow.worker_nodes import invocation_envelope
from jimu_runtime.workflow.worker_source import OpenExecution, open_execution
from jimu_runtime.workflow.worker_state import load_records
from tests.fixtures.wp02.compiler.scenario import approve_source_bytes
from tests.fixtures.wp03.intake.support import migrated_application
from tests.fixtures.wp03.worker import support as fixtures

_ACTOR = ActorRef(
    actor_kind="service", actor_id="fixture", identity_registry_id="fixture-registry"
)
_INPUT = sha256_digest(b"tool-input")


def _two_step_source(**options: bool):
    source = fixtures_original(**options)
    artifacts = []
    for value in source.artifacts:
        if value.kind is ArtifactKind.TOOL_PLAN:
            raw = loads(value.content)
            second = deepcopy(raw["steps"][0])
            second.update(step_id="diagnostic", fact_output_mappings=[])
            raw["steps"].append(second)
            slots = tuple(
                replace(slot, location=("steps", 1, *slot.location[2:]))
                for slot in value.reference_slots
                if slot.location[:2] == ("steps", 0)
            )
            value = replace(
                value,
                content=canonical_bytes(raw),
                reference_slots=(*value.reference_slots, *slots),
            )
        artifacts.append(value)
    return approve_source_bytes(replace(source, artifacts=tuple(artifacts)))


fixtures_original = fixtures.parking_source


def _result(
    opened: OpenExecution, handle: StepAttempt, step: ToolStep, *, succeeded: bool
) -> StepResult:
    child, attempt, inputs = handle.node, handle.attempt, handle.inputs
    obj = inputs.object_record
    at = attempt.started_at
    assert at is not None
    catalogue_ref, catalogue = producer_catalogue(opened.synthetic)
    tool_ref = ToolExecutionRef(
        record_kind=K.TOOL_EXECUTION, record_id=attempt.attempt_id
    )
    origin = ToolPlanStepOrigin(
        kind="tool_plan_step",
        node_id="N05",
        tool_plan_ref=inputs.tool_plan_ref,
        step_id=step.step_id,
        producer_declaration_ids=(),
    )
    object_ref = ObjectRef(record_kind=K.OBJECT, record_id=obj.object_id)
    facts = ()
    mapped = ()
    states = ()
    refs = ()
    if succeeded and step.fact_output_mappings:
        declaration = next(
            item
            for item in catalogue.declarations
            if isinstance(item.producer, ToolStepProducer)
            and item.producer.step_id == step.step_id
        )
        output_name = declaration.output_name
        fact = KnownFact(
            schema_version=VERSION,
            fact_id=f"fact-{attempt.attempt_id}",
            fact_key=declaration.fact_key,
            fact_schema_ref=declaration.fact_schema_ref,
            producer_catalogue_ref=catalogue_ref,
            producer_declaration_id=declaration.declaration_id,
            producer_kind=ProducerKind.TOOL,
            producer_id=step.tool_ref.artifact_id,
            producer_version=step.tool_ref.version,
            attempt_id=attempt.attempt_id,
            execution_id=child.execution_id,
            scope=FactScope.OBJECT,
            profile_ref=obj.profile_ref,
            object_id=obj.object_id,
            status=FactStatus.KNOWN,
            value=BooleanValue(kind="bool", value=True),
            evidence_refs=(),
            created_at=at,
            trace_id=child.trace.trace_id,
            source=ToolFactSource(
                source_kind=ProducerKind.TOOL,
                tool_execution_ref=tool_ref,
                tool_plan_ref=inputs.tool_plan_ref,
                step_id=step.step_id,
                tool_ref=step.tool_ref,
                output_name=output_name,
            ),
        )
        ref = FactRef(record_kind=K.FACT, record_id=fact.fact_id)
        facts, refs = (fact,), (ref,)
        mapped = (
            EmittedOutput(
                disposition="emitted",
                output_name=output_name,
                fact_status=FactStatus.KNOWN,
                fact_ref=ref,
            ),
        )
        states = (
            KnownToolOutputState(
                output_name=output_name, fact_status=FactStatus.KNOWN, evidence_refs=()
            ),
        )
        origin = origin.model_copy(
            update={"producer_declaration_ids": (declaration.declaration_id,)}
        )
    outcome: ReturnedToolAttempt | FailedToolAttempt
    qualification = {}
    asset = None
    if succeeded:
        definition = opened.scope.resolve(step.tool_ref, ToolDefinition)
        call = opened.scope.check_call(inputs.envelope, NodeId.N05, at)
        asset = _evidence(
            opened.scope,
            call,
            obj,
            "tool-output",
            b"verified fixture evidence",
            "application/octet-stream",
            PolicyContentRef.model_validate(
                definition.evidence_policy_ref.model_dump()
            ),
        ).record.model_copy(update={"evidence_type": EvidenceType.TOOL_OUTPUT})
        outcome = ReturnedToolAttempt(
            disposition="returned",
            result=SucceededToolResult(
                status="succeeded",
                payload=BooleanValue(kind="bool", value=True),
                diagnostics=(),
                evidence_refs=(),
                evidence=(),
                output_states=states,
            ),
        )
        qualification["authority_observation"] = ToolAuthorityObservation(
            arrival_qualification="unresolved",
            envelope_validation="trusted",
            association_validation="trusted",
            mapped_outputs=mapped,
        )
    else:
        outcome = FailedToolAttempt(
            disposition="failed",
            error=ToolError(
                error_class="transport", code="fixture-failed", diagnostic_refs=()
            ),
        )
    version = next(
        pin.implementation_version
        for pin in opened.scope.pins.tool_pins
        if pin.tool_ref == step.tool_ref
    )
    return StepResult(
        execution=ToolExecution(
            schema_version=VERSION,
            tool_execution_id=tool_ref.record_id,
            execution_id=child.execution_id,
            profile_ref=obj.profile_ref,
            tool_ref=step.tool_ref,
            node_run_ref=attempt.owner.node_run_ref,
            attempt_ref=child.attempt_history[0].attempt_ref,
            invocation_origin=origin,
            scope=FactScope.OBJECT,
            object_ref=object_ref,
            input_refs=_tool_input_refs(inputs.envelope),
            output_refs=()
            if asset is None
            else (
                ToolArtifactRef(
                    artifact_ref=ToolArtifactRecordRef(
                        record_kind=K.EVIDENCE, record_id=asset.evidence_id
                    ),
                    digest=asset.sha256,
                ),
            ),
            implementation_version_resolution="resolved",
            actual_implementation_version=version,
            status="succeeded" if succeeded else "failed",
            attempt_result=outcome,
            started_at=at,
            completed_at=at,
            generated_fact_refs=refs,
            evidence_refs=()
            if asset is None
            else (EvidenceRef(record_kind=K.EVIDENCE, record_id=asset.evidence_id),),
            trace_id=child.trace.trace_id,
            **qualification,
        ),
        facts=facts,
        evidence=() if asset is None else (asset,),
    )


async def _setup(engine: AsyncEngine, path: Path):
    worker, accepted, host = await fixtures.accepted_worker(engine, path)
    scope, eid = accepted.task.idempotency_scope, accepted.execution.execution_id
    for _ in range(4):
        assert (
            await worker.run_once(scope, eid, owner="setup", lease_ms=1000)
            is WorkerStep.APPLIED
        )
    async with engine.connect() as connection:
        locked = await repo.get_execution(connection, scope, eid)
        assert locked is not None
        records = await load_records(connection, scope, locked)
    opened = await open_execution(
        locked,
        scope,
        worker._manager,
        worker._store,
        host,
        fixtures.LIMITS,
        datetime.now(UTC),
    )
    previous = records.inputs()
    assert previous.grounding is not None
    (obj,) = previous.grounding.objects
    profile = opened.scope.resolve(obj.profile_ref, ProfileDefinition)
    plan = opened.scope.resolve(profile.tool_plan_ref, ToolPlanDefinition)
    return scope, eid, opened, obj, previous.snapshot(), profile, plan


async def _claim_parent(
    engine: AsyncEngine,
    jobs: DurableJobs,
    scope: IdempotencyScope,
    execution_id: str,
    opened: OpenExecution,
    obj: ObjectRecord,
    snapshot: ContextSnapshot,
    profile: ProfileDefinition,
    owner: str,
) -> tuple[JobClaim, ToolNodeInput, datetime]:
    claim = await jobs.claim(scope, owner, 5000, execution_id=execution_id)
    assert claim is not None
    attempt = await jobs.start(claim)
    assert attempt is not None and attempt.started_at is not None
    async with engine.connect() as connection:
        node = await repo.get_node(connection, scope, execution_id, claim.node_run_id)
    assert node is not None and node.node_id is NodeId.N05
    inputs = ToolNodeInput(
        schema_version=VERSION,
        envelope=invocation_envelope(
            opened, BranchKey(NodeId.N05, obj.object_id), node, attempt
        ),
        object_record=obj,
        image_view=ObjectImageView(roi=obj.bbox, transforms=()),
        context_snapshot_ref=snapshot_ref(snapshot),
        tool_plan_ref=profile.tool_plan_ref,
        validated_observation_fact_refs=(),
    )
    return claim, inputs, attempt.started_at


def _node_output(
    inputs: ToolNodeInput,
    successes: tuple[StepCompletion, ...],
    *,
    started_at: datetime,
    failed: bool,
) -> ToolNodeOutput:
    env = inputs.envelope
    issue = synthetic_issue(
        "fixture-failed", "Fixture Tool step returned a transient error"
    )
    tools = tuple(result.result.execution for result in successes)
    facts = tuple(fact for item in successes for fact in item.result.facts)
    at = max(tool.completed_at for tool in tools)
    evidence = (
        tuple(
            artifact(K.TOOL_EXECUTION, item.tool_execution_id, item).pin
            for item in tools
        )
        + tuple(artifact(K.FACT, item.fact_id, item).pin for item in facts)
        + tuple(
            artifact(K.EVIDENCE, item.evidence_id, item).pin
            for step in successes
            for item in step.result.evidence
        )
    )
    envelope_fields = dict(
        schema_version=VERSION,
        node_id=NodeId.N05,
        node_run_id=env.node_run_id,
        attempt_id=env.attempt_id,
        trace=env.trace,
        output_artifacts=evidence,
        started_at=started_at,
        finished_at=at,
        duration_ms=(at - started_at) // timedelta(milliseconds=1),
    )
    envelope = (
        FailedNodeOutput(
            status=NodeRunStatus.FAILED,
            error=NodeError(
                code=issue.code,
                error_class="transport",
                retryable=True,
                message=issue.message,
                validation_issues=(issue,),
            ),
            **envelope_fields,
        )
        if failed
        else SucceededNodeOutput(status=NodeRunStatus.SUCCEEDED, **envelope_fields)
    )
    return ToolNodeOutput(
        schema_version=VERSION,
        envelope=envelope,
        summary=ToolPlanRunSummary(
            tool_plan_ref=inputs.tool_plan_ref,
            object_ref=ObjectRef(
                record_kind=K.OBJECT, record_id=inputs.object_record.object_id
            ),
            status="failed" if failed else "succeeded",
            steps=tuple(
                InvokedToolStep(
                    disposition="invoked",
                    step_id=tool.invocation_origin.step_id,
                    tool_execution_ref=ToolExecutionRef(
                        record_kind=K.TOOL_EXECUTION, record_id=tool.tool_execution_id
                    ),
                )
                for tool in tools
            ),
            validation_issues=(issue,) if failed else (),
        ),
        tool_fact_refs=tuple(ref for tool in tools for ref in tool.generated_fact_refs),
        evidence_refs=tuple(ref for tool in tools for ref in tool.evidence_refs),
    )


def test_cross_round_tool_step_authority(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(fixtures, "parking_source", _two_step_source)

    async def run() -> None:
        async with migrated_application() as engine:
            scope, eid, opened, obj, snapshot, profile, plan = await _setup(
                engine, tmp_path
            )
            a, b = plan.steps
            jobs = DurableJobs(engine)

            claim_1, inputs_1, started_1 = await _claim_parent(
                engine,
                jobs,
                scope,
                eid,
                opened,
                obj,
                snapshot,
                profile,
                "first",
            )
            store = StepAttemptStore(engine, actor=_ACTOR)
            one_a = await store.begin(claim_1, inputs_1, a, _INPUT, timeout_ms=3000)
            one_b = await store.begin(claim_1, inputs_1, b, _INPUT, timeout_ms=3000)
            assert isinstance(one_a, StepAttempt) and isinstance(one_b, StepAttempt)
            assert await jobs.start(one_a.claim) is None
            assert await jobs.claim(scope, "generic", 500, execution_id=eid) is None
            async with engine.connect() as connection:
                nodes = await load_child_nodes(connection, scope, eid)
                assert {node.node_run_id for node in nodes} == {
                    one_a.node.node_run_id,
                    one_b.node.node_run_id,
                }
                assert all(node.status is NodeRunStatus.RUNNING for node in nodes)
            candidate_a = _result(opened, one_a, a, succeeded=True)
            observation = candidate_a.execution.authority_observation
            assert observation is not None
            late_a = candidate_a.execution.model_copy(
                update={
                    "authority_observation": observation.model_copy(
                        update={"arrival_qualification": "late"}
                    )
                }
            )
            assert (
                await store.complete(
                    one_a, candidate_a.model_copy(update={"execution": late_a})
                )
                is None
            )
            (asset,) = candidate_a.evidence
            other_digest = sha256_digest(b"tampered fixture evidence")
            bad_asset = asset.model_copy(
                update={
                    "sha256": other_digest,
                    "stage": asset.stage.model_copy(
                        update={"verified_sha256": other_digest}
                    ),
                }
            )
            with pytest.raises(ValueError, match="exact original Fact, evidence"):
                await store.complete(
                    one_a, candidate_a.model_copy(update={"evidence": (bad_asset,)})
                )
            original_a = await store.complete(one_a, candidate_a)
            failed_b = await store.complete(
                one_b, _result(opened, one_b, b, succeeded=False)
            )
            assert original_a is not None and original_a.selection is not None
            confirmed = original_a.result.execution.authority_observation
            assert (
                confirmed is not None and confirmed.arrival_qualification == "on_time"
            )
            assert failed_b is not None and failed_b.selection is None
            output_1 = _node_output(
                inputs_1, (original_a, failed_b), started_at=started_1, failed=True
            )
            assert (
                await jobs.complete(claim_1, output_1.envelope)
                is CompletionDisposition.RETRY_SCHEDULED
            )
            await asyncio.sleep(0.04)

            claim_2, inputs_2, started_2 = await _claim_parent(
                engine,
                jobs,
                scope,
                eid,
                opened,
                obj,
                snapshot,
                profile,
                "restarted",
            )
            restarted = StepAttemptStore(engine, actor=_ACTOR)
            with pytest.raises(ValueError, match="explicit new NodeRun"):
                await restarted.begin(
                    claim_2, inputs_2, a, sha256_digest(b"changed"), timeout_ms=3000
                )
            reused_a = await restarted.begin(
                claim_2, inputs_2, a, _INPUT, timeout_ms=3000
            )
            with pytest.raises(StepAttemptBudgetExhausted):
                await restarted.begin(
                    claim_2, inputs_2, b, _INPUT, timeout_ms=3000, max_attempts=1
                )
            two_b = await restarted.begin(
                claim_2, inputs_2, b, _INPUT, timeout_ms=3000, max_attempts=2
            )
            assert reused_a == original_a and isinstance(two_b, StepAttempt)
            assert two_b.attempt.attempt_id != one_b.attempt.attempt_id
            assert two_b.previous == failed_b
            lineage = await restarted.nested_retry_history(two_b)
            assert lineage is not None
            assert lineage.identity.parent_node_run_ref.record_id == claim_2.node_run_id
            assert lineage.identity.bound_input_digest == two_b.node.input_digest
            assert lineage.current.parent_attempt.attempt_number == 2
            (source,) = lineage.priors
            assert source.parent_attempt.status is AttemptStatus.FAILED
            assert source.child_node.node_run_id == one_b.node.node_run_id
            assert source.child_attempt.attempt_id == one_b.attempt.attempt_id
            assert await restarted.nested_retry_history(one_b) is None
            assert (
                await restarted.complete(
                    one_b, _result(opened, one_b, b, succeeded=True)
                )
                is None
            )
            with pytest.raises(ValueError, match="immutable scope"):
                await restarted.complete(
                    two_b, _result(opened, one_b, b, succeeded=True)
                )
            final_b = await restarted.complete(
                two_b, _result(opened, two_b, b, succeeded=True)
            )
            assert final_b is not None
            output_2 = _node_output(
                inputs_2, (original_a, final_b), started_at=started_2, failed=False
            )
            executions = (original_a.result.execution, final_b.result.execution)
            facts = original_a.result.facts
            async with engine.connect() as connection:
                history = await load_parent_step_history(connection, scope, inputs_2)
                assert history is not None and len(history.entries) == 2
                statuses = await connection.execute(
                    text(
                        "SELECT attempt_status FROM jimu_workflow.tool_step_completions ORDER BY attempt_status"
                    )
                )
                assert tuple(statuses.scalars()) == ("failed", "succeeded", "succeeded")
                assert all(
                    entry.child_attempt.status is AttemptStatus.SUCCEEDED
                    for entry in history.entries
                )
                assert (
                    sum(
                        entry.original_parent_attempt.status is AttemptStatus.FAILED
                        for entry in history.entries
                    )
                    == 1
                )
            output_2.check_steps(
                inputs_2, profile, plan, executions, facts, history=history
            )
            with pytest.raises(ValueError, match="actual bound"):
                output_2.check_steps(inputs_2, profile, plan, executions, facts)
            forged = facts[0].model_copy(
                update={"value": BooleanValue(kind="bool", value=False)}
            )
            with pytest.raises(ValueError, match="original child result"):
                output_2.check_steps(
                    inputs_2, profile, plan, executions, (forged,), history=history
                )
            assert (
                await jobs.complete(claim_2, output_2.envelope)
                is CompletionDisposition.APPLIED
            )

    asyncio.run(run())


@pytest.mark.parametrize(
    ("interruption", "reported_arrival"),
    [
        ("cancel", None),
        ("parent_lease", None),
        ("child_deadline", None),
        ("child_deadline", "unresolved"),
        ("child_deadline", "on_time"),
        ("child_deadline", "late"),
    ],
)
def test_tool_step_cannot_apply_after_fence_loss(
    tmp_path: Path,
    interruption: str,
    reported_arrival: Literal["unresolved", "on_time", "late"] | None,
) -> None:
    async def run() -> None:
        async with migrated_application() as engine:
            scope, eid, opened, obj, snapshot, profile, plan = await _setup(
                engine, tmp_path
            )
            (step,) = plan.steps
            jobs = DurableJobs(engine)
            claim, inputs, started = await _claim_parent(
                engine, jobs, scope, eid, opened, obj, snapshot, profile, "first"
            )
            store = StepAttemptStore(engine, actor=_ACTOR)
            handle = await store.begin(
                claim,
                inputs,
                step,
                _INPUT,
                timeout_ms=35 if interruption == "child_deadline" else 3000,
            )
            assert isinstance(handle, StepAttempt)
            if interruption == "cancel":
                assert await jobs.request_cancel(scope, eid)
            elif interruption == "parent_lease":
                async with engine.begin() as connection:
                    await connection.execute(
                        text("""
                            UPDATE jimu_workflow.jobs
                            SET lease_until = pg_catalog.clock_timestamp() - interval '1 second'
                            WHERE project_id = :project AND client_id = :client AND execution_id = :execution_id
                                AND job_id = :job_id
                        """),
                        {
                            "project": scope.project_id,
                            "client": scope.client_id,
                            "execution_id": eid,
                            "job_id": claim.job_id,
                        },
                    )
            else:
                await asyncio.sleep(0.06)
            assert (
                await store.complete(
                    handle, _result(opened, handle, step, succeeded=True)
                )
                is None
            )
            failure_result = _result(opened, handle, step, succeeded=False)
            assert isinstance(
                failure_result.execution.attempt_result, FailedToolAttempt
            )
            error = ToolError(
                error_class="timeout", code="fixture-expired", diagnostic_refs=()
            )
            update: dict[str, object] = {
                "attempt_result": FailedToolAttempt(disposition="failed", error=error)
            }
            if reported_arrival is not None:
                update.update(
                    attempt_result=ReturnedToolAttempt(
                        disposition="returned",
                        result=FailedToolResult(
                            status="failed",
                            error=error,
                            diagnostics=(),
                            evidence_refs=(),
                            evidence=(),
                            output_states=(),
                        ),
                    ),
                    authority_observation=ToolAuthorityObservation(
                        arrival_qualification=reported_arrival,
                        envelope_validation="trusted",
                        association_validation="trusted",
                        mapped_outputs=(),
                    ),
                    completed_at=datetime.now(UTC),
                )
            normalized_timeout = failure_result.model_copy(
                update={"execution": failure_result.execution.model_copy(update=update)}
            )
            terminal = await store.complete(handle, normalized_timeout)
            if interruption == "child_deadline":
                assert terminal is not None
                assert (
                    terminal.attempt_status == "timed_out"
                    and terminal.selection is None
                )
                expected_arrival = (
                    None
                    if reported_arrival is None
                    else "late"
                    if reported_arrival == "late"
                    else "unresolved"
                )
                observation = terminal.result.execution.authority_observation
                assert (
                    None if observation is None else observation.arrival_qualification
                ) == expected_arrival
                failure = _node_output(
                    inputs, (terminal,), started_at=started, failed=True
                )
                async with engine.connect() as connection:
                    history = await load_parent_step_history(connection, scope, inputs)
                    assert history is not None
                    assert (
                        history.entries[0].child_attempt.status
                        is AttemptStatus.TIMED_OUT
                    )
                    assert history.entries[0].execution == terminal.result.execution
                failure.check_steps(
                    inputs,
                    profile,
                    plan,
                    (terminal.result.execution,),
                    (),
                    history=history,
                )
                assert (
                    await jobs.complete(claim, failure.envelope)
                    is CompletionDisposition.RETRY_SCHEDULED
                )
                await asyncio.sleep(0.04)
                next_claim, next_inputs, _ = await _claim_parent(
                    engine,
                    jobs,
                    scope,
                    eid,
                    opened,
                    obj,
                    snapshot,
                    profile,
                    "successor",
                )
                successor = await store.begin(
                    next_claim, next_inputs, step, _INPUT, timeout_ms=3000
                )
                assert (
                    isinstance(successor, StepAttempt)
                    and successor.previous == terminal
                )
                nested = await store.nested_retry_history(successor)
                assert nested is not None
                (source,) = nested.priors
                assert source.child_attempt.status is AttemptStatus.TIMED_OUT
                assert not source.invocations
                assert await store.complete(handle, normalized_timeout) is None
                assert (
                    await store.complete(
                        successor, _result(opened, successor, step, succeeded=True)
                    )
                    is not None
                )
            else:
                assert terminal is None
                assert await jobs.reconcile(scope, eid)
                if interruption == "parent_lease":
                    await asyncio.sleep(0.04)
                    next_claim, next_inputs, _ = await _claim_parent(
                        engine,
                        jobs,
                        scope,
                        eid,
                        opened,
                        obj,
                        snapshot,
                        profile,
                        "replacement",
                    )
                    replacement = await store.begin(
                        next_claim, next_inputs, step, _INPUT, timeout_ms=3000
                    )
                    assert isinstance(replacement, StepAttempt)
                    assert replacement.previous is None
                    assert await store.nested_retry_history(replacement) is None
            async with engine.connect() as connection:
                attempt = await repo.get_attempt(
                    connection,
                    scope,
                    eid,
                    handle.node.node_run_id,
                    handle.attempt.attempt_id,
                )
                assert attempt is not None
                assert (
                    attempt.status
                    is (
                        {
                            "cancel": AttemptStatus.CANCELLED,
                            "parent_lease": AttemptStatus.SUPERSEDED,
                            "child_deadline": AttemptStatus.TIMED_OUT,
                        }[interruption]
                    )
                )
                result = await connection.execute(
                    text("SELECT count(*) FROM jimu_workflow.tool_step_completions")
                )
                assert result.scalar_one() == (
                    2 if interruption == "child_deadline" else 0
                )

    asyncio.run(run())
