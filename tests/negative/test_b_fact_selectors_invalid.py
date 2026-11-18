"""Reject runtime identities, wrong refs and invalid producer scope."""

from typing import cast

import pytest
from jsonschema import Draft202012Validator
from pydantic import ValidationError

from jimu_runtime.domain.fact_selectors import FactSelector


def raw() -> dict[str, object]:
    def ref(kind: str) -> dict[str, str]:
        return {
            "artifact_kind": kind,
            "artifact_id": "x",
            "version": "1.0.0",
            "digest": "a" * 64,
        }

    return {
        "scope": "object",
        "fact_key": "x",
        "field_path": [],
        "fact_schema_ref": ref("fact_schema"),
        "producer_selector": {
            "producer_kind": "vlm_question",
            "question_ref": ref("question"),
            "output_name": "primary",
        },
    }


@pytest.mark.parametrize(
    "field",
    ["fact_id", "attempt_id", "catalogue_ref", "condition_ast", "schema_version"],
)
def test_runtime_and_expression_fields_rejected(field: str) -> None:
    value = raw()
    value[field] = "invented"
    with pytest.raises(ValidationError):
        FactSelector.model_validate(value)
    assert not Draft202012Validator(FactSelector.model_json_schema()).is_valid(value)


def test_execution_question_rejected() -> None:
    value = raw()
    value["scope"] = "execution"
    with pytest.raises(ValidationError, match="system producer"):
        FactSelector.model_validate(value)


@pytest.mark.parametrize("wrong", ["tool", "value_schema", "question"])
def test_wrong_schema_ref_kind_rejected_in_both_layers(wrong: str) -> None:
    value = raw()
    cast(dict[str, object], value["fact_schema_ref"])["artifact_kind"] = wrong
    with pytest.raises(ValidationError):
        FactSelector.model_validate(value)
    assert not Draft202012Validator(FactSelector.model_json_schema()).is_valid(value)


@pytest.mark.parametrize("change", ["output", "extra", "null", "path", "family"])
def test_nested_strict_identity_rejections(change: str) -> None:
    value = raw()
    if change == "output":
        cast(dict[str, object], value["producer_selector"])["output_name"] = "secondary"
    elif change == "extra":
        cast(dict[str, object], value["producer_selector"])["attempt_ref"] = {
            "record_kind": "attempt",
            "record_id": "x",
        }
    elif change == "null":
        value["field_path"] = None
    elif change == "path":
        value["field_path"] = ["x"] * 9
    else:
        value["producer_selector"] = {
            "producer_kind": "system",
            "output_name": "x",
            "requirement": {
                "family": "tool_implementation",
                "capability_id": "x",
                "contract_version": "1.0.0",
                "allowed_implementation_versions": ["1.0.0"],
                "required_features": [],
            },
        }
    with pytest.raises(ValidationError):
        FactSelector.model_validate(value)
    assert not Draft202012Validator(FactSelector.model_json_schema()).is_valid(value)


def test_copy_injected_extra_cannot_survive_revalidation() -> None:
    value = FactSelector.model_validate(raw()).model_copy(
        update={"fact_id": "injected"}
    )
    with pytest.raises(ValidationError):
        FactSelector.model_validate(value)
