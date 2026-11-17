"""Foundation API schemas only; no Fact payload/Bundle schema generator."""

import json

import pytest
from jsonschema import Draft202012Validator
from jsonschema.exceptions import ValidationError as SchemaValidationError
from jsonschema.validators import validator_for
from pydantic import TypeAdapter, ValidationError

from jimu_runtime.domain import enums, values
from jimu_runtime.domain.primitives import ArtifactId, Digest, RecordId, Version
from jimu_runtime.domain.references import (
    ContentRef,
    IdentitySelector,
    RuntimeRecordRef,
    SchemaRef,
)

DIALECT = "https://json-schema.org/draft/2020-12/schema"
REFERENCE_PAIRS = [
    (ContentRef, "content_ref"),
    (IdentitySelector, "identity_selector"),
    (RuntimeRecordRef, "runtime_record_ref"),
    (SchemaRef, "schema_ref"),
]
SCHEMA_TYPES = [
    *[model for model, _ in REFERENCE_PAIRS],
    values.BooleanValue,
    values.IntegerValue,
    values.NumberValue,
    values.StringValue,
    values.EnumValue,
    values.ListValue,
    values.RecordValue,
    values.BooleanTypeDefinition,
    values.IntegerTypeDefinition,
    values.NumberTypeDefinition,
    values.StringTypeDefinition,
    values.EnumTypeDefinition,
    values.ListTypeDefinition,
    values.RecordTypeDefinition,
    values.RecordFieldDefinition,
    values.ValueContract,
    values.Value,
    values.TypeDefinition,
    Version,
    Digest,
    ArtifactId,
    RecordId,
    enums.ArtifactKind,
    enums.RuntimeRecordKind,
    enums.TaskStatus,
    enums.ExecutionStatus,
    enums.NodeRunStatus,
    enums.AttemptStatus,
    enums.FactStatus,
    enums.DecisionResult,
]


def foundation_schema(contract_type):
    """Use Pydantic's API, adding only the standard dialect declaration."""
    return {"$schema": DIALECT, **TypeAdapter(contract_type).json_schema()}


@pytest.mark.parametrize("contract_type", SCHEMA_TYPES)
def test_every_generated_schema_is_2020_12_and_repeatable(contract_type):
    schema = foundation_schema(contract_type)
    assert validator_for(schema) is Draft202012Validator
    Draft202012Validator.check_schema(schema)
    serialized = json.dumps(schema, sort_keys=True, ensure_ascii=False, allow_nan=False)
    assert serialized == json.dumps(
        foundation_schema(contract_type),
        sort_keys=True,
        ensure_ascii=False,
        allow_nan=False,
    )

    def check_refs(node):
        if isinstance(node, dict):
            if "$ref" in node:
                assert node["$ref"].startswith("#/$defs/")
            for child in node.values():
                check_refs(child)
        elif isinstance(node, list):
            for child in node:
                check_refs(child)

    check_refs(schema)


@pytest.mark.parametrize("model,key", REFERENCE_PAIRS)
def test_reference_schemas_accept_roundtrip_and_reject_injection(
    model, key, foundation_fixtures
):
    schema = foundation_schema(model)
    validator = Draft202012Validator(schema)
    raw = foundation_fixtures[key]
    validator.validate(raw)
    validator.validate(model.model_validate(raw).model_dump(mode="json"))
    for field in model.model_fields:
        with pytest.raises(SchemaValidationError):
            validator.validate({k: v for k, v in raw.items() if k != field})
    with pytest.raises(SchemaValidationError):
        validator.validate({**raw, "unregistered_field": True})


def test_value_schemas_accept_all_declared_positive_fixtures(foundation_fixtures):
    validator = Draft202012Validator(foundation_schema(values.ValueContract))
    value_validator = Draft202012Validator(foundation_schema(values.Value))
    type_validator = Draft202012Validator(foundation_schema(values.TypeDefinition))
    for raw in foundation_fixtures["values"]:
        validator.validate(raw)
        value_validator.validate(raw["value"])
        type_validator.validate(raw["type_definition"])
        validated = values.ValueContract.model_validate(raw)
        validator.validate(validated.model_dump(mode="json"))


