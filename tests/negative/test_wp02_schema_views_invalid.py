"""Reject unproven assertions, sidecars and schema/owner drift."""

import json

import pytest
from fixtures.wp02.schema_views.support import (
    ATTRIBUTE,
    CHILD,
    CONTEXT,
    FACT_OUTPUT,
    ROOT,
    TOOL_OUTPUT,
    Fixture,
    fact_child,
    payload,
    record,
    tool_fixture,
    tool_result,
    wrapped,
)

from jimu_runtime.bundle.artifacts import BundleArtifactError
from jimu_runtime.bundle.closure import SchemaIndex
from jimu_runtime.bundle.schema_views import build_schema_descriptors


@pytest.mark.parametrize(
    "body, code",
    [
        ({"type": "string"}, "schema_explicit_bound_missing"),
        (
            {"type": "string", "maxLength": 3, "pattern": "x"},
            "schema_assertion_unsupported",
        ),
        (
            {
                "type": "array",
                "maxItems": 2,
                "items": {"type": "boolean"},
                "uniqueItems": True,
            },
            "schema_assertion_unsupported",
        ),
        (
            {"type": "object", "properties": {"answer": {"type": "boolean"}}},
            "schema_record_unsupported",
        ),
        (
            {"type": "integer", "x-jimu-descriptor": {"kind": "string"}},
            "schema_assertion_unsupported",
        ),
        ({"$ref": "https://example.test/missing"}, "schema_resource_missing"),
        ({"$ref": "#"}, "schema_view_cycle"),
        (
            {"$ref": "#/$defs/x", "maximum": 3, "$defs": {"x": {"type": "integer"}}},
            "schema_ref_siblings_unsupported",
        ),
        (
            wrapped("enum", {"type": "string", "maxLength": 9}),
            "schema_explicit_bound_missing",
        ),
        (wrapped("bool", {"type": "integer"}), "schema_typed_payload"),
    ],
)
def test_unrepresentable_or_unproven_projection_has_named_diagnostic(body, code):
    fixture = Fixture()
    fixture.add(
        ATTRIBUTE,
        "attribute_schema",
        "attribute",
        payload("urn:test:attribute", **body),
    )
    with pytest.raises(BundleArtifactError) as error:
        build_schema_descriptors(fixture.catalogue())
    assert error.value.code == code
    assert error.value.path == ATTRIBUTE


def test_projection_does_not_take_local_alias_target_from_a_different_catalogue():
    fixture = Fixture()
    fixture.add(
        CONTEXT,
        "context_schema",
        "context",
        payload("urn:test:context", **{"$ref": "urn:test:attribute"}),
    )
    fixture.add(
        ATTRIBUTE,
        "attribute_schema",
        "attribute",
        payload("urn:test:attribute", type="boolean"),
    )
    wrong_index = SchemaIndex.from_catalogue(fixture.catalogue())
    with pytest.raises(BundleArtifactError, match="schema_reference_index_mismatch"):
        build_schema_descriptors(fixture.catalogue(), schema_index=wrong_index)


def test_different_catalogue_index_is_rejected_without_a_reference():
    fixture = Fixture()
    fixture.add(
        ATTRIBUTE,
        "attribute_schema",
        "attribute",
        payload("urn:test:attribute", type="boolean"),
    )
    catalogue = fixture.catalogue()
    different_catalogue = fixture.catalogue()
    assert catalogue == different_catalogue
    assert catalogue is not different_catalogue

    with pytest.raises(BundleArtifactError, match="schema_reference_index_mismatch"):
        build_schema_descriptors(
            catalogue, schema_index=SchemaIndex.from_catalogue(different_catalogue)
        )


def test_f04_generation_rejects_self_consistent_but_changed_child_and_reference():
    fixture = Fixture()
    fact, child = fact_child()
    child["properties"]["value"]["type"] = "integer"
    fact["payload_schema_ref"] = fixture.add(
        CHILD, "value_schema", fact["payload_schema_ref"]["artifact_id"], child
    )
    fixture.add(FACT_OUTPUT, "fact_schema", "parked", fact)
    with pytest.raises(BundleArtifactError, match="child_reference_mismatch") as error:
        build_schema_descriptors(fixture.catalogue())
    assert error.value.json_location == ("payload_schema_ref",)


def test_standalone_value_schema_requires_a_pinned_f04_source():
    fixture = Fixture()
    fact, child = fact_child()
    fixture.add(CHILD, "value_schema", fact["payload_schema_ref"]["artifact_id"], child)
    with pytest.raises(BundleArtifactError, match="value_schema_source_unavailable"):
        build_schema_descriptors(fixture.catalogue())


@pytest.mark.parametrize(
    "mutation, code",
    [
        ("generic", "tool_evidence_contract_unproven"),
        ("partial_requires_slot", "tool_evidence_contract_mismatch"),
        ("failed_payload", "tool_result_envelope_mismatch"),
        ("partial_payload_drift", "tool_result_payload_conflict"),
        ("shadowed_ref_base", "tool_evidence_contract_mismatch"),
        ("broadened_evidence_ref", "tool_result_envelope_mismatch"),
    ],
)
def test_tool_result_schema_and_declarations_detect_semantic_drift(mutation, code):
    output = tool_result()
    definitions = output["$defs"]
    succeeded = definitions["SucceededToolResult"]["properties"]
    partial = definitions["PartialToolResult"]["properties"]
    if mutation == "generic":
        generic = json.loads(
            (
                ROOT
                / "schemas/generated/runtime.tool_executions.ToolResult.schema.json"
            ).read_text()
        )
        succeeded["evidence"] = generic["$defs"]["SucceededToolResult"]["properties"][
            "evidence"
        ]
    elif mutation == "partial_requires_slot":
        partial["evidence"]["allOf"][0]["minContains"] = 1
    elif mutation == "failed_payload":
        definitions["FailedToolResult"]["properties"]["payload"] = {"type": "boolean"}
    elif mutation == "partial_payload_drift":
        partial["payload"] = wrapped("record", record({}, []))
    elif mutation == "shadowed_ref_base":
        succeeded["evidence"]["$id"] = "urn:altered:schema:base"
    else:
        definitions["EvidenceRefs"]["uniqueItems"] = False
    with pytest.raises(BundleArtifactError) as error:
        build_schema_descriptors(tool_fixture(output).catalogue())
    assert error.value.code == code
    assert error.value.path == TOOL_OUTPUT


def test_named_tool_evidence_path_does_not_create_a_slot():
    with pytest.raises(BundleArtifactError, match="tool_evidence_path_unproven"):
        build_schema_descriptors(tool_fixture(evidence_name="absent").catalogue())


def test_same_schema_uri_across_different_pins_is_ambiguous():
    fixture = Fixture()
    fixture.add(
        ATTRIBUTE,
        "attribute_schema",
        "attribute",
        payload("urn:test:schema:same", type="boolean"),
    )
    fixture.add(
        CONTEXT,
        "context_schema",
        "context",
        payload("urn:test:schema:same", type="integer"),
    )
    with pytest.raises(BundleArtifactError, match="schema_id_ambiguous"):
        build_schema_descriptors(fixture.catalogue())


def test_required_named_payload_path_cannot_represent_nonknown_output():
    output = tool_result()
    output["$defs"]["SpecificPayload"]["properties"]["value"]["required"] = ["parked"]
    with pytest.raises(
        BundleArtifactError, match="nonknown_payload_path_not_omittable"
    ):
        build_schema_descriptors(tool_fixture(output).catalogue())
