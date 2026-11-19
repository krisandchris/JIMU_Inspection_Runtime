"""Finite DTO and typed projections on exact catalogued schema resources."""

import pytest
from fixtures.wp02.schema_views.support import (
    ATTRIBUTE,
    CONTEXT,
    TOOL_INPUT,
    Fixture,
    payload,
    record,
    wrapped,
)

from jimu_runtime.bundle.closure import SchemaIndex
from jimu_runtime.bundle.schema_views import build_schema_descriptors
from jimu_runtime.domain.schema_types import (
    RawDtoSchemaDescriptor,
    ToolInputRecordDescriptor,
    TypedValueSchemaDescriptor,
)
from jimu_runtime.domain.validation.schema_descriptors import describe_field


@pytest.mark.parametrize(
    "schema, expected",
    [
        ({"type": "boolean"}, {"kind": "bool"}),
        (
            {"type": "integer", "minimum": 0, "maximum": 9},
            {"kind": "integer", "minimum": 0, "maximum": 9},
        ),
        ({"type": "number", "minimum": -0.5}, {"kind": "number", "minimum": -0.5}),
        (
            {"type": "string", "maxLength": 9},
            {"kind": "string", "min_length": 0, "max_length": 9},
        ),
        (
            {"type": "string", "enum": ["parked", "moving"]},
            {"kind": "enum", "members": ["parked", "moving"]},
        ),
        (
            {
                "type": "array",
                "minItems": 1,
                "maxItems": 2,
                "items": {"type": "integer"},
            },
            {
                "kind": "list",
                "min_items": 1,
                "max_items": 2,
                "item_type": {"kind": "integer"},
            },
        ),
    ],
)
def test_finite_raw_projection_preserves_bounds_and_identity(schema, expected):
    fixture = Fixture()
    ref = fixture.add(
        ATTRIBUTE,
        "attribute_schema",
        "attribute",
        payload("urn:test:attribute", **schema),
        dialect=True,
    )
    (view,) = build_schema_descriptors(fixture.catalogue())
    assert isinstance(view, RawDtoSchemaDescriptor)
    assert view.schema_ref.model_dump(mode="json") == ref
    assert view.type_definition.model_dump(mode="json", exclude_none=True) == expected


def test_offline_resource_resolution_uses_embedded_id_pointer_and_anchor():
    fixture = Fixture()
    fixture.add(
        ATTRIBUTE,
        "attribute_schema",
        "attribute",
        payload(
            "https://example.test/schemas/attribute",
            type="boolean",
            **{
                "$defs": {
                    "country": {
                        "$id": "region",
                        "$anchor": "name",
                        "type": "string",
                        "enum": ["cn", "uk"],
                    }
                }
            },
        ),
    )
    dto = record(
        {
            "owner": record(
                {
                    "country": {"$ref": "region#name"},
                    "pointer": {"$ref": "attribute#/$defs/country"},
                },
                ["country", "pointer"],
            ),
        },
        [],
    )
    fixture.add(
        CONTEXT,
        "context_schema",
        "context",
        payload("https://example.test/schemas/context", **dto),
    )
    catalogue = fixture.catalogue()
    views = build_schema_descriptors(
        catalogue, schema_index=SchemaIndex.from_catalogue(catalogue)
    )
    context = next(view for view in views if view.schema_ref.artifact_id == "context")
    for name in ("country", "pointer"):
        field = describe_field(context, ("owner", name))
        assert field.wire_path == ("owner", name)
        assert field.optional_ancestor_paths == (("owner",),)
        assert field.type_definition.members == ("cn", "uk")


def test_reuse_index_for_its_exact_catalogue_without_a_reference():
    fixture = Fixture()
    fixture.add(
        ATTRIBUTE,
        "attribute_schema",
        "attribute",
        payload("urn:test:attribute", type="boolean"),
    )
    catalogue = fixture.catalogue()
    index = SchemaIndex.from_catalogue(catalogue)

    assert index.catalogue is catalogue
    assert build_schema_descriptors(
        catalogue, schema_index=index
    ) == build_schema_descriptors(catalogue)


def test_mixed_tool_input_and_whole_typed_record_have_distinct_wire_paths():
    fixture = Fixture()
    typed = wrapped(
        "record",
        record(
            {
                "name": wrapped("enum", {"type": "string", "enum": ["bicycle"]}),
            },
            ["name"],
        ),
    )
    fixture.add(
        ATTRIBUTE,
        "attribute_schema",
        "attribute",
        payload("urn:test:attribute", **typed),
    )
    fixture.add(
        TOOL_INPUT,
        "tool_input_schema",
        "tool-input",
        payload(
            "urn:test:tool-input",
            **record(
                {
                    "score": {"type": "number", "maximum": 1},
                    "target": {"$ref": "urn:test:attribute"},
                },
                ["score"],
            ),
        ),
    )
    whole, mixed = build_schema_descriptors(fixture.catalogue())
    assert isinstance(whole, TypedValueSchemaDescriptor)
    assert isinstance(mixed, ToolInputRecordDescriptor)
    assert describe_field(whole, ("name",)).wire_path == ("value", "name")
    field = describe_field(mixed, ("target", "name"))
    assert field.wire_path == ("target", "value", "name")
    assert field.optional_ancestor_paths == (("target",),)
    assert field.representation == "typed_value"
    assert describe_field(mixed, ("score",)).representation == "raw_dto"


def test_nested_typed_list_keeps_item_representation_and_bounds():
    fixture = Fixture()
    schema = wrapped(
        "list",
        {
            "type": "array",
            "minItems": 1,
            "maxItems": 4,
            "items": wrapped(
                "record",
                record({"active": wrapped("bool", {"type": "boolean"})}, ["active"]),
            ),
        },
    )
    fixture.add(
        ATTRIBUTE,
        "attribute_schema",
        "attribute",
        payload("urn:test:attribute", **schema),
    )
    (descriptor,) = build_schema_descriptors(fixture.catalogue())
    projected = describe_field(descriptor, ()).type_definition
    assert projected.min_items == 1
    assert projected.max_items == 4
    (field,) = projected.item_type.fields
    assert field.name == "active" and field.required
    assert field.type_definition.kind == "bool"
