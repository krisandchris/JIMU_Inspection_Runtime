"""Fail-closed Rule, applicability and Tool static type diagnostics."""

from copy import deepcopy

import pytest
from fixtures.wp01c.rule_types.support import (
    Example,
    compare,
    literal,
    membership,
    ref,
    unit,
)

from jimu_runtime.domain.rules import FailedTypeCheck


def problem(result, code, path=None, status="invalid"):
    assert isinstance(result, FailedTypeCheck)
    assert result.status == status
    issue = next((issue for issue in result.issues if issue.code == code), None)
    assert issue is not None, result
    if path is not None:
        assert issue.path == path


@pytest.mark.parametrize(
    "right,options,code",
    [
        (literal("integer", 2), {}, "rule_type.unit_mismatch"),
        (literal("number", 2.0), {}, "rule_type.kind_mismatch"),
        (literal("integer", 2), {"member": "cm"}, "rule_type.unit_mismatch"),
    ],
)
def test_no_numeric_widening_or_unit_inference(right, options, code):
    example = Example()
    left = example.question_fact(shape={"kind": "integer"}, units=[((), unit())])
    if options:
        example.units = [{"path": ("right",), "unit": unit(**options)}]
    problem(example.check(example.rule(compare(left, right, "gt"))), code, ("right",))


def test_same_enum_members_do_not_merge_two_pinned_schemas():
    example = Example()
    shape = {"kind": "enum", "members": ["parked", "moving"]}
    left = example.question_fact("enum.one", shape=shape)
    right = example.question_fact("enum.two", shape=shape)
    problem(
        example.check(example.rule(compare(left, right))),
        "rule_type.enum_identity_mismatch",
        ("right",),
    )


def test_enum_only_literals_do_not_supply_pinned_identity():
    example = Example()
    enum = literal("enum", "parked", members=["parked"])
    rule = example.rule(compare(enum, enum))
    problem(
        example.check(rule),
        "rule_type.enum_identity_unavailable",
        ("right",),
        status="incomplete",
    )


def test_enum_literal_cannot_extend_or_narrow_pinned_members():
    example = Example()
    fact = example.question_fact(
        shape={"kind": "enum", "members": ["parked", "moving"]}
    )
    rule = example.rule(compare(fact, literal("enum", "parked", members=["parked"])))
    problem(example.check(rule), "rule_type.enum_members_mismatch", ("right",))


def test_rule_record_path_and_missing_producer_diagnostics():
    example = Example()
    fact = example.question_fact(
        shape={
            "kind": "record",
            "fields": [
                {
                    "name": "valid",
                    "required": False,
                    "type_definition": {"kind": "bool"},
                }
            ],
        },
        path=("missing",),
    )
    problem(
        example.check(example.rule(fact)), "field_unknown", ("selector", "field_path")
    )
    fact["selector"]["field_path"] = ("valid",)
    example.declarations.clear()
    problem(
        example.check(example.rule(fact)),
        "rule_type.producer_unavailable",
        ("selector",),
        status="incomplete",
    )
    problem(
        example.check(example.rule(fact), bindings=example.bindings(catalogue=False)),
        "rule_type.catalogue_unavailable",
        ("selector",),
        status="incomplete",
    )


def test_valid_rule_selector_is_located_and_forbidden_in_applicability():
    example = Example()
    fact = example.question_fact()
    result = example.check(
        {"kind": "not", "argument": fact}, "applicability", context_allowlist=()
    )
    problem(result, "condition.applicability_fact_forbidden", ("argument",))


@pytest.mark.parametrize(
    ("step", "code"),
    [
        ("third", "condition.local_not_ancestor"),
        ("sibling", "condition.local_not_ancestor"),
    ],
)
def test_tool_local_reference_cannot_read_self_or_sibling(step, code):
    example = Example()
    example.plan_chain()
    local = example.local_fact(step=step)
    result = example.check({"kind": "not", "argument": local}, "tool_step_guard")
    problem(result, code, ("argument", "selector"))


def test_tool_exact_plan_reference_cannot_replace_local_selector():
    example = Example()
    example.plan_chain()
    exact = example.tool_fact()
    problem(
        example.check(exact, "tool_step_guard"),
        "condition.exact_tool_guard_forbidden",
        ("selector",),
    )
    result = example.check(example.rule(exact))
    assert result.status == "passed"


