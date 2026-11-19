from copy import deepcopy
from dataclasses import replace

import pytest
from fixtures.wp01c.applicability.cases import wire_case
from fixtures.wp01c.decisions.cases import profile_rules
from fixtures.wp01c.nodes.cases import (
    AT,
    ISSUE,
    PIN,
    cases,
    content,
    load,
    parse,
    ref,
    root,
)

from jimu_runtime.domain.definitions.capabilities import ProfileDefinition
from jimu_runtime.domain.definitions.tools import ToolPlanDefinition, ToolStep
from jimu_runtime.domain.enums import NodeRunStatus
from jimu_runtime.domain.nodes.context import ContextBuildOutput
from jimu_runtime.domain.nodes.discovery import DiscoveryOutput
from jimu_runtime.domain.nodes.grounding import GroundingOutput
from jimu_runtime.domain.policies import ToolSkipPolicy
from jimu_runtime.domain.runtime.applicability import ProfileEligibilityEvaluation
from jimu_runtime.domain.runtime.coverage import CoverageReason
from jimu_runtime.domain.runtime.executions import ExecutionRecord, NodeId, NodeRun
from jimu_runtime.domain.runtime.perception import (
    DiscoveryOutcome,
    GroundingOutcome,
    ObjectRecord,
)
from jimu_runtime.domain.validation.decision_coverage import ProfileRuleScope
from jimu_runtime.workflow.engine import (
    BranchKey,
    BranchRun,
    DecisionWork,
    DiscoveryWork,
    GroundingWork,
    PlannerState,
    ProfilePlan,
    ToolWork,
    plan_work,
)


def branch(node, status, *, envelope=None, object_id=None, digest=PIN, reason=None):
    started = status in {"running", "succeeded", "failed"}
    settled = status in {"succeeded", "failed", "blocked", "skipped"}
    run_id = node.lower() if object_id is None else f"{node.lower()}:{object_id}"
    if envelope is not None:
        run_id = envelope["node_run_id"]
    outputs = [item["ref"] for item in envelope["output_artifacts"]] if envelope else []
    if status == "succeeded" and not outputs:
        outputs = [ref("evidence", f"{run_id}:fixture")]
    payload = root(
        node_run_id=run_id,
        execution_id="execution-1",
        node_id=node,
        trace={"trace_id": "trace-1"},
        config_ref=content("policy", "workflow"),
        upstream_inputs=[{"ref": ref("object", object_id), "digest": digest}]
        if object_id
        else [],
        input_digest=PIN,
        status=status,
        created_at=AT,
        started_at=AT if started else None,
        ended_at=AT if settled else None,
        reason={"code": reason} if reason else None,
        attempt_history=[
            {
                "attempt_ref": ref(
                    "attempt",
                    envelope["attempt_id"] if envelope else f"{run_id}:attempt",
                ),
                "attempt_number": 1,
            }
        ]
        if started
        else [],
        output_refs=outputs,
    )
    return BranchRun(BranchKey(NodeId(node), object_id), parse(NodeRun, payload))


@pytest.fixture
def scenario():
    wire = cases()
    scope, _ = wire_case("H05", object_scope=True)
    refs = wire["eligibility"]["configured_profile_refs"]
    rules = profile_rules(scope, needs_fact=False)
    rules["profile_ref"] = refs[0]
    wire["profile"]["rule_set_refs"] = [
        item["rule_set_ref"] for item in rules["rule_sets"]
    ]
    profile = ProfilePlan(
        parse(ProfileDefinition, wire["profile"]),
        parse(ProfileRuleScope, rules),
    )
    execution = load("wp01c/executions/contracts.json")["execution"]
    execution.update(
        execution_id="execution-1", status="running", created_at=AT, started_at=AT
    )
    execution["pins"]["bundle_digest"] = wire["eligibility"]["bundle_digest"]
    wire["eligibility"]["context_snapshot_ref"] = wire["context_output"][
        "context_snapshot_ref"
    ]
    execution["context_snapshot"] = dict(
        snapshot_ref=wire["context_output"]["context_snapshot_ref"],
        snapshot_digest=wire["context_output"]["context_snapshot_digest"],
    )
    state = PlannerState(
        parse(ExecutionRecord, execution),
        execution["pins"]["bundle_digest"],
        (profile,),
        runs=(branch("N01", "succeeded", envelope=wire["context_output"]["envelope"]),),
        context_output=parse(ContextBuildOutput, wire["context_output"]),
        eligibility=parse(ProfileEligibilityEvaluation, wire["eligibility"]),
    )
    return wire, state


