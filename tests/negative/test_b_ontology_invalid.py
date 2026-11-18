"""Invalid S1 inputs fail closed, without runtime or filesystem operations."""

import json
from pathlib import Path
from typing import cast

import pytest
from jsonschema import Draft202012Validator
from pydantic import ConfigDict, ValidationError

from jimu_runtime.domain.base import ContractModel
from jimu_runtime.domain.definition_support import (
    ContextFieldSelector,
    ExternalCapabilityRequirement,
    ReviewReportRef,
    VersionConstraint,
)
from jimu_runtime.domain.definitions.ontology import EntityDefinition, RegionDefinition
from jimu_runtime.domain.definitions.resources import (
    StaticAssetDescriptor,
    VocabularyDefinition,
)

ROOTS = [
    ("entity", EntityDefinition),
    ("region", RegionDefinition),
    ("vocabulary", VocabularyDefinition),
    ("asset", StaticAssetDescriptor),
]


def fixture(name: str) -> dict[str, object]:
    path = Path(__file__).parents[1] / "fixtures/wp01b/s1/contracts.json"
    return cast(dict[str, dict[str, object]], json.loads(path.read_text()))[name]


def reject(
    model: type[ContractModel], data: object, *, schema_too: bool = True
) -> None:
    with pytest.raises(ValidationError):
        model.model_validate(data)
    with pytest.raises(ValidationError):
        model.model_validate_json(json.dumps(data))
    if schema_too:
        assert not Draft202012Validator(model.model_json_schema()).is_valid(data)


@pytest.mark.parametrize("name,model", ROOTS)
@pytest.mark.parametrize("version", [None, "2.0.0", 1, True])
def test_invalid_root_version(
    name: str, model: type[ContractModel], version: object
) -> None:
    data = fixture(name)
    data["schema_version"] = version
    reject(model, data)


@pytest.mark.parametrize("name,model", ROOTS)
def test_missing_version_extra_runtime_and_own_digest(
    name: str, model: type[ContractModel]
) -> None:
    data = fixture(name)
    del data["schema_version"]
    reject(model, data)
    for key in (
        "digest",
        "object_id",
        "bbox",
        "decision",
        "profile_refs",
        "shell_command",
    ):
        reject(model, {**fixture(name), key: "forbidden"})


@pytest.mark.parametrize(
    "name,model,field",
    [
        ("region", RegionDefinition, "geometry_ref"),
        ("asset", StaticAssetDescriptor, "dimensions"),
        ("capability", ExternalCapabilityRequirement, "evaluation_ref"),
    ],
)
def test_null_is_not_absence(name: str, model: type[ContractModel], field: str) -> None:
    reject(model, {**fixture(name), field: None})


@pytest.mark.parametrize(
    "wrong_kind", ["fact_schema", "context_schema", "vocabulary", "unregistered"]
)
def test_expected_kind_visible_in_both_validators(wrong_kind: str) -> None:
    entity = fixture("entity")
    ref = cast(list[dict[str, object]], entity["attribute_schema_refs"])[0]
    ref["artifact_kind"] = wrong_kind
    reject(EntityDefinition, entity)


def test_schema_ref_dialect_is_not_added_to_four_field_content_ref() -> None:
    region = fixture("region")
    ref = cast(dict[str, object], region["context_schema_ref"])
    ref["schema_dialect"] = "https://json-schema.org/draft/2020-12/schema"
    reject(RegionDefinition, region)
    selector = fixture("selector")
    sref = cast(dict[str, object], selector["schema_ref"])
    del sref["schema_dialect"]
    reject(ContextFieldSelector, selector)


@pytest.mark.parametrize("field", ["entity_type", "lifecycle"])
def test_classification_is_exact_ref_not_uncontrolled_string(field: str) -> None:
    reject(EntityDefinition, {**fixture("entity"), field: "physical_object"})
    data = fixture("entity")
    nested = cast(dict[str, object], data[field])
    cast(dict[str, object], nested["vocabulary_ref"])["artifact_kind"] = "region"
    reject(EntityDefinition, data)


@pytest.mark.parametrize("digest", ["a" * 64, "b" * 64])
def test_duplicate_identity_even_if_digest_differs(digest: str) -> None:
    for name, model, field in (
        ("entity", EntityDefinition, "attribute_schema_refs"),
        ("region", RegionDefinition, "asset_refs"),
    ):
        data = fixture(name)
        refs = cast(list[dict[str, object]], data[field])
        refs.append({**refs[0], "digest": digest})
        reject(model, data, schema_too=False)