def test_unknown_or_unavailable_producer_cannot_pass_attached_guard():
    example = Example()
    example.plan_chain()
    system = example.system_fact()
    problem(
        example.check(system, "tool_step_guard"),
        "rule_type.prior_producer_unavailable",
        ("selector",),
        status="incomplete",
    )


def test_system_activation_cannot_read_own_output_even_if_marked_prior():
    example = Example()
    system = example.system_fact()
    example.declarations[0]["activation_condition"] = {
        "mode": "tool_condition",
        "condition_ast": system,
    }
    example.available.append("system.test")
    context = example.context("producer_activation", declaration="system.test")
    problem(
        example.check(system, "producer_activation", context=context),
        "rule_type.self_producer",
        ("selector",),
    )


def test_local_output_requires_mapping_and_pinned_schema():
    example = Example()
    example.plan_chain()
    local = example.local_fact()
    example.plan["definition"]["steps"][0]["fact_output_mappings"] = []
    problem(
        example.check(local, "tool_step_guard"),
        "condition.tool_output_identity",
        ("selector",),
    )


def test_exact_required_optional_dependency_partition_is_revalidated():
    example = Example()
    fact = example.question_fact()
    rule = example.rule(fact)
    tampered = rule.model_copy(update={"required_fact_selectors": ()})
    problem(example.check(tampered), "rule_type.invalid_contract", ())
    duplicated = rule.model_copy(
        update={"optional_fact_selectors": rule.required_fact_selectors}
    )
    problem(example.check(duplicated), "rule_type.invalid_contract", ())
    problem(example.check(fact), "rule_type.rule_required", ())


def test_foreign_rule_set_cannot_be_used_with_matching_rule():
    example = Example()
    rule = example.rule(literal())
    example.profile["definition"]["rule_set_refs"] = [ref("rule_set", "foreign-set")]
    problem(
        example.check(rule), "rule_type.rule_set_membership", ("context", "rule_set")
    )


def test_fact_schema_reference_digest_mismatch_is_invalid():
    example = Example()
    fact = example.question_fact()
    fact = deepcopy(fact)
    fact["selector"]["fact_schema_ref"]["digest"] = "b" * 64
    problem(
        example.check(example.rule(fact)), "registry.digest_mismatch", ("selector",)
    )


def test_context_registration_does_not_allow_candidate_or_decision_fields():
    example = Example()
    fake = example.context_field(path=("candidate",))
    test = compare(fake, literal("string", "violation"))
    problem(
        example.check(example.rule(test)),
        "field_unknown",
        ("left", "selector", "field_path"),
    )


def test_unbounded_or_empty_membership_operands_rejected():
    example = Example()
    collection = example.context_field(
        shape={
            "kind": "list",
            "item_type": {"kind": "integer"},
            "min_items": 0,
            "max_items": 10,
        },
    )
    rule = example.rule(membership(literal("integer", 4), collection))
    problem(example.check(rule), "rule_type.membership_shape", ())


def test_integer_literal_is_not_a_boolean_argument():
    example = Example()
    rule = example.rule(
        {"kind": "all", "arguments": [literal(), literal("integer", 1)]}
    )
    problem(example.check(rule), "rule_type.boolean_argument", ("arguments", 1))


@pytest.mark.parametrize(
    "condition",
    [
        {"kind": "all", "arguments": []},
        compare(literal("integer", True), literal("integer", 1)),
        membership(
            literal("integer", 4),
            literal(
                "list", [{"kind": "bool", "value": True}], item_type={"kind": "integer"}
            ),
        ),
        compare(literal("enum", "outside", members=["parked"]), literal()),
        {"kind": "candidate_ref"},
    ],
)
def test_strict_structure_rejects_malformed_operand_data(condition):
    example = Example()
    example.plan_chain()
    result = example.check(condition, "tool_step_guard")
    assert isinstance(result, FailedTypeCheck)
    assert result.status == "invalid"
    assert result.issues[0].path


def test_cycle_and_maximum_ast_depth_are_structural_rejections():
    example = Example()
    example.plan_chain()
    cycle = {"kind": "not"}
    cycle["argument"] = cycle
    problem(example.check(cycle, "tool_step_guard"), "condition.cycle")
    tree = literal()
    for _ in range(32):
        tree = {"kind": "not", "argument": tree}
    problem(example.check(tree, "tool_step_guard"), "condition.depth_limit")


