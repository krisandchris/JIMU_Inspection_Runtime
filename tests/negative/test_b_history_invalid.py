"""Parser abuse, atomic conflicts and finite-history counterexamples."""

import json
from copy import deepcopy
from pathlib import Path

import pytest
from pydantic import TypeAdapter

from jimu_runtime.domain.fact_schemas.models import FactSchemaDefinition
from jimu_runtime.domain.fact_schemas.projection import generate_value_schema
from jimu_runtime.domain.fact_schemas.registry import FactSchemaRegistryBuilder
from jimu_runtime.domain.references import ContentRef
from jimu_runtime.domain.runtime.authority import AuthoritativeResultSelection
from jimu_runtime.domain.runtime.candidates import (
    CandidateParseLimits,
    CandidateSnapshot,
    CandidateSnapshotLimits,
    InvalidCandidate,
    RejectedCandidates,
    UnavailablePrior,
    ValidatedPrior,
    append_candidates,
    validate_candidate_submission,
)
from jimu_runtime.domain.runtime.facts import FactRecord
from jimu_runtime.domain.validation.authority import (
    LocalAuthorityHistoryInput,
    validate_local_authority_history,
)
from jimu_runtime.domain.validation.authority_inputs import (
    AttemptDescriptor,
    AuthorityQuestionInput,
    AuthorityToolPlanInput,
    FactCandidateSubmission,
    NodeRunDescriptor,
    ObjectDescriptor,
    ObservationResultDescriptor,
    QuestionSelectionDescriptor,
    SystemOutputResultDescriptor,
    ToolStepResultDescriptor,
)
from jimu_runtime.domain.validation.tool_plans import ToolRegistration


def fixture() -> dict:
    return json.loads(
        (Path(__file__).parents[1] / "fixtures/wp01b/a2/contracts.json").read_text()
    )


def candidate(
    raw: dict | None = None, encoded: bytes | None = None
) -> FactCandidateSubmission:
    raw = (
        raw
        if raw is not None
        else fixture()["question_history"]["candidates"]["entries"][0]["fact"]
    )
    original = json.dumps(raw, separators=(",", ":")).encode()
    return FactCandidateSubmission(
        fact=TypeAdapter(FactRecord).validate_json(original),
        raw_bytes=original if encoded is None else encoded,
    )


def limits() -> dict:
    data = fixture()
    return {
        "parse_limits": CandidateParseLimits.model_validate(data["parse_limits"]),
        "snapshot_limits": CandidateSnapshotLimits.model_validate(
            data["snapshot_limits"]
        ),
    }


@pytest.mark.parametrize(
    "encoded",
    [
        b"\xff",
        b"\xef\xbb\xbf{}",
        b'{"x":1,"x":2}',
        b'{"x":{"a":1,"a":1}}',
        b'{"x":1,"\\u0078":1}',
        b'{"x":"\\ud800"}',
        b'{"\\ud800":1}',
        b'{"x":NaN}',
        b'{"x":Infinity}',
        b'{"x":-Infinity}',
        b'{"x":1e9999}',
        b"{}{}",
        b'{"x":}',
        b"{/*comment*/}",
    ],
)
def test_hostile_json_fails_before_any_authority_claim(encoded: bytes) -> None:
    result = validate_candidate_submission(
        candidate(encoded=encoded), limits=limits()["parse_limits"]
    )
    assert isinstance(result, InvalidCandidate)
    assert result.report.aggregate_status == "invalid"
    assert result.report.checks[-1].status == "deferred"
    assert not hasattr(result, "submission")


@pytest.mark.parametrize("change", ["bytes", "depth", "members", "array", "number"])
def test_explicit_parser_resource_limits(change: str) -> None:
    budget = limits()["parse_limits"]
    encoded = b"{}"
    if change == "bytes":
        budget = budget.model_copy(update={"max_bytes": 1})
    elif change == "depth":
        encoded = b"[" * 33 + b"0" + b"]" * 33
    elif change == "members":
        budget = budget.model_copy(update={"max_object_members": 1})
        encoded = b'{"a":0,"b":1}'
    elif change == "array":
        budget = budget.model_copy(update={"max_array_items": 1})
        encoded = b"[0,1]"
    else:
        budget = budget.model_copy(update={"max_number_chars": 2})
        encoded = b'{"n":123}'
    result = validate_candidate_submission(candidate(encoded=encoded), limits=budget)
    assert isinstance(result, InvalidCandidate)
    assert "limit" in result.report.checks[0].issues[0].code


