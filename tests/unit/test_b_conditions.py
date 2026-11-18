"""Condition data structure boundaries; these tests do not evaluate predicates."""

import json
from copy import deepcopy
from pathlib import Path
from typing import cast

import pytest
from pydantic import TypeAdapter, ValidationError

from jimu_runtime.domain.conditions import (
    ConditionDataError,
    ConditionExpr,
    ContextRefExpr,
    LiteralExpr,
    condition_references,
    validate_condition_data,
)
from jimu_runtime.domain.definition_enums import ConditionInputDomain


def fixture() -> dict[str, object]:
    return cast(
        dict[str, object],
        json.loads(
            (Path(__file__).parents[1] / "fixtures/wp01b/x1/contracts.json").read_text()
        ),
    )


def expr(name: str) -> dict[str, object]:
    return cast(dict[str, dict[str, object]], fixture()["expressions"])[name]


def tree(
    raw: object, domain: ConditionInputDomain = ConditionInputDomain.TOOL_CONDITION
) -> ConditionExpr:
    return validate_condition_data(raw, input_domain=domain)


@pytest.mark.parametrize(
    "name",
    [
        "literal",
        "fact_ref",
        "record_field_ref",
        "context_ref",
        "local_tool_fact_ref",
        "comparison",
        "in_expr",
        "all_expr",
        "any_expr",
        "not_expr",
    ],
)
def test_every_node_roundtrips_without_evaluation(name: str) -> None:
    result = tree(expr(name))
    adapter = TypeAdapter(ConditionExpr)
    assert tree(adapter.dump_python(result, mode="json")) == result
    assert adapter.validate_json(adapter.dump_json(result)) == result


def test_shape_does_not_prove_operand_types_or_reference_boolean_return() -> None:
    mismatch = {
        "kind": "comparison",
        "operator": "eq",
        "left": {
            "kind": "literal",
            "constant": {
                "type_definition": {"kind": "integer"},
                "value": {"kind": "integer", "value": 1},
            },
        },
        "right": {
            "kind": "literal",
            "constant": {
                "type_definition": {"kind": "string"},
                "value": {"kind": "string", "value": "one"},
            },
        },
    }
    assert tree(mismatch).kind == "comparison"
    assert tree(expr("context_ref")).kind == "context_ref"
    assert tree(expr("fact_ref")).kind == "fact_ref"


def test_false_literal_is_preserved_not_folded() -> None:
    raw = {"kind": "all", "arguments": [fixture()["bool_false"], expr("context_ref")]}
    parsed = tree(raw)
    assert parsed.kind == "all"
    assert len(condition_references(parsed).context_selectors) == 1


def nested_not(depth: int) -> dict[str, object]:
    raw = expr("context_ref")
    for _ in range(depth - 1):
        raw = {"kind": "not", "argument": raw}
    return raw


def test_depth_32_accepts_33_rejects() -> None:
    assert tree(nested_not(32)).kind == "not"
    with pytest.raises(
        (ConditionDataError, ValidationError), match="condition.depth_limit"
    ):
        tree(nested_not(33))


def occurrence_tree(total: int) -> dict[str, object]:
    # root + groups + leaf occurrences; a shared leaf counts every time.
    leaf = expr("context_ref")
    groups = []
    remaining = total - 1
    while remaining:
        leaves = min(63, remaining - 1)
        if leaves < 1:
            raise ValueError("unsupported test total")
        groups.append({"kind": "all", "arguments": [leaf] * leaves})
        remaining -= leaves + 1
    return {"kind": "all", "arguments": groups}


def test_512_occurrences_accepts_shared_leaf_513_rejects() -> None:
    result = tree(occurrence_tree(512))
    assert len(condition_references(result).context_selectors) == 503
    with pytest.raises(
        (ConditionDataError, ValidationError), match="condition.node_limit"
    ):
        tree(occurrence_tree(513))


def test_shared_subtree_is_not_cycle_but_cycle_rejects() -> None:
    shared = {"kind": "not", "argument": expr("context_ref")}
    result = tree({"kind": "all", "arguments": [shared, shared]})
    refs = condition_references(result)
    assert len(refs.context_selectors) == 2
    assert refs.context_selectors[0] == refs.context_selectors[1]
    cycle: dict[str, object] = {"kind": "not"}
    cycle["argument"] = cycle
    with pytest.raises(ConditionDataError, match="condition.cycle"):
        tree(cycle)


