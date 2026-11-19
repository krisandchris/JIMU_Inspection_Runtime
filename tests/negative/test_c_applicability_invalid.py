import json

import pytest
from fixtures.wp01c.applicability.cases import (
    content_ref,
    metadata_ref,
    runtime_ref,
    wire_case,
)
from jsonschema import Draft202012Validator
from pydantic import TypeAdapter, ValidationError

from jimu_runtime.domain.runtime.applicability import (
    ApplicabilityLevelTrace,
    ApplicabilityTrace,
    ProfileEligibilityEvaluation,
    RuleApplicabilityEvaluation,
    UnresolvedObligation,
)
from jimu_runtime.domain.validation.applicability import (
    ApplicabilityRecordSet,
    ApplicabilityScope,
    validate_applicability_records,
)


def validate(scope, raw):
    validate_applicability_records(
        ApplicabilityScope.model_validate_json(json.dumps(scope)),
        ApplicabilityRecordSet.model_validate_json(json.dumps(raw)),
    )


@pytest.mark.parametrize(("case", "index"), [("H01", 1), ("H02", 2), ("H03", 2)])
def test_offered_child_false_cannot_erase_parent_false_or_unknown(case, index):
    _, raw = wire_case(case)
    _, false = wire_case("H07-rule-set" if index == 1 else "H07-rule")
    invalid = raw["traces"][2]
    invalid["levels"][index] = false["traces"][2]["levels"][index]
    with pytest.raises(ValidationError, match="first decisive ancestor"):
        ApplicabilityTrace.model_validate_json(json.dumps(invalid))


@pytest.mark.parametrize(
    ("case", "index", "field", "value"),
    [
        ("H01", 2, "truth", "unknown"),
        ("H02", 1, "evaluation_owner", "n06"),
        ("H03", 2, "declared_input_refs", [metadata_ref()]),
        ("H03", 2, "reason_evidence_refs", [metadata_ref()]),
        ("H05", 0, "evaluation_owner", "n06"),
        ("H05", 0, "evaluation_owner", "n01"),
        ("H05", 0, "evaluation_owner", "n02"),
        ("H05", 1, "evaluation_owner", "pre_n02_gate"),
        ("H05", 2, "truth", "not_evaluated"),
        ("H05", 2, "truth", True),
        ("H05", 2, "reason_code", "metadata_unknown"),
        (
            "H05",
            2,
            "source_evaluation_ref",
            runtime_ref("profile_eligibility_evaluation", "wrong"),
        ),
        ("H07-rule-set", 1, "reason_evidence_refs", []),
        ("H04", 2, "validation_issues", []),
        ("H04", 2, "reason_code", "excluded_by_policy"),
    ],
)
def test_schema_shape_and_model_reject_wrong_level_fields(case, index, field, value):
    _, raw = wire_case(case)
    level = raw["traces"][2]["levels"][index]
    level[field] = value
    adapter = TypeAdapter(ApplicabilityLevelTrace)
    assert not Draft202012Validator(adapter.json_schema()).is_valid(level)
    with pytest.raises(ValidationError):
        adapter.validate_json(json.dumps(level))


@pytest.mark.parametrize(
    "record_kind", ["fact", "candidate_prediction", "decision", "model_invocation"]
)
def test_evaluated_declared_inputs_cannot_be_non_metadata_refs(record_kind):
    _, raw = wire_case()
    level = raw["traces"][2]["levels"][1]
    level["declared_input_refs"].append(runtime_ref(record_kind, "fake"))
    with pytest.raises(ValidationError, match="declared_input_refs"):
        TypeAdapter(ApplicabilityLevelTrace).validate_json(json.dumps(level))


def test_evidence_must_cite_exact_policy_or_declared_input():
    _, raw = wire_case("H02")
    level = raw["traces"][0]["levels"][0]
    level["reason_evidence_refs"][0]["digest"] = "e" * 64
    with pytest.raises(ValidationError, match="pinned applicability policy"):
        TypeAdapter(ApplicabilityLevelTrace).validate_json(json.dumps(level))


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("truth", "unknown"),
        ("node_run_ref", runtime_ref("node_run", "made-up")),
        ("attempt_ref", runtime_ref("attempt", "made-up")),
        ("object_ref", runtime_ref("object", "made-up")),
        ("object_ref", None),
        ("decisive_ancestor_ref", None),
    ],
)
def test_projection_forbids_fabricated_origin_and_wire_null(field, value):
    _, raw = wire_case("H03")
    rule = raw["rule_evaluations"][0]
    rule[field] = value
    schema = RuleApplicabilityEvaluation.model_json_schema()
    assert not Draft202012Validator(schema).is_valid(rule)
    with pytest.raises(ValidationError):
        RuleApplicabilityEvaluation.model_validate_json(json.dumps(rule))


