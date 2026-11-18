"""Strict use-site branches, generated JSON Schema and absence semantics agree."""

from copy import deepcopy

import pytest
from fixtures.wp01b.x4.support import context, setup
from jsonschema import Draft202012Validator
from pydantic import TypeAdapter, ValidationError

from jimu_runtime.domain.validation.condition_inputs import (
    ConditionReferenceContext,
    ConditionReferenceResult,
)


@pytest.mark.parametrize(
    "role", ["applicability", "tool_step_guard", "producer_activation"]
)
def test_each_branch_schema_roundtrip(role):
    _, raw, _ = setup()
    raw["use_role"] = role
    if role == "applicability":
        raw.pop("tool_plan_ref")
        raw.pop("step_id")
    if role == "producer_activation":
        raw.pop("policy_ref")
        raw.update(
            producer_catalogue_ref=dict(
                artifact_kind="fact_producer_catalog",
                artifact_id="catalogue",
                version="1.0.0",
                digest="a" * 64,
            ),
            declaration_id="signal",
            producer_kind="tool",
            producer_scope="object",
        )
    adapter = TypeAdapter(ConditionReferenceContext)
    schema = adapter.json_schema()
    Draft202012Validator.check_schema(schema)
    Draft202012Validator(schema).validate(raw)
    typed = context(raw)
    assert adapter.validate_json(adapter.dump_json(typed)) == typed
    Draft202012Validator.check_schema(ConditionReferenceResult.model_json_schema())


@pytest.mark.parametrize("kind", ["tool", "vlm_question"])
def test_non_system_execution_producer_is_rejected_by_model_and_schema(kind):
    _, raw, _ = setup()
    raw.update(
        use_role="producer_activation",
        producer_catalogue_ref=dict(
            artifact_kind="fact_producer_catalog",
            artifact_id="catalogue",
            version="1.0.0",
            digest="a" * 64,
        ),
        declaration_id="signal",
        producer_kind=kind,
        producer_scope="execution",
    )
    raw.pop("policy_ref")
    raw.pop("profile_ref")
    if kind != "tool":
        raw.pop("tool_plan_ref")
        raw.pop("step_id")
    with pytest.raises(ValidationError):
        context(raw)
    assert not Draft202012Validator(
        TypeAdapter(ConditionReferenceContext).json_schema()
    ).is_valid(raw)


@pytest.mark.parametrize(
    "field",
    ["region_ref", "context_registrations", "descriptors", "question_ref", "injected"],
)
def test_explicit_null_extra_and_missing_required_rejected(field):
    _, raw, _ = setup()
    raw[field] = None
    with pytest.raises(ValidationError):
        context(raw)
    assert not Draft202012Validator(
        TypeAdapter(ConditionReferenceContext).json_schema()
    ).is_valid(raw)


def test_collections_are_required_and_bounded():
    _, raw, _ = setup()
    for key in ("context_registrations", "descriptors"):
        bad = deepcopy(raw)
        bad.pop(key)
        with pytest.raises(ValidationError):
            context(bad)
        bad = deepcopy(raw)
        bad[key] = bad[key] * 1025
        with pytest.raises(ValidationError):
            context(bad)