def with_discovery(state, payload):
    node_status = payload["envelope"]["status"]
    return replace(
        state,
        discovery_output=parse(DiscoveryOutput, payload),
        runs=(*state.runs, branch("N02", node_status, envelope=payload["envelope"])),
    )


def with_grounding(state, payload, objects):
    payload = deepcopy(payload)
    payload["envelope"]["output_artifacts"] = [
        {"ref": ref("object", obj["object_id"]), "digest": PIN} for obj in objects
    ] + [
        {"ref": result["grounding_record_ref"], "digest": PIN}
        for result in payload["profile_results"]
    ]
    return replace(
        state,
        grounding_output=parse(GroundingOutput, payload),
        runs=(*state.runs, branch("N03", "succeeded", envelope=payload["envelope"])),
        objects=tuple(parse(ObjectRecord, obj) for obj in objects),
    )


def test_gate_is_attempt_free_and_pins_eligibility(scenario):
    _, state = scenario
    first = plan_work(replace(state, eligibility=None))
    assert first.eligibility is not None
    assert first.eligibility.context == state.execution.context_snapshot
    assert first.ready == ()
    after = plan_work(state)
    assert after.eligibility is None
    (item,) = after.ready
    assert item.key == BranchKey(NodeId.N02)
    assert isinstance(item.inputs, DiscoveryWork)
    assert item.inputs.eligibility == state.eligibility


def test_single_grounding_inherits_full_hit_order_then_fans_out(scenario):
    wire, state = scenario
    (first,) = state.profiles
    second_ref = dict(
        wire["eligibility"]["configured_profile_refs"][0], artifact_id="second"
    )
    second_profile = dict(wire["profile"], profile_id="second")
    second_scope = first.rules.model_dump(mode="json") | {"profile_ref": second_ref}
    gate = wire["eligibility"]
    gate["configured_profile_refs"].append(second_ref)
    gate["eligible_profiles"].append(second_ref)
    gate["applicability_traces"].append(ref("applicability_trace", "profile-trace-2"))
    hits = wire["discovery_output"]["profile_hits"]
    hits.append(dict(hits[0], profile_ref=second_ref))
    state = replace(
        state,
        profiles=(
            *state.profiles,
            ProfilePlan(
                parse(ProfileDefinition, second_profile),
                parse(ProfileRuleScope, second_scope),
            ),
        ),
        eligibility=parse(ProfileEligibilityEvaluation, gate),
    )
    state = with_discovery(state, wire["discovery_output"])
    (work,) = plan_work(state).ready
    assert work.key == BranchKey(NodeId.N03)
    assert isinstance(work.inputs, GroundingWork)
    assert (
        tuple(item.hit for item in work.inputs.profiles)
        == state.discovery_output.profile_hits
    )
    obj = wire["object_record"]
    second_obj = dict(
        obj,
        object_id="object-second",
        profile_ref=second_ref,
        grounding_record_ref=ref("grounding_record", "grounding-second"),
    )
    wire["grounding_output"]["profile_results"].append(
        dict(
            wire["grounding_output"]["profile_results"][0],
            profile_ref=second_ref,
            object_refs=[ref("object", second_obj["object_id"])],
            grounding_record_ref=second_obj["grounding_record_ref"],
        )
    )
    state = with_grounding(state, wire["grounding_output"], [second_obj, obj])
    output = plan_work(state)
    assert tuple(work.key for work in output.ready) == (
        BranchKey(NodeId.N04, obj["object_id"]),
        BranchKey(NodeId.N04, second_obj["object_id"]),
    )
    assert output == plan_work(
        replace(state, runs=state.runs[::-1], objects=state.objects[::-1])
    )

    first_observation = branch(
        "N04", "failed", object_id=obj["object_id"], reason="provider_failed"
    )
    second_observation = branch("N04", "ready", object_id=second_obj["object_id"])
    state = replace(state, runs=(*state.runs, first_observation, second_observation))
    output = plan_work(state)
    (ready,) = output.ready
    assert ready.key == BranchKey(NodeId.N05, obj["object_id"])
    assert not output.coverage.ready_to_finalize


