"""Independent JCS vectors and local projection identity; no Bundle admission."""

import json
import struct
from hashlib import sha256
from pathlib import Path
from typing import cast

import pytest
from jsonschema import Draft202012Validator

from jimu_runtime.domain.fact_schemas.canonicalization import JSONValue, canonical_bytes
from jimu_runtime.domain.fact_schemas.models import (
    FactSchemaDefinition,
    GlobalTypeLimits,
    ValueSchemaGenerationSpec,
)
from jimu_runtime.domain.fact_schemas.projection import (
    generate_value_schema,
    normalize_type,
    validate_child_reference,
)
from jimu_runtime.domain.values import validate_typed_value

VECTORS = json.loads(
    (Path(__file__).parents[1] / "fixtures/wp01b/jcs_vectors.json").read_text()
)


@pytest.mark.parametrize("hex_bits,expected", VECTORS["numbers"])
def test_rfc_appendix_b(hex_bits: str, expected: str) -> None:
    value = struct.unpack(">d", bytes.fromhex(hex_bits))[0]
    assert canonical_bytes(value) == expected.encode("ascii")


@pytest.mark.parametrize("name", ["utf16", "escaping"])
def test_independent_unicode_and_escape_vectors(name: str) -> None:
    fixture = VECTORS[name]
    assert canonical_bytes(cast(JSONValue, fixture["input"])) == fixture[
        "expected"
    ].encode("utf-8")


def spec() -> ValueSchemaGenerationSpec:
    return ValueSchemaGenerationSpec.model_validate(VECTORS["spec"])


def test_boolean_projection_has_independently_expected_content_and_hashes() -> None:
    result = generate_value_schema({"kind": "bool"}, spec())
    expected_input = (
        b'{"generation_spec":{"canonicalization_id":"JCS","generator_id":"jimu.value-schema",'
        b'"generator_version":"1.0.0","global_limits":{"list_max_items":256,"max_depth":8,'
        b'"record_max_fields":64,"string_max_length":4096},"projection_profile_version":"1.0.0",'
        b'"schema_dialect":"https://json-schema.org/draft/2020-12/schema"},"projection":{"kind":"bool"}}'
    )
    digest = sha256(expected_input).hexdigest()
    expected_schema = (
        '{"$id":"urn:jimu:value-schema:1.0.0:' + digest + '",'
        '"$schema":"https://json-schema.org/draft/2020-12/schema",'
        '"additionalProperties":false,"properties":{"kind":{"const":"bool"},'
        '"value":{"type":"boolean"}},"required":["kind","value"],"type":"object"}'
    ).encode()
    assert result.canonical_input_bytes == expected_input
    assert result.input_sha256 == digest
    assert result.canonical_schema_bytes == expected_schema
    assert result.child_digest == sha256(expected_schema).hexdigest()
    assert result.child_digest != digest
    assert result.child_ref.artifact_id == "value-schema." + digest
    assert result.child_ref.version == "1.0.0"
    assert result.child_ref.digest == result.child_digest


@pytest.mark.parametrize(
    "definition,value",
    [
        ({"kind": "bool"}, {"kind": "bool", "value": False}),
        ({"kind": "integer", "minimum": 0}, {"kind": "integer", "value": 0}),
        ({"kind": "number", "maximum": 1.5}, {"kind": "number", "value": -0.5}),
        ({"kind": "string", "max_length": 3}, {"kind": "string", "value": ""}),
        ({"kind": "enum", "members": ["z", "a"]}, {"kind": "enum", "value": "a"}),
        (
            {"kind": "list", "item_type": {"kind": "bool"}},
            {"kind": "list", "value": [{"kind": "bool", "value": True}]},
        ),
        (
            {
                "kind": "record",
                "fields": [
                    {
                        "name": "x",
                        "required": True,
                        "type_definition": {"kind": "integer"},
                    }
                ],
            },
            {"kind": "record", "value": {"x": {"kind": "integer", "value": 2}}},
        ),
    ],
)
def test_all_seven_generated_payload_schemas(definition: object, value: object) -> None:
    result = generate_value_schema(definition, spec())
    schema = json.loads(result.canonical_schema_bytes)
    Draft202012Validator.check_schema(schema)
    Draft202012Validator(schema).validate(value)
    validate_typed_value(result.generation_input.projection, value)
    assert not Draft202012Validator(schema).is_valid(
        {"kind": "unexpected", "value": None}
    )
    assert generate_value_schema(result.generation_input.projection, spec()) == result


def test_source_order_normalizes_by_utf8_independently_of_jcs_object_order() -> None:
    fields = [
        {
            "name": "😀",
            "required": True,
            "type_definition": {"kind": "enum", "members": ["😀", "דּ", "a"]},
        },
        {"name": "דּ", "required": False, "type_definition": {"kind": "string"}},
    ]
    left = generate_value_schema({"kind": "record", "fields": fields}, spec())
    right = generate_value_schema(
        {"kind": "record", "fields": list(reversed(fields))}, spec()
    )
    assert left == right
    assert [field.name for field in left.generation_input.projection.fields] == [
        "דּ",
        "😀",
    ]
    content = json.loads(left.canonical_schema_bytes)
    assert content["properties"]["value"]["required"] == ["😀"]
    assert b'"additionalProperties":false' in left.canonical_schema_bytes


def test_parent_identity_semantics_and_policies_do_not_enter_child() -> None:
    raw = {**VECTORS["fact_schema"], "value_schema_generation": VECTORS["spec"]}
    result = generate_value_schema(raw["type_definition"], spec())
    raw["payload_schema_ref"] = result.child_ref.model_dump(mode="json")
    first = FactSchemaDefinition.model_validate(raw)
    second = FactSchemaDefinition.model_validate(
        {
            **raw,
            "schema_id": "different",
            "version": "2.0.0",
            "semantic_key": "different.meaning",
            "allowed_producer_kinds": ["system"],
        }
    )
    assert validate_child_reference(first) == validate_child_reference(second)
    assert b"semantic_key" not in result.canonical_input_bytes
    assert b"payload_schema_ref" not in result.canonical_input_bytes


def test_explicit_generation_limit_changes_only_explicit_generation_identity() -> None:
    limits = GlobalTypeLimits(
        string_max_length=100, list_max_items=10, record_max_fields=4, max_depth=2
    )
    normalized = normalize_type({"kind": "string"}, limits)
    assert normalized.max_length == 100
    smaller = ValueSchemaGenerationSpec.model_validate(
        {**VECTORS["spec"], "global_limits": limits}
    )
    assert (
        generate_value_schema({"kind": "bool"}, smaller).child_ref
        != generate_value_schema({"kind": "bool"}, spec()).child_ref
    )
