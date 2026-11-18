"""Standalone producer identities; no producer activation or runtime admission."""

import json

import pytest
from jsonschema import Draft202012Validator
from pydantic import TypeAdapter

from jimu_runtime.domain.definition_enums import FactScope, ProducerKind
from jimu_runtime.domain.fact_selectors import FactSelector, ProducerSelector


def ref(kind: str) -> dict[str, str]:
    return {
        "artifact_kind": kind,
        "artifact_id": "fixture",
        "version": "1.0.0",
        "digest": "a" * 64,
    }


def producer(kind: str) -> dict[str, object]:
    if kind == "vlm_question":
        return {
            "producer_kind": kind,
            "question_ref": ref("question"),
            "output_name": "primary",
        }
    if kind == "tool":
        return {
            "producer_kind": kind,
            "tool_plan_ref": ref("tool_plan"),
            "tool_ref": ref("tool"),
            "step_id": "measure",
            "output_name": "distance",
        }
    return {
        "producer_kind": "system",
        "requirement": {
            "family": "system_fact_producer",
            "capability_id": "context",
            "contract_version": "1.0.0",
            "allowed_implementation_versions": ["1.0.0"],
            "required_features": [],
        },
        "output_name": "context",
    }


def selector(kind: str, scope: str = "object") -> dict[str, object]:
    return {
        "scope": scope,
        "fact_key": "parked",
        "fact_schema_ref": ref("fact_schema"),
        "producer_selector": producer(kind),
        "field_path": ["a.b"],
    }


@pytest.mark.parametrize("kind", ["vlm_question", "tool", "system"])
def test_identity_roundtrip_and_generated_schema(kind: str) -> None:
    raw = selector(kind)
    schema = FactSelector.model_json_schema()
    Draft202012Validator.check_schema(schema)
    Draft202012Validator(schema).validate(raw)
    item = FactSelector.model_validate(raw)
    assert item.scope is FactScope.OBJECT
    assert item.producer_selector.producer_kind is ProducerKind(kind)
    assert item.field_path == ("a.b",)
    assert item.model_dump(mode="json") == raw
    assert FactSelector.model_validate_json(item.model_dump_json()) == item
    assert FactSelector.model_validate(item) == item
    adapter = TypeAdapter(ProducerSelector)
    assert (
        adapter.validate_json(json.dumps(raw["producer_selector"]))
        == item.producer_selector
    )
    Draft202012Validator.check_schema(adapter.json_schema())


def test_system_execution_and_whole_value_are_explicit() -> None:
    raw = selector("system", "execution")
    raw["field_path"] = []
    value = FactSelector.model_validate(raw)
    assert value.scope is FactScope.EXECUTION
    assert value.field_path == ()


def test_input_containers_are_copied() -> None:
    raw = selector("system")
    item = FactSelector.model_validate(raw)
    raw["field_path"] = ["changed"]
    assert item.field_path == ("a.b",)
