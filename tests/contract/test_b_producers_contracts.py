"""Complete Producer data, independent of dynamic activation and host trust."""

import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator
from pydantic import TypeAdapter

from jimu_runtime.domain.producers import (
    ActivationCondition,
    FactProducerDeclaration,
    Producer,
    ProducerCatalogue,
    ToolConditionActivation,
)
from jimu_runtime.domain.validation.producer_inputs import (
    ProducerCatalogueContext,
    ProducerCatalogueRegistration,
    ProducerValidationContext,
    SystemCapabilityRegistration,
)


def test_actual_definition_context_and_host_descriptors_roundtrip() -> None:
    registration = ProducerCatalogueRegistration.model_validate(fixture("registration"))
    context = ProducerValidationContext.model_validate(fixture("context"))
    combined = ProducerCatalogueContext(registration=registration, context=context)
    for value in (registration, context, combined, context.system_capabilities[0]):
        schema = type(value).model_json_schema()
        Draft202012Validator.check_schema(schema)
        Draft202012Validator(schema).validate(value.model_dump(mode="json"))
        assert type(value).model_validate_json(value.model_dump_json()) == value
        assert type(value).model_validate(value) == value
    assert isinstance(context.system_capabilities[0], SystemCapabilityRegistration)
    assert context.condition_contexts == ()


def fixture(name: str) -> dict:
    return json.loads(
        (Path(__file__).parents[1] / "fixtures/wp01b/x2/contracts.json").read_text()
    )[name]


@pytest.mark.parametrize(
    "name", ["question", "tool", "system_object", "system_execution"]
)
def test_all_producer_branches_scopes_roundtrip(name: str) -> None:
    raw = fixture(name)
    schema = FactProducerDeclaration.model_json_schema()
    Draft202012Validator.check_schema(schema)
    Draft202012Validator(schema).validate(raw)
    value = FactProducerDeclaration.model_validate(raw)
    assert value.model_dump(mode="json") == raw
    assert FactProducerDeclaration.model_validate(value) == value
    assert FactProducerDeclaration.model_validate_json(value.model_dump_json()) == value


@pytest.mark.parametrize(
    "name", ["question", "tool", "system_object", "system_execution"]
)
@pytest.mark.parametrize("mode", ["always", "tool_condition"])
def test_complete_activation_union_is_preserved_for_every_kind(
    name: str, mode: str
) -> None:
    raw = fixture(name)
    raw["activation_condition"] = (
        {"mode": "always"}
        if mode == "always"
        else fixture("tool")["activation_condition"]
    )
    value = FactProducerDeclaration.model_validate(raw)
    assert value.activation_condition.mode == mode
    if mode == "tool_condition":
        assert isinstance(value.activation_condition, ToolConditionActivation)
        assert (
            value.activation_condition.condition_ast.kind == "all"
        )  # Not folded to true.


def test_catalogue_and_named_unions_generate_valid_schemas() -> None:
    raw = fixture("catalogue")
    model = ProducerCatalogue.model_validate(raw)
    assert model.model_dump(mode="json") == raw
    assert ProducerCatalogue.model_validate_json(model.model_dump_json()) == model
    Draft202012Validator(ProducerCatalogue.model_json_schema()).validate(raw)
    for alias in (Producer, ActivationCondition):
        Draft202012Validator.check_schema(TypeAdapter(alias).json_schema())
    assert (
        ProducerCatalogue(
            schema_version="1.0.0",
            catalogue_id="empty",
            version="1.0.0",
            declarations=(),
        ).declarations
        == ()
    )
