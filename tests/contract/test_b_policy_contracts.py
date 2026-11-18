"""Fourteen individually complete Policies, not a full PolicyArtifact catalogue."""

import json
from pathlib import Path
from typing import cast

import pytest
from jsonschema import Draft202012Validator
from pydantic import TypeAdapter, ValidationError

from jimu_runtime.domain.definition_enums import PolicyKind
from jimu_runtime.domain.policies import NonExpressionPolicyArtifact

KINDS = (
    "question_selection",
    "unknown",
    "question_validation",
    "evidence",
    "tool_execution",
    "tool_failure",
    "resource_requirements",
    "provider",
    "fallback",
    "rule",
    "conflict",
    "coverage",
    "context_provenance",
    "execution",
)


def fixture() -> dict[str, object]:
    return cast(
        dict[str, object],
        json.loads(
            (Path(__file__).parents[1] / "fixtures/wp01b/p1/contracts.json").read_text()
        ),
    )


@pytest.mark.parametrize("kind", KINDS)
def test_individual_policy_roundtrip_schema_and_required_root(kind: str) -> None:
    raw = cast(dict[str, object], fixture()["artifacts"])[kind]
    adapter = TypeAdapter(NonExpressionPolicyArtifact)
    model = adapter.validate_python(raw)
    schema = type(model).model_json_schema()
    Draft202012Validator.check_schema(schema)
    Draft202012Validator(schema).validate(raw)
    assert adapter.validate_json(adapter.dump_json(model)) == model
    assert adapter.validate_python(model) == model
    assert model.model_dump(mode="json") == raw
    invalid = dict(cast(dict[str, object], raw))
    invalid.pop("schema_version")
    assert not Draft202012Validator(schema).is_valid(invalid)
    with pytest.raises(ValidationError):
        adapter.validate_python(invalid)


def test_complete_catalogue_and_explicit_partial_compatibility_name() -> None:
    from jimu_runtime.domain.policies import PolicyArtifact

    complete = TypeAdapter(PolicyArtifact).json_schema()
    partial = TypeAdapter(NonExpressionPolicyArtifact).json_schema()
    assert len(PolicyKind) == 16
    assert len(complete["oneOf"]) == 16
    assert len(partial["anyOf"]) == 14
    assert len(KINDS) == 14
    assert PolicyKind.APPLICABILITY.value not in KINDS
    assert PolicyKind.TOOL_SKIP.value not in KINDS


@pytest.mark.parametrize("branch", ["evaluated_fallback", "local_provider"])
def test_provider_fallback_conditional_branches(branch: str) -> None:
    raw = fixture()[branch]
    adapter = TypeAdapter(NonExpressionPolicyArtifact)
    model = adapter.validate_json(json.dumps(raw))
    schema = type(model).model_json_schema()
    Draft202012Validator.check_schema(schema)
    Draft202012Validator(schema).validate(raw)
    assert adapter.validate_json(adapter.dump_json(model)) == model


@pytest.mark.parametrize(
    "case",
    [
        "selection_context",
        "selection_model",
        "retry_required",
        "retry_forbidden",
        "derived_transform",
        "clock_required",
        "clock_forbidden",
        "derivation_required",
    ],
)
def test_conditional_presence_is_in_generated_schema(case: str) -> None:
    data = fixture()
    artifacts = cast(dict[str, dict[str, object]], data["artifacts"])
    if case.startswith("selection"):
        raw = artifacts["question_selection"]
    elif case.startswith("retry"):
        raw = artifacts["tool_execution"]
    elif case == "derived_transform":
        raw = artifacts["evidence"]
    else:
        raw = artifacts["context_provenance"]
    adapter = TypeAdapter(NonExpressionPolicyArtifact)
    schema = type(adapter.validate_python(raw)).model_json_schema()
    policy = cast(dict[str, object], raw["policy"])
    if case == "selection_context":
        policy.update(mode="mandatory_plus_optional", optional_source="context_rules")
    elif case == "selection_model":
        policy.update(mode="mandatory_plus_optional", optional_source="model_advisory")
    elif case == "retry_required":
        policy["max_attempts"] = 2
    elif case == "retry_forbidden":
        policy["retry_backoff_ms"] = []
    elif case == "derived_transform":
        policy["allowed_evidence_types"] = ["original_image", "mask"]
    else:
        field = cast(list[dict[str, object]], policy["fields"])[0]
        if case == "clock_required":
            field["time_sensitive"] = True
        elif case == "clock_forbidden":
            field["clock_tolerance_ms"] = 0
        else:
            field["allowed_sources"] = ["declared_derivation"]
    assert not Draft202012Validator(schema).is_valid(raw)
    with pytest.raises(ValidationError):
        adapter.validate_python(raw)
