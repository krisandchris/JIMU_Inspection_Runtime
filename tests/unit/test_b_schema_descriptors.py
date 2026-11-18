"""Finite path/containment evidence; no Tool execution or full Bundle validation."""

import json
from pathlib import Path
from typing import cast

import pytest
from pydantic import TypeAdapter

from jimu_runtime.domain.schema_types import SchemaDescriptor
from jimu_runtime.domain.tool_mappings import InputBinding
from jimu_runtime.domain.validation.schema_descriptors import (
    SchemaCheckError,
    describe_field,
    require_mapping_compatibility,
    require_type_containment,
    resolve_descriptor,
    validate_descriptor,
    validate_target_bindings,
)
from jimu_runtime.domain.values import TypeDefinition


def fixture() -> dict[str, object]:
    return cast(
        dict[str, object],
        json.loads(
            (Path(__file__).parents[1] / "fixtures/wp01b/s2/contracts.json").read_text()
        ),
    )


def descriptor(name: str) -> SchemaDescriptor:
    return TypeAdapter(SchemaDescriptor).validate_python(fixture()[name])


def definition(data: object) -> TypeDefinition:
    return TypeAdapter(TypeDefinition).validate_python(data)


def test_paths_preserve_optional_parents_and_representation() -> None:
    result = describe_field(descriptor("raw"), ("outer", "leaf"))
    assert result.optional_ancestor_paths == (("outer",),)
    assert result.wire_path == ("outer", "leaf")
    assert result.representation == "raw_dto"
    typed = describe_field(descriptor("tool"), ("state",))
    assert typed.optional_ancestor_paths == ((), ("state",))
    assert typed.wire_path == ("value", "state")
    assert typed.representation == "typed_value"
    assert describe_field(descriptor("typed"), ()).wire_path == ()


def test_mixed_input_accepts_image_and_full_typed_fact_without_conversion() -> None:
    mixed = descriptor("mixed")
    image_target = describe_field(mixed, ("image",))
    state_target = describe_field(mixed, ("state",))
    assert image_target.representation == "raw_dto"
    assert state_target.representation == "typed_value"
    assert state_target.wire_path == ("state",)
    bindings = TypeAdapter(tuple[InputBinding, ...]).validate_python(
        fixture()["bindings"]
    )
    validate_target_bindings(mixed, bindings)
    require_mapping_compatibility(
        describe_field(descriptor("typed"), ()), state_target, bindings[1]
    )
    with pytest.raises(SchemaCheckError) as error:
        require_mapping_compatibility(image_target, state_target, bindings[1])
    assert error.value.issue.code == "representation_mismatch"


def test_mixed_nested_typed_path_has_raw_root_then_wrappers() -> None:
    data = cast(dict[str, object], fixture()["mixed"])
    fields = cast(list[dict[str, object]], data["fields"])
    fields[1]["type_definition"] = {
        "kind": "record",
        "fields": [
            {"name": "leaf", "required": False, "type_definition": {"kind": "bool"}}
        ],
    }
    mixed = TypeAdapter(SchemaDescriptor).validate_python(data)
    result = describe_field(mixed, ("state", "leaf"))
    assert result.wire_path == ("state", "value", "leaf")
    assert result.optional_ancestor_paths == (("state", "leaf"),)


@pytest.mark.parametrize(
    "name,path,code",
    [
        ("mixed", (), "mixed_whole_input_forbidden"),
        ("raw", ("outer", "missing"), "field_unknown"),
        ("raw", ("outer", "leaf", "value"), "path_not_record"),
        ("raw", ("outer.leaf",), "field_unknown"),
        ("raw", ("*",), "field_unknown"),
    ],
)
def test_unknown_and_non_record_paths_fail(
    name: str, path: tuple[str, ...], code: str
) -> None:
    with pytest.raises(SchemaCheckError) as error:
        describe_field(descriptor(name), path)
    assert error.value.issue.code == code
    assert error.value.issue.outcome == "invalid"


