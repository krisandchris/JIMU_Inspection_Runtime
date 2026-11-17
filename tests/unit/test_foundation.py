import json
from enum import Enum
from itertools import permutations

import pytest
from pydantic import TypeAdapter, ValidationError

from jimu_runtime.domain import enums
from jimu_runtime.domain.enums import ArtifactKind, RuntimeRecordKind
from jimu_runtime.domain.primitives import Digest, Version
from jimu_runtime.domain.references import (
    ContentRef,
    IdentitySelector,
    RuntimeRecordRef,
    SchemaRef,
)
from jimu_runtime.domain.values import ValueContract

REFERENCE_MODELS = [ContentRef, IdentitySelector, RuntimeRecordRef, SchemaRef]
REFERENCE_KEYS = [
    "content_ref",
    "identity_selector",
    "runtime_record_ref",
    "schema_ref",
]
STATUS_TYPES = [
    enums.TaskStatus,
    enums.ExecutionStatus,
    enums.NodeRunStatus,
    enums.AttemptStatus,
    enums.FactStatus,
    enums.DecisionResult,
]


def test_approved_catalogue_and_status_sets(foundation_fixtures):
    assert [kind.value for kind in ArtifactKind] == foundation_fixtures[
        "artifact_kinds"
    ]
    assert len(ArtifactKind) == 25
    for enum_type in STATUS_TYPES:
        assert [item.value for item in enum_type] == foundation_fixtures[
            "status_domains"
        ][enum_type.__name__]
    assert enums.TaskStatus.FAILED != enums.ExecutionStatus.FAILED


@pytest.mark.parametrize("enum_type", [ArtifactKind, RuntimeRecordKind, *STATUS_TYPES])
def test_every_enum_member_roundtrips(enum_type):
    adapter = TypeAdapter(enum_type)
    for member in enum_type:
        assert adapter.validate_python(member.value) is member
        assert adapter.validate_json(adapter.dump_json(member)) is member
    assert len(enum_type) == len(enum_type.__members__)


@pytest.mark.parametrize(
    ("model", "key"), zip(REFERENCE_MODELS, REFERENCE_KEYS, strict=True)
)
def test_reference_roundtrip_and_input_order(model, key, foundation_fixtures):
    raw = foundation_fixtures[key]
    obj = model.model_validate(raw)
    assert model.model_validate_json(obj.model_dump_json()) == obj
    assert model.model_validate(obj.model_dump(mode="json")) == obj
    reversed_raw = dict(reversed(list(raw.items())))
    assert model.model_validate(reversed_raw).stable_json() == obj.stable_json()
    assert (
        model.model_validate_json(json.dumps(reversed_raw)).stable_json()
        == obj.stable_json()
    )
    assert obj.model_dump(mode="json") == raw


@pytest.mark.parametrize("kind", list(ArtifactKind))
def test_content_ref_accepts_each_registered_kind(kind, foundation_fixtures):
    raw = {**foundation_fixtures["content_ref"], "artifact_kind": kind}
    assert ContentRef.model_validate(raw).artifact_kind is kind


@pytest.mark.parametrize("kind", list(RuntimeRecordKind))
def test_runtime_ref_accepts_registered_record_kinds(kind):
    ref = RuntimeRecordRef.model_validate(
        {"record_kind": kind.value, "record_id": "id-001"}
    )
    assert ref.record_kind is kind
    assert RuntimeRecordRef.model_validate_json(ref.model_dump_json()) == ref


@pytest.mark.parametrize("version", ["0.0.0", "1.0.0", "123.456.789"])
def test_version_exact_roundtrip(version):
    adapter = TypeAdapter(Version)
    assert (
        adapter.validate_json(adapter.dump_json(adapter.validate_python(version)))
        == version
    )


def test_digest_lowercase_roundtrip():
    digest = "0123456789abcdef" * 4
    adapter = TypeAdapter(Digest)
    assert adapter.validate_json(adapter.dump_json(digest)) == digest


def test_all_seven_value_contracts_roundtrip(foundation_fixtures):
    assert len(foundation_fixtures["values"]) == 7
    for raw in foundation_fixtures["values"]:
        value = ValueContract.model_validate(raw)
        assert ValueContract.model_validate_json(value.model_dump_json()) == value
        assert ValueContract.model_validate(value.model_dump(mode="json")) == value


def test_reference_and_nested_value_immutability(foundation_fixtures):
    ref = ContentRef.model_validate(foundation_fixtures["content_ref"])
    with pytest.raises(ValidationError, match="frozen_instance"):
        ref.digest = "b" * 64
    forged = ref.model_copy(update={"digest": "invalid"})
    with pytest.raises(ValidationError):
        ContentRef.model_validate(forged)
    record = ValueContract.model_validate(foundation_fixtures["values"][-1])
    with pytest.raises(TypeError):
        record.value.value["added"] = {"kind": "bool", "value": True}
    with pytest.raises(ValidationError, match="frozen_instance"):
        record.value.value["parked"].value = True
    assert record.value.value["parked"].value is False


@pytest.mark.parametrize(("target", "foreign"), list(permutations(STATUS_TYPES, 2)))
def test_status_domains_reject_foreign_enums(target, foreign):
    adapter = TypeAdapter(target)
    for member in foreign:
        with pytest.raises(ValidationError):
            adapter.validate_python(member)


def test_foreign_string_enum_is_not_a_wire_string():
    class Foreign(str, Enum):
        FAILED = "failed"

    with pytest.raises(ValidationError):
        TypeAdapter(enums.TaskStatus).validate_python(Foreign.FAILED)


@pytest.mark.parametrize(
    "kind",
    [
        ArtifactKind.FACT_SCHEMA,
        ArtifactKind.VALUE_SCHEMA,
        ArtifactKind.RESPONSE_SCHEMA,
        ArtifactKind.ATTRIBUTE_SCHEMA,
        ArtifactKind.CONTEXT_SCHEMA,
        ArtifactKind.TOOL_INPUT_SCHEMA,
        ArtifactKind.TOOL_OUTPUT_SCHEMA,
        ArtifactKind.OBJECT_SCHEMA,
        ArtifactKind.DECISION_SCHEMA,
    ],
)
def test_schema_ref_is_a_constrained_content_ref(kind, foundation_fixtures):
    obj = SchemaRef.model_validate(
        {**foundation_fixtures["schema_ref"], "artifact_kind": kind.value}
    )
    assert isinstance(obj, ContentRef)
    assert SchemaRef.model_validate_json(obj.model_dump_json()) == obj


@pytest.mark.parametrize("kind", [ArtifactKind.PROFILE, ArtifactKind.REGION])
def test_selector_only_allows_declared_applicability_targets(kind, foundation_fixtures):
    selector = IdentitySelector.model_validate(
        {**foundation_fixtures["identity_selector"], "artifact_kind": kind.value}
    )
    assert selector.artifact_kind is kind
    assert not hasattr(selector, "digest")
