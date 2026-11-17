import json

import pytest
from pydantic import TypeAdapter, ValidationError

from jimu_runtime.domain.values import (
    BooleanValue,
    IntegerValue,
    ListValue,
    NumberValue,
    RecordValue,
    StringValue,
    TypeDefinition,
    Value,
    ValueContract,
    validate_typed_value,
)


def contract(definition, value):
    return ValueContract.model_validate({"type_definition": definition, "value": value})


def test_zero_false_and_none_remain_distinct():
    assert IntegerValue.model_validate({"kind": "integer", "value": 0}).value == 0
    assert BooleanValue.model_validate({"kind": "bool", "value": False}).value is False
    assert (
        type(NumberValue.model_validate({"kind": "number", "value": 0.0}).value)
        is float
    )
    assert StringValue.model_validate({"kind": "string", "value": ""}).value == ""
    for kind in ("integer", "bool", "number", "string", "enum", "list", "record"):
        with pytest.raises(ValidationError):
            TypeAdapter(Value).validate_python({"kind": kind, "value": None})


@pytest.mark.parametrize("length", [0, 1, 4096])
def test_string_lengths_and_unicode(length):
    obj = contract({"kind": "string"}, {"kind": "string", "value": "车" * length})
    assert len(obj.value.value) == length
    assert ValueContract.model_validate_json(obj.model_dump_json()) == obj


def test_smaller_string_length_contract():
    definition = {"kind": "string", "min_length": 1, "max_length": 3}
    for length in (1, 3):
        contract(definition, {"kind": "string", "value": "a" * length})
    for length in (0, 4):
        with pytest.raises(ValidationError):
            contract(definition, {"kind": "string", "value": "a" * length})


@pytest.mark.parametrize("length", [0, 1, 256])
def test_list_lengths_and_immutable_snapshot(length):
    raw = [{"kind": "integer", "value": 0}] * length
    obj = contract(
        {"kind": "list", "item_type": {"kind": "integer"}},
        {"kind": "list", "value": raw},
    )
    raw.append({"kind": "integer", "value": 1})
    assert isinstance(obj.value.value, tuple)
    assert len(obj.value.value) == length
    assert ValueContract.model_validate_json(obj.model_dump_json()) == obj
    with pytest.raises(ValidationError):
        ListValue.model_validate(
            {"kind": "list", "value": [{"kind": "integer", "value": 0}] * 257}
        )


def test_record_fields_maximum_and_input_order():
    fields = [
        {"name": f"f{i}", "required": True, "type_definition": {"kind": "integer"}}
        for i in range(64)
    ]
    raw = {f"f{i}": {"kind": "integer", "value": 0} for i in range(64)}
    definition = {"kind": "record", "fields": fields}
    first = contract(definition, {"kind": "record", "value": raw})
    second = contract(
        definition, {"kind": "record", "value": dict(reversed(list(raw.items())))}
    )
    assert first.stable_json() == second.stable_json()
    raw["extra"] = {"kind": "integer", "value": 0}
    assert len(first.value.value) == 64
    with pytest.raises(ValidationError):
        RecordValue.model_validate({"kind": "record", "value": raw})
    with pytest.raises(ValidationError):
        TypeAdapter(TypeDefinition).validate_python(
            {
                "kind": "record",
                "fields": fields
                + [
                    {
                        "name": "extra",
                        "required": False,
                        "type_definition": {"kind": "bool"},
                    }
                ],
            }
        )


def test_optional_record_fields_may_be_absent_but_not_null():
    definition = {
        "kind": "record",
        "fields": [
            {"name": "note", "required": False, "type_definition": {"kind": "string"}}
        ],
    }
    contract(definition, {"kind": "record", "value": {}})
    with pytest.raises(ValidationError):
        contract(definition, {"kind": "record", "value": {"note": None}})


@pytest.mark.parametrize("depth", [1, 8, 9])
def test_container_depth_bound_for_values_and_definitions(depth):
    definition = {"kind": "bool"}
    value = {"kind": "bool", "value": False}
    for _ in range(depth):
        definition = {"kind": "list", "item_type": definition}
        value = {"kind": "list", "value": [value]}
    if depth <= 8:
        result = contract(definition, value)
        assert ValueContract.model_validate_json(result.model_dump_json()) == result
    else:
        with pytest.raises(ValidationError):
            TypeAdapter(TypeDefinition).validate_python(definition)
        with pytest.raises(ValidationError):
            TypeAdapter(Value).validate_python(value)


@pytest.mark.parametrize("kind,values", [("integer", [0, 2]), ("number", [0.0, 2.0])])
def test_numeric_bounds_are_inclusive(kind, values):
    definition = TypeAdapter(TypeDefinition).validate_python(
        {"kind": kind, "minimum": 0, "maximum": 2}
    )
    for value in values:
        result = validate_typed_value(definition, {"kind": kind, "value": value})
        assert result.value == value
    for value in (-1 if kind == "integer" else -1.0, 3 if kind == "integer" else 3.0):
        with pytest.raises(ValidationError):
            validate_typed_value(definition, {"kind": kind, "value": value})


def test_json_whole_object_order_and_snapshot():
    raw = {
        "type_definition": {"kind": "record", "fields": []},
        "value": {"kind": "record", "value": {}},
    }
    first = ValueContract.model_validate_json(json.dumps(raw))
    second = ValueContract.model_validate_json(
        json.dumps(dict(reversed(list(raw.items()))))
    )
    assert first.stable_json() == second.stable_json()


def test_definition_containers_are_snapshots_too():
    raw_members = ["a", "b"]
    definition = TypeAdapter(TypeDefinition).validate_python(
        {"kind": "enum", "members": raw_members}
    )
    raw_members.append("intrusion")
    assert definition.members == ("a", "b")
    with pytest.raises(ValidationError):
        validate_typed_value(definition, {"kind": "enum", "value": "intrusion"})
    record_type = TypeAdapter(TypeDefinition).validate_python(
        {"kind": "record", "fields": []}
    )
    assert record_type.fields == ()
    with pytest.raises(ValidationError, match="frozen_instance"):
        definition.members = ("intrusion",)


def test_mixed_record_list_nesting_is_bounded():
    definition = {"kind": "bool"}
    payload = {"kind": "bool", "value": False}
    for depth in range(1, 10):
        definition = {
            "kind": "record",
            "fields": [
                {"name": "nested", "required": True, "type_definition": definition}
            ],
        }
        payload = {"kind": "record", "value": {"nested": payload}}
        if depth <= 8:
            assert contract(definition, payload).value.kind == "record"
        else:
            with pytest.raises(ValidationError):
                contract(definition, payload)