@pytest.mark.parametrize("operator", ["eq", "ne"])
def test_equality_has_no_implicit_deep_list_or_record_comparison(operator):
    example = Example()
    shape = {"kind": "record", "fields": []}
    fact = example.question_fact(shape=shape)
    problem(
        example.check(example.rule(compare(fact, fact, operator))),
        "rule_type.scalar_comparison",
        (),
    )
    collection = literal(
        "list", [{"kind": "integer", "value": 1}], item_type={"kind": "integer"}
    )
    problem(
        example.check(example.rule(compare(collection, collection, operator))),
        "rule_type.scalar_comparison",
        (),
    )


def test_same_unit_member_in_different_pinned_vocabularies_is_incompatible():
    example = Example()
    different = unit()
    different["vocabulary_ref"]["digest"] = "b" * 64
    left = example.question_fact(
        "measured.left", shape={"kind": "integer"}, units=[((), unit())]
    )
    right = example.question_fact(
        "measured.right", shape={"kind": "integer"}, units=[((), different)]
    )
    problem(
        example.check(example.rule(compare(left, right))),
        "rule_type.unit_mismatch",
        ("right",),
    )


def test_same_schema_record_fields_do_not_merge_enum_identities():
    example = Example()
    enum = {"kind": "enum", "members": ["parked", "moving"]}
    shape = {
        "kind": "record",
        "fields": [
            {"name": "before", "required": True, "type_definition": enum},
            {"name": "after", "required": True, "type_definition": enum},
        ],
    }
    fact = example.question_fact(shape=shape, path=("before",))
    other = deepcopy(fact)
    other["selector"]["field_path"] = ("after",)
    problem(
        example.check(example.rule(compare(fact, other))),
        "rule_type.enum_identity_mismatch",
        ("right",),
    )


def test_ambiguous_producer_catalogue_is_revalidated_at_checker_boundary():
    example = Example()
    expr = example.question_fact()
    bindings = example.bindings()
    registration = bindings.producer_catalogue
    assert registration is not None
    declaration = registration.definition.declarations[0]
    ambiguous = registration.definition.model_copy(
        update={
            "declarations": (
                declaration,
                declaration.model_copy(update={"declaration_id": "shadow"}),
            )
        }
    )
    modified = bindings.model_copy(
        update={
            "producer_catalogue": registration.model_copy(
                update={"definition": ambiguous}
            )
        }
    )
    problem(
        example.check(example.rule(expr), bindings=modified),
        "rule_type.invalid_contract",
        ("bindings", "producer_catalogue", "definition"),
    )


def test_non_tool_producer_activation_cannot_use_local_step_binding():
    example = Example()
    example.plan_chain()
    local = example.local_fact()
    system = example.system_fact()
    context = example.context(
        "producer_activation", declaration=system["selector"]["fact_key"]
    )
    result = example.check(local, "producer_activation", context=context)
    problem(result, "rule_type.activation_binding", ())


def test_question_activation_cannot_use_other_n04_outputs():
    example = Example()
    sibling = example.question_fact("question.sibling")
    example.question_fact("question.current")
    example.declarations[1]["activation_condition"] = {
        "mode": "tool_condition",
        "condition_ast": sibling,
    }
    example.available = ["question.sibling"]
    context = example.context("producer_activation", declaration="question.current")
    problem(
        example.check(sibling, "producer_activation", context=context),
        "rule_type.question_producer_not_prior",
        ("selector",),
    )


def test_bound_tool_policy_cannot_be_replaced_with_other_condition():
    example = Example()
    example.plan_chain()
    example.policies = [
        item
        for item in example.data["input"]["policies"]
        if item["artifact"]["policy"]["policy_kind"] == "tool_skip"
    ]
    problem(
        example.check(literal(), "tool_step_guard"),
        "rule_type.policy_binding",
        ("context", "policy_ref"),
    )


def test_node_count_and_max_operator_arguments_are_bounded():
    example = Example()
    example.plan_chain()
    wide = {"kind": "all", "arguments": [literal()] * 64}
    assert example.check(wide, "tool_step_guard").status == "passed"
    problem(
        example.check({"kind": "all", "arguments": [wide] * 8}, "tool_step_guard"),
        "condition.node_limit",
    )
    problem(
        example.check(
            {"kind": "all", "arguments": [literal()] * 65}, "tool_step_guard"
        ),
        "condition.arguments_limit",
    )


def test_nonboolean_root_is_rejected_after_context_inference():
    example = Example()
    ctx = example.context_field()
    problem(example.check(example.rule(ctx)), "rule_type.nonboolean_root", ())
