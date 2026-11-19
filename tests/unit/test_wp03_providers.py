"""Behavior of the offline parking adapters with admitted synthetic Bundles."""

import socket
from pathlib import Path

import pytest

from jimu_runtime.domain.enums import DecisionResult, FactStatus
from jimu_runtime.domain.runtime.perception import GroundingOutcome
from jimu_runtime.domain.values import BooleanValue, StringValue
from jimu_runtime.workflow.providers import (
    SyntheticGroundingCase,
    discover,
    ground,
    skipped_discovery,
)
from tests.fixtures.wp02.releases.support import GLOBAL
from tests.fixtures.wp03.nodes.cases import (
    context,
    decision_case,
    discovery_input,
    grounding_input,
    observation_case,
    tools_case,
)
from tests.fixtures.wp03.nodes.scenario import parking_fixture


def test_two_profiles_three_objects_are_independent_and_deterministic(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixture = parking_fixture(tmp_path, second_profile=True)
    assert fixture.receipt.accepted
    assert (
        fixture.manager.active_activation(GLOBAL).release_ref.record_id
        == fixture.scope.task.release_id
    )

    def no_network(*args: object, **kwargs: object) -> None:
        pytest.fail("mock provider attempted network I/O")

    monkeypatch.setattr(socket, "socket", no_network)
    scope = fixture.scope
    at = scope.task.accepted_at
    ctx = context(fixture)
    first, second = ctx.gate.evaluation.eligible_profiles
    n02 = discovery_input(fixture, ctx)
    discovered = discover(scope, n02, ctx.snapshot, ctx.gate.evaluation, at)
    assert discovered == discover(scope, n02, ctx.snapshot, ctx.gate.evaluation, at)
    assert tuple(hit.profile_ref for hit in discovered.output.profile_hits) == (
        first,
        second,
    )
    n03 = grounding_input(fixture, discovered)
    split = (SyntheticGroundingCase(profile_ref=first, object_count=2),)
    grounded = ground(scope, n03, discovered, at, cases=split)
    assert grounded == ground(scope, n03, discovered, at, cases=split)
    assert tuple(obj.profile_ref for obj in grounded.objects) == (first, first, second)
    assert len({obj.object_id for obj in grounded.objects}) == 3

    decisions = []
    facts = set()
    invocations = set()
    for obj in grounded.objects:
        _, observation = observation_case(fixture, ctx, obj, discovered)
        _, tools = tools_case(fixture, ctx, obj)
        decision = decision_case(fixture, ctx, obj, observation, tools)
        assert observation.batch.object_ref.record_id == obj.object_id
        invocations.add(observation.invocation.model_invocation_id)
        assert tools.executions[0].object_ref.record_id == obj.object_id
        assert (
            decision.draft.candidate is not None
            and not decision.draft.candidate.categories
        )
        assert (
            decision.decision.explanation.evaluated_rules
            == decision.draft.inputs.mandatory_rule_paths
        )
        facts.update(fact.fact_id for fact in tools.facts)
        facts.update(
            item.primary_fact.fact_id for item in observation.batch.question_results
        )
        decisions.append(decision.decision)
    assert len(invocations) == 3
    assert len(facts) == 6
    assert {item.result for item in decisions} == {DecisionResult.VIOLATION}
    assert len({item.execution_coverage_ref for item in decisions}) == 1


def test_no_hits_empty_eligibility_and_no_targets_are_distinct(tmp_path: Path) -> None:
    normal = tmp_path / "normal"
    normal.mkdir()
    fixture = parking_fixture(normal)
    scope, ctx = fixture.scope, context(fixture)
    at = scope.task.accepted_at
    n02 = discovery_input(fixture, ctx)
    no_hits = discover(
        scope, n02, ctx.snapshot, ctx.gate.evaluation, at, hit_profiles=()
    )
    assert no_hits.output.outcome == "no_hits"
    assert len(no_hits.invocations) == 1
    assert no_hits.output.profile_hits == ()
    discovered = discover(scope, n02, ctx.snapshot, ctx.gate.evaluation, at)
    (profile,) = ctx.gate.evaluation.eligible_profiles
    result = ground(
        scope,
        grounding_input(fixture, discovered),
        discovered,
        at,
        cases=(SyntheticGroundingCase(profile_ref=profile, object_count=0),),
    )
    assert result.records[0].outcome is GroundingOutcome.NO_TARGET
    assert result.objects == ()

    empty = tmp_path / "excluded"
    empty.mkdir()
    fixture = parking_fixture(empty, excluded=True)
    ctx = context(fixture)
    assert ctx.gate.evaluation.eligible_profiles == ()
    assert (
        ctx.gate.evaluation.excluded_profiles
        == ctx.gate.evaluation.configured_profile_refs
    )
    skipped = skipped_discovery(fixture.scope, ctx.gate.evaluation, "skipped-discovery")
    assert skipped.status == "skipped"
    assert skipped.reason_code == "no_eligible_profiles"


@pytest.mark.parametrize("fault", ("question", "tool", "grounding", "discovery"))
def test_partial_and_failed_results_preserve_available_work(
    tmp_path: Path, fault: str
) -> None:
    fixture = parking_fixture(tmp_path, second_profile=True)
    scope, ctx = fixture.scope, context(fixture)
    at = scope.task.accepted_at
    first, second = ctx.gate.evaluation.eligible_profiles
    discovered = discover(
        scope,
        discovery_input(fixture, ctx),
        ctx.snapshot,
        ctx.gate.evaluation,
        at,
        failed_profiles=(second,) if fault == "discovery" else (),
    )
    assert discovered.output.outcome == ("partial" if fault == "discovery" else "hits")
    grounded = ground(
        scope,
        grounding_input(fixture, discovered),
        discovered,
        at,
        cases=(
            SyntheticGroundingCase(
                profile_ref=second, object_count=0, disposition="failed"
            ),
        )
        if fault == "grounding"
        else (),
    )
    assert grounded.objects[0].profile_ref == first
    expected = 1 if fault in {"grounding", "discovery"} else 2
    assert len(grounded.objects) == expected
    obj = grounded.objects[0]
    reading = (
        (StringValue(kind="string", value="invalid fixture Boolean"),)
        if fault == "question"
        else None
    )
    _, observation = observation_case(fixture, ctx, obj, discovered, readings=reading)
    _, tools = tools_case(fixture, ctx, obj, outside=None if fault == "tool" else True)
    if fault == "question":
        assert observation.batch.outcome == "partial"
        assert (
            observation.batch.question_results[0].primary_fact.status
            is FactStatus.ERROR
        )
        assert tools.output.summary.status == "succeeded"
    if fault == "tool":
        assert tools.output.summary.status == "failed"
        assert not tools.facts
    decision = decision_case(fixture, ctx, obj, observation, tools)
    expected_result = (
        DecisionResult.UNCERTAIN
        if fault in {"tool", "question"}
        else DecisionResult.VIOLATION
    )
    assert decision.decision.result is expected_result


def test_joint_observation_validates_questions_independently(tmp_path: Path) -> None:
    fixture = parking_fixture(tmp_path, second_question=True)
    ctx, scope = context(fixture), fixture.scope
    at = scope.task.accepted_at
    discovery = discover(
        scope, discovery_input(fixture, ctx), ctx.snapshot, ctx.gate.evaluation, at
    )
    (obj,) = ground(scope, grounding_input(fixture, discovery), discovery, at).objects
    readings = (
        StringValue(kind="string", value="invalid required answer"),
        BooleanValue(kind="bool", value=True),
    )
    n04, observation = observation_case(fixture, ctx, obj, discovery, readings=readings)
    assert observation.invocation.contract_snapshot == n04.contract_snapshot
    assert (
        observation.invocation.contract_snapshot.effective_question_refs
        == obj.effective_question_refs
    )
    assert observation.batch.outcome == "partial"
    assert tuple(
        result.primary_fact.status for result in observation.batch.question_results
    ) == (
        FactStatus.ERROR,
        FactStatus.KNOWN,
    )
    assert (
        n04.contract_snapshot.response_schema_sha256
        == observation.evidence[1].record.sha256
    )
    assert {
        item.primary_fact.source.model_invocation_ref
        for item in observation.batch.question_results
    } == {observation.batch.model_invocation_ref}

    _, tools = tools_case(fixture, ctx, obj, outside=False)
    # False geometry cannot turn blocked required observation into a negative Decision.
    assert (
        decision_case(fixture, ctx, obj, observation, tools).decision.result
        is DecisionResult.UNCERTAIN
    )