def test_reference_extraction_preorder_keeps_occurrences() -> None:
    first = expr("context_ref")
    second = deepcopy(first)
    cast(dict[str, object], second["selector"])["field_path"] = ["another"]
    result = tree(
        {
            "kind": "all",
            "arguments": [
                first,
                {"kind": "not", "argument": second},
                first,
                expr("local_tool_fact_ref"),
                expr("fact_ref"),
            ],
        }
    )
    refs = condition_references(result)
    assert tuple(ref.field_path for ref in refs.context_selectors) == (
        ("outer", "leaf"),
        ("another",),
        ("outer", "leaf"),
    )
    assert len(refs.local_tool_selectors) == len(refs.fact_selectors) == 1


def test_tainted_node_and_nested_selector_are_not_sanitized() -> None:
    parsed = tree(expr("context_ref"))
    assert isinstance(parsed, ContextRefExpr)
    for tainted in [
        parsed.model_copy(update={"extra": 1}),
        parsed.model_copy(
            update={"selector": parsed.selector.model_copy(update={"extra": 1})}
        ),
    ]:
        with pytest.raises(ValidationError):
            tree(tainted)
    value = tree(expr("literal"))
    assert isinstance(value, LiteralExpr)
    with pytest.raises(ValidationError):
        tree(
            value.model_copy(
                update={"constant": value.constant.model_copy(update={"extra": 1})}
            )
        )


@pytest.mark.parametrize("kind", ["all", "any"])
def test_64_logical_arguments_are_valid_data(kind: str) -> None:
    assert tree({"kind": kind, "arguments": [expr("literal")] * 64}).kind == kind


def test_64_in_literal_values_and_referenced_collection_supported() -> None:
    raw = expr("in_expr")
    constant = cast(
        dict[str, object], cast(dict[str, object], raw["collection"])["constant"]
    )
    cast(dict[str, object], constant["value"])["value"] = [
        {"kind": "bool", "value": True}
    ] * 64
    assert tree(raw).kind == "in"
    raw["collection"] = expr("fact_ref")
    assert tree(raw).kind == "in"  # Schema-derived list/type proof is not this parser.


@pytest.mark.parametrize("operator", ["eq", "ne", "lt", "le", "gt", "ge"])
def test_all_approved_comparison_ids_are_data_not_callable_implementations(
    operator: str,
) -> None:
    raw = expr("comparison")
    raw["operator"] = operator
    assert tree(raw).operator.value == operator


def test_value_contract_list_members_and_enum_constants_roundtrip_as_operands() -> None:
    enum = {
        "kind": "literal",
        "constant": {
            "type_definition": {"kind": "enum", "members": ["a", "b"]},
            "value": {"kind": "enum", "value": "a"},
        },
    }
    result = tree({"kind": "comparison", "operator": "eq", "left": enum, "right": enum})
    assert result.kind == "comparison"
    assert TypeAdapter(ConditionExpr).validate_json(result.model_dump_json()) == result


def test_constructed_cycle_and_nested_extra_cycle_fail_before_unbounded_recursion() -> (
    None
):
    from jimu_runtime.domain.conditions import NotExpr

    cycle = NotExpr.model_construct(kind="not", argument=expr("literal"))
    cycle.__dict__["argument"] = cycle
    with pytest.raises(ConditionDataError, match="condition.cycle"):
        tree(cycle)
    raw = expr("context_ref")
    raw["extra"] = raw
    with pytest.raises(ConditionDataError, match="condition.cycle"):
        tree(raw)


def test_system_fact_reference_keeps_absent_optional_requirement_fields() -> None:
    raw = expr("fact_ref")
    selector = cast(dict[str, object], raw["selector"])
    selector.update(
        scope="execution",
        producer_selector={
            "producer_kind": "system",
            "requirement": {
                "family": "system_fact_producer",
                "capability_id": "system-source",
                "contract_version": "1.0.0",
                "allowed_implementation_versions": ["1.0.0"],
                "required_features": [],
            },
            "output_name": "primary",
        },
    )
    parsed = tree(raw)
    assert tree(parsed) == parsed
    assert (
        "evaluation_ref"
        not in cast(dict[str, object], parsed.model_dump(mode="json")["selector"])[
            "producer_selector"
        ]["requirement"]
    )
