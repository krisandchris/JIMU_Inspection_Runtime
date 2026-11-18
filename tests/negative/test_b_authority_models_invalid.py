"""Reject malformed local candidate/selection shapes, without admission claims."""

import json
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator
from pydantic import TypeAdapter, ValidationError

from jimu_runtime.domain.runtime.authority import AuthoritativeResultSelection
from jimu_runtime.domain.runtime.facts import FactRecord
from jimu_runtime.domain.validation.authority_inputs import (
    AttemptDescriptor,
    MappedOutputResult,
    NodeRunDescriptor,
    ObservationResultDescriptor,
    QuestionSelectionDescriptor,
    SystemOutputResultDescriptor,
    ToolStepResultDescriptor,
)


def fixture(name: str) -> dict:
    return json.loads(
        (Path(__file__).parents[1] / "fixtures/wp01b/a1/contracts.json").read_text()
    )[name]


def invalid_fact(raw: dict, both_layers: bool = True) -> None:
    adapter = TypeAdapter(FactRecord)
    with pytest.raises(ValidationError):
        adapter.validate_json(json.dumps(raw))
    if both_layers:
        assert not Draft202012Validator(adapter.json_schema()).is_valid(raw)


@pytest.mark.parametrize(
    "status", ["unknown", "error", "not_applicable", "not_evaluated"]
)
@pytest.mark.parametrize("value", [None, False, {"kind": "bool", "value": False}])
def test_non_known_forbids_value_even_null(status: str, value: object) -> None:
    raw = fixture(status)
    raw["value"] = value
    invalid_fact(raw)


@pytest.mark.parametrize(
    "field,value",
    [
        ("reason", None),
        ("reason", {"reason_code": "x", "message": "x", "diagnostic_refs": []}),
        ("value", None),
        ("value", False),
        ("status", "not_detected"),
        ("schema_version", "2.0.0"),
        ("confidence", None),
        ("confidence", True),
        ("confidence", 1),
        ("confidence", float("inf")),
    ],
)
def test_known_is_strict_without_confidence_coercion(field: str, value: object) -> None:
    raw = fixture("known")
    raw[field] = value
    # JSON Schema mathematical numbers intentionally do not enforce Python float
    # representation. A already documents this separate semantic layer.
    invalid_fact(raw, both_layers=field != "confidence" or value is None)


@pytest.mark.parametrize(
    "field",
    [
        "schema_version",
        "fact_id",
        "execution_id",
        "scope",
        "object_id",
        "profile_ref",
        "fact_key",
        "fact_schema_ref",
        "producer_catalogue_ref",
        "producer_declaration_id",
        "producer_kind",
        "producer_id",
        "producer_version",
        "attempt_id",
        "source",
        "evidence_refs",
        "created_at",
        "trace_id",
        "status",
        "value",
    ],
)
def test_every_required_fact_field_is_required(field: str) -> None:
    raw = fixture("known")
    del raw[field]
    invalid_fact(raw)


@pytest.mark.parametrize("name", ["known", "tool_fact", "system_fact"])
def test_source_kind_is_required_and_matches_root_in_both_layers(name: str) -> None:
    raw = fixture(name)
    del raw["source"]["source_kind"]
    invalid_fact(raw)
    raw = fixture(name)
    raw["producer_kind"] = "system" if name != "system_fact" else "tool"
    invalid_fact(raw)


@pytest.mark.parametrize("name", ["known", "tool_fact", "system_fact"])
@pytest.mark.parametrize(
    "field,value", [("producer_id", "foreign"), ("producer_version", "2.0.0")]
)
def test_producer_identity_is_not_attempt_or_implementation_identity(
    name: str, field: str, value: str
) -> None:
    raw = fixture(name)
    raw[field] = value
    invalid_fact(
        raw, both_layers=False
    )  # Cross-field equality is explicit Pydantic validation.


