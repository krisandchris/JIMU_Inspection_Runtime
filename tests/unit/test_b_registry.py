"""Core Registry behavior, not complete Producer/Authority admission."""

from dataclasses import FrozenInstanceError

import pytest
from fixtures.wp01b.r1.support import fixtures, registration

from jimu_runtime.domain.fact_schemas.registry import (
    FactSchemaRegistryBuilder,
    RegistryValidationError,
)
from jimu_runtime.domain.validation.issues import (
    CheckResult,
    ValidationIssue,
    ValidationReport,
)


def test_register_idempotence_freeze_and_version_snapshots():
    ref, definition, _ = registration()
    builder = FactSchemaRegistryBuilder()
    assert builder.register(ref, definition) is builder
    assert builder.register(ref, definition) is builder
    first = builder.freeze()
    assert builder.freeze() is first
    assert first.resolve(ref) == definition
    with pytest.raises(RegistryValidationError, match="registry.frozen"):
        builder.register(ref, definition)
    ref2, definition2, _ = registration(version="2.0.0", digest="b" * 64)
    second = (
        FactSchemaRegistryBuilder()
        .register(ref, definition)
        .register(ref2, definition2)
        .freeze()
    )
    assert second.resolve(ref2) == definition2
    with pytest.raises(RegistryValidationError, match="registry.unknown_identity"):
        first.resolve(ref2)
    with pytest.raises(FrozenInstanceError):
        first._entries = ()


@pytest.mark.parametrize("tree,payload", fixtures()["values"])
def test_all_registered_value_kinds_and_original_input_snapshot(tree, payload):
    ref, definition, _ = registration(tree)
    registry = FactSchemaRegistryBuilder().register(ref, definition).freeze()
    value = registry.validate_payload(ref, payload)
    assert value.kind == tree["kind"]
    original = definition.model_dump_json()
    tree.clear()
    assert registry.resolve(ref).model_dump_json() == original


def test_paths_reuse_child_schema_and_optional_ancestry():
    tree = {
        "kind": "record",
        "fields": [
            {
                "name": "a.b",
                "required": False,
                "type_definition": {
                    "kind": "record",
                    "fields": [
                        {
                            "name": "required_leaf",
                            "required": True,
                            "type_definition": {"kind": "integer"},
                        },
                        {
                            "name": "optional_leaf",
                            "required": False,
                            "type_definition": {"kind": "string"},
                        },
                    ],
                },
            }
        ],
    }
    ref, definition, _ = registration(tree)
    registry = FactSchemaRegistryBuilder().register(ref, definition).freeze()
    whole = registry.describe_type(ref)
    assert whole.type_definition == definition.type_definition
    assert whole.field_path == whole.wire_path == whole.optional_ancestor_paths == ()
    assert whole.schema_ref.artifact_kind.value == "value_schema"
    assert whole.schema_ref.artifact_id == definition.payload_schema_ref.artifact_id
    assert whole.schema_ref.digest == definition.payload_schema_ref.digest
    assert (
        whole.schema_ref.schema_dialect
        == "https://json-schema.org/draft/2020-12/schema"
    )
    leaf = registry.describe_type(ref, ("a.b", "required_leaf"))
    assert leaf.optional_ancestor_paths == (("a.b",),)
    assert leaf.wire_path == ("value", "a.b", "value", "required_leaf")
    optional = registry.describe_type(ref, ["a.b", "optional_leaf"])
    assert optional.optional_ancestor_paths == (("a.b",), ("a.b", "optional_leaf"))


def test_empty_registry_and_no_parent_hash_authentication_claim():
    empty = FactSchemaRegistryBuilder().freeze()
    ref, definition, _ = registration(digest="0" * 64)
    with pytest.raises(RegistryValidationError):
        empty.resolve(ref)
    # An asserted parent identity is accepted, not certified as a digest of bytes.
    registry = FactSchemaRegistryBuilder().register(ref, definition).freeze()
    assert registry.resolve(ref) == definition
    # Full admission is explicitly deferred at package-report level; R1 has no
    # fake Producer/Authority catalogue or permissive validate_fact entry point.
    assert not hasattr(registry, "validate_fact")
    report = ValidationReport(
        scope="producer_admission",
        checks=(
            CheckResult(
                check_id="producer.full_admission",
                status="deferred",
                issues=(
                    ValidationIssue(
                        code="dependency.d01",
                        path=(),
                        message="Full Producer contract awaits D01.",
                        related_refs=(),
                    ),
                ),
            ),
        ),
    )
    assert report.aggregate_status == "deferred"


def test_capacity_overflow_is_atomic_and_duplicate_remains_idempotent():
    ref, definition, _ = registration()
    builder = FactSchemaRegistryBuilder()
    for index in range(1024):
        builder.register(
            ref.model_copy(update={"artifact_id": f"schema.s{index}"}),
            definition.model_copy(update={"schema_id": f"schema.s{index}"}),
        )
    builder.register(
        ref.model_copy(update={"artifact_id": "schema.s0"}),
        definition.model_copy(update={"schema_id": "schema.s0"}),
    )
    with pytest.raises(RegistryValidationError, match="registry.capacity"):
        builder.register(ref, definition)
    registry = builder.freeze()
    assert (
        registry.resolve(
            ref.model_copy(update={"artifact_id": "schema.s1023"})
        ).schema_id
        == "schema.s1023"
    )
    with pytest.raises(RegistryValidationError, match="registry.unknown_identity"):
        registry.resolve(ref)
