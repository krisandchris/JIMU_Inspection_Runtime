"""Negative data contracts, independent of the future semantic TypeChecker."""

import json
from copy import deepcopy
from pathlib import Path
from typing import cast

import pytest
from pydantic import ValidationError

from jimu_runtime.domain.conditions import ConditionDataError, validate_condition_data
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


def reject(
    raw: object, domain: ConditionInputDomain = ConditionInputDomain.TOOL_CONDITION
) -> None:
    with pytest.raises((ConditionDataError, ValidationError)):
        validate_condition_data(raw, input_domain=domain)


@pytest.mark.parametrize(
    "kind", ["candidate", "decision", "object_image", "eval", "is_known", "exists"]
)
def test_unapproved_nodes_reject(kind: str) -> None:
    reject({"kind": kind})


@pytest.mark.parametrize(
    "mutation", ["unknown_operator", "extra", "missing_left", "extra_argument", "null"]
)
def test_operator_arity_and_fields(mutation: str) -> None:
    raw = expr("comparison")
    if mutation == "unknown_operator":
        raw["operator"] = "contains"
    elif mutation == "extra":
        raw["python"] = "forbidden"
    elif mutation == "missing_left":
        raw.pop("left")
    elif mutation == "extra_argument":
        raw["arguments"] = []
    else:
        raw["right"] = None
    reject(raw)


@pytest.mark.parametrize("kind", ["all", "any"])
@pytest.mark.parametrize("count", [0, 65])
def test_logical_lists_have_1_to_64_arguments(kind: str, count: int) -> None:
    reject({"kind": kind, "arguments": [expr("literal")] * count})


@pytest.mark.parametrize("count", [0, 65])
def test_in_literal_collection_has_1_to_64_values(count: int) -> None:
    raw = expr("in_expr")
    constant = cast(
        dict[str, object], cast(dict[str, object], raw["collection"])["constant"]
    )
    cast(dict[str, object], constant["value"])["value"] = [
        {"kind": "bool", "value": True}
    ] * count
    reject(raw)


def test_root_scalar_rejects_and_enum_membership_is_validated() -> None:
    integer = {
        "kind": "literal",
        "constant": {
            "type_definition": {"kind": "integer"},
            "value": {"kind": "integer", "value": 1},
        },
    }
    reject(integer)
    invalid = {
        "kind": "literal",
        "constant": {
            "type_definition": {"kind": "enum", "members": ["a"]},
            "value": {"kind": "enum", "value": "b"},
        },
    }
    reject(
        {
            "kind": "comparison",
            "operator": "eq",
            "left": invalid,
            "right": expr("literal"),
        }
    )


@pytest.mark.parametrize("nested", [False, True])
def test_record_literals_are_not_part_of_scalar_list_language(nested: bool) -> None:
    definition: dict[str, object] = {"kind": "record", "fields": []}
    value: dict[str, object] = {"kind": "record", "value": {}}
    if nested:
        definition = {"kind": "list", "item_type": definition}
        value = {"kind": "list", "value": [value]}
    record = {
        "kind": "literal",
        "constant": {"type_definition": definition, "value": value},
    }
    reject(
        {
            "kind": "comparison",
            "operator": "eq",
            "left": record,
            "right": expr("literal"),
        }
    )


@pytest.mark.parametrize(
    "name", ["fact_ref", "record_field_ref", "local_tool_fact_ref"]
)
def test_applicability_cannot_hide_fact_under_false_or_operator(name: str) -> None:
    raw = {
        "kind": "all",
        "arguments": [fixture()["bool_false"], {"kind": "not", "argument": expr(name)}],
    }
    reject(raw, ConditionInputDomain.RULE_APPLICABILITY)


def test_local_tool_is_not_rule_condition_and_has_no_owner_ids() -> None:
    reject(expr("local_tool_fact_ref"), ConditionInputDomain.RULE_CONDITION)
    for field in [
        "tool_plan_ref",
        "profile_ref",
        "object_id",
        "execution_id",
        "attempt_id",
    ]:
        raw = deepcopy(expr("local_tool_fact_ref"))
        cast(dict[str, object], raw["selector"])[field] = "forbidden"
        reject(raw)
    raw = expr("local_tool_fact_ref")
    cast(dict[str, object], raw["selector"])["scope"] = "execution"
    reject(raw)


def test_fact_and_record_field_use_one_path_authority() -> None:
    raw = expr("fact_ref")
    cast(dict[str, object], raw["selector"])["field_path"] = ["field"]
    reject(raw)
    raw = expr("record_field_ref")
    cast(dict[str, object], raw["selector"])["field_path"] = []
    reject(raw)
    raw = expr("record_field_ref")
    raw["field_path"] = ["other"]
    reject(raw)


def test_list_literal_conformance_and_strict_numeric_types() -> None:
    raw = expr("in_expr")
    constant = cast(
        dict[str, object], cast(dict[str, object], raw["collection"])["constant"]
    )
    cast(dict[str, object], constant["value"])["value"] = [
        {"kind": "integer", "value": 1}
    ]
    reject(raw)
    bad = {
        "kind": "literal",
        "constant": {
            "type_definition": {"kind": "bool"},
            "value": {"kind": "bool", "value": 1},
        },
    }
    reject({"kind": "not", "argument": bad})


def test_local_selector_requires_real_exact_refs_not_placeholder_or_selector() -> None:
    raw = expr("local_tool_fact_ref")
    selector = cast(dict[str, object], raw["selector"])
    cast(dict[str, object], selector["tool_ref"])["digest"] = "placeholder"
    reject(raw)
    raw = expr("local_tool_fact_ref")
    selector = cast(dict[str, object], raw["selector"])
    selector["tool_ref"] = {
        "artifact_kind": "profile",
        "artifact_id": "profile",
        "version_selector": "1.0.0",
    }
    reject(raw)


@pytest.mark.parametrize("as_tuple", [False, True])
def test_oversized_logical_arguments_reject_before_descendant_traversal(
    as_tuple: bool,
) -> None:
    raw: dict[str, object] = {"kind": "all"}
    items = [raw] * 65
    raw["arguments"] = tuple(items) if as_tuple else items
    with pytest.raises(ConditionDataError, match="condition.arguments_limit"):
        validate_condition_data(raw, input_domain=ConditionInputDomain.TOOL_CONDITION)
