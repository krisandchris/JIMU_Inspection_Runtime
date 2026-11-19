import json

import pytest
from fixtures.wp01c.decisions.cases import optional_fact_case, supplied_case

from jimu_runtime.domain.runtime.coverage import ExecutionCoverage
from jimu_runtime.domain.validation.decision_coverage import (
    DecisionCoverageRecords,
    validate_decision_coverage,
)


def validate(coverage, relative_records):
    validate_decision_coverage(
        ExecutionCoverage.model_validate_json(json.dumps(coverage)),
        DecisionCoverageRecords.model_validate_json(json.dumps(relative_records)),
    )


def test_unknown_cannot_become_complete_false_decision():
    coverage, records = supplied_case(mode="unknown")
    records["decisions"][0]["result"] = "no_violation"
    with pytest.raises(ValueError, match="mandatory work"):
        validate(coverage, records)


def test_blocked_guard_cannot_establish_false():
    coverage, records = supplied_case(mode="blocked", needs_fact=True)
    records["decisions"][0]["result"] = "no_violation"
    with pytest.raises(ValueError, match="mandatory work"):
        validate(coverage, records)


def test_stale_authoritative_revision_cannot_be_selected_by_attempt_order():
    coverage, records = supplied_case(needs_fact=True)
    records["authoritative_selections"][0]["selected_attempt_id"] = "n04-other-attempt"
    with pytest.raises(ValueError, match="exact authoritative"):
        validate(coverage, records)


def test_crossprofile_fact_cannot_support_a_rule():
    coverage, records = supplied_case(needs_fact=True)
    records["facts"][0]["profile_ref"]["artifact_id"] = "foreign-profile"
    with pytest.raises(ValueError, match="Object/Profile"):
        validate(coverage, records)


def test_unselected_rule_applicability_revision_is_rejected():
    coverage, records = supplied_case()
    decision = records["decisions"][0]
    decision["rule_evaluations"][0]["selected_rule_evaluation_ref"]["record_id"] = (
        "stale"
    )
    with pytest.raises(ValueError, match="ledger-selected Rule revision"):
        validate(coverage, records)


def test_collect_all_must_keep_every_valid_match():
    coverage, records = supplied_case()
    records["decisions"][0]["conflict_resolution"].update(
        matched_rules=[], matched_category_ids=[]
    )
    with pytest.raises(ValueError, match="collect_all must retain every"):
        validate(coverage, records)


def test_candidate_categories_do_not_remove_mandatory_scope():
    coverage, records = supplied_case()
    omitted = coverage["mandatory_rule_coverage"]["obligations"].pop()
    refs = omitted["outcome"]["selected_applicability"]
    coverage["selected_applicability_evaluation_refs"].remove(refs["evaluation_ref"])
    coverage["applicability_trace_refs"].remove(refs["trace_ref"])
    coverage["profiles"][0]["obligation_ids"].remove(omitted["obligation_id"])
    with pytest.raises(ValueError, match="ledger must exhaust"):
        validate(coverage, records)


@pytest.mark.parametrize(
    "options",
    [{"discovery": "no_hits"}, {"grounding": "no_target"}, {"grounding": "failed"}],
)
def test_empty_object_cannot_establish_complete_negative(options):
    coverage, records = supplied_case(**options)
    coverage.update(coverage_status="complete", summary_result="no_violation")
    with pytest.raises(ValueError, match="complete coverage requires nonempty"):
        validate(coverage, records)


def test_descendant_must_cite_its_decisive_ancestor_obligation():
    coverage, records = supplied_case(hierarchy="H01")
    entry = coverage["mandatory_rule_coverage"]["obligations"][2]
    entry["outcome"]["ancestor_obligation_id"] = "foreign-obligation"
    with pytest.raises(ValueError, match="decisive ancestor obligation"):
        validate(coverage, records)


def test_coverage_cannot_drop_valid_subset_match():
    coverage, records = supplied_case(grounding="partial")
    coverage.update(matches=[], summary_result="uncertain")
    with pytest.raises(ValueError, match="retain every Object"):
        validate(coverage, records)


@pytest.mark.parametrize(
    "mutation, error",
    [
        ("no_proof", "Fact refs must exactly match"),
        ("undeclared", "not a declared optional selector"),
        ("unselected", "unselected authoritative selection revision"),
    ],
)
def test_optional_fact_must_match_declared_selector_and_authority(mutation, error):
    coverage, records = optional_fact_case()
    evaluation = records["decisions"][0]["rule_evaluations"][0]
    if mutation == "no_proof":
        evaluation["optional_fact_proofs"] = []
    elif mutation == "undeclared":
        evaluation["optional_fact_proofs"][0]["selector"]["fact_key"] = "other-fact"
    else:
        records["authoritative_selections"] = []
    with pytest.raises(ValueError, match=error):
        validate(coverage, records)
