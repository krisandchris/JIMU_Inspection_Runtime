"""Negative Policy shapes and closed branch constraints; no live execution."""

import json
from pathlib import Path
from typing import cast

import pytest
from jsonschema import Draft202012Validator
from pydantic import TypeAdapter, ValidationError

from jimu_runtime.domain.policies import NonExpressionPolicyArtifact


def fixture() -> dict[str, object]:
    return cast(
        dict[str, object],
        json.loads(
            (Path(__file__).parents[1] / "fixtures/wp01b/p1/contracts.json").read_text()
        ),
    )


def artifact(kind: str) -> dict[str, object]:
    return cast(dict[str, dict[str, object]], fixture()["artifacts"])[kind]


@pytest.mark.parametrize(
    "kind",
    [
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
    ],
)
@pytest.mark.parametrize("value", [None, {}, "synthetic"])
def test_unregistered_fields_reject_in_model_and_generated_schema(
    kind: str, value: object
) -> None:
    raw = artifact(kind)
    adapter = TypeAdapter(NonExpressionPolicyArtifact)
    model = adapter.validate_python(raw)
    cast(dict[str, object], raw["policy"])["executable_hook"] = value
    assert not Draft202012Validator(type(model).model_json_schema()).is_valid(raw)
    with pytest.raises(ValidationError):
        adapter.validate_python(raw)


@pytest.mark.parametrize(
    "kind,field,value",
    [
        ("unknown", "reason_required", 1),
        ("conflict", "early_exit", 0),
        ("conflict", "preserve_rule_refs", 1),
        ("tool_execution", "whole_step_retry", 1),
        ("evidence", "retention_duration_seconds", 0),
        ("question_validation", "max_items", 257),
        ("question_validation", "max_string_chars", None),
        ("coverage", "absence_strategy", "confidence_threshold"),
        ("resource_requirements", "resource_class", "unbounded"),
    ],
)
def test_strict_values_and_supported_enums(
    kind: str, field: str, value: object
) -> None:
    raw = artifact(kind)
    cast(dict[str, object], raw["policy"])[field] = value
    with pytest.raises(ValidationError):
        TypeAdapter(NonExpressionPolicyArtifact).validate_python(raw)


@pytest.mark.parametrize(
    "mutation",
    [
        "transform_missing",
        "original_disallowed",
        "subset",
        "duplicate",
        "transform_false",
    ],
)
def test_evidence_invariants(mutation: str) -> None:
    raw = artifact("evidence")
    policy = cast(dict[str, object], raw["policy"])
    if mutation == "transform_missing":
        policy["allowed_evidence_types"] = ["original_image", "mask"]
    elif mutation == "original_disallowed":
        policy["allowed_evidence_types"] = []
    elif mutation == "subset":
        policy["required_evidence_types"] = ["crop"]
    elif mutation == "duplicate":
        policy["allowed_evidence_types"] = ["original_image", "original_image"]
    else:
        policy["transform_required_for_derived"] = False
    with pytest.raises(ValidationError):
        TypeAdapter(NonExpressionPolicyArtifact).validate_python(raw)


@pytest.mark.parametrize(
    "mutation",
    [
        "one_schedule",
        "missing_schedule",
        "wrong_length",
        "over_limit",
        "duplicate_errors",
    ],
)
def test_retry_schedule_invariants(mutation: str) -> None:
    raw = artifact("tool_execution")
    policy = cast(dict[str, object], raw["policy"])
    if mutation == "one_schedule":
        policy["retry_backoff_ms"] = [1]
    elif mutation == "missing_schedule":
        policy["max_attempts"] = 2
    elif mutation == "wrong_length":
        policy.update(max_attempts=3, retry_backoff_ms=[1])
    elif mutation == "over_limit":
        policy.update(max_attempts=1026, retry_backoff_ms=[1] * 1024)
    else:
        policy["retryable_error_classes"] = ["timeout", "timeout"]
    with pytest.raises(ValidationError):
        TypeAdapter(NonExpressionPolicyArtifact).validate_python(raw)


@pytest.mark.parametrize(
    "mutation",
    [
        "missing_primary",
        "local_remote_mode",
        "duplicate_deployment",
        "illegal_exception",
    ],
)
def test_provider_has_no_implicit_local_route(mutation: str) -> None:
    raw = artifact("provider")
    policy = cast(dict[str, object], raw["policy"])
    deployments = cast(list[dict[str, object]], policy["deployments"])
    if mutation == "missing_primary":
        policy["primary_deployment_id"] = "missing"
    elif mutation == "local_remote_mode":
        policy["primary_deployment_id"] = "local-deployment"
    elif mutation == "duplicate_deployment":
        deployments.append(deployments[0])
    else:
        policy["local_primary_exception"] = None
    with pytest.raises(ValidationError):
        TypeAdapter(NonExpressionPolicyArtifact).validate_python(raw)


@pytest.mark.parametrize(
    "mutation",
    ["context_missing", "advisory_missing", "branch_forbidden", "partition_overlap"],
)
def test_question_selection_closed_branches(mutation: str) -> None:
    raw = artifact("question_selection")
    policy = cast(dict[str, object], raw["policy"])
    if mutation == "context_missing":
        policy.update(mode="mandatory_plus_optional", optional_source="context_rules")
    elif mutation == "advisory_missing":
        policy.update(mode="mandatory_plus_optional", optional_source="model_advisory")
    elif mutation == "branch_forbidden":
        policy["proposal_schema_ref"] = None
    else:
        policy["optional_question_refs"] = policy["mandatory_question_refs"]
    with pytest.raises(ValidationError):
        TypeAdapter(NonExpressionPolicyArtifact).validate_python(raw)


@pytest.mark.parametrize(
    "mutation",
    [
        "clock_missing",
        "clock_forbidden",
        "derivation_missing",
        "duplicate_path",
        "prefix_path",
    ],
)
def test_context_conditional_fields(mutation: str) -> None:
    raw = artifact("context_provenance")
    policy = cast(dict[str, object], raw["policy"])
    fields = cast(list[dict[str, object]], policy["fields"])
    if mutation == "clock_missing":
        fields[0]["time_sensitive"] = True
    elif mutation == "clock_forbidden":
        fields[0]["clock_tolerance_ms"] = 0
    elif mutation == "derivation_missing":
        fields[0]["allowed_sources"] = ["declared_derivation"]
    elif mutation == "duplicate_path":
        fields.append(fields[0])
    else:
        fields.append(dict(fields[0], field_path=["outer"]))
    with pytest.raises(ValidationError):
        TypeAdapter(NonExpressionPolicyArtifact).validate_python(raw)
