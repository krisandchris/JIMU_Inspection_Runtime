"""Persist deterministic branch transitions and successors in the caller's transaction."""

from collections.abc import Callable
from datetime import datetime

from sqlalchemy.ext.asyncio import AsyncConnection

from jimu_runtime.bundle.integrity import sha256_digest
from jimu_runtime.domain.enums import NodeRunStatus
from jimu_runtime.domain.nodes.context import ContextBuildInput, SuccessfulContextBuild
from jimu_runtime.domain.policies import ApplicabilityPolicyArtifact
from jimu_runtime.domain.runtime.executions import (
    LifecycleReason,
    NodeId,
    NodeRun,
    PinnedRuntimeInput,
)
from jimu_runtime.domain.runtime.intake import ContextSnapshot, IdempotencyScope
from jimu_runtime.domain.validation.applicability import (
    ApplicabilityRecordSet,
    validate_applicability_records,
)
from jimu_runtime.domain.validation.transitions import NodeTransitionGuard
from jimu_runtime.workflow import repository as repo
from jimu_runtime.workflow.engine import (
    BranchKey,
    ReadyWork,
    ToolWork,
    UninvokedWork,
    plan_work,
)
from jimu_runtime.workflow.mock_context import build_gate, snapshot_artifact
from jimu_runtime.workflow.mock_perception import skipped_discovery
from jimu_runtime.workflow.mock_support import (
    VERSION,
    K,
    SyntheticArtifact,
    artifact,
    synthetic_id,
)
from jimu_runtime.workflow.worker_nodes import authority_artifact, fact_artifact
from jimu_runtime.workflow.worker_records import GATE_KEY, SKIP_KEY, EligibilityMessage
from jimu_runtime.workflow.worker_source import OpenExecution
from jimu_runtime.workflow.worker_state import WorkerRecords, branch_name, load_records

type ProfileGate = Callable[
    [OpenExecution, ContextBuildInput, SuccessfulContextBuild, ContextSnapshot],
    EligibilityMessage,
]


def _gate_artifact(data: WorkerRecords) -> SyntheticArtifact:
    if data.gate is None:
        raise ValueError("workflow requires the selected eligibility result")
    value = data.gate.evaluation
    return artifact(K.PROFILE_ELIGIBILITY_EVALUATION, value.evaluation_id, value)


def _inputs(
    data: WorkerRecords, work: ReadyWork, *, configured: bool
) -> tuple[PinnedRuntimeInput, ...]:
    key = work.key
    previous = data.inputs(key.object_id)
    if key.node_id is NodeId.N02:
        return (snapshot_artifact(previous.snapshot()).pin, _gate_artifact(data).pin)
    if key.node_id is NodeId.N03 and previous.discovery is not None:
        return previous.discovery.output.envelope.output_artifacts
    if key.object_id is not None:
        obj = previous.object_record(key)
        refs: list[PinnedRuntimeInput] = [
            artifact(K.OBJECT, obj.object_id, obj).pin,
            snapshot_artifact(previous.snapshot()).pin,
        ]
        if key.node_id is NodeId.N04 and previous.discovery is not None:
            (selection,) = tuple(
                item
                for item in previous.discovery.selections
                if item.selection_id == obj.question_selection_ref.record_id
            )
            refs.append(
                artifact(
                    K.QUESTION_SELECTION_RECORD, selection.selection_id, selection
                ).pin
            )
        if key.node_id is NodeId.N06:
            refs.append(_gate_artifact(data).pin)
            facts, authorities = previous.selected_facts()
            refs.extend(fact_artifact(fact).pin for fact in facts)
            refs.extend(authority_artifact(selection).pin for selection in authorities)
            if configured:
                if data.gate is None:
                    raise ValueError("N06 requires the exact Profile gate")
                (trace,) = tuple(
                    item
                    for item in data.gate.traces
                    if item.profile_ref == obj.profile_ref
                )
                refs.append(
                    artifact(
                        K.APPLICABILITY_TRACE, trace.applicability_trace_id, trace
                    ).pin
                )
                refs.extend(
                    artifact(K.EVIDENCE, item.evidence_id, item).pin
                    for item in previous.selected_evidence(key.object_id)
                )
        return tuple(refs)
    raise ValueError("successor requires its exact durable predecessor records")


