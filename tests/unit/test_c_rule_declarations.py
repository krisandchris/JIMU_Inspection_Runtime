"""Static Rule declarations and operator registry tests for WP-01C."""

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from jimu_runtime.domain.conditions import (
    MAX_CONDITION_ARGUMENTS,
    condition_references,
    validate_condition_data,
)
from jimu_runtime.domain.definition_enums import (
    ComparisonOperator,
    ConditionInputDomain,
)
from jimu_runtime.domain.definitions.capabilities import RuleSetContentRef
from jimu_runtime.domain.policies import ConflictPolicy, RulePolicy
from jimu_runtime.domain.rules import (
    OPERATOR_DESCRIPTORS,
    OperatorDescriptor,
    RuleDefinition,
    RuleSetDefinition,
)

FIXTURES = Path(__file__).parents[1] / "fixtures"


def test_rule_uses_unchanged_b_condition_wire_and_separate_dependencies() -> None:
    fixture_dir = FIXTURES / "wp01c/rule_declarations"
    rule = RuleDefinition.model_validate_json(
        (fixture_dir / "rule-valid.json").read_bytes()
    )
    raw = json.loads((fixture_dir / "rule-valid.json").read_bytes())
    b = json.loads((FIXTURES / "wp01b/x1/contracts.json").read_bytes())
    arguments = raw["condition_ast"]["arguments"]
    assert arguments[0] == b["expressions"]["fact_ref"]
    assert arguments[2] == b["expressions"]["comparison"]
    ast = validate_condition_data(
        raw["condition_ast"], input_domain=ConditionInputDomain.RULE_CONDITION
    )
    assert rule.condition_ast == ast
    assert rule.model_dump(mode="json")["condition_ast"] == ast.model_dump(mode="json")
    assert condition_references(rule.condition_ast).fact_selectors == (
        *rule.required_fact_selectors,
        *rule.optional_fact_selectors,
    )
    with pytest.raises(ValidationError, match="frozen_instance"):
        rule.required_fact_selectors[0].fact_key = "rewritten"


def test_rule_accepts_repeated_exact_ast_reference_and_empty_dependencies() -> None:
    fixture_dir = FIXTURES / "wp01c/rule_declarations"
    wire = json.loads((fixture_dir / "rule-valid.json").read_bytes())
    wire["condition_ast"]["arguments"].append(wire["condition_ast"]["arguments"][0])
    repeated = RuleDefinition.model_validate(wire)
    assert len(condition_references(repeated.condition_ast).fact_selectors) == 3
    assert len(repeated.required_fact_selectors) == 1

    b = json.loads((FIXTURES / "wp01b/x1/contracts.json").read_bytes())
    wire.update(
        condition_ast=b["expressions"]["literal"],
        required_fact_selectors=[],
        optional_fact_selectors=[],
    )
    assert RuleDefinition.model_validate(wire).required_fact_selectors == ()


def test_rule_set_candidate_binding_absence_and_existing_ref_wire() -> None:
    fixture = FIXTURES / "wp01c/rule_declarations/rule-set-valid.json"
    wire = json.loads(fixture.read_bytes())
    with_candidate = RuleSetDefinition.model_validate(wire)
    assert with_candidate.model_dump(mode="json") == wire
    assert with_candidate.candidate_binding_ref is not None
    assert (
        with_candidate.candidate_binding_ref.artifact_id
        == "synthetic-candidate-binding"
    )

    del wire["candidate_binding_ref"]
    without_candidate = RuleSetDefinition.model_validate(wire)
    assert without_candidate.candidate_binding_ref is None
    assert without_candidate.model_dump(mode="json") == wire
    assert RuleSetDefinition.model_validate(without_candidate) == without_candidate
    assert (
        RuleSetContentRef.model_json_schema()["properties"]["artifact_kind"]["const"]
        == "rule_set"
    )


EXPECTED_SIGNATURES = {
    "eq": (2, 2, "same_comparable_type", "unknown_operand"),
    "ne": (2, 2, "same_comparable_type", "unknown_operand"),
    "lt": (2, 2, "same_numeric_type_unit", "unknown_operand"),
    "le": (2, 2, "same_numeric_type_unit", "unknown_operand"),
    "gt": (2, 2, "same_numeric_type_unit", "unknown_operand"),
    "ge": (2, 2, "same_numeric_type_unit", "unknown_operand"),
    "in": (2, 2, "scalar_and_nonempty_list", "unknown_operand"),
    "all": (1, MAX_CONDITION_ARGUMENTS, "boolean_arguments", "strong_kleene"),
    "any": (1, MAX_CONDITION_ARGUMENTS, "boolean_arguments", "strong_kleene"),
    "not": (1, 1, "boolean_arguments", "strong_kleene"),
}


def test_operator_whitelist_is_closed_and_immutable() -> None:
    assert set(OPERATOR_DESCRIPTORS) == set(EXPECTED_SIGNATURES)
    assert set(ComparisonOperator) == {
        item.operator_id
        for item in OPERATOR_DESCRIPTORS.values()
        if isinstance(item.operator_id, ComparisonOperator)
    }
    with pytest.raises(TypeError, match="item assignment"):
        OPERATOR_DESCRIPTORS["extra"] = OPERATOR_DESCRIPTORS["eq"]  # type: ignore[index]


@pytest.mark.parametrize("operator_id", EXPECTED_SIGNATURES)
def test_operator_signature_and_json_round_trip(operator_id: str) -> None:
    descriptor = OPERATOR_DESCRIPTORS[operator_id]
    assert descriptor.model_dump(mode="json")["operator_id"] == operator_id
    assert descriptor.version == "1.0.0"
    assert descriptor.output_type.kind == "bool"
    assert (
        descriptor.arity.min_args,
        descriptor.arity.max_args,
        descriptor.input_constraint,
        descriptor.nonknown_propagation,
    ) == EXPECTED_SIGNATURES[operator_id]
    assert (
        OperatorDescriptor.model_validate_json(descriptor.model_dump_json())
        == descriptor
    )


def test_existing_policies_own_mandatory_scan_and_collect_all() -> None:
    mandatory_policy = RulePolicy.model_validate(
        {
            "policy_kind": "rule",
            "mandatory": True,
            "missing": "unknown",
            "unknown": "unknown",
            "error": "unknown",
            "not_applicable": "unknown",
            "not_evaluated": "unknown",
            "required_guard": "block_unavailable",
            "scan": "all_mandatory",
            "true_result": "violation",
            "false_result": "no_violation",
            "unknown_result": "uncertain",
        }
    )
    collect_all = ConflictPolicy.model_validate(
        {
            "policy_kind": "conflict",
            "mode": "collect_all",
            "early_exit": False,
            "category_dedup": "set",
            "preserve_rule_refs": True,
        }
    )
    assert mandatory_policy.scan == "all_mandatory"
    assert collect_all.mode == "collect_all"
    assert not collect_all.early_exit
