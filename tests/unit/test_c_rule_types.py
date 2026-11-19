"""Type inference on actual finite definitions in the three condition domains."""

import pytest
from fixtures.wp01c.rule_types.support import (
    Example,
    compare,
    literal,
    membership,
    unit,
)

from jimu_runtime.domain.definition_support import ContextFieldSelector
from jimu_runtime.domain.rules import PassedTypeCheck


def passed(result):
    assert isinstance(result, PassedTypeCheck), result
    assert result.inferred_type.kind == "bool"


@pytest.mark.parametrize("operator", ["eq", "ne", "lt", "le", "gt", "ge"])
def test_exact_numeric_kind_and_pinned_units(operator):
    example = Example()
    shape = {"kind": "integer"}
    left = example.question_fact("left", shape=shape, units=[((), unit())])
    right = example.question_fact("right", shape=shape, units=[((), unit())])
    passed(example.check(example.rule(compare(left, right, operator))))


def test_declared_literal_units_and_optional_record_fields():
    example = Example()
    distance = example.question_fact(
        shape={
            "kind": "record",
            "fields": [
                {
                    "name": "distance",
                    "required": False,
                    "type_definition": {"kind": "number"},
                },
            ],
        },
        path=("distance",),
        units=[(("distance",), unit())],
    )
    example.units = [{"path": ("right",), "unit": unit()}]
    rule = example.rule(compare(distance, literal("number", 3.0), "le"))
    passed(example.check(rule))


def test_literal_enum_and_membership_bind_to_exact_declared_enum():
    example = Example()
    members = ["parked", "moving", "blocked"]
    fact = example.question_fact(shape={"kind": "enum", "members": members})
    scalar = literal("enum", "parked", members=list(reversed(members)))
    options = literal(
        "list",
        [{"kind": "enum", "value": "parked"}, {"kind": "enum", "value": "blocked"}],
        item_type={"kind": "enum", "members": members},
    )
    expr = {
        "kind": "all",
        "arguments": [
            compare(fact, scalar),
            {
                "kind": "any",
                "arguments": [
                    membership(fact, options),
                    {"kind": "not", "argument": literal()},
                ],
            },
        ],
    }
    passed(example.check(example.rule(expr)))


def test_membership_with_bounded_nonempty_context_list():
    example = Example()
    collection = example.context_field(
        shape={
            "kind": "list",
            "item_type": {"kind": "integer"},
            "min_items": 1,
            "max_items": 64,
        },
    )
    passed(example.check(example.rule(membership(literal("integer", 4), collection))))


def test_applicability_uses_only_allowlisted_pinned_context():
    example = Example()
    ctx = example.context_field(role="region_metadata")
    allowlist = (ContextFieldSelector.model_validate(ctx["selector"]),)
    passed(
        example.check(
            compare(ctx, literal("string", "road")),
            "applicability",
            context_allowlist=allowlist,
        )
    )


def test_tool_guard_reads_local_transitive_ancestor_and_n04_question():
    example = Example()
    example.plan_chain()
    local = example.local_fact()
    question = example.question_fact()
    expr = {"kind": "all", "arguments": [local, question]}
    passed(example.check(expr, "tool_step_guard"))


def test_tool_producer_activation_uses_matching_actual_declaration():
    example = Example()
    example.plan_chain()
    local = example.local_fact()
    example.tool_fact("activation", step="third", activation=local)
    context = example.context("producer_activation", declaration="activation")
    passed(example.check(local, "producer_activation", context=context))


def test_system_fact_requires_explicit_prior_precondition_for_tool_guard():
    example = Example()
    example.plan_chain()
    system = example.system_fact()
    example.available.append("system.test")
    passed(example.check(system, "tool_step_guard"))
    passed(example.check(example.rule(system)))


def test_true_guard_remains_an_explicit_boolean_type_only():
    example = Example()
    example.plan_chain()
    passed(example.check(True, "tool_step_guard"))


def test_context_and_literal_have_an_explicit_shared_unit():
    example = Example()
    field = example.context_field(shape={"kind": "number"})
    example.context_units = [{"selector": field["selector"], "unit": unit()}]
    example.units = [{"path": ("right",), "unit": unit()}]
    rule = example.rule(compare(field, literal("number", 1.5), "lt"))
    passed(example.check(rule))


def test_the_same_pinned_enum_can_be_compared_across_repeated_selectors():
    example = Example()
    fact = example.question_fact(shape={"kind": "enum", "members": ["parked"]})
    passed(example.check(example.rule(compare(fact, fact, "ne"))))


def test_explicit_optional_dependency_uses_the_same_exact_binding():
    example = Example()
    fact = example.question_fact()
    required_rule = example.rule(fact)
    optional_rule = example.rule(fact, optional=required_rule.required_fact_selectors)
    passed(example.check(optional_rule))


def test_actual_applicability_policy_supplies_its_own_allowlist():
    example = Example()
    example.policies = [
        item
        for item in example.data["input"]["policies"]
        if item["ref"] == example.profile["definition"]["applicability_policy_ref"]
    ]
    condition = example.policies[0]["artifact"]["policy"]["condition_ast"]
    passed(example.check(condition, "applicability"))
