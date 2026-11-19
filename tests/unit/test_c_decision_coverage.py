import json

import pytest
from fixtures.wp01c.decisions.cases import (
    optional_fact_case,
    supplied_case,
    two_rule_case,
)

from jimu_runtime.domain.runtime.coverage import ExecutionCoverage
from jimu_runtime.domain.validation.decision_coverage import (
    DecisionCoverageRecords,
    validate_decision_coverage,
)


@pytest.mark.parametrize(
    "options, coverage_status, result",
    [
        ({}, "complete", "violation"),
        ({"mode": "false"}, "complete", "no_violation"),
        ({"mode": "false", "needs_fact": True}, "complete", "no_violation"),
        ({"mode": "unknown"}, "partial", "uncertain"),
        ({"mode": "blocked", "needs_fact": True}, "partial", "uncertain"),
        ({"hierarchy": "H01"}, "none", "uncertain"),
        ({"hierarchy": "H02"}, "not_applicable", "not_applicable"),
        ({"hierarchy": "H03"}, "partial", "uncertain"),
        ({"hierarchy": "H04"}, "partial", "uncertain"),
        ({"hierarchy": "H07-rule-set"}, "complete", "not_applicable"),
        ({"hierarchy": "H07-rule"}, "complete", "not_applicable"),
        ({"discovery": "no_hits"}, "none", "uncertain"),
        ({"discovery": "failed"}, "none", "uncertain"),
        ({"grounding": "no_target"}, "none", "uncertain"),
        ({"grounding": "failed"}, "none", "uncertain"),
        ({"grounding": "partial"}, "partial", "violation"),
        ({"discovery": "partial"}, "partial", "violation"),
    ],
)
def test_supplied_records_agree(options, coverage_status, result):
    coverage, finite = supplied_case(**options)
    model = ExecutionCoverage.model_validate_json(json.dumps(coverage))
    validate_decision_coverage(
        model, DecisionCoverageRecords.model_validate_json(json.dumps(finite))
    )
    assert model.coverage_status.value == coverage_status
    assert model.summary_result.value == result


def test_upstream_context_failure_has_no_gate_or_n06():
    coverage, finite = supplied_case(discovery="no_hits")
    n01 = {"record_kind": "node_run", "record_id": "n01-run"}
    scope = coverage["applicability_scope"]
    coverage["applicability_scope"] = {
        "scope_status": "blocked",
        "configured_profile_refs": scope["configured_profile_refs"],
        "blocker_node_run_ref": n01,
    }
    coverage["discovery_outcome"] = "not_evaluated"
    coverage["reasons"] = ["context_build_blocked", "discovery_not_evaluated"]
    coverage["discovery_model_invocation_refs"] = []
    coverage["selected_applicability_evaluation_refs"] = []
    coverage["applicability_trace_refs"] = []
    for obligation in coverage["mandatory_rule_coverage"]["obligations"]:
        obligation["outcome"] = {
            "disposition": "dependency_not_evaluated",
            "reason": "context_build_blocked",
            "blocker_node_run_ref": n01,
        }
    finite["applicability"] = None
    finite["model_invocations"] = []
    finite["node_runs"] = [
        {
            "node_run_ref": n01,
            "execution_id": coverage["execution_id"],
            "node_id": "N01",
            "status": "failed",
        }
    ]
    validate_decision_coverage(
        ExecutionCoverage.model_validate_json(json.dumps(coverage)),
        DecisionCoverageRecords.model_validate_json(json.dumps(finite)),
    )


def test_collect_all_keeps_two_rules_with_one_shared_category():
    coverage, finite = two_rule_case()
    model = ExecutionCoverage.model_validate_json(json.dumps(coverage))
    records = DecisionCoverageRecords.model_validate_json(json.dumps(finite))
    validate_decision_coverage(model, records)
    assert len(model.matches) == 2
    assert len(records.decisions[0].conflict_resolution.matched_category_ids) == 1


def test_match_survives_remaining_mandatory_blocker():
    coverage, finite = two_rule_case(second_outcome="blocked")
    model = ExecutionCoverage.model_validate_json(json.dumps(coverage))
    validate_decision_coverage(
        model, DecisionCoverageRecords.model_validate_json(json.dumps(finite))
    )
    assert model.summary_result.value == "violation"
    assert model.coverage_status.value == "partial"


def test_consumed_optional_fact_uses_explicit_selected_operand_proof():
    coverage, finite = optional_fact_case()
    model = ExecutionCoverage.model_validate_json(json.dumps(coverage))
    records = DecisionCoverageRecords.model_validate_json(json.dumps(finite))
    validate_decision_coverage(model, records)
    assert len(records.decisions[0].rule_evaluations[0].optional_fact_proofs) == 1
