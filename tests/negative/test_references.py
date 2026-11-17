import json

import pytest
from pydantic import TypeAdapter, ValidationError

from jimu_runtime.domain.enums import ArtifactKind, RuntimeRecordKind, TaskStatus
from jimu_runtime.domain.primitives import ArtifactId, Digest, RecordId, Version
from jimu_runtime.domain.references import (
    ContentRef,
    IdentitySelector,
    RuntimeRecordRef,
    SchemaRef,
)


@pytest.mark.parametrize(
    "bad",
    [
        "",
        "latest",
        "1",
        "1.2",
        "v1.2.3",
        "01.2.3",
        "1.02.3",
        "1.2.03",
        "1.2.3-rc1",
        "1.2.3+build",
        ">=1.0.0",
        "1.*",
        " 1.2.3",
        "1.2.3\n",
        "1.2.3 ",
        "1.2.-1",
        "１.2.3",
        "1.2.3/",
        "1.2." + "9" * 129,
        None,
        0,
        False,
        1.0,
        b"1.2.3",
    ],
)
def test_invalid_version(bad):
    with pytest.raises(ValidationError):
        TypeAdapter(Version).validate_python(bad)


@pytest.mark.parametrize(
    "bad",
    [
        "",
        "a" * 63,
        "a" * 65,
        "A" * 64,
        "g" * 64,
        "sha256:" + "a" * 64,
        "0x" + "a" * 64,
        " " + "a" * 63,
        "a" * 64 + "\n",
        "REPLACE_WITH_REAL_SHA256",
        None,
        False,
        0,
        b"a" * 64,
    ],
)
def test_invalid_digest(bad):
    with pytest.raises(ValidationError):
        TypeAdapter(Digest).validate_python(bad)


@pytest.mark.parametrize("primitive", [ArtifactId, RecordId])
@pytest.mark.parametrize("bad", ["", " ", "abc\n", "a" * 129, None, False, 5, b"id"])
def test_invalid_identity(primitive, bad):
    with pytest.raises(ValidationError):
        TypeAdapter(primitive).validate_python(bad)


@pytest.mark.parametrize(
    "bad", ["unknown_kind", "fact", "discovery_capability", "ENTITY", None, 0]
)
def test_unregistered_artifact_kind(bad):
    with pytest.raises(ValidationError):
        TypeAdapter(ArtifactKind).validate_python(bad)


@pytest.mark.parametrize(
    "model,key",
    [
        (ContentRef, "content_ref"),
        (IdentitySelector, "identity_selector"),
        (RuntimeRecordRef, "runtime_record_ref"),
        (SchemaRef, "schema_ref"),
    ],
)
def test_missing_and_extra_fields(model, key, foundation_fixtures):
    raw = foundation_fixtures[key]
    for field in model.model_fields:
        missing = {k: v for k, v in raw.items() if k != field}
        with pytest.raises(ValidationError):
            model.model_validate(missing)
        with pytest.raises(ValidationError):
            model.model_validate_json(json.dumps(missing))
    for field in ("unexpected", "command", "__class__", "extensions"):
        with pytest.raises(ValidationError):
            model.model_validate({**raw, field: "injection"})


def test_selector_cannot_carry_digest_or_range(foundation_fixtures):
    raw = foundation_fixtures["identity_selector"]
    with pytest.raises(ValidationError):
        IdentitySelector.model_validate({**raw, "digest": "a" * 64})
    for kind in [ArtifactKind.QUESTION, ArtifactKind.TOOL_PLAN]:
        with pytest.raises(ValidationError):
            IdentitySelector.model_validate({**raw, "artifact_kind": kind})
    with pytest.raises(ValidationError):
        IdentitySelector.model_validate({**raw, "version_selector": ">=1.0.0"})


def test_different_reference_types_never_implicitly_substitute(foundation_fixtures):
    models = [ContentRef, IdentitySelector, RuntimeRecordRef]
    keys = ["content_ref", "identity_selector", "runtime_record_ref"]
    instances = [
        model.model_validate(foundation_fixtures[key])
        for model, key in zip(models, keys, strict=True)
    ]
    for index, model in enumerate(models):
        for other_index, instance in enumerate(instances):
            if index != other_index:
                with pytest.raises(ValidationError):
                    model.model_validate(instance)
                with pytest.raises(ValidationError):
                    model.model_validate_json(instance.model_dump_json())
    with pytest.raises(ValidationError):
        SchemaRef.model_validate(instances[0])


def test_schema_kind_dialect_and_runtime_kind_are_strict(foundation_fixtures):
    raw = foundation_fixtures["schema_ref"]
    with pytest.raises(ValidationError):
        SchemaRef.model_validate({**raw, "artifact_kind": "profile"})
    with pytest.raises(ValidationError):
        SchemaRef.model_validate(
            {**raw, "schema_dialect": "http://json-schema.org/draft-07/schema#"}
        )
    with pytest.raises(ValidationError):
        ContentRef.model_validate(
            {
                **foundation_fixtures["content_ref"],
                "artifact_kind": RuntimeRecordKind.FACT,
            }
        )
    with pytest.raises(ValidationError):
        RuntimeRecordRef.model_validate(
            {"record_kind": ArtifactKind.PROFILE, "record_id": "r1"}
        )
    with pytest.raises(ValidationError):
        TypeAdapter(TaskStatus).validate_python("unknown")
