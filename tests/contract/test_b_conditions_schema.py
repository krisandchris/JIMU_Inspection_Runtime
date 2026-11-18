"""Schema shape is engineering evidence; budgets/domain/types require other checks."""

import json
from pathlib import Path
from typing import cast

import pytest
from jsonschema import Draft202012Validator
from pydantic import TypeAdapter

from jimu_runtime.domain.conditions import (
    ConditionExpr,
    ConditionReferences,
    LocalToolFactSelector,
)


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
def test_every_node_generated_schema_shape(name: str) -> None:
    data = cast(
        dict[str, dict[str, object]],
        json.loads(
            (Path(__file__).parents[1] / "fixtures/wp01b/x1/contracts.json").read_text()
        ),
    )
    adapter = TypeAdapter(ConditionExpr)
    schema = adapter.json_schema()
    Draft202012Validator.check_schema(schema)
    Draft202012Validator(schema).validate(data["expressions"][name])
    assert adapter.validate_json(json.dumps(data["expressions"][name])).kind


def test_shared_helper_schemas_are_closed() -> None:
    for model in [ConditionReferences, LocalToolFactSelector]:
        schema = model.model_json_schema()
        Draft202012Validator.check_schema(schema)
        assert schema["additionalProperties"] is False
    properties = LocalToolFactSelector.model_json_schema()["properties"]
    assert "tool_plan_ref" not in properties
    assert set(properties) == {
        "scope",
        "step_id",
        "tool_ref",
        "output_name",
        "fact_key",
        "fact_schema_ref",
        "field_path",
    }
