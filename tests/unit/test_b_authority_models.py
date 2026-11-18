"""Precomputed evidence model invariants; no selection algorithm or parser."""

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic import TypeAdapter, ValidationError

from jimu_runtime.domain.definition_enums import FactScope
from jimu_runtime.domain.enums import AttemptStatus, FactStatus
from jimu_runtime.domain.runtime.authority import AuthoritativeResultSelection
from jimu_runtime.domain.runtime.facts import FactRecord
from jimu_runtime.domain.validation.authority_inputs import (
    AttemptDescriptor,
    FactCandidateSubmission,
    ObservationResultDescriptor,
    SystemOutputResultDescriptor,
)


def fixture(name: str) -> dict:
    return json.loads(
        (Path(__file__).parents[1] / "fixtures/wp01b/a1/contracts.json").read_text()
    )[name]


def test_known_false_and_model_confidence_are_preserved_without_scale() -> None:
    raw = fixture("known")
    raw["confidence"] = -2.5
    fact = TypeAdapter(FactRecord).validate_json(json.dumps(raw))
    assert fact.value.value is False
    assert fact.confidence == -2.5
    assert fact.status is FactStatus.KNOWN


def test_system_can_be_object_scoped_without_second_scope_object() -> None:
    raw = fixture("system_fact")
    raw.update(
        scope="object",
        object_id="object-1",
        profile_ref=fixture("known")["profile_ref"],
    )
    value = TypeAdapter(FactRecord).validate_json(json.dumps(raw))
    assert value.scope is FactScope.OBJECT
    result = fixture("system_result")
    result.update(scope="object", object_id="object-1", profile_ref=raw["profile_ref"])
    assert SystemOutputResultDescriptor.model_validate(result).scope is FactScope.OBJECT


def test_failed_and_late_candidates_remain_recordable_data() -> None:
    raw = fixture("node_attempt")
    raw["status"] = "failed"
    assert AttemptDescriptor.model_validate(raw).status is AttemptStatus.FAILED
    result = fixture("observation_result")
    result["arrival_qualification"] = "late"
    assert (
        ObservationResultDescriptor.model_validate(result).arrival_qualification
        == "late"
    )
    # Models preserve evidence. They do not confer authority eligibility.


def test_selection_revision_has_explicit_predecessor_not_latest_write() -> None:
    raw = fixture("observation_selection")
    raw.update(
        selection_id="selection-2",
        authority_revision=2,
        expected_previous_selection_ref={
            "record_kind": "authoritative_result_selection",
            "record_id": "selection-1",
        },
    )
    value = TypeAdapter(AuthoritativeResultSelection).validate_json(json.dumps(raw))
    assert value.expected_previous_selection_ref.record_id == "selection-1"
    assert value.authority_revision == 2


def test_submission_preserves_bytes_without_claiming_to_parse_them() -> None:
    fact = TypeAdapter(FactRecord).validate_json(json.dumps(fixture("known")))
    original = b'{"not":"parsed in models-first"}'
    candidate = FactCandidateSubmission(fact=fact, raw_bytes=original)
    assert candidate.raw_bytes == original
    assert candidate.fact == fact


def test_candidate_and_descriptors_are_deeply_immutable() -> None:
    raw = fixture("observation_result")
    value = ObservationResultDescriptor.model_validate(raw)
    raw["question_results"].clear()
    assert len(value.question_results) == 1
    assert isinstance(value.question_results, tuple)
    with pytest.raises(ValidationError):
        value.question_results[0].fact_ref.record_id = "rewritten"


def test_strict_python_utc_datetime_is_accepted_without_string_coercion() -> None:
    raw = fixture("known")
    raw["created_at"] = datetime(2026, 11, 17, 12, tzinfo=UTC)
    value = TypeAdapter(FactRecord).validate_python(raw)
    assert value.created_at is raw["created_at"]
