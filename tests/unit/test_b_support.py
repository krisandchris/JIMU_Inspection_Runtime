"""S1 shared contracts: strict identities, immutable arrays and budgets."""

import json
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from typing import cast

import pytest
from pydantic import TypeAdapter, ValidationError

from jimu_runtime.domain.definition_enums import (
    CapabilityFamily,
    CapabilityFeature,
    ContextSource,
    FactScope,
    ModelCapability,
    ProducerKind,
)
from jimu_runtime.domain.definition_support import (
    ContextFieldSelector,
    Description,
    ExternalCapabilityRequirement,
    FieldPath,
    NonNegativeBudget,
    PositiveBudget,
    ShortText,
    UTCDateTime,
    VersionConstraint,
    reference_identity,
)
from jimu_runtime.domain.enums import ArtifactKind
from jimu_runtime.domain.references import ContentRef, SchemaRef


def fixture(name: str) -> dict[str, object]:
    path = Path(__file__).parents[1] / "fixtures/wp01b/s1/contracts.json"
    return cast(dict[str, dict[str, object]], json.loads(path.read_text()))[name]


@pytest.mark.parametrize("annotation,maximum", [(ShortText, 128), (Description, 4096)])
def test_text_boundaries(annotation: object, maximum: int) -> None:
    adapter = TypeAdapter(annotation)
    assert adapter.validate_python(" a ") == " a "
    assert adapter.validate_python("x" * maximum) == "x" * maximum
    for invalid in ("", "  \t", "x" * (maximum + 1), 3, True, None):
        with pytest.raises(ValidationError):
            adapter.validate_python(invalid)


@pytest.mark.parametrize(
    "annotation,minimum", [(PositiveBudget, 1), (NonNegativeBudget, 0)]
)
def test_budget_boundaries(annotation: object, minimum: int) -> None:
    adapter = TypeAdapter(annotation)
    assert adapter.validate_python(minimum) == minimum
    assert adapter.validate_python(2**63 - 1) == 2**63 - 1
    for invalid in (minimum - 1, 2**63, True, 1.0, "1", None):
        with pytest.raises(ValidationError):
            adapter.validate_python(invalid)


def test_utc_no_clock_or_naive_coercion() -> None:
    adapter = TypeAdapter(UTCDateTime)
    timestamp = datetime(2026, 1, 1, tzinfo=UTC)
    assert adapter.validate_python(timestamp) == timestamp
    assert adapter.validate_json('"2026-01-01T00:00:00Z"') == timestamp
    for invalid in (
        timestamp.replace(tzinfo=None),
        timestamp.astimezone(timezone(timedelta(hours=8))),
        "2026-01-01T00:00:00Z",
        0,
    ):
        with pytest.raises(ValidationError):
            adapter.validate_python(invalid)


def test_selector_and_fields_are_exact_immutable_tuples() -> None:
    data = fixture("selector")
    selector = ContextFieldSelector.model_validate(data)
    cast(list[str], data["field_path"])[0] = "mutated"
    assert selector.field_path == ("scene", "a.b")
    assert selector.source is ContextSource.CONTEXT_SNAPSHOT
    with pytest.raises(ValidationError):
        selector.source = ContextSource.REGION_METADATA
    adapter = TypeAdapter(FieldPath)
    assert adapter.validate_python([]) == ()
    assert adapter.validate_python(["a"] * 8) == ("a",) * 8
    for invalid in (["a"] * 9, [""], ["x" * 129], {"a"}, iter(["a"]), "a"):
        with pytest.raises(ValidationError):
            adapter.validate_python(invalid)


def test_reference_identity_preserves_content_not_model_class() -> None:
    schema = ContextFieldSelector.model_validate(fixture("selector")).schema_ref
    content = ContentRef.model_validate(
        {
            key: value
            for key, value in schema.model_dump(mode="json").items()
            if key != "schema_dialect"
        }
    )
    assert isinstance(schema, SchemaRef)
    assert reference_identity(content) == reference_identity(schema)
    assert reference_identity(schema) == (
        ArtifactKind.CONTEXT_SCHEMA,
        "test",
        "1.0.0",
        "a" * 64,
    )


def test_capability_versions_features_copy_and_freeze() -> None:
    data = fixture("capability")
    result = ExternalCapabilityRequirement.model_validate(data)
    cast(list[str], data["required_features"]).append("ocr_text")
    assert result.required_features == (CapabilityFeature.RGB_IMAGE,)
    assert result.allowed_implementation_versions == ("1.0.0",)
    assert "evaluation_ref" not in result.model_dump(mode="json")
    versions = VersionConstraint.model_validate(fixture("versions"))
    assert versions.allowed_model_versions == ("2.0.0",)


@pytest.mark.parametrize(
    "enum,expected",
    [
        (FactScope, {"object", "execution"}),
        (ProducerKind, {"vlm_question", "tool", "system"}),
        (
            ModelCapability,
            {
                "profile_discovery",
                "object_grounding",
                "object_observation",
                "candidate_prediction",
                "vision_auxiliary",
            },
        ),
        (
            CapabilityFamily,
            {
                "model_deployment",
                "tool_implementation",
                "system_fact_producer",
                "context_derivation",
            },
        ),
        (
            CapabilityFeature,
            {
                "rgb_image",
                "typed_context",
                "structured_output",
                "original_xyxy",
                "mask_output",
                "geometry_output",
                "ocr_text",
            },
        ),
        (ContextSource, {"context_snapshot", "region_metadata", "profile_metadata"}),
    ],
)
def test_closed_enum_values(enum: type, expected: set[str]) -> None:
    assert {value.value for value in enum} == expected
    adapter = TypeAdapter(enum)
    with pytest.raises(ValidationError):
        adapter.validate_python("unregistered")


def test_revalidating_absence_does_not_allow_explicit_constructed_null() -> None:
    valid = ExternalCapabilityRequirement.model_validate(fixture("capability"))
    assert ExternalCapabilityRequirement.model_validate(valid) == valid
    invalid = valid.model_copy(update={"evaluation_ref": None})
    with pytest.raises(ValidationError):
        ExternalCapabilityRequirement.model_validate(invalid)


def test_capability_accepts_all_declared_features_and_maximum_versions() -> None:
    data = fixture("capability")
    data["required_features"] = [feature.value for feature in CapabilityFeature]
    data["allowed_implementation_versions"] = [f"1.0.{i}" for i in range(1024)]
    result = ExternalCapabilityRequirement.model_validate(data)
    assert len(result.required_features) == 7
    assert len(result.allowed_implementation_versions) == 1024


def test_nested_models_reject_mutation_and_copy_source_arrays() -> None:
    data = fixture("versions")
    result = VersionConstraint.model_validate(data)
    cast(list[str], data["allowed_model_versions"])[0] = "9.0.0"
    assert result.allowed_model_versions == ("2.0.0",)
    with pytest.raises(ValidationError):
        result.allowed_model_versions = ("3.0.0",)
