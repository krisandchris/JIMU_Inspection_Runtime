"""Nonpersistent finite input schemas and strict caller identity binding."""

import json

import pytest
from fixtures.wp01b.v1.support import fixture, inputs
from jsonschema import Draft202012Validator
from pydantic import TypeAdapter, ValidationError

from jimu_runtime.domain.validation.definition_inputs import (
    DefinitionRegistration,
    DefinitionValidationInput,
    ModelPromptUse,
    ProfileUseContext,
    QuestionMetadataView,
)


def test_all_registration_branches_and_root_roundtrip_schema():
    data = fixture()
    model = inputs(data)
    assert len({entry.registration_kind for entry in model.definitions}) == 11
    for annotation in (
        DefinitionRegistration,
        DefinitionValidationInput,
        ProfileUseContext,
        QuestionMetadataView,
        ModelPromptUse,
    ):
        schema = TypeAdapter(annotation).json_schema()
        Draft202012Validator.check_schema(schema)
    schema = DefinitionValidationInput.model_json_schema()
    Draft202012Validator(schema).validate(data["input"])
    assert (
        DefinitionValidationInput.model_validate_json(model.model_dump_json()) == model
    )
    assert "schema_version" not in schema["properties"]
    assert DefinitionValidationInput.model_validate(model) == model


@pytest.mark.parametrize("field", ["validation_at"])
def test_absent_optional_is_not_nullable(field):
    data = fixture()["input"]
    data[field] = None
    with pytest.raises(ValidationError):
        DefinitionValidationInput.model_validate_json(json.dumps(data))
    assert not Draft202012Validator(
        DefinitionValidationInput.model_json_schema()
    ).is_valid(data)


def test_shape_binding_extra_and_copied_instance_taint():
    data = fixture()
    data["input"]["definitions"][0]["ref"]["artifact_id"] = "wrong"
    with pytest.raises(ValidationError):
        inputs(data)
    valid = inputs(fixture())
    forged = valid.model_copy(update={"unexpected": True})
    with pytest.raises(ValidationError):
        DefinitionValidationInput.model_validate(forged)
    forged_entry = valid.definitions[0].model_copy(update={"unexpected": True})
    with pytest.raises(ValidationError):
        DefinitionValidationInput.model_validate(
            valid.model_copy(update={"definitions": (forged_entry,)})
        )
