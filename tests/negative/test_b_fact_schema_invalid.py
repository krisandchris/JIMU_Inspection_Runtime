"""Projection precision/limits, tampered models and strict metadata rejections."""

import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator
from pydantic import ValidationError

from jimu_runtime.domain.fact_schemas.canonicalization import canonical_bytes
from jimu_runtime.domain.fact_schemas.models import (
    FactSchemaDefinition,
    GlobalTypeLimits,
    ProjectionValidationError,
    ValueSchemaGenerationInput,
    ValueSchemaGenerationSpec,
)
from jimu_runtime.domain.fact_schemas.projection import (
    generate_value_schema,
    normalize_type,
    validate_child_reference,
)
from jimu_runtime.domain.values import (
    IntegerTypeDefinition,
    StringTypeDefinition,
    validate_typed_value,
)


def data() -> dict:
    return json.loads(
        (Path(__file__).parents[1] / "fixtures/wp01b/jcs_vectors.json").read_text()
    )


def spec() -> ValueSchemaGenerationSpec:
    return ValueSchemaGenerationSpec.model_validate(data()["spec"])


def root() -> dict:
    raw = data()
    return {**raw["fact_schema"], "value_schema_generation": raw["spec"]}


@pytest.mark.parametrize("integer", [2**53, 2**53 + 1, 2**60, 10**1000, -(2**53)])
def test_library_integer_support_domain_does_not_change_a(integer: int) -> None:
    original = IntegerTypeDefinition(kind="integer", minimum=integer)
    assert original.minimum == integer
    with pytest.raises(ProjectionValidationError) as caught:
        generate_value_schema(original, spec())
    assert caught.value.code == "canonicalizer_integer_domain"
    assert caught.value.path == ("minimum",)


@pytest.mark.parametrize("integer", [2**53 - 1, -(2**53 - 1)])
def test_library_safe_integer_boundaries(integer: int) -> None:
    assert generate_value_schema({"kind": "integer", "minimum": integer}, spec())


@pytest.mark.parametrize(
    "tree",
    [
        {"kind": "bool", "extra": True},
        {"kind": "enum", "members": ["a", "a"]},
        {"kind": "enum", "members": ["a", 1]},
        {"kind": "integer", "minimum": True},
        {"kind": "number", "minimum": float("nan")},
        {"kind": "number", "maximum": float("inf")},
        {"kind": "string", "min_length": 10, "max_length": 2},
        {
            "kind": "record",
            "fields": [
                {"name": "x", "required": True, "type_definition": {"kind": "bool"}},
                {"name": "x", "required": False, "type_definition": {"kind": "bool"}},
            ],
        },
        {"kind": "schema_ref", "ref": "remote"},
    ],
)
def test_invalid_type_inputs(tree: object) -> None:
    with pytest.raises((ValidationError, ProjectionValidationError)):
        generate_value_schema(tree, spec())


@pytest.mark.parametrize(
    "tree",
    [
        {"kind": "enum", "members": ["\ud800"]},
        {
            "kind": "record",
            "fields": [
                {
                    "name": "\ud800",
                    "required": True,
                    "type_definition": {"kind": "bool"},
                }
            ],
        },
        {"kind": "bool", "\ud800": "x"},
    ],
)
def test_surrogates_return_typed_path_errors(tree: object) -> None:
    with pytest.raises(ProjectionValidationError) as caught:
        generate_value_schema(tree, spec())
    assert caught.value.code == "invalid_unicode"
    assert caught.value.path


def test_cycle_and_excess_depth_fail_boundedly() -> None:
    cycle = {"kind": "list"}
    cycle["item_type"] = cycle
    with pytest.raises(ProjectionValidationError, match="cyclic_type"):
        generate_value_schema(cycle, spec())
    tree: object = {"kind": "bool"}
    for _ in range(9):
        tree = {"kind": "list", "item_type": tree}
    with pytest.raises(ProjectionValidationError, match="type_limit"):
        generate_value_schema(tree, spec())