def test_long_key_diagnostic_reports_parser_root_without_echoing_input() -> None:
    encoded = json.dumps({"x" * 200: "\ud800"}).encode()
    result = validate_candidate_submission(
        candidate(encoded=encoded), limits=limits()["parse_limits"]
    )
    assert isinstance(result, InvalidCandidate)
    problem = result.report.checks[0].issues[0]
    assert problem.path == ()
    assert "Parser-root" in problem.message
    assert "x" * 200 not in problem.message


@pytest.mark.parametrize("change", ["utc_numeric", "value", "number_kind"])
def test_bytes_must_match_complete_strict_fact(change: str) -> None:
    raw = fixture()["question_history"]["candidates"]["entries"][0]["fact"]
    if change == "utc_numeric":
        raw["created_at"] = "0"
    elif change == "value":
        raw["value"]["value"] = True
    else:
        raw["value"] = {"kind": "number", "value": 1}
    result = validate_candidate_submission(
        candidate(encoded=json.dumps(raw).encode()), limits=limits()["parse_limits"]
    )
    assert isinstance(result, InvalidCandidate)


def test_invalid_or_over_budget_prior_is_unavailable_not_validated() -> None:
    prior = CandidateSnapshot(entries=(candidate(encoded=b"not json"),))
    result = append_candidates(prior, (), **limits())
    assert isinstance(result, RejectedCandidates)
    assert isinstance(result.prior, UnavailablePrior)
    assert not hasattr(result.prior, "snapshot")
    valid = CandidateSnapshot(entries=(candidate(),))
    budgets = limits()
    budgets["parse_limits"] = budgets["parse_limits"].model_copy(
        update={"max_bytes": 1}
    )
    result = append_candidates(valid, (), **budgets)
    assert isinstance(result, RejectedCandidates)
    assert isinstance(result.prior, UnavailablePrior)


def test_atomic_batch_conflict_and_fact_id_rebinding() -> None:
    prior = CandidateSnapshot(entries=())
    first = candidate()
    raw = json.loads(first.raw_bytes)
    raw["attempt_id"] = "attempt-2"
    result = append_candidates(prior, (first, candidate(raw)), **limits())
    assert isinstance(result, RejectedCandidates)
    assert isinstance(result.prior, ValidatedPrior)
    assert result.prior.snapshot.entries == ()
    conflict = result.conflicts[0]
    assert conflict.conflict_kind == "fact_id_rebinding"
    assert (
        conflict.old_location.origin,
        conflict.old_location.index,
        conflict.new_index,
    ) == ("batch", 0, 1)


def test_budget_counts_duplicate_deliveries_prior_plus_batch() -> None:
    entry = candidate()
    prior = CandidateSnapshot(entries=(entry,))
    budgets = limits()
    budgets["snapshot_limits"] = budgets["snapshot_limits"].model_copy(
        update={"max_total_raw_bytes": len(entry.raw_bytes)}
    )
    result = append_candidates(prior, (entry,), **budgets)
    assert isinstance(result, RejectedCandidates)
    assert isinstance(result.prior, ValidatedPrior)
    assert result.conflicts == ()
    assert result.report.checks[0].issues[0].code == "total_byte_budget"


def test_conflict_overflow_retains_bounded_prefix_and_explicit_marker() -> None:
    prior = CandidateSnapshot(entries=(candidate(),))
    submissions = []
    for index in range(65):
        raw = json.loads(prior.entries[0].raw_bytes)
        raw["fact_id"] = f"conflicting-{index}"
        submissions.append(candidate(raw))
    result = append_candidates(prior, tuple(submissions), **limits())
    assert isinstance(result, RejectedCandidates)
    assert len(result.conflicts) == 64
    assert result.conflict_completeness == "overflow"
    assert any(
        problem.code == "conflict_overflow"
        for problem in result.report.checks[0].issues
    )


