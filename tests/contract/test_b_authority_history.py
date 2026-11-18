"""Local association evidence and explicit full-admission deferral."""

import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator
from pydantic import TypeAdapter

from jimu_runtime.domain.fact_schemas.models import FactSchemaDefinition
from jimu_runtime.domain.fact_schemas.projection import generate_value_schema
from jimu_runtime.domain.fact_schemas.registry import FactSchemaRegistryBuilder
from jimu_runtime.domain.references import ContentRef
from jimu_runtime.domain.runtime.authority import AuthoritativeResultSelection
from jimu_runtime.domain.runtime.candidates import (
    CandidateAppendOutcome,
    CandidateParseLimits,
    CandidateParseOutcome,
    CandidateSnapshot,
    CandidateSnapshotLimits,
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


def registry(data: dict):
    raw = data["fact_schema"]
    root = FactSchemaDefinition.model_validate(raw)
    raw["payload_schema_ref"] = generate_value_schema(
        root.type_definition, root.value_schema_generation
    ).child_ref.model_dump(mode="json")
    definition = FactSchemaDefinition.model_validate(raw)
    ref = data["question_history"]["candidates"]["entries"][0]["fact"][
        "fact_schema_ref"
    ]
    return (
        FactSchemaRegistryBuilder()
        .register(ContentRef.model_validate(ref), definition)
        .freeze()
    )


def history_input(raw: dict) -> LocalAuthorityHistoryInput:
    # This is a nonpersistent Python input carrying actual bytes, not a proposed
    # JSON byte-encoding wire. Each existing A1 JSON root is decoded independently.
    values = dict(raw)
    values["candidates"] = CandidateSnapshot(
        entries=tuple(
            FactCandidateSubmission(
                fact=TypeAdapter(FactRecord).validate_json(json.dumps(entry["fact"])),
                raw_bytes=entry["raw_bytes"].encode("utf-8"),
            )
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


@pytest.mark.parametrize("name", ["question_history", "tool_history", "system_history"])
def test_local_valid_history_has_no_unconditional_authority_pass(name: str) -> None:
    data = fixture()
    inputs = history_input(data[name])
    report = validate_local_authority_history(
        inputs,
        fact_schemas=registry(data),
        parse_limits=CandidateParseLimits.model_validate(data["parse_limits"]),
        snapshot_limits=CandidateSnapshotLimits.model_validate(data["snapshot_limits"]),
    )
    assert report.aggregate_status == "deferred", report
    assert report.scope == "authority_admission"
    local = [check for check in report.checks if check.check_id.startswith("history_")]
    assert all(check.status == "passed" for check in local), report
    assert any(
        check.check_id == "system_output_declaration" and check.status == "deferred"
        for check in report.checks
    )
    assert not hasattr(report, "selected_fact")
    assert not hasattr(report, "authority_view")


def test_empty_input_still_explicitly_defers_full_admission() -> None:
    data = fixture()
    raw = {key: [] for key in data["question_history"]}
    raw["candidates"] = {"entries": []}
    inputs = LocalAuthorityHistoryInput.model_validate(raw)
    report = validate_local_authority_history(
        inputs,
        fact_schemas=registry(data),
        parse_limits=CandidateParseLimits.model_validate(data["parse_limits"]),
        snapshot_limits=CandidateSnapshotLimits.model_validate(data["snapshot_limits"]),
    )
    assert report.aggregate_status == "deferred"
    assert any(
        check.check_id == "full_authority_admission" and check.status == "deferred"
        for check in report.checks
    )


def test_a2_closed_outcome_and_input_schemas() -> None:
    for alias in (CandidateAppendOutcome, CandidateParseOutcome):
        Draft202012Validator.check_schema(TypeAdapter(alias).json_schema())
    schema = LocalAuthorityHistoryInput.model_json_schema()
    Draft202012Validator.check_schema(schema)
    Draft202012Validator(schema).validate(fixture()["question_history"])