def test_root_and_input_cannot_skip_normalization_or_revalidation() -> None:
    small_spec = spec().model_copy(
        update={
            "global_limits": GlobalTypeLimits(
                string_max_length=100,
                list_max_items=10,
                record_max_fields=4,
                max_depth=2,
            )
        }
    )
    for tree in (
        {"kind": "string", "max_length": 4096},
        StringTypeDefinition(kind="string"),
    ):
        with pytest.raises(
            (ValidationError, ProjectionValidationError), match="type_limit"
        ):
            ValueSchemaGenerationInput.model_validate(
                {"projection": tree, "generation_spec": small_spec}
            )
        raw = root()
        raw.update(type_definition=tree, value_schema_generation=small_spec)
        with pytest.raises(
            (ValidationError, ProjectionValidationError), match="type_limit"
        ):
            FactSchemaDefinition.model_validate(raw)
    raw = root()
    existing = FactSchemaDefinition.model_validate(raw)
    forged = existing.model_copy(
        update={
            "type_definition": StringTypeDefinition(kind="string"),
            "value_schema_generation": small_spec,
        }
    )
    with pytest.raises(
        (ValidationError, ProjectionValidationError), match="type_limit"
    ):
        validate_child_reference(forged)
    with pytest.raises(ValidationError):
        generate_value_schema(
            {"kind": "bool"}, spec().model_copy(update={"generator_version": "9.0.0"})
        )
    with pytest.raises(ValidationError):
        generate_value_schema(
            {"kind": "bool"},
            ValueSchemaGenerationSpec.model_construct(generator_id="unregistered"),
        )
    original = ValueSchemaGenerationInput.model_validate(
        {"projection": {"kind": "bool"}, "generation_spec": spec()}
    )
    forged_input = original.model_copy(
        update={"projection": {"kind": "bool", "extra": True}}
    )
    with pytest.raises(ValidationError):
        ValueSchemaGenerationInput.model_validate(forged_input)


@pytest.mark.parametrize(
    "field",
    [
        "schema_version",
        "schema_id",
        "version",
        "semantic_key",
        "scope",
        "type_definition",
        "payload_schema_ref",
        "value_schema_generation",
        "allowed_producer_kinds",
        "status_policy",
        "units",
        "coordinate_system",
        "evidence_policy_ref",
        "confidence_metadata_policy",
    ],
)
def test_required_root_fields_and_nulls(field: str) -> None:
    raw = root()
    del raw[field]
    with pytest.raises(ValidationError):
        FactSchemaDefinition.model_validate(raw)
    raw[field] = None
    with pytest.raises(ValidationError):
        FactSchemaDefinition.model_validate(raw)
    assert not Draft202012Validator(FactSchemaDefinition.model_json_schema()).is_valid(
        raw
    )


@pytest.mark.parametrize(
    "field,value",
    [
        ("units", {"mode": "not_applicable", "bindings": []}),
        ("units", {"mode": "declared", "bindings": []}),
        ("coordinate_system", {"mode": "not_applicable", "free_text": "guess"}),
        ("confidence_metadata_policy", {"mode": "forbidden", "threshold": 0.8}),
        (
            "confidence_metadata_policy",
            {
                "mode": "optional",
                "allowed_fact_statuses": ["known", "known"],
                "interpretation": "model_reported_not_calibrated_probability",
                "numeric_format": "finite_number",
            },
        ),
        ("allowed_producer_kinds", ["system", "system"]),
        ("allowed_producer_kinds", []),
        ("scope", "task"),
    ],
)
def test_closed_parent_metadata(field: str, value: object) -> None:
    raw = root()
    raw[field] = value
    with pytest.raises(ValidationError):
        FactSchemaDefinition.model_validate(raw)


def test_execution_scope_cannot_allow_tool_and_question_producers() -> None:
    raw = root()
    raw["scope"] = "execution"
    with pytest.raises(ValidationError):
        FactSchemaDefinition.model_validate(raw)
    assert not Draft202012Validator(FactSchemaDefinition.model_json_schema()).is_valid(
        raw
    )
    raw["allowed_producer_kinds"] = ["system"]
    assert FactSchemaDefinition.model_validate(raw)


@pytest.mark.parametrize(
    "paths",
    [[[], []], [[], ["x"]], [["x"], ["x", "y"]], [["missing"]], [["x", "missing"]]],
)
def test_annotation_paths_are_declared_unique_and_nonoverlapping(
    paths: list[list[str]],
) -> None:
    raw = root()
    raw["type_definition"] = {
        "kind": "record",
        "fields": [
            {"name": "x", "required": True, "type_definition": {"kind": "bool"}}
        ],
    }
    member = {
        "vocabulary_ref": {
            "artifact_kind": "vocabulary",
            "artifact_id": "unit",
            "version": "1.0.0",
            "digest": "a" * 64,
        },
        "member": "synthetic",
    }
    raw["units"] = {
        "mode": "declared",
        "bindings": [{"field_path": path, "unit": member} for path in paths],
    }
    with pytest.raises(ValidationError):
        FactSchemaDefinition.model_validate(raw)