def history_input(raw: dict) -> LocalAuthorityHistoryInput:
    values = dict(raw)
    values["candidates"] = CandidateSnapshot(
        entries=tuple(
            candidate(entry["fact"], entry["raw_bytes"].encode())
            for entry in raw["candidates"]["entries"]
        )
    )
    models = {
        "prior_selections": AuthoritativeResultSelection,
        "proposed_selections": AuthoritativeResultSelection,
        "objects": ObjectDescriptor,
        "node_runs": NodeRunDescriptor,
        "attempts": AttemptDescriptor,
        "question_selections": QuestionSelectionDescriptor,
        "observation_results": ObservationResultDescriptor,
        "tool_results": ToolStepResultDescriptor,
        "system_results": SystemOutputResultDescriptor,
        "questions": AuthorityQuestionInput,
        "tools": ToolRegistration,
        "plans": AuthorityToolPlanInput,
    }
    for field, model in models.items():
        values[field] = tuple(
            TypeAdapter(model).validate_json(json.dumps(entry)) for entry in raw[field]
        )
    return LocalAuthorityHistoryInput.model_validate(values)


def registry(data: dict):
    raw = data["fact_schema"]
    model = FactSchemaDefinition.model_validate(raw)
    raw["payload_schema_ref"] = generate_value_schema(
        model.type_definition, model.value_schema_generation
    ).child_ref.model_dump(mode="json")
    ref = data["question_history"]["candidates"]["entries"][0]["fact"][
        "fact_schema_ref"
    ]
    return (
        FactSchemaRegistryBuilder()
        .register(
            ContentRef.model_validate(ref), FactSchemaDefinition.model_validate(raw)
        )
        .freeze()
    )


def run_history(data: dict, raw: dict, schemas=None):
    return validate_local_authority_history(
        history_input(raw),
        fact_schemas=registry(data) if schemas is None else schemas,
        **limits(),
    )


def codes(report) -> set[str]:
    return {problem.code for check in report.checks for problem in check.issues}


@pytest.mark.parametrize(
    "change,expected",
    [
        ("late", "result_arrival"),
        ("failed", "ineligible_attempt"),
        ("wrong_node", "wrong_node"),
        ("untrusted", "untrusted_result"),
        ("anchor", "question_anchor_mismatch"),
        ("cherry_pick", "whole_result_selection"),
        ("primary_key", "question_primary_contract"),
        ("question_digest", "definition_digest_mismatch"),
    ],
)
def test_n04_known_contradictions_reject(change: str, expected: str) -> None:
    data = fixture()
    raw = data["question_history"]
    if change == "late":
        raw["observation_results"][0]["arrival_qualification"] = "late"
    elif change == "failed":
        raw["attempts"][0]["status"] = "failed"
    elif change == "wrong_node":
        raw["node_runs"][0]["node_id"] = "N05"
    elif change == "untrusted":
        raw["observation_results"][0]["association_validation"] = "untrusted"
    elif change == "anchor":
        raw["question_selections"][0]["effective_question_refs"][0]["artifact_id"] = (
            "other"
        )
    elif change == "cherry_pick":
        raw["proposed_selections"][0]["selected_fact_refs"] = []
    elif change == "primary_key":
        raw["questions"][0]["definition"]["primary_fact_key"] = "other"
    else:
        raw["questions"][0]["ref"]["digest"] = "b" * 64
    report = run_history(data, raw)
    assert report.aggregate_status == "invalid", report
    assert expected in codes(report)