def _prerequisites(work: ReadyWork) -> tuple[BranchKey, ...]:
    key = work.key
    if key.node_id is NodeId.N02:
        return (BranchKey(NodeId.N01),)
    if key.node_id is NodeId.N03:
        return (BranchKey(NodeId.N02),)
    if key.node_id is NodeId.N04:
        return (BranchKey(NodeId.N03),)
    if isinstance(work.inputs, ToolWork):
        grounding = BranchKey(NodeId.N03)
        if work.inputs.needs_observation:
            return (grounding, BranchKey(NodeId.N04, key.object_id))
        return (grounding,)
    if key.node_id is NodeId.N06:
        return (
            BranchKey(NodeId.N04, key.object_id),
            BranchKey(NodeId.N05, key.object_id),
        )
    raise ValueError("the root comes from durable intake")


async def _create(
    connection: AsyncConnection,
    scope: IdempotencyScope,
    data: WorkerRecords,
    key: BranchKey,
    now: datetime,
    upstream: tuple[PinnedRuntimeInput, ...],
) -> NodeRun:
    execution = data.locked.execution
    name = branch_name(key)
    identity = (execution.execution_id, key.node_id.value, name)
    digest = sha256_digest(
        synthetic_id(
            "node-input",
            data.locked.execution_policy_ref.stable_json(),
            execution.pins.input_digest,
            *identity,
            *(pin.stable_json() for pin in upstream),
        ).encode()
    )
    pending = NodeRun(
        schema_version=VERSION,
        node_run_id=synthetic_id("node", *identity),
        execution_id=execution.execution_id,
        node_id=key.node_id,
        trace=execution.trace,
        config_ref=data.locked.execution_policy_ref,
        upstream_inputs=upstream,
        input_digest=digest,
        status=NodeRunStatus.PENDING,
        created_at=now,
        attempt_history=(),
        output_refs=(),
    )
    return await repo.insert_node(
        connection, scope, execution.execution_id, pending, branch_key=name
    )


async def _ready(
    connection: AsyncConnection,
    scope: IdempotencyScope,
    data: WorkerRecords,
    work: ReadyWork,
    now: datetime,
    *,
    configured: bool,
) -> None:
    pending = await _create(
        connection,
        scope,
        data,
        work.key,
        now,
        _inputs(data, work, configured=configured),
    )
    for key in _prerequisites(work):
        await repo.link_successor(
            connection,
            scope,
            pending.execution_id,
            data.runs[key].node_run_id,
            pending.node_run_id,
        )
    ready = NodeRun.model_validate(
        pending.model_copy(update={"status": NodeRunStatus.READY})
    )
    await repo.transition_node(
        connection,
        scope,
        pending.execution_id,
        pending,
        ready,
        now=now,
        guard=NodeTransitionGuard.PREREQUISITES_AVAILABLE,
    )
    data.runs[work.key] = ready
    await repo.insert_job(
        connection,
        scope,
        pending.execution_id,
        pending.node_run_id,
        repo.NewJob(
            job_id=f"job.{pending.node_run_id}",
            ready_at=now,
            budget=data.locked.scheduling_limits.for_node(pending.node_id),
        ),
    )


async def _uninvoked(
    connection: AsyncConnection,
    scope: IdempotencyScope,
    data: WorkerRecords,
    opened: OpenExecution,
    work: UninvokedWork,
    now: datetime,
) -> None:
    pending = await _create(connection, scope, data, work.key, now, ())
    if work.blocker_node_run_ref is not None:
        await repo.link_successor(
            connection,
            scope,
            pending.execution_id,
            work.blocker_node_run_ref.record_id,
            pending.node_run_id,
        )
    terminal = NodeRun.model_validate(
        pending.model_copy(
            update={
                "status": work.status,
                "reason": LifecycleReason(code=work.reason_code),
                "ended_at": now,
            }
        )
    )
    if work.key.node_id is NodeId.N02 and work.status is NodeRunStatus.SKIPPED:
        if data.gate is None:
            raise ValueError("N02 can only skip on an authoritative empty Profile gate")
        skip = skipped_discovery(
            opened.scope, data.gate.evaluation, pending.node_run_id
        )
        await repo.store_engine_output(
            connection,
            scope,
            pending.execution_id,
            pending.node_run_id,
            output_key=SKIP_KEY,
            output_kind="skipped_discovery",
            record=skip,
        )
    guard = (
        NodeTransitionGuard.DETERMINISTIC_SKIP
        if work.status is NodeRunStatus.SKIPPED
        else NodeTransitionGuard.PREREQUISITE_UNAVAILABLE
    )
    await repo.transition_node(
        connection, scope, pending.execution_id, pending, terminal, now=now, guard=guard
    )