def test_flat_scope_and_object_requirements_in_both_layers() -> None:
    raw = fixture("known")
    raw["scope"] = {"scope": "object", "object_id": "object-1"}
    invalid_fact(raw)
    for field in ("object_id", "profile_ref"):
        raw = fixture("system_fact")
        raw[field] = fixture("known")[field]
        invalid_fact(raw)
        raw[field] = None
        invalid_fact(raw)
    raw = fixture("tool_fact")
    raw["scope"] = "execution"
    del raw["object_id"]
    del raw["profile_ref"]
    invalid_fact(raw)


@pytest.mark.parametrize(
    "name,path",
    [
        ("known", ["source", "question_ref", "artifact_kind"]),
        ("known", ["source", "observation_batch_ref", "record_kind"]),
        ("known", ["source", "model_invocation_ref", "record_kind"]),
        ("tool_fact", ["source", "tool_execution_ref", "record_kind"]),
        ("tool_fact", ["source", "tool_plan_ref", "artifact_kind"]),
        ("tool_fact", ["source", "tool_ref", "artifact_kind"]),
        ("system_fact", ["source", "system_output_ref", "record_kind"]),
        ("known", ["fact_schema_ref", "artifact_kind"]),
        ("known", ["producer_catalogue_ref", "artifact_kind"]),
    ],
)
def test_refs_are_expected_kind_in_schema_and_pydantic(
    name: str, path: list[str]
) -> None:
    raw = fixture(name)
    current = raw
    for part in path[:-1]:
        current = current[part]
    current[path[-1]] = "task" if path[-1] == "record_kind" else "entity"
    invalid_fact(raw)


@pytest.mark.parametrize(
    "name", ["known", "unknown", "error", "not_applicable", "not_evaluated"]
)
def test_copy_injected_extra_cannot_be_dropped_on_revalidation(name: str) -> None:
    adapter = TypeAdapter(FactRecord)
    value = adapter.validate_json(json.dumps(fixture(name)))
    forged = value.model_copy(update={"hidden_authority": True})
    with pytest.raises(ValidationError):
        adapter.validate_python(forged)


def test_error_and_inapplicability_have_structured_evidence() -> None:
    raw = fixture("error")
    raw["reason"]["diagnostics"] = []
    invalid_fact(raw)
    raw = fixture("error")
    raw["reason"]["diagnostics"][0]["path"] = [True]
    invalid_fact(raw)
    for refs in ([], [fixture("not_applicable")["reason"]["basis_refs"][0]] * 2):
        raw = fixture("not_applicable")
        raw["reason"]["basis_refs"] = refs
        invalid_fact(raw)
    raw = fixture("known")
    raw["evidence_refs"] *= 2
    invalid_fact(raw)


@pytest.mark.parametrize(
    "name,wrong",
    [
        ("observation_selection", "fact"),
        ("tool_selection", "observation_batch"),
        ("system_selection", "tool_execution"),
    ],
)
def test_authority_result_kind_is_not_a_generic_ref(name: str, wrong: str) -> None:
    raw = fixture(name)
    raw["selected_result_ref"]["record_kind"] = wrong
    adapter = TypeAdapter(AuthoritativeResultSelection)
    with pytest.raises(ValidationError):
        adapter.validate_json(json.dumps(raw))
    assert not Draft202012Validator(adapter.json_schema()).is_valid(raw)


