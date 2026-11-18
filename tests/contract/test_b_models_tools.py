"""Strict persisted Model/Tool/ToolPlan JSON Schema and roundtrip evidence."""

import json
from pathlib import Path
from typing import cast

import pytest
from jsonschema import Draft202012Validator

from jimu_runtime.domain.base import ContractModel
from jimu_runtime.domain.definitions.models import ModelBinding
from jimu_runtime.domain.definitions.tools import ToolDefinition, ToolPlanDefinition
from jimu_runtime.domain.tool_outputs import ToolFactOutputDeclaration


@pytest.mark.parametrize(
    "name,model",
    [
        ("model", ModelBinding),
        ("tool", ToolDefinition),
        ("plan", ToolPlanDefinition),
        ("output", ToolFactOutputDeclaration),
    ],
)
def test_models_schema_roundtrip(name: str, model: type[ContractModel]) -> None:
    fixtures = cast(
        dict[str, object],
        json.loads(
            (Path(__file__).parents[1] / "fixtures/wp01b/m1/contracts.json").read_text()
        ),
    )
    data = fixtures[name]
    schema = model.model_json_schema()
    Draft202012Validator.check_schema(schema)
    Draft202012Validator(schema).validate(data)
    result = model.model_validate(data)
    assert model.model_validate(result) == result
    assert model.model_validate_json(result.model_dump_json()) == result
    assert result.model_dump(mode="json") == data
