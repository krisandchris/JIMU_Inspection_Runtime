"""Generated payload Schema plus strict Registry validation; no runtime jsonschema import."""

import json

import pytest
from fixtures.wp01b.r1.support import fixtures, registration
from jsonschema import Draft202012Validator

from jimu_runtime.domain.fact_schemas.registry import (
    FactSchemaRegistryBuilder,
    RegistryValidationError,
)


@pytest.mark.parametrize("tree,payload", fixtures()["values"])
def test_generated_payload_and_registry_two_stage_acceptance(tree, payload):
    ref, definition, generated = registration(tree)
    schema = json.loads(generated.canonical_schema_bytes)
    Draft202012Validator.check_schema(schema)
    Draft202012Validator(schema).validate(payload)
    registry = FactSchemaRegistryBuilder().register(ref, definition).freeze()
    accepted = registry.validate_payload(ref, payload)
    assert registry.validate_payload(ref, accepted.model_dump(mode="json")) == accepted
    described = registry.describe_type(ref)
    Draft202012Validator.check_schema(type(described).model_json_schema())
    Draft202012Validator(type(described).model_json_schema()).validate(
        described.model_dump(mode="json")
    )


@pytest.mark.parametrize("kind,value", [("integer", 1.0), ("number", 1)])
def test_json_schema_numeric_representation_does_not_weaken_a(kind, value):
    ref, definition, generated = registration({"kind": kind})
    payload = {"kind": kind, "value": value}
    Draft202012Validator(json.loads(generated.canonical_schema_bytes)).validate(payload)
    registry = FactSchemaRegistryBuilder().register(ref, definition).freeze()
    with pytest.raises(RegistryValidationError, match="registry.invalid_payload"):
        registry.validate_payload(ref, payload)
