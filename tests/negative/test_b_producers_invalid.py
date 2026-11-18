"""Reject malformed definitions, ambiguous keys and unbound local tool data."""

import json
from copy import deepcopy
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator
from pydantic import ValidationError

from jimu_runtime.domain.producers import FactProducerDeclaration, ProducerCatalogue
from jimu_runtime.domain.runtime.facts import ProfileContentRef as RuntimeProfileRef
from jimu_runtime.domain.validation.producer_inputs import (
    ProducerCatalogueRegistration,
    ProducerValidationContext,
    SystemCapabilityRegistration,
)


def test_cross_class_reference_taint_is_not_sanitized_by_wire_conversion() -> None:
    value = RuntimeProfileRef.model_validate(fixture("question")["profile_ref"])
    with pytest.raises(ValidationError):
        RuntimeProfileRef.model_validate(value.model_copy(update={"hidden": True}))


@pytest.mark.parametrize("change", ["kind", "versions", "features", "null"])
def test_host_registration_is_closed_and_unique(change: str) -> None:
    raw = fixture("context")["system_capabilities"][0]
    if change == "kind":
        raw["family"] = "tool_implementation"
    elif change == "versions":
        raw["available_implementation_versions"] *= 2
    elif change == "features":
        raw["supported_features"] = ["typed_context", "typed_context"]
    else:
        raw["registry_id"] = None
    with pytest.raises(ValidationError):
        SystemCapabilityRegistration.model_validate(raw)


def test_catalogue_registration_and_context_required_fields_are_not_defaults() -> None:
    raw = fixture("registration")
    raw["ref"]["artifact_id"] = "foreign"
    with pytest.raises(ValidationError):
        ProducerCatalogueRegistration.model_validate(raw)
    for field in fixture("context"):
        raw = fixture("context")
        del raw[field]
        with pytest.raises(ValidationError):
            ProducerValidationContext.model_validate(raw)


def test_only_producer_activation_contexts_can_supply_producer_uses() -> None:
    raw = fixture("context")
    raw["condition_contexts"] = [
        {
            "use_role": "applicability",
            "profile_ref": fixture("question")["profile_ref"],
            "policy_ref": raw["policies"][0]["ref"],
            "context_registrations": [],
            "descriptors": [],
        }
    ]
    with pytest.raises(ValidationError):
        ProducerValidationContext.model_validate(raw)


def fixture(name: str) -> dict:
    return json.loads(
        (Path(__file__).parents[1] / "fixtures/wp01b/x2/contracts.json").read_text()
    )[name]


@pytest.mark.parametrize("field", ["profile_ref", "description"])
def test_null_is_not_absence(field: str) -> None:
    raw = fixture("question")
    raw[field] = None
    with pytest.raises(ValidationError):
        FactProducerDeclaration.model_validate(raw)
    assert not Draft202012Validator(
        FactProducerDeclaration.model_json_schema()
    ).is_valid(raw)


@pytest.mark.parametrize(
    "change",
    ["root_kind", "scope", "output_name", "cardinality", "extra", "id", "version"],
)
def test_question_fixed_contract_and_identity(change: str) -> None:
    raw = fixture("question")
    field, value = {
        "root_kind": ("producer_kind", "system"),
        "scope": ("scope", "execution"),
        "output_name": ("output_name", "secondary"),
        "cardinality": ("cardinality", "zero_or_one"),
        "extra": ("attempt_id", "forbidden"),
        "id": ("producer_id", "other"),
        "version": ("producer_version", "2.0.0"),
    }[change]
    raw[field] = value
    with pytest.raises(ValidationError):
        FactProducerDeclaration.model_validate(raw)
    if change not in ("id", "version"):
        assert not Draft202012Validator(
            FactProducerDeclaration.model_json_schema()
        ).is_valid(raw)


@pytest.mark.parametrize("name", ["question", "system_object", "system_execution"])
def test_non_tool_condition_rejects_nested_local_selector(name: str) -> None:
    raw = fixture(name)
    raw["activation_condition"] = {
        "mode": "tool_condition",
        "condition_ast": {
            "kind": "not",
            "argument": {
                "kind": "local_tool_fact_ref",
                "selector": {
                    "scope": "object",
                    "step_id": "prior",
                    "tool_ref": fixture("tool")["producer"]["tool_ref"],
                    "output_name": "value",
                    "fact_key": "value",
                    "fact_schema_ref": raw["fact_schema_ref"],
                    "field_path": [],
                },
            },
        },
    }
    with pytest.raises(ValidationError, match="non-Tool"):
        FactProducerDeclaration.model_validate(raw)


@pytest.mark.parametrize(
    "name", ["question", "tool", "system_object", "system_execution"]
)
def test_forbidden_runtime_and_nested_output_fields(name: str) -> None:
    raw = fixture(name)
    raw["producer"]["output_name"] = "duplicate"
    with pytest.raises(ValidationError):
        FactProducerDeclaration.model_validate(raw)
    raw = fixture(name)
    raw["activation_condition"] = {
        "mode": "always",
        "condition_ast": fixture("literal_true"),
    }
    with pytest.raises(ValidationError):
        FactProducerDeclaration.model_validate(raw)


@pytest.mark.parametrize(
    "change",
    ["id", "key", "system_slot", "question_slot", "tool_slot", "conditional_collision"],
)
def test_catalogue_collisions_never_use_activation_or_last_write(change: str) -> None:
    first = fixture(
        "system_object"
        if change == "system_slot"
        else "tool"
        if change == "tool_slot"
        else "question"
    )
    second = deepcopy(first)
    second["declaration_id"] = "other"
    if change == "id":
        second["declaration_id"] = first["declaration_id"]
    elif change in ("system_slot", "question_slot", "tool_slot"):
        second["fact_key"] = "different.fact"
    elif change == "conditional_collision":
        second["producer"]["question_ref"]["artifact_id"] = "another"
        second["producer_id"] = "another"
        second["activation_condition"] = {
            "mode": "tool_condition",
            "condition_ast": fixture("literal_false"),
        }
    root = fixture("catalogue")
    root["declarations"] = [first, second]
    with pytest.raises(ValidationError):
        ProducerCatalogue.model_validate(root)


def test_execution_scope_and_tool_schema_restrictions() -> None:
    raw = fixture("system_execution")
    raw["profile_ref"] = fixture("question")["profile_ref"]
    with pytest.raises(ValidationError):
        FactProducerDeclaration.model_validate(raw)
    raw = fixture("tool")
    raw["scope"] = "execution"
    del raw["profile_ref"]
    with pytest.raises(ValidationError):
        FactProducerDeclaration.model_validate(raw)
    raw = fixture("tool")
    raw["fact_schema_ref"]["artifact_kind"] = "value_schema"
    with pytest.raises(ValidationError):
        FactProducerDeclaration.model_validate(raw)
    raw = fixture("system_execution")
    raw["producer"]["requirement"]["family"] = "tool_implementation"
    with pytest.raises(ValidationError):
        FactProducerDeclaration.model_validate(raw)


def test_copy_injected_extra_and_nested_identity_are_revalidated() -> None:
    value = FactProducerDeclaration.model_validate(fixture("question"))
    with pytest.raises(ValidationError):
        FactProducerDeclaration.model_validate(
            value.model_copy(update={"hidden": True})
        )
    altered = value.producer.model_copy(
        update={
            "question_ref": value.producer.question_ref.model_copy(
                update={"version": "2.0.0"}
            )
        }
    )
    with pytest.raises(ValidationError):
        FactProducerDeclaration.model_validate(
            value.model_copy(update={"producer": altered})
        )