def check_configured_gate(
    opened: OpenExecution, context: SuccessfulContextBuild, gate: EligibilityMessage
) -> None:
    expected_profiles = tuple(item.rules.profile_ref for item in opened.profiles)
    state = gate.scope
    if (
        state.execution_id,
        state.release_id,
        state.bundle_digest,
        state.workflow_version,
        state.configured_profile_refs,
        state.context_snapshot_ref,
        state.context_snapshot_digest,
        state.context_node_run.node_run_ref.record_id,
    ) != (
        opened.scope.execution_id,
        opened.scope.pins.release_id,
        opened.scope.pins.bundle_digest,
        opened.scope.pins.workflow_version,
        expected_profiles,
        context.context_snapshot_ref,
        context.context_snapshot_digest,
        context.envelope.node_run_id,
    ):
        raise ValueError(
            "Profile gate must match the pinned Context and admitted Profile set"
        )
    for binding, profile in zip(state.target_bindings, opened.profiles, strict=True):
        if (
            binding.target_ref.stable_json() != profile.rules.profile_ref.stable_json()
            or binding.policy_ref.stable_json()
            != profile.definition.applicability_policy_ref.stable_json()
            or binding.child_refs != profile.definition.rule_set_refs
            or binding.policy
            != opened.scope.resolve(binding.policy_ref, ApplicabilityPolicyArtifact)
        ):
            raise ValueError(
                "Profile gate must retain the admitted policy and RuleSets"
            )
    validate_applicability_records(
        state,
        ApplicabilityRecordSet(
            profile_eligibility=gate.evaluation,
            traces=gate.traces,
            rule_set_evaluations=(),
            rule_evaluations=(),
            unresolved_obligations=(),
            node_runs=(),
            attempts=(),
            objects=(),
        ),
    )


async def schedule_after_terminal(
    connection: AsyncConnection,
    scope: IdempotencyScope,
    opened: OpenExecution,
    execution_id: str,
    now: datetime,
    *,
    profile_gate: ProfileGate | None = None,
) -> None:
    locked = await repo.get_execution(connection, scope, execution_id)
    if locked is None:
        raise ValueError("terminal callback requires an Execution")
    if locked.cancel_requested_at is not None:
        return
    data = await load_records(connection, scope, locked)
    configured = profile_gate is not None
    plan = plan_work(data.planner_state(opened, configured=configured))
    if plan.eligibility is not None:
        previous = data.inputs()
        context = previous.context
        if context is None or not isinstance(context.output, SuccessfulContextBuild):
            raise ValueError("Pre-N02 requires the selected successful ContextSnapshot")
        snapshot = previous.snapshot()
        if profile_gate is None:
            gate = build_gate(
                opened.scope, context.inputs, context.output, snapshot, now
            )
            data.gate = EligibilityMessage(
                schema_version=VERSION,
                evaluation=gate.evaluation,
                traces=gate.traces,
                scope=gate.scope,
            )
        else:
            data.gate = profile_gate(opened, context.inputs, context.output, snapshot)
            check_configured_gate(opened, context.output, data.gate)
        await repo.store_engine_output(
            connection,
            scope,
            execution_id,
            context.output.envelope.node_run_id,
            output_key=GATE_KEY,
            output_kind="profile_eligibility",
            record=data.gate,
        )
        plan = plan_work(data.planner_state(opened, configured=configured))
    if plan.uninvoked:
        # Empty eligibility creates N02 first; materialize its output before
        # asking the planner for the remaining attempt-free branches.
        for work in plan.uninvoked:
            await _uninvoked(connection, scope, data, opened, work, now)
        data = await load_records(connection, scope, locked)
        plan = plan_work(data.planner_state(opened, configured=configured))
        for work in plan.uninvoked:
            await _uninvoked(connection, scope, data, opened, work, now)
    for ready in plan.ready:
        await _ready(
            connection,
            scope,
            data,
            ready,
            now,
            configured=configured,
        )
