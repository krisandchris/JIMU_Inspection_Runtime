"""O1 shape rejections, including generated Schema and instance-taint regressions."""

import json
from pathlib import Path
from typing import cast

import pytest
from jsonschema import Draft202012Validator
from pydantic import ValidationError

from jimu_runtime.domain.base import ContractModel
from jimu_runtime.domain.definitions.capabilities import (
    FacetDefinition,
    GroundingInputBinding,
    ModelGroundingBinding,
    ProfileDefinition,
    QuestionDefinition,
    ToolGroundingBinding,
)
from jimu_runtime.domain.definitions.prompts import (
    PromptTemplateDefinition,
    PromptVariable,
)
from jimu_runtime.domain.tool_mappings import InputBinding

ROOTS = [
    ("facet", FacetDefinition),
    ("question", QuestionDefinition),
    ("profile", ProfileDefinition),
    ("prompt", PromptTemplateDefinition),
]


def fixture(name: str) -> dict[str, object]:
    path = Path(__file__).parents[1] / "fixtures/wp01b/o1/contracts.json"
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
def test_version_and_forbidden_runtime_reverse_fields(
    name: str, model: type[ContractModel]
) -> None:
    data = fixture(name)
    del data["schema_version"]
    reject(model, data)
    reject(model, {**fixture(name), "schema_version": "2.0.0"})
    for key in ("object_id", "profile_refs", "decision", "digest", "bundle_digest"):
        reject(model, {**fixture(name), key: "forbidden"})


@pytest.mark.parametrize(
    "name,model,field",
    [
        ("facet", FacetDefinition, "metadata"),
        ("facet", FacetDefinition, "tags"),
        ("prompt", PromptTemplateDefinition, "partial_refs"),
        ("model_grounding", ModelGroundingBinding, "input_bindings"),
        ("tool_grounding", ToolGroundingBinding, "input_bindings"),
    ],
)
def test_null_optional_field_rejected_by_both_validators(
    name: str, model: type[ContractModel], field: str
) -> None:
    reject(model, {**fixture(name), field: None})


@pytest.mark.parametrize("field", ["fact_schema_refs", "facet_refs"])
def test_approved_nonempty_arrays_remain_nonempty(field: str) -> None:
    name, model = (
        ("facet", FacetDefinition)
        if field == "fact_schema_refs"
        else ("question", QuestionDefinition)
    )
    reject(model, {**fixture(name), field: []})


@pytest.mark.parametrize(
    "name,model,field",
    [
        ("facet", FacetDefinition, "fact_schema_refs"),
        ("question", QuestionDefinition, "facet_refs"),
        ("profile", ProfileDefinition, "question_refs"),
        ("prompt", PromptTemplateDefinition, "partial_refs"),
    ],
)
@pytest.mark.parametrize("digest", ["a" * 64, "b" * 64])
def test_duplicate_ref_identity_with_same_or_conflicting_digest(
    name: str, model: type[ContractModel], field: str, digest: str
) -> None:
    data = fixture(name)
    if field in ("question_refs", "partial_refs"):
        kind = "question" if field == "question_refs" else "prompt_template"
        data[field] = [
            {
                "artifact_kind": kind,
                "artifact_id": "synthetic",
                "version": "1.0.0",
                "digest": "a" * 64,
            }
        ]
    refs = cast(list[dict[str, object]], data[field])
    refs.append({**refs[0], "digest": digest})
    reject(model, data, schema_too=digest == "a" * 64)


@pytest.mark.parametrize(
    "field,kind",
    [
        ("prompt_template_ref", "response_schema"),
        ("primary_fact_schema_ref", "value_schema"),
        ("evidence_policy_ref", "question"),
    ],
)
def test_question_expected_kinds_and_contentref_wire(field: str, kind: str) -> None:
    data = fixture("question")
    cast(dict[str, object], data[field])["artifact_kind"] = kind
    reject(QuestionDefinition, data)
    data = fixture("question")
    cast(dict[str, object], data[field])["schema_dialect"] = (
        "https://json-schema.org/draft/2020-12/schema"
    )
    reject(QuestionDefinition, data)


def test_facet_metadata_namespace_and_tag_uniqueness() -> None:
    entry = fixture("metadata")
    data = {
        **fixture("facet"),
        "metadata": [entry, {**entry, "value": {"kind": "record", "value": {}}}],
    }
    reject(FacetDefinition, data, schema_too=False)
    reject(FacetDefinition, {**fixture("facet"), "metadata": []})
    tag = {
        "vocabulary_ref": {
            "artifact_kind": "vocabulary",
            "artifact_id": "tags",
            "version": "1.0.0",
            "digest": "a" * 64,
        },
        "member": "synthetic",
    }
    reject(FacetDefinition, {**fixture("facet"), "tags": [tag, tag]})
    reject(
        FacetDefinition,
        {**fixture("facet"), "metadata": [{**entry, "value": {"free": "json"}}]},
    )