def test_wrong_child_digest_identity_and_kind_rejected() -> None:
    raw = root()
    with pytest.raises(ProjectionValidationError, match="child_reference_mismatch"):
        validate_child_reference(FactSchemaDefinition.model_validate(raw))
    raw["payload_schema_ref"]["artifact_kind"] = "fact_schema"
    with pytest.raises(ValidationError):
        FactSchemaDefinition.model_validate(raw)
    assert not Draft202012Validator(FactSchemaDefinition.model_json_schema()).is_valid(
        raw
    )


@pytest.mark.parametrize(
    "payload",
    [
        {
            "kind": "record",
            "value": {
                "x": {"kind": "bool", "value": True},
                "extra": {"kind": "bool", "value": False},
            },
        },
        {"kind": "record", "value": {}},
        {"kind": "record", "value": {"x": True}},
        {"kind": "record", "value": {"x": {"kind": "bool", "value": 1}}},
    ],
)
def test_nested_generated_schema_rejects_invalid_payload(payload: object) -> None:
    result = generate_value_schema(
        {
            "kind": "record",
            "fields": [
                {"name": "x", "required": True, "type_definition": {"kind": "bool"}}
            ],
        },
        spec(),
    )
    assert not Draft202012Validator(json.loads(result.canonical_schema_bytes)).is_valid(
        payload
    )
    with pytest.raises(ValidationError):
        validate_typed_value(result.generation_input.projection, payload)


def test_generator_entry_points_reject_extra_injected_fields() -> None:
    limits = spec().global_limits.model_copy(update={"hidden": 1})
    with pytest.raises(ValidationError):
        normalize_type({"kind": "bool"}, limits)
    raw = FactSchemaDefinition.model_validate(root()).model_copy(
        update={"parent_digest": "a" * 64}
    )
    with pytest.raises(ValidationError):
        validate_child_reference(raw)


def test_canonicalization_unicode_and_finite_errors_are_typed() -> None:
    for value in ({"\ud800": "x"}, {"x": "\ud800"}, [float("inf")]):
        with pytest.raises(ProjectionValidationError) as caught:
            canonical_bytes(value)
        assert caught.value.path


def test_global_limits_cover_enum_members_fields_and_nested_containers() -> None:
    limits = GlobalTypeLimits(
        string_max_length=2, list_max_items=2, record_max_fields=1, max_depth=1
    )
    trees = [
        {"kind": "enum", "members": ["long"]},
        {"kind": "list", "max_items": 3, "item_type": {"kind": "bool"}},
        {
            "kind": "record",
            "fields": [
                {"name": name, "required": True, "type_definition": {"kind": "bool"}}
                for name in ("x", "y")
            ],
        },
        {"kind": "list", "item_type": {"kind": "list", "item_type": {"kind": "bool"}}},
    ]
    for tree in trees:
        with pytest.raises(ProjectionValidationError, match="type_limit"):
            normalize_type(tree, limits)


@pytest.mark.parametrize(
    "tree,payload",
    [
        (
            {"kind": "integer", "minimum": 1, "maximum": 2},
            {"kind": "integer", "value": 0},
        ),
        (
            {"kind": "number", "minimum": 1.5, "maximum": 2.5},
            {"kind": "number", "value": 3.0},
        ),
        (
            {"kind": "string", "min_length": 1, "max_length": 2},
            {"kind": "string", "value": "long"},
        ),
        ({"kind": "enum", "members": ["a", "b"]}, {"kind": "enum", "value": "c"}),
        (
            {
                "kind": "list",
                "min_items": 1,
                "max_items": 2,
                "item_type": {"kind": "bool"},
            },
            {"kind": "list", "value": []},
        ),
    ],
)
def test_generated_payload_constraints_match_bound_semantics(
    tree: object, payload: object
) -> None:
    result = generate_value_schema(tree, spec())
    assert not Draft202012Validator(json.loads(result.canonical_schema_bytes)).is_valid(
        payload
    )
    with pytest.raises(ValidationError):
        validate_typed_value(result.generation_input.projection, payload)


def test_model_construct_cannot_hide_extra_type_constraints() -> None:
    forged = StringTypeDefinition.model_construct(kind="string", max_length=2)
    forged = forged.model_copy(update={"arbitrary_schema_ref": "remote"})
    with pytest.raises(ValidationError):
        generate_value_schema(forged, spec())
