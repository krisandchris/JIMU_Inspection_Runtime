"""Finite reference success remains distinct from deferred operand typing/truth."""

from copy import deepcopy

import pytest
from fixtures.wp01b.x4.support import applicability, bind, codes, literal, setup

from jimu_runtime.domain.definition_support import ContextFieldSelector


@pytest.mark.parametrize("ancestor", ["first", "second"])
def test_local_strict_ancestor_projection_does_not_mutate_policy(ancestor):
    data, ctx, expr = setup()
    expr["selector"]["step_id"] = ancestor
    before = deepcopy(expr)
    result = bind(data, ctx, expr)
    assert result.report.aggregate_status == "deferred", result.report.model_dump_json()
    assert any(
        c.check_id == "condition.references" and c.status == "passed"
        for c in result.report.checks
    )
    selector = result.selectors_for_matching[0]
    assert (
        selector.producer_selector.tool_plan_ref.model_dump(mode="json")
        == ctx["tool_plan_ref"]
    )
    assert selector.producer_selector.step_id == ancestor
    assert expr == before and "tool_plan_ref" not in expr["selector"]
    assert "condition.deferred.operand_types_and_truth" in codes(result)


@pytest.mark.parametrize("value", [True, False])
def test_constant_applicability_keeps_declared_context_obligations(value):
    data, ctx, _ = setup()
    ctx = applicability(ctx)
    selector = ContextFieldSelector.model_validate(
        {
            "source": "context_snapshot",
            "schema_ref": ctx["context_registrations"][0]["schema_ref"],
            "field_path": ["scene"],
        }
    )
    result = bind(data, ctx, literal(value), (selector,))
    assert result.report.aggregate_status == "deferred"
    assert result.selectors_for_matching == ()


def test_question_forward_exact_reference_remains_data_for_manifest_activation():
    data, ctx, _ = setup()
    ctx.update(
        use_role="producer_activation",
        producer_catalogue_ref=dict(
            artifact_kind="fact_producer_catalog",
            artifact_id="catalogue",
            version="1.0.0",
            digest="a" * 64,
        ),
        declaration_id="question",
        producer_kind="vlm_question",
        producer_scope="object",
    )
    ctx.pop("policy_ref")
    ctx.pop("tool_plan_ref")
    ctx.pop("step_id")
    selector = dict(
        scope="object",
        fact_key="primary",
        fact_schema_ref=data["fact_schemas"][0]["ref"],
        field_path=[],
        producer_selector=dict(producer_kind="tool"),
    )
    from fixtures.wp01b.v1.support import entry

    selector["producer_selector"].update(
        tool_plan_ref=entry(data, "tool_plan")["ref"],
        step_id="first",
        tool_ref=entry(data, "tool")["ref"],
        output_name="signal",
    )
    result = bind(data, ctx, dict(kind="fact_ref", selector=selector))
    assert result.report.aggregate_status == "deferred", result.report.model_dump_json()
    assert len(result.selectors_for_matching) == 1


def test_local_policy_reuse_resolves_each_actual_plan_without_digest_writeback():
    from fixtures.wp01b.v1.support import entry

    data, ctx, expr = setup()
    original = deepcopy(expr)
    first = bind(data, ctx, expr)
    new_plan = entry(data, "tool_plan")
    new_plan["ref"]["artifact_id"] = "another-plan"
    new_plan["definition"]["tool_plan_id"] = "another-plan"
    new_plan["ref"]["digest"] = "b" * 64
    entry(data, "profile")["definition"]["tool_plan_ref"] = deepcopy(new_plan["ref"])
    ctx["tool_plan_ref"] = deepcopy(new_plan["ref"])
    second = bind(data, ctx, expr)
    assert first.report.aggregate_status == second.report.aggregate_status == "deferred"
    assert (
        first.selectors_for_matching[0].producer_selector.tool_plan_ref
        != second.selectors_for_matching[0].producer_selector.tool_plan_ref
    )
    assert expr == original
