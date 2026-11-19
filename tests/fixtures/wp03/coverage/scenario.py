"""Actual mock-stage records assembled into a settled planner snapshot."""

from dataclasses import dataclass, replace
from pathlib import Path

from jimu_runtime.domain.definitions.capabilities import ProfileDefinition
from jimu_runtime.domain.definitions.models import PolicyContentRef
from jimu_runtime.domain.enums import (
    ArtifactKind,
    AttemptStatus,
    ExecutionStatus,
    NodeRunStatus,
)
from jimu_runtime.domain.nodes.context import SuccessfulContextBuild
from jimu_runtime.domain.nodes.envelopes import NodeInputEnvelope, NodeOutputEnvelope
from jimu_runtime.domain.policies import EvidencePolicyArtifact, RulePolicyArtifact
from jimu_runtime.domain.rules.definitions import RuleDefinition, RuleSetDefinition
from jimu_runtime.domain.runtime.executions import (
    Attempt,
    AttemptLink,
    ContextSnapshotPin,
    ExecutionRecord,
    LifecycleReason,
    NodeId,
    NodeRun,
    PinnedRuntimeInput,
)
from jimu_runtime.domain.runtime.facts import (
    AttemptRef,
    ModelInvocationRef,
    NodeRunRef,
    ObjectRef,
    ProfileContentRef,
)
from jimu_runtime.domain.validation.authority_inputs import NodeAttemptOwner
from jimu_runtime.domain.validation.coverage_sources import (
    CoverageSources,
    ObjectDecisionBlocker,
)
from jimu_runtime.domain.validation.decision_coverage import (
    BoundRule,
    BoundRuleSet,
    ProfileRuleScope,
)
from jimu_runtime.domain.validation.runtime_authority import RuntimeAuthorityRecords
from jimu_runtime.domain.values import RecordValue
from jimu_runtime.workflow.engine import (
    BranchKey,
    BranchRun,
    ExecutionFinalization,
    PlannerState,
    ProfilePlan,
    plan_work,
)
from jimu_runtime.workflow.mock_support import synthetic_issue
from jimu_runtime.workflow.pins import ResolvedExecutionPins, resolve_execution_pins
from jimu_runtime.workflow.providers import (
    SyntheticDecisionResult,
    SyntheticExecution,
    SyntheticGateResult,
    SyntheticGroundingCase,
    build_context,
    discover,
    ground,
    record_digest,
    skipped_discovery,
    stage_decision,
)
from jimu_runtime.workflow.worker_coverage import (
    build_coverage_authority,
    finalize_execution,
)
from tests.fixtures.wp02.compiler.scenario import AT, LIMITS, capability_snapshot
from tests.fixtures.wp02.releases.support import GLOBAL
from tests.fixtures.wp03.nodes.cases import (
    authority_cases,
    context,
    decision_input,
    discovery_input,
    grounding_input,
    observation_case,
    tools_case,
)
from tests.fixtures.wp03.nodes.scenario import VERSION, K, parking_fixture


@dataclass(frozen=True, slots=True)
class Scenario:
    scope: SyntheticExecution
    state: PlannerState
    sources: CoverageSources
    gate: SyntheticGateResult | None
    stages: tuple[SyntheticDecisionResult, ...]
    resolved: ResolvedExecutionPins
    runtime: RuntimeAuthorityRecords | None

    def finalize(self) -> ExecutionFinalization:
        runtime = self.runtime
        return finalize_execution(
            self.state,
            plan_work(self.state).coverage,
            scope=self.scope,
            sources=self.sources,
            gate=self.gate,
            stages=self.stages,
            facts=runtime.facts if runtime else (),
            authorities=runtime.authority_selections if runtime else (),
            evidence=tuple(item for stage in self.stages for item in stage.evidence),
            invocations=runtime.model_invocations if runtime else (),
            authority=build_coverage_authority(self.resolved, runtime)
            if runtime is not None and (runtime.facts or runtime.authority_selections)
            else None,
            now=self.scope.task.accepted_at,
        )