def test_question_context_selectors_are_unique() -> None:
    data = fixture("question")
    contexts = cast(list[dict[str, object]], data["context_allowlist"])
    contexts.append(contexts[0])
    reject(QuestionDefinition, data)


@pytest.mark.parametrize("source_kind", ["object", "fact", "step_output", "decision"])
@pytest.mark.parametrize(
    "name,model",
    [
        ("model_grounding", ModelGroundingBinding),
        ("tool_grounding", ToolGroundingBinding),
    ],
)
def test_grounding_source_union_schema_really_excludes_postobject_sources(
    source_kind: str, name: str, model: type[ContractModel]
) -> None:
    binding = fixture("image_binding")
    cast(dict[str, object], binding["source"])["source_kind"] = source_kind
    reject(model, {**fixture(name), "input_bindings": [binding]})


def test_required_grounding_input_blocks_in_both_validators() -> None:
    reject(GroundingInputBinding, {**fixture("context_binding"), "on_missing": "omit"})


def test_grounding_overlapping_targets_reject_without_descriptor_claim() -> None:
    a, b = fixture("context_binding"), fixture("image_binding")
    b["target"] = a["target"]
    reject(
        ModelGroundingBinding,
        {**fixture("model_grounding"), "input_bindings": [a, b]},
        schema_too=False,
    )
    a, b = fixture("context_binding"), fixture("image_binding")
    cast(dict[str, object], b["target"])["field_path"] = []
    reject(
        ModelGroundingBinding,
        {**fixture("model_grounding"), "input_bindings": [a, b]},
        schema_too=False,
    )


@pytest.mark.parametrize(
    "invalid",
    [
        "{}",
        "{name.x}",
        "{x[0]}",
        "{x!r}",
        "{x:format}",
        "{中文}",
        "{",
        "}",
        "x" * 65537,
    ],
)
def test_literal_grammar_matches_generated_schema(invalid: str) -> None:
    reject(PromptTemplateDefinition, {**fixture("prompt"), "template_text": invalid})


@pytest.mark.parametrize(
    "required,on_missing",
    [(True, "omit"), (False, "block"), (1, "block"), ("true", "block")],
)
def test_prompt_variable_pairing_and_bool_strictness(
    required: object, on_missing: str
) -> None:
    reject(
        PromptVariable,
        {**fixture("variable"), "required": required, "on_missing": on_missing},
    )


@pytest.mark.parametrize(
    "source,kind",
    [
        ("context", "attribute_schema"),
        ("profile_metadata", "context_schema"),
        ("question_metadata", "object_schema"),
        ("object_geometry", "attribute_schema"),
        ("image_reference", "tool_input_schema"),
    ],
)
def test_prompt_variable_source_kind_pair_is_schema_visible(
    source: str, kind: str
) -> None:
    data = fixture("variable")
    data["source"] = source
    cast(dict[str, object], data["schema_ref"])["artifact_kind"] = kind
    reject(PromptVariable, data)


@pytest.mark.parametrize(
    "name", ["", "0name", "a.b", "a-b", "中文", "a" * 129, "name\n"]
)
def test_prompt_variable_ascii_identifier_schema(name: str) -> None:
    reject(PromptVariable, {**fixture("variable"), "name": name})


def test_prompt_variable_names_unique_even_when_declarations_differ() -> None:
    data = fixture("prompt")
    variables = cast(list[dict[str, object]], data["variables"])
    variables.append({**variables[0], "field_path": ["another"]})
    reject(PromptTemplateDefinition, data, schema_too=False)
    reject(
        PromptTemplateDefinition, {**fixture("prompt"), "purpose": "vision_auxiliary"}
    )


@pytest.mark.parametrize("name,model", ROOTS)
def test_copied_and_nested_instance_taint_is_not_discarded(
    name: str, model: type[ContractModel]
) -> None:
    valid = model.model_validate(fixture(name))
    tainted = valid.model_copy(update={"injected_field": "must reject"})
    with pytest.raises(ValidationError):
        model.model_validate(tainted)
    if model is ProfileDefinition:
        profile = cast(ProfileDefinition, valid)
        nested = profile.grounding_binding.model_copy(
            update={"injected_field": "must reject"}
        )
        with pytest.raises(ValidationError):
            ProfileDefinition.model_validate(
                profile.model_copy(update={"grounding_binding": nested})
            )


def test_ref_collections_and_input_bindings_are_bounded() -> None:
    data = fixture("profile")
    data["question_refs"] = [
        {
            "artifact_kind": "question",
            "artifact_id": "synthetic",
            "version": f"1.0.{i}",
            "digest": "a" * 64,
        }
        for i in range(1025)
    ]
    reject(ProfileDefinition, data)
    reject(
        ModelGroundingBinding,
        {
            **fixture("model_grounding"),
            "input_bindings": [fixture("context_binding")] * 65,
        },
    )


