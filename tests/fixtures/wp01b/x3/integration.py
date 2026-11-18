"""Local association evidence and explicit full-admission deferral."""

import json
from pathlib import Path

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
)
from jimu_runtime.domain.runtime.facts import FactRecord
from jimu_runtime.domain.validation.authority import (
    LocalAuthorityHistoryInput,
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
from jimu_runtime.domain.validation.producer_authority import (
    validate_producer_authority_contracts,
)
from jimu_runtime.domain.validation.producer_authority_inputs import (
    ExecutionDefinitionPin,
    ProducerAuthorityValidationInput,
)
from jimu_runtime.domain.validation.producer_inputs import ProducerCatalogueContext
from jimu_runtime.domain.validation.tool_plans import ToolRegistration


def data():
    return json.loads(Path(__file__).with_name("composition.json").read_text())


def fixture(name="question"):
    return data()["cases"][name]


def parse(raw):
    return ProducerAuthorityValidationInput.model_validate(
        {
            "history": history_input(raw["history"]),
            "producer_catalogues": tuple(
                ProducerCatalogueContext.model_validate_json(json.dumps(item))
                for item in raw["producer_catalogues"]
            ),
            "execution_pins": tuple(
                ExecutionDefinitionPin.model_validate_json(json.dumps(item))
                for item in raw["execution_pins"]
            ),
        }
    )


def registry():
    raw = data()["fact_schema"]
    initial = FactSchemaDefinition.model_validate(raw)
    raw["payload_schema_ref"] = generate_value_schema(
        initial.type_definition, initial.value_schema_generation
    ).child_ref.model_dump(mode="json")
    builder = FactSchemaRegistryBuilder()
    for schema_id, scope in (("fixture", "object"), ("execution-schema", "execution")):
        raw.update(
            schema_id=schema_id,
            scope=scope,
            allowed_producer_kinds=["system"]
            if scope == "execution"
            else ["vlm_question", "tool", "system"],
        )
        ref = {
            "artifact_kind": "fact_schema",
            "artifact_id": schema_id,
            "version": "1.0.0",
            "digest": "a" * 64,
        }
        builder.register(
            ContentRef.model_validate(ref), FactSchemaDefinition.model_validate(raw)
        )
    return builder.freeze()


def check(raw):
    d = data()
    return validate_producer_authority_contracts(
        parse(raw),
        fact_schemas=registry(),
        parse_limits=CandidateParseLimits.model_validate(d["parse_limits"]),
        snapshot_limits=CandidateSnapshotLimits.model_validate(d["snapshot_limits"]),
    )


def codes(report):
    return {issue.code for check in report.checks for issue in check.issues}


def sync_fact(raw):
    for entry in raw["history"]["candidates"]["entries"]:
        entry["raw_bytes"] = json.dumps(entry["fact"], separators=(",", ":"))


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