@pytest.mark.parametrize(
    "variant",
    ("independent", "dependent", "mixed", "excluded", "condition", "transitive"),
)
def test_tool_first_frontier_uses_effective_question_dependencies(scenario, variant):
    wire, state = scenario
    state = with_discovery(state, wire["discovery_output"])
    state = with_grounding(state, wire["grounding_output"], [wire["object_record"]])
    obj = state.objects[0]
    raw_plan = wire["tool_plan"]
    raw_root = deepcopy(raw_plan["steps"][0])
    fact_step = deepcopy(raw_root)
    fact_step["step_id"] = "fact"
    question_ref = obj.effective_question_refs[0].model_dump(mode="json")
    if variant == "excluded":
        question_ref["artifact_id"] = "excluded"
    fact_step["input_bindings"][0]["source"] = {
        "source_kind": "fact",
        "selector": {
            "scope": "object",
            "fact_key": "classification",
            "fact_schema_ref": content("fact_schema", "classification"),
            "producer_selector": {
                "producer_kind": "vlm_question",
                "question_ref": question_ref,
                "output_name": "primary",
            },
            "field_path": [],
        },
    }
    skip_policies = ()
    if variant == "condition":
        policy = parse(
            ToolSkipPolicy,
            {
                "policy_kind": "tool_skip",
                "condition_ast": {
                    "kind": "fact_ref",
                    "selector": fact_step["input_bindings"][0]["source"]["selector"],
                },
                "on_false": "skip_with_reason",
                "on_unknown": "block",
                "on_missing_input": "block",
            },
        )
        skip_policies = ((parse(ToolStep, raw_root).skip_policy_ref, policy),)
        raw_plan["steps"] = [raw_root]
    elif variant in {"mixed", "transitive"}:
        if variant == "transitive":
            raw_root["depends_on"] = ["fact"]
        raw_plan["steps"] = [raw_root, fact_step]
    elif variant == "independent":
        raw_plan["steps"] = [raw_root]
    else:
        raw_plan["steps"] = [fact_step]
    plan_ref = state.profiles[0].definition.tool_plan_ref
    state = replace(
        state,
        tool_plans=((plan_ref, parse(ToolPlanDefinition, raw_plan)),),
        tool_skip_policies=skip_policies,
    )
    observation_key = BranchKey(NodeId.N04, obj.object_id)
    tools_key = BranchKey(NodeId.N05, obj.object_id)
    work = plan_work(state)
    if variant in {"dependent", "condition", "transitive"}:
        assert tuple(item.key for item in work.ready) == (observation_key,)
    else:
        assert tuple(item.key for item in work.ready) == (observation_key, tools_key)
        tool_work = work.ready[1].inputs
        assert isinstance(tool_work, ToolWork)
        assert tool_work.observation_node is None
        assert tool_work.needs_observation is (variant == "mixed")
        # Even a completed independent ToolPlan cannot release Decision by itself.
        finished_tools = branch("N05", "succeeded", object_id=obj.object_id)
        tool_first = replace(state, runs=(*state.runs, finished_tools))
        assert {item.key for item in plan_work(tool_first).ready} == {observation_key}
        assert not plan_work(tool_first).coverage.ready_to_finalize

    observation = branch(
        "N04", "failed", object_id=obj.object_id, reason="provider_failed"
    )
    state = replace(state, runs=(*state.runs, observation))
    (tool_work,) = plan_work(state).ready
    assert tool_work.key == tools_key
    assert isinstance(tool_work.inputs, ToolWork)
    assert tool_work.inputs.observation_node == observation.run