@pytest.mark.parametrize("source_kind", ["object", "fact", "step_output"])
def test_grounding_rejects_otherwise_valid_generic_input_sources(
    source_kind: str,
) -> None:
    binding = fixture("image_binding")
    schema_ref = {
        "artifact_kind": "object_schema",
        "artifact_id": "synthetic",
        "version": "1.0.0",
        "digest": "a" * 64,
        "schema_dialect": "https://json-schema.org/draft/2020-12/schema",
    }
    if source_kind == "fact":
        source = {
            "source_kind": "fact",
            "selector": {
                "scope": "object",
                "fact_key": "synthetic.fact",
                "field_path": [],
                "fact_schema_ref": {
                    "artifact_kind": "fact_schema",
                    "artifact_id": "primary",
                    "version": "1.0.0",
                    "digest": "a" * 64,
                },
                "producer_selector": {
                    "producer_kind": "vlm_question",
                    "question_ref": {
                        "artifact_kind": "question",
                        "artifact_id": "synthetic.question",
                        "version": "1.0.0",
                        "digest": "a" * 64,
                    },
                    "output_name": "primary",
                },
            },
        }
    elif source_kind == "step_output":
        source = {
            "source_kind": "step_output",
            "step_id": "ancestor",
            "selector": {
                "schema_ref": {**schema_ref, "artifact_kind": "tool_output_schema"},
                "field_path": [],
            },
        }
    else:
        source = {
            "source_kind": "object",
            "selector": {"schema_ref": schema_ref, "field_path": []},
        }
    binding["source"] = source
    assert InputBinding.model_validate(binding).source.source_kind == source_kind
    Draft202012Validator(InputBinding.model_json_schema()).validate(binding)
    reject(GroundingInputBinding, binding)


@pytest.mark.parametrize(
    "name,model,field,wrong_kind",
    [
        ("profile", ProfileDefinition, "entity_ref", "profile"),
        ("profile", ProfileDefinition, "discovery_binding_ref", "tool"),
        ("profile", ProfileDefinition, "observation_binding_ref", "policy"),
        ("profile", ProfileDefinition, "tool_plan_ref", "tool"),
        ("profile", ProfileDefinition, "question_selection_policy_ref", "question"),
        ("profile", ProfileDefinition, "applicability_policy_ref", "rule"),
        ("prompt", PromptTemplateDefinition, "response_schema_ref", "attribute_schema"),
    ],
)
def test_expected_kinds_for_direct_profile_and_prompt_refs(
    name: str, model: type[ContractModel], field: str, wrong_kind: str
) -> None:
    data = fixture(name)
    cast(dict[str, object], data[field])["artifact_kind"] = wrong_kind
    reject(model, data)


def test_prompt_nested_variable_instance_taint_rejected() -> None:
    valid = PromptTemplateDefinition.model_validate(fixture("prompt"))
    tainted_variable = valid.variables[0].model_copy(update={"injected": "must reject"})
    tainted_prompt = valid.model_copy(
        update={"variables": (tainted_variable, *valid.variables[1:])}
    )
    with pytest.raises(ValidationError):
        PromptTemplateDefinition.model_validate(tainted_prompt)


def test_constructed_invalid_optional_and_unsupported_question_output_slot() -> None:
    valid = PromptTemplateDefinition.model_validate(fixture("prompt"))
    with pytest.raises(ValidationError):
        PromptTemplateDefinition.model_validate(
            valid.model_copy(update={"partial_refs": None})
        )
    reject(
        QuestionDefinition,
        {
            **fixture("question"),
            "secondary_fact_schema_ref": fixture("question")["primary_fact_schema_ref"],
        },
    )


def test_metadata_and_variables_upper_limits_rejected() -> None:
    entry = fixture("metadata")
    reject(
        FacetDefinition,
        {
            **fixture("facet"),
            "metadata": [{**entry, "namespace": f"synthetic.n{i}"} for i in range(65)],
        },
    )
    variable = fixture("variable")
    reject(
        PromptTemplateDefinition,
        {
            **fixture("prompt"),
            "variables": [{**variable, "name": f"v{i}"} for i in range(1025)],
        },
    )


def test_only_list_tuple_arrays_accepted_without_generator_coercion() -> None:
    for model, name, field in (
        (ProfileDefinition, "profile", "question_refs"),
        (PromptTemplateDefinition, "prompt", "variables"),
        (FacetDefinition, "facet", "fact_schema_refs"),
    ):
        data = fixture(name)
        for invalid in (set(), iter([])):
            with pytest.raises(ValidationError):
                model.model_validate({**data, field: invalid})
