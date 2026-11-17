import json

import pytest
from pydantic import TypeAdapter, ValidationError

from jimu_runtime.domain.values import (
    BooleanValue,
    EnumValue,
    IntegerValue,
    ListValue,
    NumberValue,
    RecordValue,
    StringValue,
    TypeDefinition,
    Value,
    ValueContract,
)


def rejects_contract(definition, payload):
    raw = {"type_definition": definition, "value": payload}
    with pytest.raises(ValidationError):
        ValueContract.model_validate(raw)
    with pytest.raises(ValidationError):
        ValueContract.model_validate_json(json.dumps(raw))


@pytest.mark.parametrize("bad", [True, False, "0", 0.0, None])
def test_integer_refuses_boolean_and_coercion(bad):
    rejects_contract({"kind": "integer"}, {"kind": "integer", "value": bad})


@pytest.mark.parametrize(
    "bad", [0, 1, "0.0", True, False, None, float("nan"), float("inf"), -float("inf")]
)
def test_number_is_finite_and_never_coerces(bad):
    rejects_contract({"kind": "number"}, {"kind": "number", "value": bad})


@pytest.mark.parametrize("bad", [0, 1, "false", "True", None])
def test_boolean_is_not_truthiness(bad):
    rejects_contract({"kind": "bool"}, {"kind": "bool", "value": bad})


@pytest.mark.parametrize("bad", [0, False, None, ["text"], "x" * 4097])
def test_string_is_strict_and_bounded(bad):
    rejects_contract({"kind": "string"}, {"kind": "string", "value": bad})


@pytest.mark.parametrize(
    "model,kind",
    [
        (BooleanValue, "bool"),
        (IntegerValue, "integer"),
        (NumberValue, "number"),
        (StringValue, "string"),
        (EnumValue, "enum"),
        (ListValue, "list"),
        (RecordValue, "record"),
    ],
)
def test_missing_null_and_injected_fields(model, kind):
    for raw in ({"kind": kind}, {"kind": kind, "value": None}):
        with pytest.raises(ValidationError):
            model.model_validate(raw)
    with pytest.raises(ValidationError):
        TypeAdapter(Value).validate_python(
            {"kind": kind, "value": None, "command": "injected"}
        )


@pytest.mark.parametrize("bad", ["unknown", "null", "object", "float", "boolean"])
def test_unregistered_value_kind(bad):
    with pytest.raises(ValidationError):
        TypeAdapter(Value).validate_python({"kind": bad, "value": 0})


def test_enum_membership_is_external_to_payload():
    definition = {"kind": "enum", "members": ["parked", "moving"]}
    rejects_contract(definition, {"kind": "enum", "value": "missing"})
    rejects_contract(
        definition, {"kind": "enum", "value": "missing", "members": ["missing"]}
    )


def test_list_element_type_and_bounds():
    definition = {
        "kind": "list",
        "item_type": {"kind": "integer"},
        "min_items": 1,
        "max_items": 2,
    }
    rejects_contract(definition, {"kind": "list", "value": []})
    rejects_contract(
        definition, {"kind": "list", "value": [{"kind": "integer", "value": 0}] * 3}
    )
    rejects_contract(
        definition, {"kind": "list", "value": [{"kind": "bool", "value": False}]}
    )
    rejects_contract(
        definition,
        {
            "kind": "list",
            "value": [
                {"kind": "integer", "value": 0},
                {"kind": "bool", "value": False},
            ],
        },
    )
    with pytest.raises(ValidationError):
        ListValue.model_validate({"kind": "list", "value": {1, 2}})


def test_record_required_closed_optional_and_wrong_field_type():
    definition = {
        "kind": "record",
        "fields": [
            {"name": "count", "required": True, "type_definition": {"kind": "integer"}}
        ],
    }
    rejects_contract(definition, {"kind": "record", "value": {}})
    rejects_contract(
        definition,
        {
            "kind": "record",
            "value": {
                "count": {"kind": "integer", "value": 0},
                "extra": {"kind": "bool", "value": False},
            },
        },
    )
    rejects_contract(
        definition,
        {"kind": "record", "value": {"count": {"kind": "bool", "value": False}}},
    )
    rejects_contract(
        {"kind": "record", "fields": []},
        {"kind": "record", "value": {"undeclared": {"kind": "integer", "value": 0}}},
    )


@pytest.mark.parametrize(
    "definition",
    [
        {"kind": "enum", "members": []},
        {"kind": "enum", "members": ["a", "a"]},
        {"kind": "enum", "members": [True]},
        {"kind": "integer", "minimum": 2, "maximum": 1},
        {"kind": "number", "minimum": float("nan")},
        {"kind": "string", "min_length": 3, "max_length": 2},
        {"kind": "string", "max_length": 4097},
        {"kind": "list", "item_type": {"kind": "bool"}, "min_items": -1},
        {"kind": "list", "item_type": {"kind": "bool"}, "max_items": 257},
        {"kind": "list", "item_type": {"kind": "bool"}, "min_items": 2, "max_items": 1},
        {
            "kind": "record",
            "fields": [
                {"name": "x", "required": True, "type_definition": {"kind": "bool"}}
            ]
            * 2,
        },
        {
            "kind": "record",
            "fields": [{"name": "x", "type_definition": {"kind": "bool"}}],
        },
        {"kind": "record", "fields": [], "additional_fields": True},
        {"kind": "bool", "extensions": {}},
        {"kind": "any"},
    ],
)
def test_invalid_type_definitions(definition):
    with pytest.raises(ValidationError):
        TypeAdapter(TypeDefinition).validate_python(definition)


def test_recursive_type_and_value_are_rejected():
    definition = {"kind": "list"}
    definition["item_type"] = definition
    with pytest.raises(ValidationError):
        TypeAdapter(TypeDefinition).validate_python(definition)
    payload = {"kind": "list", "value": []}
    payload["value"].append(payload)
    with pytest.raises(ValidationError):
        TypeAdapter(Value).validate_python(payload)


@pytest.mark.parametrize("fixture_index", range(7))
def test_valid_payload_cannot_inject_extra_model_fields(
    fixture_index, foundation_fixtures
):
    raw = foundation_fixtures["values"][fixture_index]
    ValueContract.model_validate(raw)
    injected = {**raw, "value": {**raw["value"], "type_definition": {"kind": "any"}}}
    with pytest.raises(ValidationError, match="extra_forbidden"):
        ValueContract.model_validate(injected)


def test_contract_type_and_empty_enums_cannot_be_self_authorized():
    with pytest.raises(ValidationError):
        ValueContract.model_validate(
            {
                "type_definition": {"kind": "enum", "members": ["allowed"]},
                "value": {
                    "kind": "enum",
                    "value": "forbidden",
                    "allowed_values": ["forbidden"],
                },
            }
        )