def test_tool_failure_releases_full_mandatory_decision_scan(scenario):
    wire, state = scenario
    state = with_discovery(state, wire["discovery_output"])
    state = with_grounding(state, wire["grounding_output"], [wire["object_record"]])
    object_id = state.objects[0].object_id
    observation = branch("N04", "failed", object_id=object_id, reason="timed_out")
    state = replace(state, runs=(*state.runs, observation))
    (next_work,) = plan_work(state).ready
    assert next_work.key == BranchKey(NodeId.N05, object_id)
    assert isinstance(next_work.inputs, ToolWork)
    assert next_work.inputs.tool_plan_ref == state.profiles[0].definition.tool_plan_ref
    tools = branch("N05", "failed", object_id=object_id, reason="tool_failed")
    state = replace(state, runs=(*state.runs, tools))
    plan = plan_work(state)
    (next_work,) = plan.ready
    assert next_work.key == BranchKey(NodeId.N06, object_id)
    assert isinstance(next_work.inputs, DecisionWork)
    assert (
        next_work.inputs.mandatory_rule_paths == state.profiles[0].mandatory_rule_paths
    )
    assert len(next_work.inputs.mandatory_rule_paths) == 1
    assert CoverageReason.REQUIRED_EVIDENCE_BLOCKED in plan.coverage.objects[0].reasons
    assert not plan.coverage.ready_to_finalize
    decision = branch("N06", "failed", object_id=object_id, reason="decision_failed")
    settled = plan_work(replace(state, runs=(*state.runs, decision)))
    assert settled.coverage.ready_to_finalize
    assert CoverageReason.DECISION_NOT_EVALUATED in settled.coverage.objects[0].reasons


@pytest.mark.parametrize("unresolved", [False, True])
def test_empty_eligible_skips_without_fabricating_no_hits(scenario, unresolved):
    wire, state = scenario
    raw_gate = wire["eligibility"]
    (ref,) = raw_gate["eligible_profiles"]
    raw_gate["eligible_profiles"] = []
    raw_gate["unresolved_profiles" if unresolved else "excluded_profiles"] = [ref]
    state = replace(state, eligibility=parse(ProfileEligibilityEvaluation, raw_gate))
    plan = plan_work(state)
    assert plan.coverage.discovery_outcome is DiscoveryOutcome.NOT_EVALUATED
    assert not plan.ready
    (item,) = plan.uninvoked
    assert item.key == BranchKey(NodeId.N02)
    assert item.status is NodeRunStatus.SKIPPED
    skip = branch("N02", "skipped", reason=item.reason_code)
    assert not skip.run.attempt_history
    state = replace(
        state,
        runs=(*state.runs, skip),
        discovery_output=parse(DiscoveryOutput, wire["skipped_discovery"]),
    )
    plan = plan_work(state)
    assert len(plan.uninvoked) == 4
    assert all(item.status is NodeRunStatus.SKIPPED for item in plan.uninvoked)
    assert (
        CoverageReason.APPLICABILITY_UNRESOLVED in plan.coverage.reasons
    ) is unresolved


def test_actual_no_hits_has_attempt_and_uncovered_profile(scenario):
    wire, state = scenario
    payload = dict(wire["discovery_output"], outcome="no_hits", profile_hits=[])
    state = with_discovery(state, payload)
    planned = plan_work(state)
    assert planned.coverage.discovery_outcome is DiscoveryOutcome.NO_HITS
    assert state.runs[-1].run.attempt_history
    assert CoverageReason.PROFILE_NOT_DISCOVERED in planned.coverage.profiles[0].reasons
    assert len(planned.uninvoked) == 4


@pytest.mark.parametrize("outcome", ["no_target", "failed", "partial"])
def test_grounding_outcomes_preserved_when_no_objects(scenario, outcome):
    wire, state = scenario
    state = with_discovery(state, wire["discovery_output"])
    result = wire["grounding_output"]["profile_results"][0]
    result.update(
        outcome=outcome,
        object_refs=[],
        validation_issues=[] if outcome == "no_target" else [ISSUE],
    )
    state = with_grounding(state, wire["grounding_output"], [])
    plan = plan_work(state)
    assert plan.coverage.profiles[0].grounding_outcome is GroundingOutcome(outcome)
    assert len(plan.uninvoked) == 3
    assert all(
        item.status
        is (NodeRunStatus.SKIPPED if outcome == "no_target" else NodeRunStatus.BLOCKED)
        for item in plan.uninvoked
    )
    assert not plan.ready


