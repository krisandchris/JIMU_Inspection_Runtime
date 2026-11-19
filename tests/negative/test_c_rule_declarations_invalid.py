"""Fail-closed declarations, without semantic type checking or Rule evaluation."""

import json
from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from jimu_runtime.domain.rules import (
    OPERATOR_DESCRIPTORS,
    OperatorDescriptor,
    RuleDefinition,
    RuleSetDefinition,
)

FIXTURES = Path(__file__).parents[1] / "fixtures"


def read_rule_fixture(name: str) -> dict[str, Any]:
    return json.loads(
        (FIXTURES / f"wp01c/rule_declarations/{name}-valid.json").read_bytes()
    )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("schema_version", "1.1.0"),
        ("violation_category", {"category": "synthetic-parking"}),
        ("applicability_policy_ref", {"artifact_kind": "profile"}),
        ("required_fact_selectors", None),
        ("optional_fact_selectors", {}),
        ("condition_ast", "lambda: True"),
        ("condition_ast", {"kind": "all", "arguments": []}),
        ("condition_ast", {"kind": "candidate_prediction"}),
        ("mandatory", True),
        ("priority", 1),
    ],
)
def test_rule_rejects_bad_field_and_unregistered_controls(
    field: str, value: object
) -> None:
    raw = read_rule_fixture("rule")
    raw[field] = value
    with pytest.raises(ValidationError):
        RuleDefinition.model_validate(raw)


@pytest.mark.parametrize(
    "field", ["applicability_policy_ref", "rule_policy_ref", "evidence_policy_ref"]
)
def test_rule_policy_ref_is_exact_and_kind_pinned(field: str) -> None:
    raw = read_rule_fixture("rule")
    for invalid_ref in [
        {**raw[field], "version": "latest"},
        {**raw[field], "digest": "a" * 63},
        {**raw[field], "artifact_kind": "model_binding"},
    ]:
        with pytest.raises(ValidationError):
            RuleDefinition.model_validate({**raw, field: invalid_ref})


@pytest.mark.parametrize(
    ("mutation", "error"),
    [
        ("duplicate_required", "must not contain duplicates"),
        ("duplicate_optional", "must not contain duplicates"),
        ("overlap", "must be disjoint"),
        ("missing_required", "must exactly match"),
        ("unused_optional", "must exactly match"),
    ],
)
def test_rule_dependency_partition_is_exact(mutation: str, error: str) -> None:
    raw = read_rule_fixture("rule")
    required = raw["required_fact_selectors"]
    optional = raw["optional_fact_selectors"]
    match mutation:
        case "duplicate_required":
            required.append(deepcopy(required[0]))
        case "duplicate_optional":
            optional.append(deepcopy(optional[0]))
        case "overlap":
            optional.append(deepcopy(required[0]))
        case "missing_required":
            required.clear()
        case "unused_optional":
            extra = deepcopy(optional[0])
            extra["field_path"] = ["other"]
            optional.append(extra)
    with pytest.raises(ValidationError, match=error):
        RuleDefinition.model_validate(raw)


@pytest.mark.parametrize(
    "field", ["fact_key", "fact_schema_ref", "producer_selector", "field_path"]
)
def test_rule_partition_compares_entire_selector_identity(field: str) -> None:
    raw = read_rule_fixture("rule")
    selector = raw["optional_fact_selectors"][0]
    if field == "fact_schema_ref":
        selector[field]["digest"] = "0" * 64
    elif field == "producer_selector":
        selector[field]["question_ref"]["digest"] = "0" * 64
    elif field == "field_path":
        selector[field] = ["different"]
    else:
        selector[field] = "different"
    with pytest.raises(ValidationError, match="must exactly match"):
        RuleDefinition.model_validate(raw)