@pytest.mark.parametrize(
    "members", [[], ["synthetic", "synthetic"], [" "], ["x" * 129], ["x"] * 1025]
)
def test_vocabulary_bounds_and_uniqueness(members: object) -> None:
    reject(
        VocabularyDefinition,
        {**fixture("vocabulary"), "members": members},
        schema_too=members != ["synthetic", "synthetic"],
    )


@pytest.mark.parametrize(
    "field", ["allowed_implementation_versions", "required_features"]
)
def test_capability_duplicate_arrays(field: str) -> None:
    data = fixture("capability")
    items = cast(list[str], data[field])
    items.append(items[0])
    reject(ExternalCapabilityRequirement, data, schema_too=False)


@pytest.mark.parametrize(
    "field", ["allowed_deployment_versions", "allowed_model_versions"]
)
def test_version_constraint_rejects_duplicates_empty_and_latest(field: str) -> None:
    data = fixture("versions")
    for values in ([], ["latest"], ["1.0.0", "1.0.0"], ["1.0.0"] * 1025):
        reject(VersionConstraint, {**data, field: values}, schema_too=len(values) != 2)


@pytest.mark.parametrize(
    "field,value",
    [
        ("source", "device_registry"),
        ("field_path", []),
        ("field_path", ["a"] * 9),
        ("field_path", [1]),
        ("field_path", "$.scene"),
    ],
)
def test_context_selector_sources_and_paths(field: str, value: object) -> None:
    reject(ContextFieldSelector, {**fixture("selector"), field: value})


@pytest.mark.parametrize(
    "path",
    [
        "/resources/assets/data/a",
        "resources/assets/data/",
        "resources/assets/data/.",
        "resources/assets/data/..",
        "resources/assets/data/a/../b",
        "resources/assets/data/a/./b",
        "resources/assets/data//a",
        "resources/assets/data/a/",
        "resources/assets/data/a\\b",
        "resources/assets/data/a?x",
        "resources/assets/data/a#x",
        "resources/assets/data/http:a",
        "https://example.com/a",
        "resources/assets/data/a\n",
        "resources/assets/data/a\x00",
        "resources/assets/data/a\x85",
        "resources/assets/other/a",
    ],
)
def test_unsafe_asset_paths(path: str) -> None:
    reject(StaticAssetDescriptor, {**fixture("asset"), "data_path": path})


@pytest.mark.parametrize(
    "mime",
    [
        "text",
        "text/plain;charset=UTF-8",
        "text/pläin",
        "text/plain\n",
        "text/ plain",
        "text/(plain)",
    ],
)
def test_invalid_mime_tokens(mime: str) -> None:
    reject(StaticAssetDescriptor, {**fixture("asset"), "mime_type": mime})


@pytest.mark.parametrize("value", [True, -1, 2**63, None, "0"])
def test_asset_size_is_strict_nonnegative(value: object) -> None:
    reject(StaticAssetDescriptor, {**fixture("asset"), "byte_size": value})


def test_nested_optional_null_rejected_even_in_generated_schema() -> None:
    capability = fixture("capability")
    capability["evaluation_ref"] = None
    reject(ExternalCapabilityRequirement, capability)


def test_reference_collection_over_limit_rejected() -> None:
    data = fixture("entity")
    original = cast(list[dict[str, object]], data["attribute_schema_refs"])[0]
    data["attribute_schema_refs"] = [
        {**original, "version": f"1.0.{i}"} for i in range(1025)
    ]
    reject(EntityDefinition, data)


@pytest.mark.parametrize(
    "name,model",
    [
        ("capability", ExternalCapabilityRequirement),
        ("selector", ContextFieldSelector),
        ("versions", VersionConstraint),
    ],
)
def test_shared_models_reject_unregistered_fields(
    name: str, model: type[ContractModel]
) -> None:
    reject(model, {**fixture(name), "arbitrary_extension": "not_registered"})


