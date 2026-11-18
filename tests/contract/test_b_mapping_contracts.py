"""Generated schema/strict roundtrip coverage for every frozen S2 union branch."""

import json
from pathlib import Path
from typing import cast

import pytest
from jsonschema import Draft202012Validator, FormatChecker
from pydantic import TypeAdapter, ValidationError

from jimu_runtime.domain.schema_types import SchemaDescriptor
from jimu_runtime.domain.tool_mappings import (
    InputBinding,
    InputSource,
    MappingTypeContext,
)
from jimu_runtime.domain.validation.review_inputs import (
    ReviewAssessment,
    ReviewMetadata,
    ReviewScope,
)


def fixtures() -> dict[str, object]:
    return cast(
        dict[str, object],
        json.loads(
            (Path(__file__).parents[1] / "fixtures/wp01b/s2/contracts.json").read_text()
        ),
    )


@pytest.mark.parametrize("name", ["raw", "typed", "mixed", "tool"])
def test_all_descriptor_branches_schema_and_roundtrip(name: str) -> None:
    adapter = TypeAdapter(SchemaDescriptor)
    schema = adapter.json_schema()
    Draft202012Validator.check_schema(schema)
    raw = fixtures()[name]
    Draft202012Validator(schema).validate(raw)
    model = adapter.validate_python(raw)
    assert adapter.validate_json(adapter.dump_json(model)) == model
    assert adapter.validate_python(model) == model
    assert adapter.dump_python(model, mode="json") == raw
    with pytest.raises(ValidationError):
        model.schema_ref = model.schema_ref


@pytest.mark.parametrize("index", range(5))
def test_all_input_source_branches(index: int) -> None:
    raw = cast(list[object], fixtures()["sources"])[index]
    adapter = TypeAdapter(InputSource)
    schema = adapter.json_schema()
    Draft202012Validator.check_schema(schema)
    Draft202012Validator(schema).validate(raw)
    model = adapter.validate_python(raw)
    assert adapter.validate_json(adapter.dump_json(model)) == model


@pytest.mark.parametrize("index", range(4))
def test_all_review_scope_branches(index: int) -> None:
    adapter = TypeAdapter(ReviewScope)
    raw = cast(list[object], fixtures()["scopes"])[index]
    schema = adapter.json_schema()
    Draft202012Validator.check_schema(schema)
    Draft202012Validator(schema).validate(raw)
    model = adapter.validate_python(raw)
    assert adapter.validate_json(adapter.dump_json(model)) == model


def test_review_metadata_and_both_assessments_are_data_not_approval() -> None:
    raw = fixtures()["review"]
    schema = ReviewMetadata.model_json_schema()
    Draft202012Validator.check_schema(schema)
    Draft202012Validator(schema, format_checker=FormatChecker()).validate(raw)
    review = ReviewMetadata.model_validate_json(json.dumps(raw))
    assert ReviewMetadata.model_validate(review) == review
    assert ReviewMetadata.model_validate_json(review.model_dump_json()) == review
    assessment_adapter = TypeAdapter(ReviewAssessment)
    for assessment in review.assessments:
        Draft202012Validator(assessment_adapter.json_schema()).validate(
            assessment.model_dump(mode="json")
        )
    # A valid synthetic assertion is not an actual reviewed report verification.
    assert review.verdict == "approved"


def test_binding_schema_and_mapping_context_root_are_closed() -> None:
    adapter = TypeAdapter(tuple[InputBinding, ...])
    bindings = adapter.validate_python(fixtures()["bindings"])
    assert adapter.validate_json(adapter.dump_json(bindings)) == bindings
    schema = MappingTypeContext.model_json_schema()
    Draft202012Validator.check_schema(schema)
    assert schema["additionalProperties"] is False
    assert "schema_version" not in schema["properties"]


def test_mapping_context_finite_registration_roundtrip() -> None:
    raw = fixtures()["mapping_context"]
    schema = MappingTypeContext.model_json_schema()
    Draft202012Validator(schema).validate(raw)
    model = MappingTypeContext.model_validate(raw)
    assert MappingTypeContext.model_validate_json(model.model_dump_json()) == model
