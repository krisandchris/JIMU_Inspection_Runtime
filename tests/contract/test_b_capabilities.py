"""Local O1 models and generated Schema; finite catalogue admission is separate."""

import json
from pathlib import Path
from typing import cast

import pytest
from jsonschema import Draft202012Validator
from pydantic import TypeAdapter

from jimu_runtime.domain.base import ContractModel
from jimu_runtime.domain.definitions.capabilities import (
    FacetDefinition,
    FacetMetadataEntry,
    GroundingBinding,
    GroundingInputBinding,
    ModelGroundingBinding,
    ProfileDefinition,
    QuestionDefinition,
    ToolGroundingBinding,
)
from jimu_runtime.domain.definitions.prompts import (
    PromptTemplateDefinition,
    PromptVariable,
    PromptVariableName,
    TemplateText,
)

MODELS = [
    ("facet", FacetDefinition),
    ("question", QuestionDefinition),
    ("profile", ProfileDefinition),
    ("prompt", PromptTemplateDefinition),
    ("metadata", FacetMetadataEntry),
    ("variable", PromptVariable),
    ("model_grounding", ModelGroundingBinding),
    ("tool_grounding", ToolGroundingBinding),
    ("context_binding", GroundingInputBinding),
    ("image_binding", GroundingInputBinding),
]


def all_fixtures() -> dict[str, object]:
    path = Path(__file__).parents[1] / "fixtures/wp01b/o1/contracts.json"
    return cast(dict[str, object], json.loads(path.read_text()))


def fixture(name: str) -> dict[str, object]:
    return cast(dict[str, object], all_fixtures()[name])


@pytest.mark.parametrize("name,model", MODELS)
def test_generated_schema_strict_json_and_instance_roundtrip(
    name: str, model: type[ContractModel]
) -> None:
    schema = model.model_json_schema()
    Draft202012Validator.check_schema(schema)
    data = fixture(name)
    Draft202012Validator(schema).validate(data)
    result = model.model_validate(data)
    assert model.model_validate(result) == result
    assert model.model_validate_json(result.model_dump_json()) == result
    assert result.model_dump(mode="json") == data
    assert schema == model.model_json_schema()
    assert schema["additionalProperties"] is False


@pytest.mark.parametrize(
    "annotation", [GroundingBinding, PromptVariableName, TemplateText]
)
def test_union_and_alias_schemas(annotation: object) -> None:
    Draft202012Validator.check_schema(TypeAdapter(annotation).json_schema())


def test_profile_empty_catalogues_are_shapes_not_empty_runtime_success() -> None:
    profile = ProfileDefinition.model_validate(fixture("profile"))
    assert profile.question_refs == profile.facet_refs == profile.rule_set_refs == ()
    assert "input_bindings" not in profile.grounding_binding.model_dump(mode="json")
    assert not hasattr(profile, "coverage_status")


def test_facet_typed_metadata_and_multiple_members_in_one_vocabulary() -> None:
    data = fixture("facet")
    data["metadata"] = [fixture("metadata")]
    data["tags"] = all_fixtures()["tags"]
    facet = FacetDefinition.model_validate(data)
    assert facet.tags is not None and len(facet.tags) == 2
    assert facet.metadata is not None and facet.metadata[0].value.kind == "record"
    Draft202012Validator(facet.model_json_schema()).validate(data)
    assert FacetDefinition.model_validate(facet) == facet


@pytest.mark.parametrize(
    "name,model",
    [
        ("model_grounding", ModelGroundingBinding),
        ("tool_grounding", ToolGroundingBinding),
    ],
)
def test_grounding_accepts_declared_preobject_sources_only(
    name: str, model: type[ContractModel]
) -> None:
    data = fixture(name)
    data["input_bindings"] = [fixture("context_binding"), fixture("image_binding")]
    result = model.model_validate(data)
    Draft202012Validator(model.model_json_schema()).validate(data)
    assert model.model_validate(result) == result
    assert model.model_validate_json(result.model_dump_json()) == result


def test_model_and_tool_grounding_union_discriminator() -> None:
    adapter = TypeAdapter(GroundingBinding)
    assert isinstance(
        adapter.validate_python(fixture("model_grounding")), ModelGroundingBinding
    )
    assert isinstance(
        adapter.validate_python(fixture("tool_grounding")), ToolGroundingBinding
    )


def test_empty_explicit_optional_arrays_are_preserved() -> None:
    prompt = PromptTemplateDefinition.model_validate(
        {**fixture("prompt"), "partial_refs": []}
    )
    facet = FacetDefinition.model_validate({**fixture("facet"), "tags": []})
    binding = ModelGroundingBinding.model_validate(
        {**fixture("model_grounding"), "input_bindings": []}
    )
    assert prompt.model_dump(mode="json")["partial_refs"] == []
    assert facet.model_dump(mode="json")["tags"] == []
    assert binding.model_dump(mode="json")["input_bindings"] == []


def test_referenced_question_facets_are_not_resolved_by_local_shape() -> None:
    question = QuestionDefinition.model_validate(fixture("question"))
    assert question.primary_fact_key == "synthetic.primary"
    # The model has not asserted Schema scope, semantic meaning or Facet allowlist.
    assert len(question.facet_refs) == 1


def test_profile_reference_upper_bound_and_unique_versions() -> None:
    data = fixture("profile")
    data["question_refs"] = [
        {
            "artifact_kind": "question",
            "artifact_id": "synthetic",
            "version": f"1.0.{i}",
            "digest": "a" * 64,
        }
        for i in range(1024)
    ]
    profile = ProfileDefinition.model_validate(data)
    assert len(profile.question_refs) == 1024


def test_maximum_metadata_and_variable_lists_are_locally_valid() -> None:
    metadata = fixture("metadata")
    facet = FacetDefinition.model_validate(
        {
            **fixture("facet"),
            "metadata": [
                {**metadata, "namespace": f"synthetic.n{i}"} for i in range(64)
            ],
        }
    )
    variable = fixture("variable")
    prompt = PromptTemplateDefinition.model_validate(
        {
            **fixture("prompt"),
            "variables": [{**variable, "name": f"v{i}"} for i in range(1024)],
        }
    )
    assert facet.metadata is not None and len(facet.metadata) == 64
    assert len(prompt.variables) == 1024
    # Unused declaration and injected descriptor checks remain complete-graph work.


def test_grounding_context_preserves_declared_region_profile_views() -> None:
    for source in ("region_metadata", "profile_metadata"):
        binding = fixture("context_binding")
        cast(dict[str, object], cast(dict[str, object], binding["source"])["selector"])[
            "source"
        ] = source
        result = GroundingInputBinding.model_validate(binding)
        Draft202012Validator(result.model_json_schema()).validate(binding)
        assert result.source.source_kind == "context"
