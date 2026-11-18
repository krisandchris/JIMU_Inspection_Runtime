"""Isolated synthetic V1 finite relation inputs, with no external content trust claim."""

import json
from copy import deepcopy
from pathlib import Path

from jimu_runtime.domain.fact_schemas.models import FactSchemaDefinition
from jimu_runtime.domain.fact_schemas.registry import FactSchemaRegistryBuilder
from jimu_runtime.domain.references import ContentRef
from jimu_runtime.domain.validation.definition_inputs import DefinitionValidationInput
from jimu_runtime.domain.validation.definitions import validate_definition_relations


def fixture():
    return json.loads(Path(__file__).with_name("contracts.json").read_text())


def entry(data, kind, name=None):
    return next(
        item
        for item in data["input"]["definitions"]
        if item["registration_kind"] == kind
        and (name is None or item["ref"]["artifact_id"] == name)
    )


def registry(data):
    builder = FactSchemaRegistryBuilder()
    for item in data["fact_schemas"]:
        builder.register(
            ContentRef.model_validate(item["ref"]),
            FactSchemaDefinition.model_validate(item["definition"]),
        )
    return builder.freeze()


def inputs(data):
    return DefinitionValidationInput.model_validate_json(json.dumps(data["input"]))


def report(data):
    return validate_definition_relations(inputs(data), registry(data))


def codes(result):
    return {issue.code for check in result.checks for issue in check.issues}


def variable(
    name="scene",
    source="context",
    kind="context_schema",
    schema_id="context",
    path=None,
):
    return {
        "name": name,
        "source": source,
        "field_path": ["scene"] if path is None else path,
        "schema_ref": {
            "artifact_kind": kind,
            "artifact_id": schema_id,
            "version": "1.0.0",
            "digest": "a" * 64,
            "schema_dialect": "https://json-schema.org/draft/2020-12/schema",
        },
        "required": True,
        "on_missing": "block",
    }


def prompt_with_variable(data, prompt_name="prompt-question", **kwargs):
    prompt = entry(data, "prompt_template", prompt_name)["definition"]
    item = variable(**kwargs)
    prompt["variables"] = [item]
    prompt["template_text"] = "{" + item["name"] + "}"
    return prompt


def with_step(data):
    use = data["input"]["use_contexts"][0]
    profile = entry(data, "profile")["definition"]
    mapping = deepcopy(profile["grounding_binding"]["input_bindings"][0])
    plan = entry(data, "tool_plan")["definition"]
    plan["steps"] = [
        {
            "step_id": "step",
            "tool_ref": entry(data, "tool")["ref"],
            "depends_on": [],
            "input_bindings": [mapping],
            "fact_output_mappings": [],
            "requirement": "required",
            "failure_policy_ref": {
                "artifact_kind": "policy",
                "artifact_id": "failure",
                "version": "1.0.0",
                "digest": "a" * 64,
            },
            "skip_policy_ref": {
                "artifact_kind": "policy",
                "artifact_id": "external-skip",
                "version": "1.0.0",
                "digest": "a" * 64,
            },
        }
    ]
    # ToolSkip content is now mandatory; preserve the existing Plan assertions.
    skip_ref = deepcopy(plan["steps"][0]["skip_policy_ref"])
    data["input"]["policies"].append(
        {
            "ref": skip_ref,
            "artifact": {
                "schema_version": "1.0.0",
                "policy_id": skip_ref["artifact_id"],
                "version": skip_ref["version"],
                "policy": {
                    "policy_kind": "tool_skip",
                    "condition_ast": True,
                    "on_false": "skip_with_reason",
                    "on_unknown": "block",
                    "on_missing_input": "block",
                },
            },
        }
    )
    use["mapping_contexts"] = [
        {
            "profile_ref": deepcopy(use["profile_ref"]),
            "tool_plan_ref": deepcopy(profile["tool_plan_ref"]),
            "current_step_id": "step",
            "context_registrations": deepcopy(use["context_registrations"]),
            "object_schema_ref": deepcopy(use["object_geometry_schema_ref"]),
            "image_schema_ref": deepcopy(use["image_reference_schema_ref"]),
            "descriptors": deepcopy(data["input"]["descriptors"]),
        }
    ]
    return data
