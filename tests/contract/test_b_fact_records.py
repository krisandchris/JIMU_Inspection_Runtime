"""Local runtime data shapes, not producer/authority admission or invocation proof."""

import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator, FormatChecker
from pydantic import TypeAdapter

from jimu_runtime.domain.enums import FactStatus
from jimu_runtime.domain.runtime.authority import AuthoritativeResultSelection
from jimu_runtime.domain.runtime.facts import FactRecord, FactSource
from jimu_runtime.domain.validation.authority_inputs import (
    AttemptDescriptor,
    MappedOutputResult,
    NodeRunDescriptor,
    ObjectDescriptor,
    ObservationResultDescriptor,
    QuestionSelectionDescriptor,
    SystemOutputResultDescriptor,
    ToolStepResultDescriptor,
)


def fixture(name: str) -> dict:
    path = Path(__file__).parents[1] / "fixtures/wp01b/a1/contracts.json"
    return json.loads(path.read_text())[name]


@pytest.mark.parametrize(
    "name",
    [
        "known",
        "unknown",
        "error",
        "not_applicable",
        "not_evaluated",
        "tool_fact",
        "system_fact",
    ],
)
def test_five_fact_statuses_and_three_sources_roundtrip(name: str) -> None:
    raw = fixture(name)
    adapter = TypeAdapter(FactRecord)
    schema = adapter.json_schema()
    Draft202012Validator.check_schema(schema)
    Draft202012Validator(schema, format_checker=FormatChecker()).validate(raw)
    item = adapter.validate_json(json.dumps(raw))
    assert item.status is FactStatus(raw["status"])
    assert item.model_dump(mode="json") == raw
    assert adapter.validate_python(item) == item
    assert adapter.validate_json(item.model_dump_json()) == item
    assert TypeAdapter(FactSource).validate_python(item.source) == item.source
    assert "scope" not in type(item.source).model_fields


@pytest.mark.parametrize(
    "name", ["observation_selection", "tool_selection", "system_selection"]
)
def test_authority_group_shapes_preserve_explicit_pins(name: str) -> None:
    raw = fixture(name)
    adapter = TypeAdapter(AuthoritativeResultSelection)
    schema = adapter.json_schema()
    Draft202012Validator.check_schema(schema)
    Draft202012Validator(schema, format_checker=FormatChecker()).validate(raw)
    item = adapter.validate_json(json.dumps(raw))
    assert item.model_dump(mode="json") == raw
    assert adapter.validate_python(item) == item
    assert item.expected_previous_selection_ref is None


@pytest.mark.parametrize(
    "name,model",
    [
        ("object", ObjectDescriptor),
        ("node", NodeRunDescriptor),
        ("node_attempt", AttemptDescriptor),
        ("system_attempt", AttemptDescriptor),
        ("question_selection", QuestionSelectionDescriptor),
        ("observation_result", ObservationResultDescriptor),
        ("tool_result", ToolStepResultDescriptor),
        ("system_result", SystemOutputResultDescriptor),
    ],
)
def test_descriptor_shapes_and_schema(name: str, model: type) -> None:
    raw = fixture(name)
    schema = model.model_json_schema()
    Draft202012Validator.check_schema(schema)
    Draft202012Validator(schema).validate(raw)
    item = model.model_validate(raw)
    assert item.model_dump(mode="json") == raw
    assert model.model_validate(item) == item
    assert model.model_validate_json(item.model_dump_json()) == item


def test_mapped_output_is_emitted_fact_or_explicit_absence() -> None:
    adapter = TypeAdapter(MappedOutputResult)
    for raw in (
        fixture("tool_result")["mapped_outputs"][0],
        fixture("system_result")["output"],
    ):
        Draft202012Validator(adapter.json_schema()).validate(raw)
        assert adapter.validate_python(raw).model_dump(mode="json") == raw
