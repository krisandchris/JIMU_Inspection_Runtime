"""Synthetic node boundaries reject foreign pins and invalid context."""

from pathlib import Path

import pytest

from jimu_runtime.domain.nodes.context import FailedContextBuild
from jimu_runtime.domain.values import IntegerValue, RecordValue
from jimu_runtime.workflow.providers import (
    build_context,
    discover,
    ground,
    observe,
    stage_decision,
)
from tests.fixtures.wp03.nodes.cases import (
    authority_cases,
    context,
    decision_input,
    discovery_input,
    grounding_input,
    observation_case,
    tools_case,
)
from tests.fixtures.wp03.nodes.scenario import parking_fixture


@pytest.mark.parametrize(
    "fields", ({}, {"zone": IntegerValue(kind="integer", value=2)})
)
def test_n01_required_context_failure_has_no_snapshot(
    tmp_path: Path, fields: dict
) -> None:
    fixture = parking_fixture(
        tmp_path, metadata=RecordValue(kind="record", value=fields)
    )
    output = build_context(
        fixture.scope, fixture.context_input(), fixture.scope.task.accepted_at
    )
    assert isinstance(output.output, FailedContextBuild)
    assert output.snapshot is None
    assert not output.artifacts
    assert len(output.output.validation_report.required_issues) == 1


def test_discovery_rejects_foreign_execution_and_mismatched_digest(
    tmp_path: Path,
) -> None:
    fixture = parking_fixture(tmp_path)
    ctx, scope = context(fixture), fixture.scope
    at = scope.task.accepted_at
    inputs = discovery_input(fixture, ctx)
    foreign = inputs.model_copy(
        update={
            "envelope": inputs.envelope.model_copy(
                update={"execution_id": "foreign-execution"}
            )
        }
    )
    with pytest.raises(ValueError, match="foreign execution"):
        discover(scope, foreign, ctx.snapshot, ctx.gate.evaluation, at)
    corrupted = inputs.model_copy(update={"context_snapshot_digest": "0" * 64})
    with pytest.raises(ValueError, match="exact image and Snapshot"):
        discover(scope, corrupted, ctx.snapshot, ctx.gate.evaluation, at)
    artifact_pin = inputs.envelope.upstream_artifacts[0].model_copy(
        update={"digest": "0" * 64}
    )
    corrupted_envelope = inputs.envelope.model_copy(
        update={
            "upstream_artifacts": (
                artifact_pin,
                *inputs.envelope.upstream_artifacts[1:],
            )
        }
    )
    with pytest.raises(ValueError, match="exact upstream artifact"):
        discover(
            scope,
            inputs.model_copy(update={"envelope": corrupted_envelope}),
            ctx.snapshot,
            ctx.gate.evaluation,
            at,
        )


def test_n03_requires_all_hit_profiles_and_n04_cannot_share_selection(
    tmp_path: Path,
) -> None:
    fixture = parking_fixture(tmp_path, second_profile=True)
    ctx, scope = context(fixture), fixture.scope
    at = scope.task.accepted_at
    discovered = discover(
        scope, discovery_input(fixture, ctx), ctx.snapshot, ctx.gate.evaluation, at
    )
    inputs = grounding_input(fixture, discovered)
    with pytest.raises(ValueError, match="exactly the accepted N02"):
        ground(
            scope,
            inputs.model_copy(update={"profiles": inputs.profiles[:1]}),
            discovered,
            at,
        )
    grounded = ground(scope, inputs, discovered, at)
    first, _ = grounded.objects
    n04, _ = observation_case(fixture, ctx, first, discovered)
    with pytest.raises(ValueError, match="Question selection must match"):
        observe(scope, n04, ctx.snapshot, discovered.selections[1], at)


def test_n06_foreign_fact_authority_and_elided_mandatory_rule_are_rejected(
    tmp_path: Path,
) -> None:
    fixture = parking_fixture(tmp_path, second_profile=True)
    ctx, scope = context(fixture), fixture.scope
    at = scope.task.accepted_at
    discovered = discover(
        scope, discovery_input(fixture, ctx), ctx.snapshot, ctx.gate.evaluation, at
    )
    first, second = ground(
        scope, grounding_input(fixture, discovered), discovered, at
    ).objects
    _, observation = observation_case(fixture, ctx, first, discovered)
    _, tools = tools_case(fixture, ctx, first)
    facts, authority = authority_cases(fixture, first, observation, tools)
    n06 = decision_input(fixture, ctx, first, facts, authority)
    with pytest.raises(ValueError, match="mandatory scan"):
        stage_decision(
            scope,
            n06.model_copy(update={"mandatory_rule_paths": ()}),
            ctx.snapshot,
            ctx.gate,
            at,
            facts=facts,
            authorities=authority,
        )
    foreign = authority[0].model_copy(update={"object_id": second.object_id})
    with pytest.raises(ValueError, match="foreign Object"):
        stage_decision(
            scope,
            n06,
            ctx.snapshot,
            ctx.gate,
            at,
            facts=facts,
            authorities=(foreign, *authority[1:]),
        )