@pytest.mark.parametrize(
    "change",
    [
        "missing_previous",
        "first_previous",
        "null_previous",
        "wrong_previous",
        "duplicate_facts",
        "actor",
        "actor_extra",
        "revision_zero",
        "tool_execution_scope",
    ],
)
def test_selection_local_constraints(change: str) -> None:
    raw = fixture("tool_selection")
    if change == "missing_previous":
        raw["authority_revision"] = 2
    elif change in ("first_previous", "null_previous", "wrong_previous"):
        raw["expected_previous_selection_ref"] = (
            None
            if change == "null_previous"
            else {
                "record_kind": "fact"
                if change == "wrong_previous"
                else "authoritative_result_selection",
                "record_id": "selection-0",
            }
        )
        if change == "wrong_previous":
            raw["authority_revision"] = 2
    elif change == "duplicate_facts":
        raw["selected_fact_refs"] *= 2
    elif change == "actor":
        raw["actor_ref"]["actor_kind"] = "model"
    elif change == "actor_extra":
        raw["actor_ref"]["credential"] = "forbidden"
    elif change == "revision_zero":
        raw["authority_revision"] = 0
    else:
        raw["scope"] = "execution"
        raw.pop("object_id")
        raw.pop("profile_ref")
    adapter = TypeAdapter(AuthoritativeResultSelection)
    with pytest.raises(ValidationError):
        adapter.validate_json(json.dumps(raw))
    assert not Draft202012Validator(adapter.json_schema()).is_valid(raw)


@pytest.mark.parametrize(
    "name,model,path",
    [
        ("node", NodeRunDescriptor, ["node_run_ref"]),
        ("node_attempt", AttemptDescriptor, ["attempt_ref"]),
        ("node_attempt", AttemptDescriptor, ["owner", "node_run_ref"]),
        ("question_selection", QuestionSelectionDescriptor, ["question_selection_ref"]),
        ("observation_result", ObservationResultDescriptor, ["result_ref"]),
        ("tool_result", ToolStepResultDescriptor, ["result_ref"]),
        ("system_result", SystemOutputResultDescriptor, ["result_ref"]),
    ],
)
def test_descriptor_runtime_ref_kinds(name: str, model: type, path: list[str]) -> None:
    raw = fixture(name)
    current = raw
    for segment in path:
        current = current[segment]
    current["record_kind"] = "decision"
    with pytest.raises(ValidationError):
        model.model_validate(raw)
    assert not Draft202012Validator(model.model_json_schema()).is_valid(raw)


@pytest.mark.parametrize(
    "same_question,same_fact", [(True, False), (False, True), (True, True)]
)
def test_question_and_fact_uniqueness_are_independent(
    same_question: bool, same_fact: bool
) -> None:
    raw = fixture("observation_result")
    second = deepcopy(raw["question_results"][0])
    if not same_question:
        second["question_ref"]["artifact_id"] = "another"
    if not same_fact:
        second["fact_ref"]["record_id"] = "fact-2"
    raw["question_results"].append(second)
    with pytest.raises(ValidationError):
        ObservationResultDescriptor.model_validate(raw)


def test_question_identity_cannot_repeat_with_changed_digest() -> None:
    raw = fixture("question_selection")
    another = {**raw["effective_question_refs"][0], "digest": "b" * 64}
    raw["effective_question_refs"].append(another)
    with pytest.raises(ValidationError):
        QuestionSelectionDescriptor.model_validate(raw)


@pytest.mark.parametrize("field", ["fact_ref", "fact_status", "value"])
def test_optional_absence_is_not_a_fact_or_sixth_status(field: str) -> None:
    raw = fixture("system_result")["output"]
    raw[field] = None
    adapter = TypeAdapter(MappedOutputResult)
    with pytest.raises(ValidationError):
        adapter.validate_python(raw)
    assert not Draft202012Validator(adapter.json_schema()).is_valid(raw)


def test_system_result_name_and_actual_version_are_not_invented() -> None:
    for change in ("name", "version", "missing_version"):
        raw = fixture("system_result")
        if change == "name":
            raw["output"]["output_name"] = "other"
        elif change == "version":
            raw["actual_implementation_version"] = "3.0.0"
        else:
            del raw["actual_implementation_version"]
        with pytest.raises(ValidationError):
            SystemOutputResultDescriptor.model_validate(raw)


def test_no_seventh_node_or_unregistered_scope_model() -> None:
    raw = fixture("node")
    raw["node_id"] = "N07"
    with pytest.raises(ValidationError):
        NodeRunDescriptor.model_validate(raw)
    raw = fixture("node_attempt")
    raw["owner"] = {"owner_kind": "agent", "node_run_ref": raw["owner"]["node_run_ref"]}
    with pytest.raises(ValidationError):
        AttemptDescriptor.model_validate(raw)