@pytest.mark.parametrize(
    "kind,bad",
    [
        ("integer", False),
        ("integer", "0"),
        ("bool", 0),
        ("bool", "false"),
        ("number", "0.0"),
        ("string", False),
        ("enum", 0),
        ("string", "x" * 4097),
        ("list", [{"kind": "integer", "value": 0}] * 257),
        ("record", {f"f{i}": {"kind": "bool", "value": False} for i in range(65)}),
    ],
)
def test_schema_and_pydantic_reject_structural_invalid_values(kind, bad):
    payload = {"kind": kind, "value": bad}
    with pytest.raises(SchemaValidationError):
        Draft202012Validator(foundation_schema(values.Value)).validate(payload)
    with pytest.raises(ValidationError):
        TypeAdapter(values.Value).validate_python(payload)


@pytest.mark.parametrize(
    "primitive,bad",
    [
        (Version, "01.0.0"),
        (Version, "1.2.3\n"),
        (Version, "latest"),
        (ArtifactId, "id\n"),
        (RecordId, "ref\n"),
        (Digest, "A" * 64),
        (Digest, "a" * 63),
    ],
)
def test_schema_and_pydantic_reject_invalid_lexical_forms(primitive, bad):
    with pytest.raises(SchemaValidationError):
        Draft202012Validator(foundation_schema(primitive)).validate(bad)
    with pytest.raises(ValidationError):
        TypeAdapter(primitive).validate_python(bad)


def test_crossfield_semantics_require_domain_validation(foundation_fixtures):
    """JSON Schema cannot compare runtime members/fields across two properties."""
    cases = [
        {
            "type_definition": {"kind": "enum", "members": ["a"]},
            "value": {"kind": "enum", "value": "b"},
        },
        {
            "type_definition": {"kind": "record", "fields": []},
            "value": {
                "kind": "record",
                "value": {"extra": {"kind": "bool", "value": False}},
            },
        },
    ]
    validator = Draft202012Validator(foundation_schema(values.ValueContract))
    for raw in cases:
        validator.validate(raw)
        with pytest.raises(ValidationError):
            values.ValueContract.model_validate(raw)


def test_json_numeric_mathematics_do_not_define_python_coercion():
    """JSON Schema integer includes 1.0; strict Python integer does not."""
    raw = {"kind": "integer", "value": 1.0}
    Draft202012Validator(foundation_schema(values.IntegerValue)).validate(raw)
    with pytest.raises(ValidationError):
        values.IntegerValue.model_validate(raw)
    raw = {"kind": "number", "value": 1}
    Draft202012Validator(foundation_schema(values.NumberValue)).validate(raw)
    with pytest.raises(ValidationError):
        values.NumberValue.model_validate(raw)


def test_nonfinite_numbers_are_not_serializable_json():
    for value in [float("nan"), float("inf"), -float("inf")]:
        with pytest.raises(ValueError):
            json.dumps({"kind": "number", "value": value}, allow_nan=False)


@pytest.mark.parametrize(
    "definition",
    [
        {"kind": "enum", "members": [str(i) for i in range(257)]},
        {
            "kind": "record",
            "fields": [
                {"name": f"f{i}", "required": True, "type_definition": {"kind": "bool"}}
                for i in range(65)
            ],
        },
    ],
)
def test_type_container_limits_are_real_json_schema_constraints(definition):
    with pytest.raises(SchemaValidationError):
        Draft202012Validator(foundation_schema(values.TypeDefinition)).validate(
            definition
        )
    with pytest.raises(ValidationError):
        TypeAdapter(values.TypeDefinition).validate_python(definition)


def test_two_stage_validation_covers_optional_record_list_fields():
    definition = {
        "kind": "list",
        "item_type": {
            "kind": "record",
            "fields": [
                {
                    "name": "count",
                    "required": True,
                    "type_definition": {"kind": "integer"},
                },
                {
                    "name": "note",
                    "required": False,
                    "type_definition": {"kind": "string"},
                },
            ],
        },
    }
    payload = {
        "kind": "list",
        "value": [
            {"kind": "record", "value": {"count": {"kind": "integer", "value": 0}}},
            {
                "kind": "record",
                "value": {
                    "count": {"kind": "integer", "value": 0},
                    "note": {"kind": "string", "value": ""},
                },
            },
        ],
    }
    raw = {"type_definition": definition, "value": payload}
    Draft202012Validator(foundation_schema(values.ValueContract)).validate(raw)
    assert values.ValueContract.model_validate(raw).value.kind == "list"
    raw["value"]["value"][0]["value"]["count"] = {"kind": "bool", "value": False}
    with pytest.raises(ValidationError):
        values.ValueContract.model_validate(raw)
