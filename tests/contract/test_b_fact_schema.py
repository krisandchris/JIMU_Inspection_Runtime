"""Root normalization and closed metadata contract roundtrips."""

import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator
from pydantic import TypeAdapter

from jimu_runtime.domain.fact_schemas.models import (
    ConfidenceMetadataPolicy,
    CoordinateMetadata,
    FactSchemaDefinition,
    FactStatusPolicy,
    GlobalTypeLimits,
    UnitMetadata,
    ValueSchemaGenerationInput,
    ValueSchemaGenerationSpec,
)
from jimu_runtime.domain.fact_schemas.projection import generate_value_schema


def fixture() -> dict[str, object]:
    data = json.loads(
        (Path(__file__).parents[1] / "fixtures/wp01b/jcs_vectors.json").read_text()
    )
    return {**data["fact_schema"], "value_schema_generation": data["spec"]}


@pytest.mark.parametrize("kind", ["bool", "string", "integer", "number"])
def test_root_roundtrip_and_generated_schema(kind: str) -> None:
    raw = fixture()
    raw["type_definition"] = {"kind": kind}
    value = FactSchemaDefinition.model_validate(raw)
    schema = FactSchemaDefinition.model_json_schema()
    Draft202012Validator.check_schema(schema)
    Draft202012Validator(schema).validate(json.loads(value.model_dump_json()))
    assert FactSchemaDefinition.model_validate_json(value.model_dump_json()) == value
    assert FactSchemaDefinition.model_validate(value) == value


def test_every_helper_schema_is_draft202012() -> None:
    for model in (
        FactStatusPolicy,
        GlobalTypeLimits,
        ValueSchemaGenerationSpec,
        ValueSchemaGenerationInput,
    ):
        Draft202012Validator.check_schema(model.model_json_schema())
    for alias in (UnitMetadata, CoordinateMetadata, ConfidenceMetadataPolicy):
        Draft202012Validator.check_schema(TypeAdapter(alias).json_schema())


def test_root_and_generation_input_normalize_before_a_defaults_recursively() -> None:
    raw = fixture()
    small = dict(raw["value_schema_generation"])
    small["global_limits"] = {
        "string_max_length": 100,
        "list_max_items": 12,
        "record_max_fields": 4,
        "max_depth": 3,
    }
    raw["value_schema_generation"] = small
    tree = {
        "kind": "record",
        "fields": [
            {
                "name": "x",
                "required": True,
                "type_definition": {"kind": "list", "item_type": {"kind": "string"}},
            }
        ],
    }
    raw["type_definition"] = tree
    root = FactSchemaDefinition.model_validate(raw)
    child = root.type_definition.fields[0].type_definition
    assert child.max_items == 12
    assert child.item_type.max_length == 100
    generation_input = ValueSchemaGenerationInput.model_validate(
        {"projection": tree, "generation_spec": small}
    )
    assert generation_input.projection == root.type_definition
    assert (
        generate_value_schema(tree, generation_input.generation_spec).generation_input
        == generation_input
    )


def test_units_coordinates_and_confidence_are_parent_only_and_closed() -> None:
    raw = fixture()
    member = {
        "vocabulary_ref": {
            "artifact_kind": "vocabulary",
            "artifact_id": "units",
            "version": "1.0.0",
            "digest": "a" * 64,
        },
        "member": "synthetic-pixel",
    }
    raw["units"] = {
        "mode": "declared",
        "bindings": [{"field_path": [], "unit": member}],
    }
    raw["coordinate_system"] = {
        "mode": "declared",
        "bindings": [{"field_path": [], "coordinate_system": member}],
    }
    raw["confidence_metadata_policy"] = {
        "mode": "required",
        "allowed_fact_statuses": ["known", "unknown"],
        "interpretation": "model_reported_not_calibrated_probability",
        "numeric_format": "finite_number",
    }
    result = FactSchemaDefinition.model_validate(raw)
    assert result.units.bindings[0].field_path == ()
    assert result.confidence_metadata_policy.mode == "required"
    Draft202012Validator(result.model_json_schema()).validate(
        json.loads(result.model_dump_json())
    )
    # Membership/domain and actual EvidencePolicy subtype are deliberately not
    # claimed by this local shape check; later finite catalogue checks own them.
