"""Transport byte identity and atomic immutable candidate snapshots."""

import json
from pathlib import Path

from pydantic import TypeAdapter

from jimu_runtime.domain.runtime.candidates import (
    AppendedCandidates,
    CandidateParseLimits,
    CandidateSnapshot,
    CandidateSnapshotLimits,
    DuplicateCandidates,
    LocallyValidCandidate,
    RejectedCandidates,
    ValidatedPrior,
    append_candidates,
    candidate_key,
    validate_candidate_submission,
)
from jimu_runtime.domain.runtime.facts import FactRecord
from jimu_runtime.domain.validation.authority_inputs import FactCandidateSubmission


def fixture() -> dict:
    return json.loads(
        (Path(__file__).parents[1] / "fixtures/wp01b/a2/contracts.json").read_text()
    )


def submission(raw: dict | None = None) -> FactCandidateSubmission:
    raw = (
        raw
        if raw is not None
        else fixture()["question_history"]["candidates"]["entries"][0]["fact"]
    )
    encoded = json.dumps(raw, separators=(",", ":")).encode()
    return FactCandidateSubmission(
        fact=TypeAdapter(FactRecord).validate_json(encoded), raw_bytes=encoded
    )


def budgets() -> dict:
    data = fixture()
    return {
        "parse_limits": CandidateParseLimits.model_validate(data["parse_limits"]),
        "snapshot_limits": CandidateSnapshotLimits.model_validate(
            data["snapshot_limits"]
        ),
    }


def test_parse_keeps_original_bytes_and_never_claims_authority() -> None:
    original = submission()
    outcome = validate_candidate_submission(original, limits=budgets()["parse_limits"])
    assert isinstance(outcome, LocallyValidCandidate)
    assert outcome.submission.raw_bytes == original.raw_bytes
    assert outcome.report.aggregate_status == "deferred"
    assert outcome.report.scope == "authority_admission"


def test_atomic_append_duplicate_and_empty_batch_preserve_history() -> None:
    empty = CandidateSnapshot(entries=())
    first = append_candidates(empty, (submission(),), **budgets())
    assert isinstance(first, AppendedCandidates)
    assert empty.entries == ()
    again = append_candidates(first.snapshot, (submission(),), **budgets())
    assert isinstance(again, DuplicateCandidates)
    assert again.duplicate_count == 1
    assert again.snapshot == first.snapshot
    no_change = append_candidates(first.snapshot, (), **budgets())
    assert isinstance(no_change, DuplicateCandidates)
    assert no_change.duplicate_count == 0
    assert no_change.report.aggregate_status == "deferred"


def test_new_and_duplicate_mixture_appends_only_distinct_candidates() -> None:
    prior = CandidateSnapshot(entries=(submission(),))
    raw = fixture()["question_history"]["candidates"]["entries"][0]["fact"]
    raw.update(fact_id="fact-2", attempt_id="attempt-2")
    result = append_candidates(prior, (submission(), submission(raw)), **budgets())
    assert isinstance(result, AppendedCandidates)
    assert (result.appended_count, result.duplicate_count) == (1, 1)
    assert len(result.snapshot.entries) == 2
    assert len(prior.entries) == 1


def test_equivalent_typed_content_different_bytes_is_conflict() -> None:
    prior = CandidateSnapshot(entries=(submission(),))
    incoming = FactCandidateSubmission(
        fact=prior.entries[0].fact, raw_bytes=b" \n" + prior.entries[0].raw_bytes
    )
    assert isinstance(
        validate_candidate_submission(incoming, limits=budgets()["parse_limits"]),
        LocallyValidCandidate,
    )
    result = append_candidates(prior, (incoming,), **budgets())
    assert isinstance(result, RejectedCandidates)
    assert isinstance(result.prior, ValidatedPrior)
    assert result.prior.snapshot == prior
    assert result.conflicts[0].old_submission.raw_bytes == prior.entries[0].raw_bytes
    assert result.conflicts[0].new_submission.raw_bytes == incoming.raw_bytes


def test_system_requirement_metadata_does_not_create_new_candidate_identity() -> None:
    data = json.loads(
        (Path(__file__).parents[1] / "fixtures/wp01b/a1/contracts.json").read_text()
    )["system_fact"]
    first = submission(data)
    data["source"]["requirement"]["allowed_implementation_versions"].append("2.0.0")
    changed = submission(data)
    assert candidate_key(first.fact) == candidate_key(changed.fact)
    result = append_candidates(
        CandidateSnapshot(entries=(first,)), (changed,), **budgets()
    )
    assert isinstance(result, RejectedCandidates)


def test_braces_and_escaped_quotes_inside_strings_do_not_count_as_depth() -> None:
    data = json.loads(
        (Path(__file__).parents[1] / "fixtures/wp01b/a1/contracts.json").read_text()
    )["unknown"]
    data["reason"]["message"] = '[{"quoted": "\\""}]' * 80
    item = submission(data)
    assert isinstance(
        validate_candidate_submission(item, limits=budgets()["parse_limits"]),
        LocallyValidCandidate,
    )
