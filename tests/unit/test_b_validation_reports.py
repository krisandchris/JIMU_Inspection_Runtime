"""Finite diagnostic outcomes do not confer Producer or runtime admission."""

import json

import pytest
from jsonschema import Draft202012Validator
from pydantic import ValidationError

from jimu_runtime.domain.validation.issues import (
    CheckResult,
    ValidationIssue,
    ValidationReport,
)


def issue():
    return {
        "code": "missing.catalogue",
        "path": ["items", 0],
        "message": "Required catalogue was not supplied.",
        "related_refs": [],
    }


def check(status="passed", check_id="registry.reference_shape"):
    return {
        "check_id": check_id,
        "status": status,
        "issues": [] if status == "passed" else [issue()],
    }


@pytest.mark.parametrize("status", ["passed", "invalid", "incomplete", "deferred"])
def test_outcomes_roundtrip_and_generated_schema(status):
    raw = {"scope": "registry_core", "checks": [check(status)]}
    result = ValidationReport.model_validate(raw)
    assert result.aggregate_status == status
    assert ValidationReport.model_validate_json(result.model_dump_json()) == result
    schema = ValidationReport.model_json_schema()
    Draft202012Validator.check_schema(schema)
    Draft202012Validator(schema).validate(json.loads(result.model_dump_json()))
    assert "aggregate_status" not in schema["properties"]
    assert "schema_version" not in schema["properties"]


@pytest.mark.parametrize(
    "statuses,expected",
    [
        (["passed", "deferred"], "deferred"),
        (["deferred", "incomplete"], "incomplete"),
        (["incomplete", "invalid", "deferred"], "invalid"),
    ],
)
def test_aggregate_priority(statuses, expected):
    report = ValidationReport.model_validate(
        {
            "scope": "producer_admission",
            "checks": [
                check(status, f"check.c{i}") for i, status in enumerate(statuses)
            ],
        }
    )
    assert report.aggregate_status == expected


@pytest.mark.parametrize(
    "status,issues",
    [("passed", [issue()]), ("invalid", []), ("incomplete", []), ("deferred", [])],
)
def test_status_issue_inconsistency_rejected(status, issues):
    with pytest.raises(ValidationError):
        CheckResult.model_validate({**check(), "status": status, "issues": issues})


@pytest.mark.parametrize("bad_path", [[True], [-1], [1.0], [None], [""], ["a"] * 33])
def test_issue_path_strictness_and_bound(bad_path):
    with pytest.raises(ValidationError):
        ValidationIssue.model_validate({**issue(), "path": bad_path})


def test_all_bounds_are_rejected_without_truncation():
    for count in (1, 64):
        assert (
            len(
                CheckResult.model_validate(
                    {**check("invalid"), "issues": [issue()] * count}
                ).issues
            )
            == count
        )
    with pytest.raises(ValidationError):
        CheckResult.model_validate({**check("invalid"), "issues": [issue()] * 65})
    assert (
        len(
            ValidationReport.model_validate(
                {
                    "scope": "registry_core",
                    "checks": [check(check_id=f"check.c{i}") for i in range(128)],
                }
            ).checks
        )
        == 128
    )
    for checks in (
        [],
        [check()] * 2,
        [check(check_id=f"check.c{i}") for i in range(129)],
    ):
        with pytest.raises(ValidationError):
            ValidationReport.model_validate(
                {"scope": "registry_core", "checks": checks}
            )
    ref = {
        "artifact_kind": "fact_schema",
        "artifact_id": "a",
        "version": "1.0.0",
        "digest": "a" * 64,
    }
    with pytest.raises(ValidationError):
        ValidationIssue.model_validate({**issue(), "related_refs": [ref, ref]})
    refs = [{**ref, "artifact_id": f"a{i}"} for i in range(17)]
    assert (
        len(
            ValidationIssue.model_validate(
                {**issue(), "related_refs": refs[:16]}
            ).related_refs
        )
        == 16
    )
    with pytest.raises(ValidationError):
        ValidationIssue.model_validate({**issue(), "related_refs": refs})
    assert (
        len(ValidationIssue.model_validate({**issue(), "path": ["a"] * 32}).path) == 32
    )


@pytest.mark.parametrize(
    "model,raw",
    [
        (ValidationIssue, issue()),
        (CheckResult, check()),
        (ValidationReport, {"scope": "registry_core", "checks": [check()]}),
    ],
)
def test_required_extra_null_and_containers(model, raw):
    schema = model.model_json_schema()
    Draft202012Validator.check_schema(schema)
    Draft202012Validator(schema).validate(raw)
    assert schema == model.model_json_schema()
    for name in raw:
        with pytest.raises(ValidationError):
            model.model_validate(
                {key: value for key, value in raw.items() if key != name}
            )
        with pytest.raises(ValidationError):
            model.model_validate({**raw, name: None})
    with pytest.raises(ValidationError):
        model.model_validate({**raw, "valid": True})


def test_unregistered_outcomes_and_scopes_rejected():
    with pytest.raises(ValidationError):
        CheckResult.model_validate({**check(), "status": "known"})
    with pytest.raises(ValidationError):
        ValidationReport.model_validate({"scope": "full_bundle", "checks": [check()]})


def test_mutation_and_forged_instances_rejected():
    raw = {"scope": "authority_admission", "checks": [check("deferred")]}
    report = ValidationReport.model_validate(raw)
    raw["checks"].clear()
    assert len(report.checks) == 1
    with pytest.raises(ValidationError):
        report.scope = "registry_core"
    forged = report.checks[0].model_copy(update={"status": "passed"})
    with pytest.raises(ValidationError):
        ValidationReport.model_validate(
            {"scope": "authority_admission", "checks": [forged]}
        )
    for bad in ({"check.a"}, (value for value in [check()])):
        with pytest.raises(ValidationError):
            ValidationReport.model_validate({"scope": "registry_core", "checks": bad})