@pytest.mark.parametrize(
    "change",
    [
        "result",
        "attempt",
        "object",
        "definition",
        "anchor",
        "arrival",
        "qualification",
        "schema",
    ],
)
def test_missing_or_unresolved_evidence_is_incomplete(change: str) -> None:
    data = fixture()
    raw = data["question_history"]
    schemas = None
    if change == "result":
        raw["observation_results"] = []
    elif change == "attempt":
        raw["attempts"] = []
    elif change == "object":
        raw["objects"] = []
    elif change == "definition":
        raw["questions"] = []
    elif change == "anchor":
        raw["question_selections"] = []
    elif change == "arrival":
        raw["observation_results"][0]["arrival_qualification"] = "unresolved"
    elif change == "qualification":
        raw["question_selections"][0]["eligibility_qualification"] = "unresolved"
    else:
        schemas = FactSchemaRegistryBuilder().freeze()
    assert run_history(data, raw, schemas).aggregate_status == "incomplete"


@pytest.mark.parametrize(
    "change,expected",
    [
        ("fork", "authority_revision_fork"),
        ("known_gap", "authority_previous_mismatch"),
        ("rebind", "record_rebinding"),
    ],
)
def test_explicit_selection_history_contradictions(change: str, expected: str) -> None:
    data = fixture()
    raw = data["question_history"]
    first = deepcopy(raw["proposed_selections"][0])
    raw["prior_selections"] = [first]
    newer = raw["proposed_selections"][0]
    if change == "fork":
        newer["selection_id"] = "selection-2"
    elif change == "known_gap":
        newer.update(
            selection_id="selection-3",
            authority_revision=3,
            expected_previous_selection_ref={
                "record_kind": "authoritative_result_selection",
                "record_id": "selection-1",
            },
        )
    else:
        newer["reason"] = "manual_review"
    report = run_history(data, raw)
    assert report.aggregate_status == "invalid"
    assert expected in codes(report)


def test_missing_previous_is_incomplete_and_order_does_not_select_latest() -> None:
    data = fixture()
    raw = data["question_history"]
    first = deepcopy(raw["proposed_selections"][0])
    raw["proposed_selections"][0].update(
        selection_id="selection-2",
        authority_revision=2,
        expected_previous_selection_ref={
            "record_kind": "authoritative_result_selection",
            "record_id": "selection-1",
        },
    )
    assert run_history(data, raw).aggregate_status == "incomplete"
    raw["prior_selections"] = [raw["proposed_selections"][0], first]
    raw["proposed_selections"] = []
    failed = deepcopy(raw["attempts"][0])
    failed["attempt_ref"]["record_id"] = "unselected-failed"
    failed.update(attempt_number=9, status="failed")
    raw["attempts"].append(failed)
    assert run_history(data, raw).aggregate_status == "deferred"


@pytest.mark.parametrize(
    "change,expected",
    [
        ("required_absence", "required_tool_absence"),
        ("unmapped", "tool_output_set"),
        ("alias", "tool_key_alias"),
        ("implementation", "tool_implementation"),
    ],
)
def test_n05_uses_actual_plan_and_tool_contracts(change: str, expected: str) -> None:
    data = fixture()
    raw = data["tool_history"]
    if change == "required_absence":
        raw["candidates"]["entries"] = []
        raw["proposed_selections"][0]["selected_fact_refs"] = []
        raw["tool_results"][0]["mapped_outputs"] = [
            {
                "disposition": "optional_absence",
                "output_name": "relation",
                "reason": {
                    "reason_code": "missing",
                    "message": "Synthetic missing output",
                    "diagnostic_refs": [],
                },
            }
        ]
    elif change == "unmapped":
        raw["plans"][0]["definition"]["steps"][0]["fact_output_mappings"] = []
    elif change == "alias":
        raw["plans"][0]["definition"]["steps"][0]["fact_output_mappings"][0][
            "target_fact_key"
        ] = "alias"
    else:
        raw["tool_results"][0]["actual_implementation_version"] = "9.0.0"
    report = run_history(data, raw)
    assert report.aggregate_status == "invalid"
    assert expected in codes(report)


def test_optional_tool_absence_and_system_absence_are_not_false_facts() -> None:
    data = fixture()
    raw = data["tool_history"]
    raw["tools"][0]["definition"]["fact_outputs"][0]["required"] = False
    raw["candidates"]["entries"] = []
    raw["proposed_selections"][0]["selected_fact_refs"] = []
    raw["tool_results"][0]["mapped_outputs"] = [
        {
            "disposition": "optional_absence",
            "output_name": "relation",
            "reason": {
                "reason_code": "missing",
                "message": "Synthetic absence",
                "diagnostic_refs": [],
            },
        }
    ]
    assert run_history(data, raw).aggregate_status == "deferred"
    report = run_history(data, data["system_history"])
    assert report.aggregate_status == "deferred"
    assert "system_output_declaration" in codes(report)


