"""Negative S2 inputs cannot become approximate schema or successful validation."""

import json
from pathlib import Path
from typing import cast

import pytest
from jsonschema import Draft202012Validator
from pydantic import TypeAdapter, ValidationError

from jimu_runtime.domain.schema_types import SchemaDescriptor
from jimu_runtime.domain.tool_mappings import InputBinding, InputSource
from jimu_runtime.domain.validation.review_inputs import ReviewMetadata, ReviewScope
from jimu_runtime.domain.validation.schema_descriptors import (
    SchemaCheckError,
    validate_target_bindings,
)


def fixture() -> dict[str, object]:
    return cast(
        dict[str, object],
        json.loads(
            (Path(__file__).parents[1] / "fixtures/wp01b/s2/contracts.json").read_text()
        ),
    )


@pytest.mark.parametrize("name", ["raw", "typed", "tool", "mixed"])
@pytest.mark.parametrize("extra", [None, "shell", {}])
def test_descriptor_extra_and_null_fields_rejected_in_schema_and_model(
    name: str, extra: object
) -> None:
    data = cast(dict[str, object], fixture()[name])
    data["executable_hook"] = extra
    adapter = TypeAdapter(SchemaDescriptor)
    assert not Draft202012Validator(adapter.json_schema()).is_valid(data)
    with pytest.raises(ValidationError):
        adapter.validate_python(data)


@pytest.mark.parametrize("index", range(5))
def test_source_cannot_have_foreign_owner_or_n06_inputs(index: int) -> None:
    raw = cast(list[dict[str, object]], fixture()["sources"])[index]
    raw["object_id"] = "another-object"
    with pytest.raises(ValidationError):
        TypeAdapter(InputSource).validate_python(raw)
    raw["source_kind"] = "decision"
    with pytest.raises(ValidationError):
        TypeAdapter(InputSource).validate_python(raw)


def test_required_target_cannot_omit_or_synthesize_defaults() -> None:
    binding = cast(list[dict[str, object]], fixture()["bindings"])[0]
    binding["on_missing"] = "omit"
    with pytest.raises(ValidationError):
        InputBinding.model_validate(binding)
    binding["on_missing"] = "block"
    binding["default"] = False
    with pytest.raises(ValidationError):
        InputBinding.model_validate(binding)


def test_wrong_kind_and_unknown_representation_are_not_accepted() -> None:
    data = cast(dict[str, object], fixture()["typed"])
    ref = cast(dict[str, object], data["schema_ref"])
    ref["artifact_kind"] = "context_schema"
    with pytest.raises(ValidationError):
        TypeAdapter(SchemaDescriptor).validate_python(data)
    mixed = cast(dict[str, object], fixture()["mixed"])
    cast(list[dict[str, object]], mixed["fields"])[0]["representation"] = "automatic"
    with pytest.raises(ValidationError):
        TypeAdapter(SchemaDescriptor).validate_python(mixed)


def test_duplicate_mixed_names_and_evidence_slots_rejected() -> None:
    for name, collection in [
        ("mixed", "fields"),
        ("tool", "evidence_fields"),
        ("tool", "output_declarations"),
    ]:
        data = cast(dict[str, object], fixture()[name])
        entries = cast(list[object], data[collection])
        entries.append(entries[0])
        with pytest.raises(ValidationError):
            TypeAdapter(SchemaDescriptor).validate_python(data)


def test_targets_reject_missing_duplicate_overlap_and_wrong_requirement() -> None:
    adapter = TypeAdapter(tuple[InputBinding, ...])
    bindings = adapter.validate_python(fixture()["bindings"])
    descriptor = TypeAdapter(SchemaDescriptor).validate_python(fixture()["mixed"])
    for candidate, code in [
        ((bindings[0],), "required_target_unmapped"),
        ((bindings[0], bindings[0]), "overlapping_targets"),
    ]:
        with pytest.raises(SchemaCheckError) as error:
            validate_target_bindings(descriptor, candidate)
        assert error.value.issue.code == code
    data = cast(list[dict[str, object]], fixture()["bindings"])
    first = data[0]
    first["required"] = False
    with pytest.raises(SchemaCheckError) as error:
        validate_target_bindings(descriptor, adapter.validate_python(data))
    assert error.value.issue.code == "target_requirement_mismatch"
    data = cast(list[dict[str, object]], fixture()["bindings"])
    nested = dict(data[0])
    target = dict(cast(dict[str, object], nested["target"]))
    target["field_path"] = ["image", "asset_id"]
    nested["target"] = target
    data.append(nested)
    with pytest.raises(SchemaCheckError) as error:
        validate_target_bindings(descriptor, adapter.validate_python(data))
    assert error.value.issue.code == "overlapping_targets"


