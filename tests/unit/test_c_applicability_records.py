import json

import pytest
from fixtures.wp01c.applicability.cases import PATHS, wire_case

from jimu_runtime.domain.runtime.applicability import NotEvaluatedApplicabilityLevel
from jimu_runtime.domain.validation.applicability import (
    ApplicabilityRecordSet,
    ApplicabilityScope,
    validate_applicability_records,
)


@pytest.mark.parametrize("case", PATHS, ids=lambda case: case["id"])
def test_ordered_paths_and_shared_unresolved_ledger(case):
    scope, raw = wire_case(case["id"])
    records = ApplicabilityRecordSet.model_validate_json(json.dumps(raw))
    validate_applicability_records(
        ApplicabilityScope.model_validate_json(json.dumps(scope)), records
    )
    rule_trace = records.traces[-1]
    assert rule_trace.decisive_level.value == case["decisive_level"]
    assert rule_trace.disposition.value == case["disposition"]
    for level, truth in zip(rule_trace.levels, case["truths"], strict=True):
        if truth is None:
            assert isinstance(level, NotEvaluatedApplicabilityLevel)
            assert "truth" not in level.model_dump(mode="json")
        else:
            assert level.truth.value == truth
    if case["disposition"] == "unresolved":
        assert {item.obligation_id for item in records.unresolved_obligations} == {
            "missing-region-metadata"
        }
        assert records.objects == ()


def test_reached_exclusion_preserves_independent_sibling_rule_set():
    scope, raw = wire_case("H07-rule-set", sibling=True)
    records = ApplicabilityRecordSet.model_validate_json(json.dumps(raw))
    validate_applicability_records(
        ApplicabilityScope.model_validate_json(json.dumps(scope)), records
    )
    assert [trace.disposition.value for trace in records.traces] == [
        "included",
        "excluded",
        "excluded",
        "included",
        "included",
    ]


def test_empty_configured_scope_is_explicit():
    scope, raw = wire_case()
    scope.update(configured_profile_refs=[], target_bindings=[])
    raw["profile_eligibility"].update(
        configured_profile_refs=[], eligible_profiles=[], applicability_traces=[]
    )
    for key in raw.keys() - {"profile_eligibility"}:
        raw[key] = []
    validate_applicability_records(
        ApplicabilityScope.model_validate_json(json.dumps(scope)),
        ApplicabilityRecordSet.model_validate_json(json.dumps(raw)),
    )


def test_object_scoped_n06_attempt_pins_obligation():
    scope, raw = wire_case("H04", object_scope=True)
    records = ApplicabilityRecordSet.model_validate_json(json.dumps(raw))
    validate_applicability_records(
        ApplicabilityScope.model_validate_json(json.dumps(scope)), records
    )
    assert (
        records.rule_evaluations[0].object_ref
        == records.unresolved_obligations[0].object_ref
    )


def test_selected_revisions_preserve_both_n06_attempt_histories():
    scope, raw = wire_case()
    original = ApplicabilityRecordSet.model_validate_json(json.dumps(raw))
    _, repeated = wire_case()
    second_attempt = repeated["attempts"][0]
    second_attempt["attempt"]["attempt_number"] = 2
    second_attempt["attempt"]["attempt_ref"]["record_id"] = "n06-attempt2"
    raw["attempts"].append(second_attempt)

    second_set = repeated["rule_set_evaluations"][0]
    second_rule = repeated["rule_evaluations"][0]
    for wrapper, trace, revision in (
        (second_set, repeated["traces"][1], "rule-set-rev2"),
        (second_rule, repeated["traces"][2], "rule-rev2"),
    ):
        trace["applicability_trace_id"] += "-attempt2"
        wrapper["applicability_trace_ref"]["record_id"] = trace[
            "applicability_trace_id"
        ]
        wrapper["evaluation_id"] = revision
        wrapper["attempt_ref"] = second_attempt["attempt"]["attempt_ref"]
        target_level = trace["levels"][-1]
        target_level["source_evaluation_ref"]["record_id"] = revision
        raw["traces"].append(trace)
    repeated["traces"][2]["levels"][1] = repeated["traces"][1]["levels"][1]
    second_rule["rule_set_applicability_evaluation_ref"]["record_id"] = second_set[
        "evaluation_id"
    ]
    raw["rule_set_evaluations"].append(second_set)
    raw["rule_evaluations"].append(second_rule)

    selected = ApplicabilityRecordSet.model_validate_json(json.dumps(raw))
    validate_applicability_records(
        ApplicabilityScope.model_validate_json(json.dumps(scope)), selected
    )
    assert selected.rule_evaluations[0] == original.rule_evaluations[0]
    assert selected.traces[:3] == original.traces
    assert (
        selected.rule_evaluations[0].attempt_ref
        != selected.rule_evaluations[1].attempt_ref
    )


def test_invalid_metadata_unknown_and_projection_order():
    scope, raw = wire_case("H03")
    for trace in raw["traces"][1:]:
        evaluated = trace["levels"][1]
        evaluated["reason_code"] = "metadata_invalid"
        evaluated["validation_issues"][0]["code"] = "metadata.invalid"
    raw["unresolved_obligations"][0]["reason_code"] = "metadata_invalid"
    raw["unresolved_obligations"].reverse()
    validate_applicability_records(
        ApplicabilityScope.model_validate_json(json.dumps(scope)),
        ApplicabilityRecordSet.model_validate_json(json.dumps(raw)),
    )
