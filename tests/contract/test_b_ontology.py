"""Generated Schema and Pydantic shape contracts; not catalogue admission."""

import json
from pathlib import Path
from typing import cast

import pytest
from jsonschema import Draft202012Validator, FormatChecker
from pydantic import TypeAdapter

from jimu_runtime.domain.base import ContractModel
from jimu_runtime.domain.definition_support import (
    AssetDimensions,
    ContextFieldSelector,
    DefinitionContract,
    Description,
    ExternalCapabilityRequirement,
    FieldPath,
    NonNegativeBudget,
    PositiveBudget,
    ReviewReportRef,
    ShortText,
    UTCDateTime,
    VersionConstraint,
    VocabularyMemberRef,
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
HELPERS = [
    ("capability", ExternalCapabilityRequirement),
    ("selector", ContextFieldSelector),
    ("review", ReviewReportRef),
    ("versions", VersionConstraint),
    ("dimensions", AssetDimensions),
    ("member", VocabularyMemberRef),
]


def fixture(name: str) -> dict[str, object]:
    path = Path(__file__).parents[1] / "fixtures/wp01b/s1/contracts.json"
    return cast(dict[str, dict[str, object]], json.loads(path.read_text()))[name]


@pytest.mark.parametrize("name,model", ROOTS + HELPERS)
def test_schema_structure_and_strict_json_roundtrip(
    name: str, model: type[ContractModel]
) -> None:
    data = fixture(name)
    schema = model.model_json_schema()
    Draft202012Validator.check_schema(schema)
    Draft202012Validator(schema).validate(data)
    result = model.model_validate(data)
    assert model.model_validate(result) == result
    assert model.model_validate_json(result.model_dump_json()) == result
    assert result.model_dump(mode="json") == data
    assert schema == model.model_json_schema()
    assert schema["additionalProperties"] is False
    assert ("schema_version" in schema["properties"]) == issubclass(
        model, DefinitionContract
    )


@pytest.mark.parametrize(
    "annotation",
    [ShortText, Description, FieldPath, PositiveBudget, NonNegativeBudget, UTCDateTime],
)
def test_exported_alias_schema(annotation: object) -> None:
    Draft202012Validator.check_schema(TypeAdapter(annotation).json_schema())


@pytest.mark.parametrize(
    "name,model,field",
    [
        ("region", RegionDefinition, "geometry_ref"),
        ("asset", StaticAssetDescriptor, "dimensions"),
        ("capability", ExternalCapabilityRequirement, "evaluation_ref"),
    ],
)
def test_optional_fields_omit_but_reject_null_in_generated_schema(
    name: str, model: type[ContractModel], field: str
) -> None:
    data = fixture(name)
    assert field not in model.model_validate(data).model_dump(mode="json")
    validator = Draft202012Validator(model.model_json_schema())
    assert validator.is_valid(data)
    data[field] = None
    assert not validator.is_valid(data)


def test_valid_optional_refs_dimensions_and_review() -> None:
    region = fixture("region")
    region["geometry_ref"] = {
        "artifact_kind": "geometry_resource",
        "artifact_id": "synthetic.geometry",
        "version": "1.0.0",
        "digest": "b" * 64,
    }
    asset = fixture("asset")
    asset["dimensions"] = fixture("dimensions")
    capability = fixture("capability")
    capability["evaluation_ref"] = fixture("review")
    for model, data in (
        (RegionDefinition, region),
        (StaticAssetDescriptor, asset),
        (ExternalCapabilityRequirement, capability),
    ):
        result = model.model_validate(data)
        Draft202012Validator(model.model_json_schema()).validate(
            result.model_dump(mode="json")
        )
        assert model.model_validate_json(result.model_dump_json()) == result


def test_different_exact_versions_can_coexist_without_catalogue_claim() -> None:
    data = fixture("entity")
    refs = cast(list[dict[str, object]], data["attribute_schema_refs"])
    refs.append({**refs[0], "version": "2.0.0"})
    entity = EntityDefinition.model_validate(data)
    assert len(entity.attribute_schema_refs) == 2
    # Local shape validation deliberately does not assert member/domain existence.
    assert entity.entity_type.member == "synthetic"


def test_empty_reference_lists_and_asset_bytes_are_valid() -> None:
    entity = fixture("entity")
    entity["attribute_schema_refs"] = []
    region = fixture("region")
    region["asset_refs"] = []
    assert EntityDefinition.model_validate(entity).attribute_schema_refs == ()
    assert RegionDefinition.model_validate(region).asset_refs == ()
    assert StaticAssetDescriptor.model_validate(fixture("asset")).byte_size == 0


def test_valid_paths_and_mime_preserve_exact_text_without_filesystem_access() -> None:
    data = fixture("asset")
    data["data_path"] = "resources/assets/data/目录/space allowed.data"
    data["mime_type"] = "application/vnd.example+json"
    result = StaticAssetDescriptor.model_validate(data)
    Draft202012Validator(result.model_json_schema()).validate(data)
    assert result.data_path == data["data_path"]


def test_reference_collection_upper_bound_and_immutable_snapshot() -> None:
    data = fixture("entity")
    original = cast(list[dict[str, object]], data["attribute_schema_refs"])[0]
    data["attribute_schema_refs"] = [
        {**original, "version": f"1.0.{i}"} for i in range(1024)
    ]
    result = EntityDefinition.model_validate(data)
    cast(list[dict[str, object]], data["attribute_schema_refs"])[0]["version"] = "9.0.0"
    assert len(result.attribute_schema_refs) == 1024
    assert result.attribute_schema_refs[0].version == "1.0.0"


def test_utc_schema_requires_explicit_zero_offset() -> None:
    schema = TypeAdapter(UTCDateTime).json_schema()
    validator = Draft202012Validator(schema, format_checker=FormatChecker())
    assert validator.is_valid("2026-01-01T00:00:00Z")
    assert validator.is_valid("2026-01-01T00:00:00+00:00")
    for invalid in (
        "2026-01-01T00:00:00",
        "2026-01-01T00:00:00+08:00",
        "not-a-date",
        None,
    ):
        assert not validator.is_valid(invalid)