def scenario(
    path: Path,
    *,
    mode: str = "violation",
) -> Scenario:
    fixture = parking_fixture(
        path,
        second_profile=mode in ("mixed", "partial-discovery"),
        excluded=mode == "excluded",
        metadata=RecordValue(kind="record", value={})
        if mode == "context-failed"
        else None,
    )
    scope = fixture.scope
    at = scope.task.accepted_at
    resolved = resolve_execution_pins(
        fixture.manager,
        fixture.store,
        scope.task,
        scope=scope.task.idempotency_scope,
        host=capability_snapshot(),
        validation_at=AT,
        limits=LIMITS,
    )
    scope = replace(scope, pins=resolved.pins)
    fixture = replace(fixture, scope=scope)
    ctx = context(fixture) if mode != "context-failed" else None
    refs = tuple(
        ProfileContentRef.model_validate(item.ref.model_dump())
        for item in scope.catalogue.of_kind(ArtifactKind.PROFILE)
    )
    plans = []
    for ref in refs:
        profile = scope.resolve(ref, ProfileDefinition)
        rules = []
        for set_ref in profile.rule_set_refs:
            definition = scope.resolve(set_ref, RuleSetDefinition)
            bound = []
            for rule_ref in definition.rule_refs:
                rule = scope.resolve(rule_ref, RuleDefinition)
                bound.append(
                    BoundRule(
                        rule_ref=rule_ref,
                        definition=rule,
                        rule_policy=scope.resolve(
                            rule.rule_policy_ref, RulePolicyArtifact
                        ),
                        evidence_policy=scope.resolve(
                            rule.evidence_policy_ref, EvidencePolicyArtifact
                        ),
                    )
                )
            rules.append(
                BoundRuleSet(
                    rule_set_ref=set_ref, definition=definition, rules=tuple(bound)
                )
            )
        plans.append(
            ProfilePlan(
                profile, ProfileRuleScope(profile_ref=ref, rule_sets=tuple(rules))
            )
        )

    runs: list[BranchRun] = []
    attempts: list[Attempt] = []

    def save(
        inputs: NodeInputEnvelope,
        output: NodeOutputEnvelope,
        object_id: str | None = None,
    ) -> None:
        node_ref = NodeRunRef(record_kind=K.NODE_RUN, record_id=inputs.node_run_id)
        attempt_ref = AttemptRef(record_kind=K.ATTEMPT, record_id=inputs.attempt_id)
        reason = (
            LifecycleReason(code="fixture_failure")
            if output.status is NodeRunStatus.FAILED
            else None
        )
        attempts.append(
            Attempt(
                schema_version=VERSION,
                attempt_id=inputs.attempt_id,
                execution_id=scope.execution_id,
                owner=NodeAttemptOwner(owner_kind="node", node_run_ref=node_ref),
                attempt_number=1,
                status=AttemptStatus(output.status.value),
                created_at=at,
                started_at=at,
                ended_at=at,
                reason=reason,
            )
        )
        runs.append(
            BranchRun(
                BranchKey(inputs.node_id, object_id),
                NodeRun(
                    schema_version=VERSION,
                    node_run_id=inputs.node_run_id,
                    execution_id=scope.execution_id,
                    node_id=inputs.node_id,
                    trace=inputs.trace,
                    config_ref=PolicyContentRef.model_validate(
                        inputs.node_config_ref.model_dump()
                    ),
                    upstream_inputs=tuple(
                        PinnedRuntimeInput(ref=item.ref, digest=item.digest)
                        for item in inputs.upstream_artifacts
                    ),
                    input_digest=record_digest(inputs),
                    status=output.status,
                    created_at=at,
                    started_at=at,
                    ended_at=at,
                    reason=reason,
                    attempt_history=(
                        AttemptLink(attempt_ref=attempt_ref, attempt_number=1),
                    ),
                    output_refs=tuple(item.ref for item in output.output_artifacts),
                ),
            )
        )

    n01 = fixture.context_input()
    context_result = build_context(scope, n01, at)
    save(n01.envelope, context_result.output.envelope)
    n02 = None
    n03 = None
    discovered = None
    grounded = None
    invocations = []
    discovery = None
    if mode == "excluded":
        assert ctx is not None
        skipped_run = fixture.envelope(NodeId.N02)
        discovery = skipped_discovery(
            scope, ctx.gate.evaluation, skipped_run.node_run_id
        )
        runs.append(
            BranchRun(
                BranchKey(NodeId.N02),
                NodeRun(
                    schema_version=VERSION,
                    node_run_id=skipped_run.node_run_id,
                    execution_id=scope.execution_id,
                    node_id=NodeId.N02,
                    trace=skipped_run.trace,
                    config_ref=PolicyContentRef.model_validate(
                        skipped_run.node_config_ref.model_dump()
                    ),
                    upstream_inputs=(),
                    input_digest=record_digest(skipped_run),
                    status=NodeRunStatus.SKIPPED,
                    created_at=at,
                    ended_at=at,
                    reason=LifecycleReason(code=discovery.reason_code),
                    attempt_history=(),
                    output_refs=(),
                ),
            )
        )
    elif ctx is not None:
        n02 = discovery_input(fixture, ctx)
        discovered = discover(
            scope,
            n02,
            ctx.snapshot,
            ctx.gate.evaluation,
            at,
            hit_profiles=() if mode == "no-hits" else None,
            failed_profiles=(refs[-1],)
            if mode in ("discovery-failed", "partial-discovery")
            else (),
        )
        discovery = discovered.output
        save(n02.envelope, discovery.envelope)
        invocations.extend(discovered.invocations)
        if discovery.profile_hits and mode != "grounding-unreached":
            n03 = grounding_input(fixture, discovered)
            cases = ()
            if mode in ("mixed", "grounding-failed", "no-target"):
                cases = (
                    SyntheticGroundingCase(
                        profile_ref=refs[-1],
                        object_count=0,
                        disposition="failed" if mode != "no-target" else "complete",
                    ),
                )
            grounded = ground(scope, n03, discovered, at, cases=cases)
            save(n03.envelope, grounded.output.envelope)
            invocations.extend(grounded.invocations)
    stages = []
    facts = []
    selections = []
    observations = []
    tool_executions = []
    decision_blockers = []
    if grounded is not None:
        assert discovered is not None and ctx is not None
        for obj in grounded.objects:
            n04, observation = observation_case(fixture, ctx, obj, discovered)
            save(n04.envelope, observation.envelope, obj.object_id)
            n05, tools = tools_case(
                fixture,
                ctx,
                obj,
                outside=None if mode == "tool-failed" else mode != "negative",
            )
            save(n05.envelope, tools.output.envelope, obj.object_id)
            selected_facts, selected = authority_cases(fixture, obj, observation, tools)
            n06 = decision_input(fixture, ctx, obj, selected_facts, selected)
            facts.extend(selected_facts)
            selections.extend(selected)
            observations.append(observation.batch)
            tool_executions.extend(tools.executions)
            invocations.append(observation.invocation)
            if mode == "decision-failed":
                call = scope.check_call(n06.envelope, NodeId.N06, at)
                output = call.output(
                    (), synthetic_issue("fixture-failure", "Synthetic N06 failure")
                )
                save(n06.envelope, output, obj.object_id)
                decision_blockers.append(
                    ObjectDecisionBlocker(
                        object_ref=ObjectRef(
                            record_kind=K.OBJECT, record_id=obj.object_id
                        ),
                        node_run_ref=call.node_run_ref,
                        attempt_ref=call.attempt_ref,
                    )
                )
            else:
                stage = stage_decision(
                    scope,
                    n06,
                    ctx.snapshot,
                    ctx.gate,
                    at,
                    facts=selected_facts,
                    authorities=selected,
                )
                save(n06.envelope, stage.draft.output_envelope, obj.object_id)
                stages.append(stage)
                invocations.append(stage.invocation)
    execution = ExecutionRecord(
        schema_version=VERSION,
        execution_id=scope.execution_id,
        task_id=scope.task.task_id,
        pins=scope.pins,
        trace=scope.task.trace,
        status=ExecutionStatus.RUNNING,
        created_at=at,
        started_at=at,
        context_snapshot=ContextSnapshotPin(
            snapshot_ref=context_result.output.context_snapshot_ref,
            snapshot_digest=context_result.output.context_snapshot_digest,
        )
        if isinstance(context_result.output, SuccessfulContextBuild)
        else None,
        model_invocation_refs=tuple(
            ModelInvocationRef(
                record_kind=K.MODEL_INVOCATION, record_id=item.model_invocation_id
            )
            for item in invocations
        ),
    )
    state = PlannerState(
        execution,
        execution.pins.bundle_digest,
        tuple(plans),
        tuple(runs),
        context_output=context_result.output,
        eligibility=ctx.gate.evaluation if ctx else None,
        discovery_output=discovery,
        grounding_output=grounded.output if grounded else None,
        objects=grounded.objects if grounded else (),
        decision_drafts=tuple(item.draft for item in stages),
    )
    if mode == "grounding-unreached":
        interrupted = fixture.envelope(NodeId.N03)
        runs.append(
            BranchRun(
                BranchKey(NodeId.N03),
                NodeRun(
                    schema_version=VERSION,
                    node_run_id=interrupted.node_run_id,
                    execution_id=scope.execution_id,
                    node_id=NodeId.N03,
                    trace=interrupted.trace,
                    config_ref=PolicyContentRef.model_validate(
                        interrupted.node_config_ref.model_dump()
                    ),
                    upstream_inputs=(),
                    input_digest=record_digest(interrupted),
                    status=NodeRunStatus.BLOCKED,
                    created_at=at,
                    ended_at=at,
                    reason=LifecycleReason(code="fixture_failure"),
                    attempt_history=(),
                    output_refs=(),
                ),
            )
        )
        state = replace(state, runs=tuple(runs))
    for item in plan_work(state).uninvoked:
        envelope = fixture.envelope(item.key.node_id)
        runs.append(
            BranchRun(
                item.key,
                NodeRun(
                    schema_version=VERSION,
                    node_run_id=envelope.node_run_id,
                    execution_id=scope.execution_id,
                    node_id=item.key.node_id,
                    trace=envelope.trace,
                    config_ref=PolicyContentRef.model_validate(
                        envelope.node_config_ref.model_dump()
                    ),
                    upstream_inputs=(),
                    input_digest=record_digest(envelope),
                    status=item.status,
                    created_at=at,
                    ended_at=at,
                    reason=LifecycleReason(code=item.reason_code),
                    predecessor_ref=item.blocker_node_run_ref,
                    attempt_history=(),
                    output_refs=(),
                ),
            )
        )
    state = replace(state, runs=tuple(runs))
    sources = CoverageSources(
        execution=execution,
        context_output=context_result.output,
        context_snapshot=ctx.snapshot if ctx else None,
        discovery_output=discovery,
        discovery_input=n02,
        question_selections=discovered.selections if discovered else (),
        grounding_records=grounded.records if grounded else (),
        objects=state.objects,
        node_runs=tuple(item.run for item in state.runs),
        attempts=tuple(attempts),
        decision_blockers=tuple(decision_blockers),
    )
    if ctx is None:
        return Scenario(scope, state, sources, None, (), resolved, None)
    release = fixture.manager.get_release(execution.pins.release_id, scope=GLOBAL)
    assert release is not None
    runtime = RuntimeAuthorityRecords(
        execution=execution,
        release=release,
        manifest=resolved.files.manifest,
        eligibility=ctx.gate.evaluation,
        objects=sources.objects,
        node_runs=sources.node_runs,
        attempts=sources.attempts,
        question_selections=sources.question_selections,
        observations=tuple(observations),
        tool_executions=tuple(tool_executions),
        model_invocations=tuple(invocations),
        execution_events=(),
        facts=tuple(facts),
        authority_selections=tuple(selections),
    )
    return Scenario(scope, state, sources, ctx.gate, tuple(stages), resolved, runtime)
