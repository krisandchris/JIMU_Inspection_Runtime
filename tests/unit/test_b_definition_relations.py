"""Actual independent relation checks retain explicit incomplete/deferred boundaries."""

from copy import deepcopy

import pytest
from fixtures.wp01b.v1.support import (
    codes,
    entry,
    fixture,
    prompt_with_variable,
    report,
    with_step,
)


def test_positive_finite_fixture_has_only_explicit_deferred_boundaries():
    result = report(fixture())
    assert result.aggregate_status == "deferred", result.model_dump_json(indent=2)
    assert not any(check.status in ("invalid", "incomplete") for check in result.checks)
    assert "definitions.deferred.producer_authority_admission" in codes(result)
    assert "definitions.deferred.rule_set_body" in codes(result)
    assert any(
        check.check_id == "definitions.grounding_io.passed" for check in result.checks
    )


def test_model_grounding_uses_same_generic_target_coverage_without_fake_step():
    data = fixture()
    profile = entry(data, "profile")["definition"]
    profile["grounding_binding"] = {
        "kind": "model",
        "model_binding_ref": entry(data, "model_binding", "grounding")["ref"],
        "input_bindings": profile["grounding_binding"]["input_bindings"],
    }
    result = report(data)
    assert result.aggregate_status == "deferred", result.model_dump_json(indent=2)


def test_partial_inherited_variables_and_exact_question_allowlist():
    data = fixture()
    root = entry(data, "prompt_template", "prompt-question")
    child = deepcopy(root)
    child["ref"]["artifact_id"] = "partial"
    child["definition"]["prompt_template_id"] = "partial"
    data["input"]["definitions"].append(child)
    prompt_with_variable(data, "partial")
    root["definition"]["partial_refs"] = [child["ref"]]
    result = report(data)
    assert result.aggregate_status == "deferred", result.model_dump_json(indent=2)


@pytest.mark.parametrize("with_reviews", [False, True])
def test_missing_validation_at_does_not_skip_independent_checks(with_reviews):
    data = fixture()
    data["input"].pop("validation_at")
    if with_reviews:
        # Trusted metadata is still not permission to use an ambient clock.
        import json
        from pathlib import Path

        data["input"]["reviews"] = [
            json.loads(
                (
                    Path(__file__).parents[1] / "fixtures/wp01b/p1/contracts.json"
                ).read_text()
            )["review"]
        ]
    result = report(data)
    assert "definitions.validation_at_missing" in codes(result)
    assert any(
        check.check_id == "definitions.questions.passed" for check in result.checks
    )
    assert any(
        check.check_id == "definitions.grounding_io.passed" for check in result.checks
    )


def test_question_metadata_is_explicit_for_question_owned_prompt():
    data = fixture()
    prompt_with_variable(
        data,
        source="question_metadata",
        kind="attribute_schema",
        schema_id="question-meta",
        path=["flag"],
    )
    result = report(data)
    assert result.aggregate_status == "deferred", result.model_dump_json(indent=2)


def test_empty_input_never_means_complete_admission():
    data = fixture()
    data["input"].update(
        definitions=[],
        descriptors=[],
        fact_schema_refs=[],
        policies=[],
        use_contexts=[],
        reviews=[],
    )
    data["fact_schemas"] = []
    assert report(data).aggregate_status == "deferred"


def test_nested_views_delegate_real_step_plan_without_full_admission():
    result = report(with_step(fixture()))
    assert result.aggregate_status == "deferred", result.model_dump_json(indent=2)
    assert any(
        check.check_id == "definitions.tool_plans.passed" for check in result.checks
    )


def test_unit_and_coordinate_vocabularies_are_checked_without_changing_child():
    data = fixture()
    schema = data["fact_schemas"][0]["definition"]
    original = deepcopy(schema["payload_schema_ref"])
    schema["units"] = {
        "mode": "declared",
        "bindings": [
            {
                "field_path": [],
                "unit": {
                    "vocabulary_ref": entry(data, "vocabulary", "unit")["ref"],
                    "member": "synthetic",
                },
            }
        ],
    }
    schema["coordinate_system"] = {
        "mode": "declared",
        "bindings": [
            {
                "field_path": [],
                "coordinate_system": {
                    "vocabulary_ref": entry(data, "vocabulary", "coordinate")["ref"],
                    "member": "synthetic",
                },
            }
        ],
    }
    assert report(data).aggregate_status == "deferred"
    assert schema["payload_schema_ref"] == original


def test_unowned_tool_does_not_grant_its_model_the_current_profile_owner(monkeypatch):
    from jimu_runtime.domain.validation import definitions

    data = fixture()
    model = deepcopy(entry(data, "model_binding", "observation"))
    model["ref"]["artifact_id"] = model["definition"]["model_binding_id"] = (
        "unowned-model"
    )
    model["definition"]["capability"] = "vision_auxiliary"
    model["definition"].pop("prompt_template_ref")
    tool = deepcopy(entry(data, "tool"))
    tool["ref"]["artifact_id"] = tool["definition"]["tool_id"] = "unowned-tool"
    tool["definition"]["model_binding_ref"] = model["ref"]
    data["input"]["definitions"].extend((model, tool))
    original = definitions.validate_model_policies
    owners = []

    def record_owner(binding, *args, **kwargs):
        if binding.model_binding_id == "unowned-model":
            owners.append(kwargs["profile_ref"])
        return original(binding, *args, **kwargs)

    monkeypatch.setattr(definitions, "validate_model_policies", record_owner)
    result = report(data)
    assert result.aggregate_status == "deferred", result.model_dump_json(indent=2)
    assert owners == [None]
