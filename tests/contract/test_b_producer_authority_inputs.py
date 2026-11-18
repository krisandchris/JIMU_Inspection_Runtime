"""X3 association input contracts only; no complete finite admission claim."""

import json

import pytest
from fixtures.wp01b.x3.support import fixture, parse
from jsonschema import Draft202012Validator
from pydantic import ValidationError

from jimu_runtime.domain.validation.producer_authority_inputs import (
    ExecutionDefinitionPin,
    ProducerAuthorityValidationInput,
)


@pytest.mark.parametrize(
    "model", [ExecutionDefinitionPin, ProducerAuthorityValidationInput]
)
def test_input_schemas_and_roundtrip(model):
    raw = fixture()
    raw = raw["execution_pins"][0] if model is ExecutionDefinitionPin else raw
    schema = model.model_json_schema()
    Draft202012Validator.check_schema(schema)
    Draft202012Validator(schema).validate(raw)
    assert schema == model.model_json_schema()
    parsed = model.model_validate_json(json.dumps(raw))
    assert model.model_validate_json(parsed.model_dump_json()) == parsed
    assert model.model_validate(parsed) == parsed
    assert "schema_version" not in schema["properties"]
    assert schema["additionalProperties"] is False


@pytest.mark.parametrize(
    "model", [ExecutionDefinitionPin, ProducerAuthorityValidationInput]
)
def test_required_null_extra_and_unknown_wire_fields(model):
    raw = fixture()
    raw = raw["execution_pins"][0] if model is ExecutionDefinitionPin else raw
    validator = Draft202012Validator(model.model_json_schema())
    for field in raw:
        for invalid in (
            {key: value for key, value in raw.items() if key != field},
            {**raw, field: None},
        ):
            with pytest.raises(ValidationError):
                model.model_validate_json(json.dumps(invalid))
            assert not validator.is_valid(invalid)
    invalid = {**raw, "authority_verified": True}
    with pytest.raises(ValidationError):
        model.model_validate_json(json.dumps(invalid))
    assert not validator.is_valid(invalid)


def test_profile_set_comparison_is_not_storage_order():
    data = fixture()
    current = data["producer_catalogues"][0]["context"]["profiles"][0]
    import copy

    other = copy.deepcopy(current)
    other["ref"]["artifact_id"] = other["definition"]["profile_id"] = "other-profile"
    data["producer_catalogues"][0]["context"]["profiles"].append(other)
    data["execution_pins"][0]["configured_profile_refs"].insert(0, other["ref"])
    parsed = parse(data)
    assert (
        parsed.execution_pins[0].configured_profile_refs[0].artifact_id
        == "other-profile"
    )
    assert (
        parsed.producer_catalogues[0].context.profiles[0].ref.artifact_id
        != "other-profile"
    )
