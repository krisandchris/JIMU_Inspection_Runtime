"""Definition consumers bind actual Policy subtype and full declared Context contract."""

from copy import deepcopy

import pytest
from fixtures.wp01b.v1.support import codes, entry, fixture, report, with_step


def applicability(data):
    ref = entry(data, "profile")["definition"]["applicability_policy_ref"]
    return next(
        x["artifact"]["policy"] for x in data["input"]["policies"] if x["ref"] == ref
    )


@pytest.mark.parametrize("field", ["profile", "step"])
def test_missing_actual_expression_policy_is_incomplete(field):
    data = with_step(fixture())
    ref = (
        entry(data, "profile")["definition"]["applicability_policy_ref"]
        if field == "profile"
        else entry(data, "tool_plan")["definition"]["steps"][0]["skip_policy_ref"]
    )
    data["input"]["policies"] = [
        x for x in data["input"]["policies"] if x["ref"] != ref
    ]
    result = report(data)
    assert result.aggregate_status == "incomplete"
    assert "policy.content_unavailable" in codes(result)
    assert "definitions.deferred.applicability_content" not in codes(result)
    assert "definitions.deferred.tool_skip_content" not in codes(result)


@pytest.mark.parametrize(
    "mutation,expected",
    [("path", "field_unknown"), ("digest", "schema_digest_mismatch")],
)
def test_definition_checks_unused_applicability_allowlist(mutation, expected):
    data = fixture()
    policy = applicability(data)
    selector = deepcopy(entry(data, "question")["definition"]["context_allowlist"][0])
    if mutation == "path":
        selector["field_path"] = ["not_declared"]
    else:
        selector["schema_ref"]["digest"] = "b" * 64
    policy["context_allowlist"] = [selector]
    result = report(data)
    assert result.aggregate_status == "invalid"
    assert expected in codes(result)


def test_foreign_identity_constraint_is_unverified_not_false_or_invalid():
    data = fixture()
    applicability(data)["identity_constraints"] = [
        dict(
            artifact_kind="profile",
            artifact_id="other-profile",
            version_selector="1.0.0",
        )
    ]
    result = report(data)
    assert result.aggregate_status == "deferred"
    assert "definitions.deferred.applicability_identity_eligibility" in codes(result)


def test_context_selection_rule_uses_its_actual_applicability_policy():
    data = fixture()
    profile = entry(data, "profile")["definition"]
    policy = next(
        x["artifact"]["policy"]
        for x in data["input"]["policies"]
        if x["ref"] == profile["question_selection_policy_ref"]
    )
    policy.update(
        mode="mandatory_plus_optional",
        optional_source="context_rules",
        mandatory_question_refs=[],
        optional_question_refs=profile["question_refs"],
        context_selection_rules=[
            dict(
                question_ref=profile["question_refs"][0],
                applicability_policy_ref=profile["applicability_policy_ref"],
            )
        ],
    )
    result = report(data)
    assert result.aggregate_status == "deferred", result.model_dump_json()