@pytest.mark.parametrize(
    "source,target,code",
    [
        ({"kind": "integer"}, {"kind": "number"}, "kind_mismatch"),
        ({"kind": "bool"}, {"kind": "integer"}, "kind_mismatch"),
        ({"kind": "integer"}, {"kind": "integer", "minimum": 0}, "range_not_contained"),
        (
            {"kind": "number", "maximum": 5.0},
            {"kind": "number", "maximum": 4.0},
            "range_not_contained",
        ),
        (
            {"kind": "string", "max_length": 20},
            {"kind": "string", "max_length": 10},
            "length_not_contained",
        ),
        (
            {"kind": "enum", "members": ["a", "b"]},
            {"kind": "enum", "members": ["a"]},
            "enum_not_contained",
        ),
        (
            {"kind": "list", "item_type": {"kind": "bool"}, "max_items": 20},
            {"kind": "list", "item_type": {"kind": "bool"}, "max_items": 10},
            "length_not_contained",
        ),
        (
            {"kind": "list", "item_type": {"kind": "string"}},
            {"kind": "list", "item_type": {"kind": "integer"}},
            "kind_mismatch",
        ),
    ],
)
def test_recursive_containment_rejects_wider_or_different_types(
    source: object, target: object, code: str
) -> None:
    with pytest.raises(SchemaCheckError) as error:
        require_type_containment(definition(source), definition(target))
    assert error.value.issue.code == code


@pytest.mark.parametrize(
    "source,target",
    [
        ({"kind": "bool"}, {"kind": "bool"}),
        (
            {"kind": "integer", "minimum": 2, "maximum": 5},
            {"kind": "integer", "minimum": 0},
        ),
        ({"kind": "number", "minimum": 2.0, "maximum": 5.0}, {"kind": "number"}),
        ({"kind": "enum", "members": ["a"]}, {"kind": "enum", "members": ["a", "b"]}),
        ({"kind": "string", "min_length": 2, "max_length": 5}, {"kind": "string"}),
        (
            {
                "kind": "list",
                "item_type": {"kind": "integer", "minimum": 2},
                "min_items": 1,
                "max_items": 2,
            },
            {"kind": "list", "item_type": {"kind": "integer"}},
        ),
    ],
)
def test_containment_accepts_provable_subset(source: object, target: object) -> None:
    require_type_containment(definition(source), definition(target))


def record(name: str, required: bool) -> dict[str, object]:
    return {
        "kind": "record",
        "fields": [
            {"name": name, "required": required, "type_definition": {"kind": "bool"}}
        ],
    }


def test_whole_records_check_extras_and_internal_requiredness() -> None:
    for source, target, code in [
        (record("a", False), record("a", True), "optional_source_member"),
        (record("extra", False), record("a", False), "extra_source_field"),
        ({"kind": "record", "fields": []}, record("a", True), "required_target_member"),
    ]:
        with pytest.raises(SchemaCheckError) as error:
            require_type_containment(definition(source), definition(target))
        assert error.value.issue.code == code
    require_type_containment(
        definition(record("a", True)), definition(record("a", False))
    )


def test_result_output_paths_must_be_omittable_and_unknown_descriptors_incomplete() -> (
    None
):
    tool = descriptor("tool")
    validate_descriptor(tool)
    data = cast(dict[str, object], fixture()["tool"])
    payload = cast(dict[str, object], data["payload_type"])
    cast(list[dict[str, object]], payload["fields"])[0]["required"] = True
    invalid = TypeAdapter(SchemaDescriptor).validate_python(data)
    with pytest.raises(SchemaCheckError) as error:
        validate_descriptor(invalid)
    assert error.value.issue.code == "nonknown_payload_path_not_omittable"
    with pytest.raises(SchemaCheckError) as missing:
        resolve_descriptor(tool.schema_ref, ())
    assert missing.value.issue.outcome == "incomplete"
    with pytest.raises(SchemaCheckError) as conflict:
        resolve_descriptor(tool.schema_ref, (tool, tool))
    assert conflict.value.issue.code == "descriptor_conflict"


def test_optional_parent_construction_requires_all_required_children() -> None:
    data = cast(dict[str, object], fixture()["mixed"])
    fields = cast(list[dict[str, object]], data["fields"])
    fields[0]["required"] = False
    fields[0]["type_definition"] = {
        "kind": "record",
        "fields": [
            {"name": "a", "required": True, "type_definition": {"kind": "bool"}},
            {"name": "b", "required": True, "type_definition": {"kind": "bool"}},
        ],
    }
    mixed = TypeAdapter(SchemaDescriptor).validate_python(data)
    bindings = TypeAdapter(tuple[InputBinding, ...]).validate_python(
        fixture()["bindings"]
    )
    validate_target_bindings(mixed, (bindings[1],))  # optional parent never constructed
    raw_binding = cast(list[dict[str, object]], fixture()["bindings"])[0]
    raw_binding["target"] = {
        "schema_ref": mixed.schema_ref.model_dump(mode="json"),
        "field_path": ["image", "a"],
    }
    raw_binding["required"] = False
    partial = InputBinding.model_validate(raw_binding)
    with pytest.raises(SchemaCheckError) as error:
        validate_target_bindings(mixed, (partial, bindings[1]))
    assert error.value.issue.code == "required_target_unmapped"
    assert error.value.issue.field_path == ("image", "b")