def test_not_evaluated_requires_original_ancestor_identity():
    _, raw = wire_case("H03")
    trace = raw["traces"][2]
    trace["levels"][-1]["decisive_ancestor_ref"]["record_id"] = "superseded"
    with pytest.raises(ValidationError, match="first decisive ancestor"):
        ApplicabilityTrace.model_validate_json(json.dumps(trace))


@pytest.mark.parametrize(
    ("case", "path", "change", "message"),
    [
        ("H05", ("scope", "context_node_run", "status"), "failed", "successful N01"),
        ("H05", ("scope", "context_node_run", "node_id"), "N02", "successful N01"),
        (
            "H05",
            ("scope", "context_node_run", "node_id"),
            "N07",
            "context_node_run.node_id",
        ),
        (
            "H05",
            ("records", "profile_eligibility", "workflow_version"),
            "2.0.0",
            "workflow version",
        ),
        (
            "H05",
            ("records", "profile_eligibility", "release_id"),
            "other",
            "mismatched",
        ),
        (
            "H05",
            ("records", "profile_eligibility", "applicability_traces", 0, "record_id"),
            "missing",
            "missing applicability reference",
        ),
        (
            "H05",
            ("records", "traces", 1, "context_snapshot_digest"),
            "e" * 64,
            "ContextSnapshot digest",
        ),
        ("H05", ("records", "traces", 2, "bundle_digest"), "e" * 64, "mismatched"),
        ("H05", ("records", "traces", 2, "execution_id"), "foreign", "mismatched"),
        (
            "H05",
            ("records", "traces", 2, "context_snapshot_ref", "record_id"),
            "foreign",
            "mismatched",
        ),
        (
            "H05",
            ("records", "traces", 2, "levels", 2, "declared_input_refs"),
            [metadata_ref("undeclared")],
            "policy allowlist",
        ),
        (
            "H05",
            ("scope", "target_bindings", 1, "child_refs"),
            [],
            "forward Bundle membership",
        ),
        (
            "H05",
            ("scope", "target_bindings", 2, "policy_ref", "digest"),
            "e" * 64,
            "outside the pinned Bundle",
        ),
        (
            "H05",
            ("records", "traces", 2, "levels", 0, "declared_input_refs"),
            [],
            "exact selected parent prefix",
        ),
        (
            "H05",
            ("records", "traces", 2, "levels", 2, "source_evaluation_ref", "record_id"),
            "foreign",
            "owning N06 evaluation",
        ),
        (
            "H05",
            (
                "records",
                "rule_evaluations",
                0,
                "rule_set_applicability_evaluation_ref",
                "record_id",
            ),
            "unselected-revision",
            "missing applicability reference",
        ),
        (
            "H05",
            (
                "records",
                "rule_evaluations",
                0,
                "profile_eligibility_evaluation_ref",
                "record_id",
            ),
            "unselected-revision",
            "selected Profile eligibility",
        ),
        (
            "H05",
            ("records", "attempts", 0, "attempt", "execution_id"),
            "foreign",
            "actual N06 node run",
        ),
        ("H05", ("records", "node_runs", 0, "node_id"), "N05", "actual N06 node run"),
        (
            "H05",
            ("records", "node_runs", 0, "status"),
            "skipped",
            "actual N06 node run",
        ),
        (
            "H05",
            ("records", "attempts", 0, "attempt", "owner", "node_run_ref", "record_id"),
            "foreign",
            "actual N06 node run",
        ),
        (
            "H05",
            ("records", "attempts", 0, "attempt", "status"),
            "created",
            "actual N06 node run",
        ),
        (
            "H04",
            ("records", "objects", 0, "execution_id"),
            "foreign",
            "different Execution or Profile",
        ),
        (
            "H04",
            ("records", "attempts", 0, "object_ref", "record_id"),
            "foreign",
            "actual N06 node run",
        ),
        (
            "H04",
            ("records", "unresolved_obligations"),
            [],
            "requires an unresolved obligation",
        ),
        (
            "H04",
            ("records", "unresolved_obligations", 0, "object_ref", "record_id"),
            "foreign",
            "missing applicability reference",
        ),
        (
            "H03",
            ("records", "unresolved_obligations", 1, "obligation_id"),
            "double-counted",
            "share the ancestor obligation ID",
        ),
        (
            "H03",
            ("records", "unresolved_obligations", 1, "diagnostic_refs", 0, "level"),
            "rule",
            "decisive UNKNOWN metadata issue",
        ),
        (
            "H01",
            (
                "records",
                "unresolved_obligations",
                1,
                "diagnostic_refs",
                0,
                "issue_index",
            ),
            99,
            "decisive UNKNOWN metadata issue",
        ),
    ],
)
def test_finite_catalogues_reject_conflicting_references(case, path, change, message):
    scope, raw = wire_case(case, object_scope=(case == "H04"))
    cursor = {"scope": scope, "records": raw}
    for part in path[:-1]:
        cursor = cursor[part]
    cursor[path[-1]] = change
    with pytest.raises(ValueError, match=message):
        validate(scope, raw)


