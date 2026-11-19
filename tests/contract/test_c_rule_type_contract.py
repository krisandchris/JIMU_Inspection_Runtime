"""Finite checker contracts round-trip independently from condition wire models."""

from fixtures.wp01c.rule_types.support import Example, compare, literal, unit
from jsonschema import Draft202012Validator
from pydantic import TypeAdapter

from jimu_runtime.domain.rules import TypeCheckResult
from jimu_runtime.domain.validation.rule_type_inputs import (
    RuleTypeBindings,
    RuleTypeUseContext,
)


def test_checker_inputs_and_diagnostic_result_have_stable_roundtrip():
    example = Example()
    fact = example.question_fact(shape={"kind": "integer"}, units=[((), unit())])
    rule = example.rule(compare(fact, literal("integer", 4), "lt"))
    types = (
        (RuleTypeUseContext, example.context("rule_condition")),
        (RuleTypeBindings, example.bindings()),
        (TypeCheckResult, example.check(rule)),
    )
    for contract, original in types:
        adapter = TypeAdapter(contract)
        schema = adapter.json_schema()
        Draft202012Validator.check_schema(schema)
        Draft202012Validator(schema).validate(
            adapter.dump_python(original, mode="json")
        )
        assert adapter.validate_json(adapter.dump_json(original)) == original