@pytest.mark.parametrize("index", range(4))
def test_review_scope_empty_or_duplicate_identity_rejected(index: int) -> None:
    data = cast(list[dict[str, object]], fixture()["scopes"])[index]
    refs = cast(list[object], data["binding_refs"])
    refs.append(refs[0])
    with pytest.raises(ValidationError):
        TypeAdapter(ReviewScope).validate_python(data)
    data["binding_refs"] = []
    with pytest.raises(ValidationError):
        TypeAdapter(ReviewScope).validate_python(data)


@pytest.mark.parametrize(
    "mutation",
    [
        "naive",
        "nonutc",
        "reversed",
        "quality_int",
        "extra_local",
        "remote_local",
        "duplicate_trigger",
        "null",
    ],
)
def test_invalid_review_windows_and_branch_fields(mutation: str) -> None:
    data = cast(dict[str, object], fixture()["review"])
    assessments = cast(list[dict[str, object]], data["assessments"])
    if mutation == "naive":
        data["valid_from"] = "2026-01-01T00:00:00"
    elif mutation == "nonutc":
        data["valid_from"] = "2026-01-01T00:00:00+08:00"
    elif mutation == "reversed":
        data["valid_until"] = data["valid_from"]
    elif mutation == "quality_int":
        assessments[0]["quality_evaluated"] = 1
    elif mutation == "extra_local":
        assessments[1]["source"] = assessments[0]["source"]
    elif mutation == "remote_local":
        cast(dict[str, object], assessments[1]["primary"])["provider_kind"] = "remote"
    elif mutation == "duplicate_trigger":
        assessments[0]["trigger_errors"] = ["timeout", "timeout"]
    else:
        data["valid_from"] = None
    with pytest.raises(ValidationError):
        ReviewMetadata.model_validate_json(json.dumps(data))


@pytest.mark.parametrize(
    "mutation", ["duplicate", "conflict", "owner", "source_owner", "null"]
)
def test_finite_mapping_context_rejects_conflicts_and_foreign_owners(
    mutation: str,
) -> None:
    from jimu_runtime.domain.tool_mappings import MappingTypeContext

    data = cast(dict[str, object], fixture()["mapping_context"])
    registrations = cast(list[dict[str, object]], data["context_registrations"])
    descriptors = cast(list[dict[str, object]], data["descriptors"])
    if mutation == "duplicate":
        registrations.append(registrations[0])
    elif mutation == "conflict":
        descriptor = dict(descriptors[0])
        ref = dict(cast(dict[str, object], descriptor["schema_ref"]))
        ref["digest"] = "b" * 64
        descriptor["schema_ref"] = ref
        descriptors.append(descriptor)
    elif mutation == "owner":
        cast(dict[str, object], registrations[1]["profile_ref"])["artifact_id"] = (
            "other-profile"
        )
    elif mutation == "source_owner":
        registrations[0]["region_ref"] = registrations[2]["region_ref"]
    else:
        data["object_schema_ref"] = None
    with pytest.raises(ValidationError):
        MappingTypeContext.model_validate(data)


def test_region_context_owner_same_version_different_digest_rejected() -> None:
    from copy import deepcopy

    from jimu_runtime.domain.tool_mappings import MappingTypeContext

    data = cast(dict[str, object], fixture()["mapping_context"])
    registrations = cast(list[dict[str, object]], data["context_registrations"])
    conflicting = deepcopy(registrations[2])
    cast(dict[str, object], conflicting["region_ref"])["digest"] = "b" * 64
    registrations.append(conflicting)
    with pytest.raises(ValidationError):
        MappingTypeContext.model_validate(data)


def test_region_context_owner_different_exact_version_is_not_shape_conflict() -> None:
    from copy import deepcopy

    from jimu_runtime.domain.tool_mappings import MappingTypeContext

    data = cast(dict[str, object], fixture()["mapping_context"])
    registrations = cast(list[dict[str, object]], data["context_registrations"])
    other = deepcopy(registrations[2])
    cast(dict[str, object], other["region_ref"])["version"] = "2.0.0"
    registrations.append(other)
    assert len(MappingTypeContext.model_validate(data).context_registrations) == 4