@pytest.mark.parametrize("change", ["value", "confidence", "schema_digest"])
def test_registry_conformance_is_distinct_from_producer_admission(change: str) -> None:
    data = fixture()
    raw = data["question_history"]
    fact = raw["candidates"]["entries"][0]["fact"]
    if change == "value":
        fact["value"] = {"kind": "integer", "value": 1}
    elif change == "confidence":
        fact["confidence"] = 2.5
    else:
        fact["fact_schema_ref"]["digest"] = "b" * 64
    raw["candidates"]["entries"][0]["raw_bytes"] = json.dumps(fact)
    report = run_history(data, raw)
    # For the digest case keep the registry's known digest fixed instead of
    # taking the changed candidate assertion as the registration authority.
    if change == "schema_digest":
        original = fixture()
        report = run_history(data, raw, registry(original))
    assert report.aggregate_status == "invalid"
    assert any(
        check.check_id == "full_authority_admission" and check.status == "deferred"
        for check in report.checks
    )


def test_bad_batch_candidate_does_not_commit_other_valid_candidates() -> None:
    prior = CandidateSnapshot(entries=())
    bad = candidate(encoded=b'{"x":1,"x":2}')
    result = append_candidates(prior, (candidate(), bad), **limits())
    assert isinstance(result, RejectedCandidates)
    assert isinstance(result.prior, ValidatedPrior)
    assert result.prior.snapshot == prior
    assert result.report.checks[0].issues[0].path[:3] == ("submissions", 1, "raw_bytes")
    assert result.conflicts == ()


def test_same_fact_id_cannot_rebind_in_another_execution() -> None:
    first = candidate()
    changed = json.loads(first.raw_bytes)
    changed["execution_id"] = "another-execution"
    result = append_candidates(
        CandidateSnapshot(entries=(first,)), (candidate(changed),), **limits()
    )
    assert isinstance(result, RejectedCandidates)
    assert result.conflicts[0].conflict_kind == "fact_id_rebinding"


def test_diagnostic_overflow_is_explicit_and_raw_invalid_input_is_not_echoed() -> None:
    invalid = candidate(encoded=b"not-json")
    result = append_candidates(
        CandidateSnapshot(entries=()), (invalid,) * 65, **limits()
    )
    assert isinstance(result, RejectedCandidates)
    assert result.conflicts == ()
    assert len(result.report.checks[0].issues) == 64
    assert result.report.checks[0].issues[-1].code == "diagnostic_overflow"
    assert "not-json" not in result.report.model_dump_json()


def test_snapshot_size_and_invalid_model_prior_never_fabricate_validated_history() -> (
    None
):
    first = candidate()
    raw = json.loads(first.raw_bytes)
    raw.update(fact_id="fact-2", attempt_id="attempt-2")
    prior = CandidateSnapshot(entries=(first, candidate(raw)))
    options = limits()
    options["snapshot_limits"] = options["snapshot_limits"].model_copy(
        update={"max_entries": 1}
    )
    result = append_candidates(prior, (), **options)
    assert isinstance(result, RejectedCandidates)
    assert isinstance(result.prior, UnavailablePrior)
    tainted = CandidateSnapshot(entries=()).model_copy(update={"hidden": True})
    result = append_candidates(tainted, (), **limits())
    assert isinstance(result, RejectedCandidates)
    assert isinstance(result.prior, UnavailablePrior)


def test_scoped_directory_conflict_is_not_overwritten_and_other_execution_not_resolved() -> (
    None
):
    data = fixture()
    raw = data["question_history"]
    duplicate = deepcopy(raw["observation_results"][0])
    duplicate["arrival_qualification"] = "late"
    raw["observation_results"].append(duplicate)
    report = run_history(data, raw)
    assert "record_rebinding" in codes(report)
    assert report.aggregate_status == "invalid"
    data = fixture()
    raw = data["question_history"]
    raw["objects"][0]["execution_id"] = "other"
    report = run_history(data, raw)
    assert "missing_object" in codes(report)
    assert report.aggregate_status == "incomplete"


