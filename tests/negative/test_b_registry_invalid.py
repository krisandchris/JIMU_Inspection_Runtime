"""Malformed and forged Registry inputs never mutate registered truth."""

import pytest
from fixtures.wp01b.r1.support import registration

from jimu_runtime.domain.fact_schemas.registry import (
    FactSchemaRegistry,
    FactSchemaRegistryBuilder,
    RegistryValidationError,
)


def test_snapshot_cannot_accept_an_unvalidated_mutable_constructor_input():
    with pytest.raises(TypeError):
        FactSchemaRegistry([])
    with pytest.raises(TypeError):
        FactSchemaRegistry()


@pytest.mark.parametrize(
    "changes,code",
    [
        ({"artifact_kind": "tool"}, "registry.invalid_reference"),
        ({"digest": "bad"}, "registry.invalid_reference"),
        ({"artifact_id": "another"}, "registry.parent_identity_mismatch"),
        ({"version": "2.0.0"}, "registry.parent_identity_mismatch"),
        ({"unknown": True}, "registry.invalid_reference"),
    ],
)
def test_invalid_parent_and_forged_refs_rejected_atomically(changes, code):
    ref, definition, _ = registration()
    builder = FactSchemaRegistryBuilder()
    with pytest.raises(RegistryValidationError, match=code) as failure:
        builder.register(ref.model_copy(update=changes), definition)
    assert failure.value.report.aggregate_status == "invalid"
    with pytest.raises(RegistryValidationError, match="registry.unknown_identity"):
        builder.freeze().resolve(ref)


@pytest.mark.parametrize(
    "child_changes",
    [{"artifact_id": "wrong.child"}, {"version": "2.0.0"}, {"digest": "b" * 64}],
)
def test_forged_child_identity_rejected(child_changes):
    ref, definition, _ = registration()
    child = definition.payload_schema_ref.model_copy(update=child_changes)
    with pytest.raises(RegistryValidationError, match="child_reference_mismatch"):
        FactSchemaRegistryBuilder().register(
            ref, definition.model_copy(update={"payload_schema_ref": child})
        )


def test_identity_conflicts_and_forged_definition_preserve_existing_entry():
    ref, definition, _ = registration()
    builder = FactSchemaRegistryBuilder().register(ref, definition)
    for candidate_ref, candidate in [
        (ref.model_copy(update={"digest": "b" * 64}), definition),
        (ref, definition.model_copy(update={"semantic_key": "changed.meaning"})),
        (ref, definition.model_copy(update={"schema_version": "2.0.0"})),
        (ref, definition.model_copy(update={"unexpected": "not_allowed"})),
    ]:
        with pytest.raises(RegistryValidationError):
            builder.register(candidate_ref, candidate)
    registry = builder.freeze()
    assert registry.resolve(ref) == definition
    with pytest.raises(RegistryValidationError, match="registry.digest_mismatch"):
        registry.resolve(ref.model_copy(update={"digest": "c" * 64}))


@pytest.mark.parametrize(
    "payload",
    [
        {"kind": "enum", "value": "unregistered"},
        {"kind": "enum", "value": "unregistered", "members": ["unregistered"]},
        {"kind": "enum", "value": None},
        {"kind": "enum"},
        {"kind": "enum", "value": "yes", "schema": {"members": ["yes"]}},
    ],
)
def test_payload_cannot_self_authorize_schema(payload):
    ref, definition, _ = registration({"kind": "enum", "members": ["yes", "no"]})
    registry = FactSchemaRegistryBuilder().register(ref, definition).freeze()
    with pytest.raises(RegistryValidationError, match="registry.invalid_payload"):
        registry.validate_payload(ref, payload)


@pytest.mark.parametrize(
    "tree,payload",
    [
        ({"kind": "bool"}, {"kind": "bool", "value": 0}),
        ({"kind": "integer"}, {"kind": "integer", "value": False}),
        ({"kind": "number"}, {"kind": "number", "value": float("nan")}),
        ({"kind": "number"}, {"kind": "number", "value": float("inf")}),
        ({"kind": "string", "max_length": 2}, {"kind": "string", "value": "abc"}),
        (
            {"kind": "list", "item_type": {"kind": "integer"}, "max_items": 1},
            {"kind": "list", "value": [{"kind": "bool", "value": False}]},
        ),
        (
            {"kind": "list", "item_type": {"kind": "integer"}, "max_items": 1},
            {"kind": "list", "value": [{"kind": "integer", "value": 0}] * 2},
        ),
        (
            {
                "kind": "record",
                "fields": [
                    {"name": "a", "required": True, "type_definition": {"kind": "bool"}}
                ],
            },
            {"kind": "record", "value": {}},
        ),
        (
            {"kind": "record", "fields": []},
            {"kind": "record", "value": {"injected": {"kind": "bool", "value": True}}},
        ),
        (
            {
                "kind": "record",
                "fields": [
                    {
                        "name": "a",
                        "required": False,
                        "type_definition": {"kind": "bool"},
                    }
                ],
            },
            {"kind": "record", "value": {"a": None}},
        ),
    ],
)
def test_registered_value_constraints_never_reduce_to_shape_only(tree, payload):
    ref, definition, _ = registration(tree)
    registry = FactSchemaRegistryBuilder().register(ref, definition).freeze()
    with pytest.raises(RegistryValidationError, match="registry.invalid_payload"):
        registry.validate_payload(ref, payload)


def test_forged_value_instance_is_revalidated():
    from jimu_runtime.domain.values import IntegerValue

    ref, definition, _ = registration({"kind": "integer"})
    registry = FactSchemaRegistryBuilder().register(ref, definition).freeze()
    forged = IntegerValue.model_construct(kind="integer", value=True)
    with pytest.raises(RegistryValidationError, match="registry.invalid_payload"):
        registry.validate_payload(ref, forged)


@pytest.mark.parametrize("path", [("absent",), (0,), ("a",) * 9, "a", ("a", "child")])
def test_invalid_paths_never_return_empty_types(path):
    ref, definition, _ = registration(
        {
            "kind": "record",
            "fields": [
                {
                    "name": "a",
                    "required": True,
                    "type_definition": {"kind": "list", "item_type": {"kind": "bool"}},
                }
            ],
        }
    )
    registry = FactSchemaRegistryBuilder().register(ref, definition).freeze()
    with pytest.raises(RegistryValidationError):
        registry.describe_type(ref, path)