def test_wrong_branch_artifact_digest_rejects_successor(scenario):
    wire, state = scenario
    state = with_discovery(state, wire["discovery_output"])
    state = with_grounding(state, wire["grounding_output"], [wire["object_record"]])
    wrong = branch(
        "N04", "ready", object_id=state.objects[0].object_id, digest="b" * 64
    )
    with pytest.raises(ValueError, match="authoritative N03"):
        plan_work(replace(state, runs=(*state.runs, wrong)))


def test_cross_execution_gate_rejected(scenario):
    _, state = scenario
    bad_gate = state.eligibility.model_copy(update={"execution_id": "another-exec"})
    with pytest.raises(ValueError, match="ContextSnapshot and Profile scope"):
        plan_work(replace(state, eligibility=bad_gate))


def test_context_failure_blocks_dependents_without_new_attempts(scenario):
    wire, state = scenario
    failure = wire["context_failure"]
    state = replace(
        state,
        execution=state.execution.model_copy(update={"context_snapshot": None}),
        runs=(branch("N01", "failed", envelope=failure["envelope"]),),
        context_output=parse(ContextBuildOutput, failure),
        eligibility=None,
    )
    output = plan_work(state)
    assert output.eligibility is None
    assert not output.ready
    assert len(output.uninvoked) == 5
    assert all(
        item.status is NodeRunStatus.BLOCKED
        and item.reason_code == "context_build_blocked"
        and item.blocker_node_run_ref.record_id == "n01"
        for item in output.uninvoked
    )
    skipped_runs = tuple(
        branch(item.key.node_id.value, item.status.value, reason=item.reason_code)
        for item in output.uninvoked
    )
    assert not any(entry.run.attempt_history for entry in skipped_runs)
    output = plan_work(replace(state, runs=(*state.runs, *skipped_runs)))
    assert output.coverage.ready_to_finalize


def test_omitted_or_extra_successors_are_not_inferred_from_grounding(scenario):
    wire, state = scenario
    state = with_discovery(state, wire["discovery_output"])
    state = with_grounding(state, wire["grounding_output"], [wire["object_record"]])
    object_id = state.objects[0].object_id
    extra = branch("N05", "ready", object_id=object_id)
    with pytest.raises(ValueError, match="selected predecessors"):
        plan_work(replace(state, runs=(*state.runs, extra)))
    duplicate = branch("N04", "ready", object_id=object_id)
    with pytest.raises(ValueError, match="multiple logical branches"):
        plan_work(replace(state, runs=(*state.runs, duplicate, duplicate)))


@pytest.mark.parametrize("reason", ["lease_expired", "attempt_deadline_elapsed"])
def test_interrupted_discovery_without_provider_output_settles(scenario, reason):
    _, state = scenario
    interrupted = branch("N02", "failed", reason=reason)
    output = plan_work(replace(state, runs=(*state.runs, interrupted)))
    assert output.coverage.discovery_outcome is DiscoveryOutcome.NOT_EVALUATED
    assert output.coverage.discovery is None
    assert CoverageReason.DISCOVERY_NOT_EVALUATED in output.coverage.reasons
    assert all(item.status is NodeRunStatus.BLOCKED for item in output.uninvoked)
    assert all(
        item.blocker_node_run_ref.record_id == interrupted.run.node_run_id
        for item in output.uninvoked
    )


def test_discovery_failure_other_than_scheduler_interruption_requires_output(scenario):
    _, state = scenario
    failed = branch("N02", "failed", reason="invalid_model_output")
    with pytest.raises(ValueError, match="retain its typed output"):
        plan_work(replace(state, runs=(*state.runs, failed)))


def test_selected_output_ref_requires_same_kind_and_id(scenario):
    wire, state = scenario
    typed = state.context_output.context_snapshot_ref
    first = state.runs[0]
    assert type(typed) is not type(first.run.output_refs[0])
    run = first.run.model_copy(update={"output_refs": (typed,)})
    assert plan_work(replace(state, runs=(replace(first, run=run),))).ready

    bad = deepcopy(wire["context_output"]["envelope"])
    bad["output_artifacts"][0]["ref"]["record_kind"] = "evidence"
    mismatched = branch("N01", "succeeded", envelope=bad)
    with pytest.raises(ValueError, match="terminal NodeRun and Attempt"):
        plan_work(replace(state, runs=(mismatched,)))
