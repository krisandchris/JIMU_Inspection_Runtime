"""Contract errors do not become routing defaults or inferred output aliases."""

import json
from pathlib import Path
from typing import cast

import pytest
from jsonschema import Draft202012Validator
from pydantic import ValidationError

from jimu_runtime.domain.definitions.models import ModelBinding
from jimu_runtime.domain.definitions.tools import ToolDefinition
from jimu_runtime.domain.tool_outputs import ToolFactOutputDeclaration


def fixture() -> dict[str, object]:
    return cast(
        dict[str, object],
        json.loads(
            (Path(__file__).parents[1] / "fixtures/wp01b/m1/contracts.json").read_text()
        ),
    )


@pytest.mark.parametrize(
    "mutation",
    ["no_prompt", "null_prompt", "duplicate_constraints", "extra_secret", "wrong_ref"],
)
def test_model_invalid(mutation: str) -> None:
    data = cast(dict[str, object], fixture()["model"])
    if mutation == "no_prompt":
        del data["prompt_template_ref"]
    elif mutation == "null_prompt":
        data["prompt_template_ref"] = None
    elif mutation == "duplicate_constraints":
        data["deployments"] = []
    elif mutation == "extra_secret":
        data["api_key"] = "synthetic-forbidden"
    else:
        cast(dict[str, object], data["provider_policy_ref"])["artifact_kind"] = "tool"
    with pytest.raises(ValidationError):
        ModelBinding.model_validate(data)
    assert not Draft202012Validator(ModelBinding.model_json_schema()).is_valid(data)


def test_auxiliary_prompt_may_be_absent_but_not_null() -> None:
    data = cast(dict[str, object], fixture()["model"])
    data["capability"] = "vision_auxiliary"
    del data["prompt_template_ref"]
    assert ModelBinding.model_validate(data).prompt_template_ref is None
    data["prompt_template_ref"] = None
    with pytest.raises(ValidationError):
        ModelBinding.model_validate(data)


@pytest.mark.parametrize(
    "mutation",
    [
        "duplicate_tag",
        "wrong_family",
        "duplicate_name",
        "duplicate_key",
        "unknown_type",
    ],
)
def test_tool_invalid(mutation: str) -> None:
    data = cast(dict[str, object], fixture()["tool"])
    if mutation == "duplicate_tag":
        data["capability_tags"] = ["geometry", "geometry"]
    elif mutation == "wrong_family":
        cast(dict[str, object], data["implementation_requirement"])["family"] = (
            "model_deployment"
        )
    elif mutation == "unknown_type":
        data["tool_type"] = "shell"
    else:
        first = cast(dict[str, object], fixture()["output"])
        second = dict(first)
        if mutation == "duplicate_name":
            second["fact_key"] = "different-key"
        else:
            second["output_name"] = "different-name"
        data["fact_outputs"] = [first, second]
    with pytest.raises(ValidationError):
        ToolDefinition.model_validate(data)


@pytest.mark.parametrize(
    "mutation",
    ["execution", "null_confidence", "bad_evidence_path", "duplicate_evidence"],
)
def test_output_invalid(mutation: str) -> None:
    data = cast(dict[str, object], fixture()["output"])
    if mutation == "execution":
        data["scope"] = "execution"
    elif mutation == "null_confidence":
        data["confidence_path"] = None
    elif mutation == "bad_evidence_path":
        data["evidence_paths"] = [["slot", "nested"]]
    else:
        data["evidence_paths"] = [["mask"], ["mask"]]
    with pytest.raises(ValidationError):
        ToolFactOutputDeclaration.model_validate(data)
