"""Independent executable wire-schema and finite view checks."""

import pytest
from fixtures.wp02.schema_views.support import tool_fixture, tool_result
from jsonschema import Draft202012Validator
from pydantic import TypeAdapter

from jimu_runtime.bundle.schema_views import build_schema_descriptors
from jimu_runtime.domain.enums import ArtifactKind
from jimu_runtime.domain.fact_schemas.registry import FactSchemaRegistryBuilder
from jimu_runtime.domain.runtime.tool_executions import ToolResult
from jimu_runtime.domain.schema_types import (
    ToolResultSchemaDescriptor,
    TypedValueSchemaDescriptor,
)
from jimu_runtime.domain.validation.schema_descriptors import describe_field
from jimu_runtime.domain.validation.tool_plans import validate_tool_definition
from jimu_runtime.domain.values import validate_typed_value


def test_tool_output_and_f04_reconstruct_exact_typed_views():
    catalogue = tool_fixture().catalogue()
    descriptors = build_schema_descriptors(catalogue)
    result = next(d for d in descriptors if isinstance(d, ToolResultSchemaDescriptor))
    fact = next(d for d in descriptors if isinstance(d, TypedValueSchemaDescriptor))
    assert (result.schema_ref.artifact_id, fact.schema_ref.artifact_kind.value) == (
        "tool-result",
        "value_schema",
    )
    assert [(s.name, s.required, s.multiple) for s in result.evidence_fields] == [
        ("primary", True, False),
        ("supporting", False, True),
    ]
    (output,) = result.output_declarations
    assert output.output_name == "parked" and output.required
    field = describe_field(result, output.value_path)
    assert field.type_definition == fact.type_definition
    assert field.wire_path == ("value", "parked")
    assert field.optional_ancestor_paths == ((), ("parked",))


def test_derived_view_passes_wp01_tool_definition_checks():
    catalogue = tool_fixture().catalogue()
    descriptors = build_schema_descriptors(catalogue)
    (fact,) = catalogue.of_kind(ArtifactKind.FACT_SCHEMA)
    (tool,) = catalogue.of_kind(ArtifactKind.TOOL)
    registry = FactSchemaRegistryBuilder().register(fact.ref, fact.value).freeze()
    report = validate_tool_definition(tool.value, descriptors, registry)
    assert {check.check_id for check in report.checks if check.status == "passed"} >= {
        "tool.output_set",
        "tool.output.0",
        "tool.io",
    }
    assert {check.check_id for check in report.checks if check.status != "passed"} == {
        "tool.external_admission",
    }


def ref(record_id):
    return {"record_kind": "evidence", "record_id": record_id}


def valid_result():
    return {
        "status": "succeeded",
        "diagnostics": [],
        "evidence_refs": [ref("one"), ref("two"), ref("three")],
        "evidence": [
            {"name": "primary", "evidence_refs": [ref("one")]},
            {"name": "supporting", "evidence_refs": [ref("two"), ref("three")]},
        ],
        "output_states": [
            {
                "output_name": "parked",
                "fact_status": "known",
                "evidence_refs": [ref("one")],
            }
        ],
        "payload": {
            "kind": "record",
            "value": {"parked": {"kind": "bool", "value": False}},
        },
    }


def test_complete_normalized_tool_result_accepts_real_named_slots_and_payload():
    candidate = valid_result()
    schema = Draft202012Validator(tool_result())
    schema.validate(candidate)
    parsed = TypeAdapter(ToolResult).validate_python(candidate)
    result = next(
        view
        for view in build_schema_descriptors(tool_fixture().catalogue())
        if isinstance(view, ToolResultSchemaDescriptor)
    )
    validate_typed_value(result.payload_type, parsed.payload)
    candidate["evidence"].pop()
    schema.validate(candidate)


@pytest.mark.parametrize(
    "change", ["missing", "duplicate", "multiple", "empty", "unknown", "payload"]
)
def test_specialized_tool_result_rejects_invalid_slot_and_payload_data(change):
    candidate = valid_result()
    slots = candidate["evidence"]
    if change == "missing":
        slots.pop(0)
    elif change == "duplicate":
        slots.append({"name": "primary", "evidence_refs": [ref("two")]})
    elif change == "multiple":
        slots[0]["evidence_refs"].append(ref("two"))
    elif change == "empty":
        slots[1]["evidence_refs"].clear()
    elif change == "unknown":
        slots[0]["name"] = "external"
    else:
        candidate["payload"]["value"]["parked"] = {"kind": "integer", "value": 0}
    assert not Draft202012Validator(tool_result()).is_valid(candidate)