@pytest.mark.parametrize(
    ("field", "value", "error"),
    [
        ("scope", "execution", "execution Fact selectors require a system producer"),
        ("field_path", ["field"], "condition.fact_ref_requires_whole_value"),
    ],
)
def test_rule_rejects_wrong_scope_or_ast_path(
    field: str, value: object, error: str
) -> None:
    raw = read_rule_fixture("rule")
    raw["condition_ast"]["arguments"][0]["selector"][field] = value
    with pytest.raises(ValidationError, match=error):
        RuleDefinition.model_validate(raw)


def test_rule_rejects_ast_nodes_from_tool_domain_and_nonboolean_root() -> None:
    raw = read_rule_fixture("rule")
    b = json.loads((FIXTURES / "wp01b/x1/contracts.json").read_bytes())
    raw["condition_ast"]["arguments"].append(b["expressions"]["local_tool_fact_ref"])
    with pytest.raises(ValidationError, match="condition.local_tool_domain"):
        RuleDefinition.model_validate(raw)
    raw.update(
        condition_ast=b["expressions"]["comparison"]["right"],
        required_fact_selectors=[],
        optional_fact_selectors=[],
    )
    with pytest.raises(ValidationError, match="condition.nonboolean_literal_root"):
        RuleDefinition.model_validate(raw)


def test_rule_rejects_cyclic_ast_before_recursive_validation() -> None:
    raw = read_rule_fixture("rule")
    cycle: dict[str, object] = {"kind": "not"}
    cycle["argument"] = cycle
    raw["condition_ast"] = cycle
    with pytest.raises(ValidationError, match="condition.cycle"):
        RuleDefinition.model_validate(raw)


def test_rule_revalidation_rejects_copy_injected_dependency_bypass() -> None:
    good = RuleDefinition.model_validate(read_rule_fixture("rule"))
    forged = good.model_copy(update={"required_fact_selectors": ()})
    with pytest.raises(ValidationError, match="must exactly match"):
        RuleDefinition.model_validate(forged)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("rule_refs", []),
        ("rule_refs", [{"artifact_kind": "rule_set"}]),
        ("candidate_binding_ref", None),
        ("candidate_binding_ref", {"artifact_kind": "policy"}),
        ("conflict_policy_ref", {"artifact_kind": "rule"}),
        ("mode", "winner_takes_all"),
        ("early_exit", True),
    ],
)
def test_rule_set_rejects_invalid_refs_and_behavior_overrides(
    field: str, value: object
) -> None:
    raw = read_rule_fixture("rule-set")
    raw[field] = value
    with pytest.raises(ValidationError):
        RuleSetDefinition.model_validate(raw)


@pytest.mark.parametrize("different_digest", [False, True])
def test_rule_set_rejects_duplicate_or_conflicting_rule_refs(
    different_digest: bool,
) -> None:
    raw = read_rule_fixture("rule-set")
    ref = deepcopy(raw["rule_refs"][0])
    if different_digest:
        ref["digest"] = "0" * 64
    raw["rule_refs"].append(ref)
    with pytest.raises(ValidationError, match="unique kind/id/version"):
        RuleSetDefinition.model_validate(raw)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("operator_id", "exists"),
        ("version", "2.0.0"),
        ("arity", {"min_args": True, "max_args": 2}),
        ("arity", {"min_args": 3, "max_args": 2}),
        ("arity", {"min_args": 1, "max_args": 2}),
        ("input_constraint", "custom_expression"),
        ("input_constraint", "boolean_arguments"),
        ("output_type", {"kind": "string"}),
        ("nonknown_propagation", "coerce_false"),
        ("nonknown_propagation", "strong_kleene"),
        ("implementation", "unsafe.handler"),
    ],
)
def test_operator_descriptor_rejects_unknown_fields_and_signatures(
    field: str, value: object
) -> None:
    raw = OPERATOR_DESCRIPTORS["eq"].model_dump(mode="json")
    raw[field] = value
    with pytest.raises(ValidationError):
        OperatorDescriptor.model_validate(raw)