def test_candidate_attempt_splicing_is_rejected_without_latest_selection() -> None:
    data = fixture()
    raw = data["question_history"]
    fact = raw["candidates"]["entries"][0]["fact"]
    fact["attempt_id"] = "attempt-2"
    raw["candidates"]["entries"][0]["raw_bytes"] = json.dumps(fact)
    assert "fact_scope_attempt" in codes(run_history(data, raw))


def test_trustworthy_unknown_primary_is_complete_and_remains_unknown() -> None:
    data = fixture()
    raw = data["question_history"]
    fact = raw["candidates"]["entries"][0]["fact"]
    fact.pop("value")
    fact.update(
        status="unknown",
        reason={
            "reason_code": "visibility",
            "message": "Synthetic unknown",
            "diagnostic_refs": [],
        },
    )
    raw["candidates"]["entries"][0]["raw_bytes"] = json.dumps(fact)
    report = run_history(data, raw)
    assert report.aggregate_status == "deferred", report


def test_unmapped_tool_output_is_not_forced_into_selected_fact_set() -> None:
    data = fixture()
    raw = data["tool_history"]
    unused = deepcopy(raw["tools"][0]["definition"]["fact_outputs"][0])
    unused.update(output_name="unused", fact_key="unused.key")
    raw["tools"][0]["definition"]["fact_outputs"].append(unused)
    assert run_history(data, raw).aggregate_status == "deferred"


def test_system_emitted_key_schema_permission_is_always_deferred() -> None:
    data = fixture()
    raw = data["system_history"]
    fact = json.loads(
        (Path(__file__).parents[1] / "fixtures/wp01b/a1/contracts.json").read_text()
    )["system_fact"]
    fact["fact_key"] = "self.asserted.system.key"
    raw["candidates"]["entries"] = [{"fact": fact, "raw_bytes": json.dumps(fact)}]
    raw["proposed_selections"][0]["selected_fact_refs"] = [
        {"record_kind": "fact", "record_id": "fact-1"}
    ]
    raw["system_results"][0]["output"] = {
        "disposition": "emitted",
        "output_name": "context",
        "fact_status": "known",
        "fact_ref": {"record_kind": "fact", "record_id": "fact-1"},
    }
    data["fact_schema"].update(scope="execution", allowed_producer_kinds=["system"])
    report = run_history(data, raw)
    assert report.aggregate_status == "deferred", report
    assert any(
        check.check_id == "system_output_declaration" and check.status == "deferred"
        for check in report.checks
    )


@pytest.mark.parametrize(
    "mode,expected", [("required", "invalid"), ("optional", "deferred")]
)
def test_registered_confidence_presence_policy_has_no_invented_scale(
    mode: str, expected: str
) -> None:
    data = fixture()
    raw = data["question_history"]
    data["fact_schema"]["confidence_metadata_policy"] = {
        "mode": mode,
        "allowed_fact_statuses": ["known"],
        "interpretation": "model_reported_not_calibrated_probability",
        "numeric_format": "finite_number",
    }
    if mode == "optional":
        fact = raw["candidates"]["entries"][0]["fact"]
        fact["confidence"] = -2.5
        raw["candidates"]["entries"][0]["raw_bytes"] = json.dumps(fact)
    assert run_history(data, raw).aggregate_status == expected


def test_nested_construct_taint_does_not_become_history_pass() -> None:
    data = fixture()
    inputs = history_input(data["question_history"])
    altered = inputs.model_copy(
        update={"node_runs": (inputs.node_runs[0].model_copy(update={"hidden": True}),)}
    )
    report = validate_local_authority_history(
        altered, fact_schemas=registry(data), **limits()
    )
    assert report.aggregate_status == "invalid"
    assert (
        next(
            check for check in report.checks if check.check_id == "history_chain"
        ).status
        == "incomplete"
    )
