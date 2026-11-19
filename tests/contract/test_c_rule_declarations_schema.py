"""Source-derived Rule and static type-check contract schemas."""

import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator
from jsonschema import ValidationError as SchemaValidationError
from pydantic import TypeAdapter, ValidationError

from jimu_runtime.domain.rules import (
    FailedTypeCheck,
    PassedTypeCheck,
    RuleDefinition,
    RuleSetDefinition,
    TypeCheckResult,
)

FIXTURES = Path(__file__).parents[1] / "fixtures/wp01c/rule_declarations"


@pytest.mark.parametrize(
    ("model", "name"),
    [(RuleDefinition, "rule"), (RuleSetDefinition, "rule-set")],
)
def test_definition_schemas_validate_strict_persisted_roots(
    model: type[RuleDefinition] | type[RuleSetDefinition], name: str
) -> None:
    schema = model.model_json_schema()
    Draft202012Validator.check_schema(schema)
    assert schema["additionalProperties"] is False
    assert "schema_version" in schema["required"]
    validator = Draft202012Validator(schema)
    raw_valid = (FIXTURES / f"{name}-valid.json").read_bytes()
    raw_invalid = (FIXTURES / f"{name}-invalid.json").read_bytes()
    validator.validate(json.loads(raw_valid))
    parsed = model.model_validate_json(raw_valid)
    validator.validate(parsed.model_dump(mode="json"))
    assert model.model_validate_json(parsed.model_dump_json()) == parsed
    with pytest.raises(SchemaValidationError):
        validator.validate(json.loads(raw_invalid))
    with pytest.raises(ValidationError):
        model.model_validate_json(raw_invalid)


def test_type_check_result_discriminates_domain_boolean_result_and_diagnostics() -> (
    None
):
    adapter = TypeAdapter(TypeCheckResult)
    passed = {
        "status": "passed",
        "input_domain": "rule_condition",
        "inferred_type": {"kind": "bool"},
    }
    failed = {
        "status": "incomplete",
        "input_domain": "tool_condition",
        "issues": [
            {
                "code": "producer.unavailable",
                "path": ["condition_ast", "arguments", 1],
                "message": "No exact producer was supplied",
                "related_refs": [],
            }
        ],
    }
    schema = adapter.json_schema()
    Draft202012Validator.check_schema(schema)
    assert schema["discriminator"]["propertyName"] == "status"
    assert set(schema["discriminator"]["mapping"]) == {
        "passed",
        "invalid",
        "incomplete",
    }
    validator = Draft202012Validator(schema)
    for raw, model in [(passed, PassedTypeCheck), (failed, FailedTypeCheck)]:
        validator.validate(raw)
        result = adapter.validate_python(raw)
        assert isinstance(result, model)
        assert adapter.validate_json(adapter.dump_json(result)) == result


@pytest.mark.parametrize(
    "bad",
    [
        {"status": "passed", "inferred_type": {"kind": "string"}},
        {"status": "passed", "inferred_type": {"kind": "bool"}, "issues": []},
        {"status": "invalid", "issues": []},
        {"status": "incomplete", "inferred_type": {"kind": "bool"}, "issues": []},
        {"status": "deferred", "issues": []},
        {
            "status": "passed",
            "inferred_type": {"kind": "bool"},
            "input_domain": "producer_activation",
        },
    ],
)
def test_type_check_result_rejects_incompatible_branch_and_unknown_domain(
    bad: dict[str, object],
) -> None:
    raw = {"input_domain": "rule_condition", **bad}
    adapter = TypeAdapter(TypeCheckResult)
    with pytest.raises(ValidationError):
        adapter.validate_python(raw)
    with pytest.raises(SchemaValidationError):
        Draft202012Validator(adapter.json_schema()).validate(raw)
