"""Literal syntax only: no render, variable resolution or partial compilation."""

import json
from pathlib import Path
from typing import cast

import pytest
from pydantic import TypeAdapter, ValidationError

from jimu_runtime.domain.definitions.prompts import (
    PromptTemplateDefinition,
    PromptVariable,
    PromptVariableName,
    TemplateText,
    placeholder_names,
)


def fixture(name: str) -> dict[str, object]:
    path = Path(__file__).parents[1] / "fixtures/wp01b/o1/contracts.json"
    return cast(dict[str, dict[str, object]], json.loads(path.read_text()))[name]


@pytest.mark.parametrize(
    "text,names",
    [
        ("literal text", ()),
        ("{{", ()),
        ("}}", ()),
        ("{{scene}}", ()),
        ("{{{scene}}}", ("scene",)),
        ("{scene} {scene}", ("scene", "scene")),
        ("{_name1}\n{Z}", ("_name1", "Z")),
        ("中文 {scene}", ("scene",)),
        (" \n ", ()),
        ("{{}}", ()),
    ],
)
def test_placeholder_syntax_and_order(text: str, names: tuple[str, ...]) -> None:
    assert placeholder_names(text) == names
    assert TypeAdapter(TemplateText).validate_python(text) == text


@pytest.mark.parametrize(
    "text",
    [
        "",
        "{",
        "}",
        "{}",
        "{0}",
        "{a.b}",
        "{a[0]}",
        "{x!r}",
        "{x:format}",
        "{a b}",
        "{中文}",
        "{x()}",
        "{{{",
        "}}}",
        "{" + "x" * 129 + "}",
        "x" * 65537,
    ],
)
def test_invalid_syntax_is_rejected(text: str) -> None:
    with pytest.raises(ValidationError):
        placeholder_names(text)


def test_literal_and_identifier_upper_bound() -> None:
    assert len(TypeAdapter(TemplateText).validate_python("x" * 65536)) == 65536
    name = "A" + "0" * 127
    assert TypeAdapter(PromptVariableName).validate_python(name) == name
    assert placeholder_names("{" + name + "}") == (name,)


def test_required_pair_and_source_schema_are_strict() -> None:
    variable = PromptVariable.model_validate(fixture("variable"))
    assert variable.required is True
    image = PromptVariable.model_validate(fixture("image_variable"))
    assert image.required is False and image.on_missing == "omit"
    for invalid in (1, 0, "true", None):
        with pytest.raises(ValidationError):
            PromptVariable.model_validate({**fixture("variable"), "required": invalid})


def test_prompt_preserves_source_and_revalidates_immutable_instances() -> None:
    data = fixture("prompt")
    prompt = PromptTemplateDefinition.model_validate(data)
    assert placeholder_names(prompt.template_text) == ("scene", "scene", "image")
    assert PromptTemplateDefinition.model_validate(prompt) == prompt
    cast(list[dict[str, object]], data["variables"])[0]["name"] = "mutated"
    assert prompt.variables[0].name == "scene"
    with pytest.raises(ValidationError):
        prompt.template_text = "mutated"


def test_root_shape_does_not_resolve_partial_or_registration() -> None:
    data = fixture("prompt")
    data["variables"] = []
    data["partial_refs"] = [
        {
            "artifact_kind": "prompt_template",
            "artifact_id": "other",
            "version": "1.0.0",
            "digest": "b" * 64,
        }
    ]
    prompt = PromptTemplateDefinition.model_validate(data)
    # Actual declarations may be supplied by a partial. Only D-owned injected
    # relation validation can establish scope, references, cycles and allowlists.
    assert placeholder_names(prompt.template_text) == ("scene", "scene", "image")
    assert prompt.variables == ()