def test_optional_source_to_required_target_blocks_without_default() -> None:
    source = describe_field(descriptor("tool"), ("state",))
    target = describe_field(descriptor("mixed"), ("state",))
    bindings = TypeAdapter(tuple[InputBinding, ...]).validate_python(
        fixture()["bindings"]
    )
    assert source.optional_ancestor_paths
    assert bindings[1].on_missing == "block"
    require_mapping_compatibility(source, target, bindings[1])


def test_deep_payload_missing_root_and_eight_optional_fields_are_preserved() -> None:
    node: dict[str, object] = {"kind": "bool"}
    for _ in range(8):
        node = {
            "kind": "record",
            "fields": [{"name": "x", "required": False, "type_definition": node}],
        }
    data = cast(dict[str, object], fixture()["tool"])
    data["payload_type"] = node
    data["output_declarations"] = []
    result = describe_field(
        TypeAdapter(SchemaDescriptor).validate_python(data), ("x",) * 8
    )
    assert len(result.optional_ancestor_paths) == 9
    assert result.optional_ancestor_paths[0] == ()
    assert result.wire_path == ("value", "x") * 8


def test_wrong_digest_is_invalid_not_missing_catalogue() -> None:
    from jimu_runtime.domain.references import SchemaRef

    known = descriptor("typed")
    raw = known.schema_ref.model_dump(mode="json")
    raw["digest"] = "b" * 64
    requested = SchemaRef.model_validate(raw)
    with pytest.raises(SchemaCheckError) as error:
        resolve_descriptor(requested, (known,))
    assert error.value.issue.outcome == "invalid"
    assert error.value.issue.code == "schema_digest_mismatch"


def test_diagnostic_prefix_boundary_does_not_expand_selector_paths() -> None:
    source = definition({"kind": "bool"})
    target = definition({"kind": "integer"})
    with pytest.raises(SchemaCheckError) as error:
        require_type_containment(source, target, ("owner",) * 24)
    assert error.value.issue.code == "kind_mismatch"
    assert len(error.value.issue.field_path) == 24
    for prefix in [("x",) * 25, ("x",) * 33, (False,), ("",)]:
        with pytest.raises(SchemaCheckError) as error:
            require_type_containment(source, target, prefix)
        assert error.value.issue.code == "diagnostic_prefix_invalid"
    nested_source: dict[str, object] = {"kind": "bool"}
    nested_target: dict[str, object] = {"kind": "integer"}
    for _ in range(8):
        nested_source = record("x", True) | {
            "fields": [
                {"name": "x", "required": True, "type_definition": nested_source}
            ]
        }
        nested_target = record("x", True) | {
            "fields": [
                {"name": "x", "required": True, "type_definition": nested_target}
            ]
        }
    with pytest.raises(SchemaCheckError) as error:
        require_type_containment(
            definition(nested_source), definition(nested_target), ("p",) * 24
        )
    assert error.value.issue.code == "kind_mismatch"
    assert len(error.value.issue.field_path) == 32


@pytest.mark.parametrize(
    "name,path,required", [("raw", ("outer", "leaf"), False), ("typed", (), True)]
)
def test_non_tool_target_coverage_does_not_forge_schema_kind(
    name: str, path: tuple[str, ...], required: bool
) -> None:
    from jimu_runtime.domain.validation.schema_descriptors import (
        validate_target_coverage,
    )

    target = descriptor(name)
    raw = cast(list[dict[str, object]], fixture()["bindings"])[0]
    raw["target"] = {
        "schema_ref": target.schema_ref.model_dump(mode="json"),
        "field_path": path,
    }
    raw["required"] = required
    binding = InputBinding.model_validate(raw)
    validate_target_coverage(target, (binding,))
    with pytest.raises(SchemaCheckError) as error:
        validate_target_bindings(target, (binding,))
    assert error.value.issue.code == "target_schema_kind"


def test_generic_target_coverage_rejects_tool_result_envelope() -> None:
    from jimu_runtime.domain.validation.schema_descriptors import (
        validate_target_coverage,
    )

    with pytest.raises(SchemaCheckError) as error:
        validate_target_coverage(descriptor("tool"), ())
    assert error.value.issue.code == "target_schema_kind"