def test_required_reference_fields_and_dialect_values_rejected() -> None:
    for key in ("digest", "version", "artifact_id"):
        data = fixture("region")
        del cast(dict[str, object], data["context_schema_ref"])[key]
        reject(RegionDefinition, data)
    for dialect in ("http://json-schema.org/draft-07/schema#", None):
        data = fixture("selector")
        cast(dict[str, object], data["schema_ref"])["schema_dialect"] = dialect
        reject(ContextFieldSelector, data)


@pytest.mark.parametrize(
    "name,model", ROOTS + [("capability", ExternalCapabilityRequirement)]
)
def test_revalidation_rejects_model_copy_injected_extra(
    name: str, model: type[ContractModel]
) -> None:
    original = model.model_validate(fixture(name))
    tainted = original.model_copy(update={"injected_field": "must reject"})
    assert tainted.__dict__["injected_field"] == "must reject"
    with pytest.raises(ValidationError) as exc:
        model.model_validate(tainted)
    assert any(error["type"] == "extra_forbidden" for error in exc.value.errors())


def test_nested_tainted_optional_contract_instance_is_rejected() -> None:
    class RequirementHolder(ContractModel):
        requirement: ExternalCapabilityRequirement

    valid = ExternalCapabilityRequirement.model_validate(fixture("capability"))
    assert RequirementHolder.model_validate({"requirement": valid}).requirement == valid
    tainted = valid.model_copy(update={"injected_field": "must reject"})
    with pytest.raises(ValidationError) as exc:
        RequirementHolder.model_validate({"requirement": tainted})
    assert any(
        error["loc"] == ("requirement", "injected_field")
        for error in exc.value.errors()
    )


def test_subclass_declared_fields_are_not_lost_when_revalidating_base() -> None:
    class ExtendedRequirement(ExternalCapabilityRequirement):
        subclass_field: str

    extended = ExtendedRequirement.model_validate(
        {**fixture("capability"), "subclass_field": "must reject"}
    )
    with pytest.raises(ValidationError) as exc:
        ExternalCapabilityRequirement.model_validate(extended)
    assert any(error["loc"] == ("subclass_field",) for error in exc.value.errors())


def test_subclass_allowed_extras_are_not_lost_when_revalidating_base() -> None:
    class ExtendedRequirement(ExternalCapabilityRequirement):
        model_config = ConfigDict(extra="allow")

    extended = ExtendedRequirement.model_validate(
        {**fixture("capability"), "subclass_extra": "must reject"}
    )
    assert extended.__pydantic_extra__ == {"subclass_extra": "must reject"}
    with pytest.raises(ValidationError) as exc:
        ExternalCapabilityRequirement.model_validate(extended)
    assert any(error["loc"] == ("subclass_extra",) for error in exc.value.errors())


def test_constructed_invalid_or_missing_fields_are_still_revalidated() -> None:
    data = fixture("capability")
    missing = {name: value for name, value in data.items() if name != "family"}
    invalid_instances = (
        ExternalCapabilityRequirement.model_construct(**missing),
        ExternalCapabilityRequirement.model_construct(
            **{**data, "contract_version": "latest"}
        ),
        ExternalCapabilityRequirement.model_construct(
            **{**data, "evaluation_ref": None}
        ),
        ExternalCapabilityRequirement.model_construct(
            _fields_set=set(), **{**data, "evaluation_ref": "must reject"}
        ),
    )
    for invalid in invalid_instances:
        with pytest.raises(ValidationError):
            ExternalCapabilityRequirement.model_validate(invalid)


def test_optional_nested_ref_taint_rejected_without_dumping_it_away() -> None:
    valid_review = ReviewReportRef.model_validate(fixture("review"))
    tainted_review = valid_review.model_copy(update={"nested_injected": "must reject"})
    with pytest.raises(ValidationError) as exc:
        ExternalCapabilityRequirement.model_validate(
            {**fixture("capability"), "evaluation_ref": tainted_review}
        )
    assert any(
        error["loc"] == ("evaluation_ref", "nested_injected")
        for error in exc.value.errors()
    )


def test_constructed_instance_retained_extra_data_is_not_discarded() -> None:
    constructed = ExternalCapabilityRequirement.model_construct(**fixture("capability"))
    tainted = constructed.model_copy(update={"constructed_extra": "must reject"})
    with pytest.raises(ValidationError) as exc:
        ExternalCapabilityRequirement.model_validate(tainted)
    assert any(error["loc"] == ("constructed_extra",) for error in exc.value.errors())