def test_profile_partition_is_disjoint_exhaustive_ordered_and_digest_exact():
    _, raw = wire_case()
    source = raw["profile_eligibility"]
    profile = source["configured_profile_refs"][0]
    extra = content_ref("profile", "second")
    for changes, error in [
        ({"excluded_profiles": [profile]}, "unique kind/id/version"),
        ({"eligible_profiles": []}, "exhaust the configured scope"),
        ({"eligible_profiles": [profile | {"digest": "e" * 64}]}, "exact refs"),
        ({"eligible_profiles": [extra]}, "exact refs"),
        (
            {
                "configured_profile_refs": [profile, extra],
                "eligible_profiles": [extra, profile],
            },
            "follow configured order",
        ),
    ]:
        with pytest.raises(ValidationError, match=error):
            ProfileEligibilityEvaluation.model_validate_json(
                json.dumps(source | changes)
            )


def test_trace_partition_truth_cannot_be_claimed_by_caller():
    scope, raw = wire_case("H01")
    gate = raw["profile_eligibility"]
    gate["eligible_profiles"] = gate.pop("unresolved_profiles")
    gate["unresolved_profiles"] = []
    with pytest.raises(ValueError, match="owning gate and partition"):
        validate(scope, raw)


def test_obligation_rule_scope_requires_exact_parent_ref():
    _, raw = wire_case("H04")
    obligation = raw["unresolved_obligations"][0]
    obligation.pop("rule_set_ref")
    assert not Draft202012Validator(UnresolvedObligation.model_json_schema()).is_valid(
        obligation
    )
    with pytest.raises(ValidationError, match="exact parent RuleSet"):
        UnresolvedObligation.model_validate_json(json.dumps(obligation))


def test_model_history_cannot_be_mutated_in_place():
    _, raw = wire_case("H01")
    trace = ApplicabilityTrace.model_validate_json(json.dumps(raw["traces"][2]))
    with pytest.raises(ValidationError, match="frozen"):
        trace.effective_truth = "false"
    with pytest.raises(ValidationError, match="frozen"):
        trace.levels[0].truth = "false"


def test_same_attempt_cannot_publish_two_evaluations_of_one_target_path():
    scope, raw = wire_case()
    _, other = wire_case()
    rule_set = other["rule_set_evaluations"][0]
    rule_set["evaluation_id"] = "duplicate-rule-set-revision"
    trace = other["traces"][1]
    trace["applicability_trace_id"] = "duplicate-rule-set-trace"
    trace["levels"][1]["source_evaluation_ref"]["record_id"] = rule_set["evaluation_id"]
    rule_set["applicability_trace_ref"]["record_id"] = trace["applicability_trace_id"]
    raw["rule_set_evaluations"].append(rule_set)
    raw["traces"].append(trace)
    with pytest.raises(
        ValueError, match="one Attempt cannot author multiple evaluations"
    ):
        validate(scope, raw)