@pytest.mark.parametrize(
    "timestamp", ["2026-11-17T12:00:00", "2026-11-17T12:00:00+08:00", None, 123, True]
)
def test_timestamp_decoder_keeps_strict_utc_json_semantics(timestamp: object) -> None:
    raw = fixture("known")
    raw["created_at"] = timestamp
    invalid_fact(raw, both_layers=False)


def test_python_datetime_strings_are_not_silently_coerced() -> None:
    with pytest.raises(ValidationError):
        TypeAdapter(FactRecord).validate_python(fixture("known"))


@pytest.mark.parametrize(
    "created_at",
    [
        datetime(2026, 11, 17, 12),
        datetime(2026, 11, 17, 12, tzinfo=timezone(timedelta(hours=8))),
    ],
)
def test_python_naive_and_non_utc_aware_datetimes_rejected(
    created_at: datetime,
) -> None:
    for name, adapter in (
        ("known", TypeAdapter(FactRecord)),
        ("tool_selection", TypeAdapter(AuthoritativeResultSelection)),
    ):
        raw = fixture(name)
        raw["created_at"] = created_at
        with pytest.raises(ValidationError):
            adapter.validate_python(raw)


def test_nested_copy_tampering_is_revalidated_for_facts_and_selections() -> None:
    adapter = TypeAdapter(FactRecord)
    fact = adapter.validate_json(json.dumps(fixture("known")))
    wrong_question = fact.source.question_ref.model_copy(update={"version": "2.0.0"})
    altered_source = fact.source.model_copy(update={"question_ref": wrong_question})
    with pytest.raises(ValidationError):
        adapter.validate_python(fact.model_copy(update={"source": altered_source}))
    with pytest.raises(ValidationError):
        adapter.validate_python(
            fact.model_copy(
                update={"value": fact.value.model_copy(update={"value": 1})}
            )
        )
    with pytest.raises(ValidationError):
        adapter.validate_python(
            fact.model_copy(update={"created_at": "2026-11-17T12:00:00Z"})
        )
    authority = TypeAdapter(AuthoritativeResultSelection)
    selection = authority.validate_json(json.dumps(fixture("observation_selection")))
    wrong_actor = selection.actor_ref.model_copy(update={"actor_kind": "model"})
    with pytest.raises(ValidationError):
        authority.validate_python(
            selection.model_copy(update={"actor_ref": wrong_actor})
        )


@pytest.mark.parametrize(
    "field", ["arrival_qualification", "envelope_validation", "association_validation"]
)
def test_result_qualification_is_explicit_never_inferred(field: str) -> None:
    raw = fixture("observation_result")
    del raw[field]
    with pytest.raises(ValidationError):
        ObservationResultDescriptor.model_validate(raw)
    assert not Draft202012Validator(
        ObservationResultDescriptor.model_json_schema()
    ).is_valid(raw)


def test_duplicate_tool_outputs_and_mismatched_implementation_evidence_reject() -> None:
    raw = fixture("tool_result")
    raw["mapped_outputs"].append(deepcopy(raw["mapped_outputs"][0]))
    with pytest.raises(ValidationError):
        ToolStepResultDescriptor.model_validate(raw)
    raw = fixture("system_fact")
    raw["source"]["requirement"]["family"] = "tool_implementation"
    invalid_fact(raw)


def test_system_selection_keeps_object_scope_and_explicit_absence_data() -> None:
    raw = fixture("system_selection")
    raw.update(
        scope="object",
        object_id="object-1",
        profile_ref=fixture("known")["profile_ref"],
    )
    adapter = TypeAdapter(AuthoritativeResultSelection)
    value = adapter.validate_json(json.dumps(raw))
    assert value.selected_fact_refs == ()
    assert Draft202012Validator(adapter.json_schema()).is_valid(raw)
