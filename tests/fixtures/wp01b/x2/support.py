"""Synthetic independent Schema registrations and full Producer context."""

import json
from pathlib import Path

from fixtures.wp01b.r1.support import registration

from jimu_runtime.domain.fact_schemas.models import FactSchemaDefinition
from jimu_runtime.domain.fact_schemas.registry import FactSchemaRegistryBuilder
from jimu_runtime.domain.validation.producer_inputs import (
    ProducerCatalogueRegistration,
    ProducerValidationContext,
)
from jimu_runtime.domain.validation.producers import validate_producer_catalogue


def fixture():
    return json.loads(Path(__file__).with_name("contracts.json").read_text())


def registry():
    builder = FactSchemaRegistryBuilder()
    for schema_id, scope in (("fixture", "object"), ("execution-schema", "execution")):
        ref, definition, _ = registration(schema_id=schema_id)
        raw = definition.model_dump(mode="json")
        raw.update(
            scope=scope,
            allowed_producer_kinds=["system"]
            if scope == "execution"
            else ["vlm_question", "tool", "system"],
        )
        builder.register(ref, FactSchemaDefinition.model_validate(raw))
    return builder.freeze()


def check(data):
    return validate_producer_catalogue(
        ProducerCatalogueRegistration.model_validate(data["registration"]),
        ProducerValidationContext.model_validate(data["context"]),
        fact_schemas=registry(),
    )


def codes(report):
    return {issue.code for check in report.checks for issue in check.issues}


def fact(data, name="question", status="known"):
    from pydantic import TypeAdapter

    from jimu_runtime.domain.runtime.facts import FactRecord

    old = json.loads((Path(__file__).parents[1] / "a1/contracts.json").read_text())
    raw = old[
        "tool_fact"
        if name == "tool"
        else "system_fact"
        if name.startswith("system")
        else status
    ]
    declaration = next(
        item
        for item in data["registration"]["definition"]["declarations"]
        if item["declaration_id"] == data[name]["declaration_id"]
    )
    raw.update(
        producer_catalogue_ref=data["registration"]["ref"],
        producer_declaration_id=declaration["declaration_id"],
        fact_key=declaration["fact_key"],
        fact_schema_ref=declaration["fact_schema_ref"],
        scope=declaration["scope"],
        producer_kind=declaration["producer_kind"],
        producer_id=declaration["producer_id"],
        producer_version=declaration["producer_version"],
    )
    if declaration["scope"] == "object":
        raw.update(profile_ref=declaration["profile_ref"], object_id="object-1")
    else:
        raw.pop("profile_ref", None)
        raw.pop("object_id", None)
    if name.startswith("system"):
        raw["source"].update(
            requirement=declaration["producer"]["requirement"],
            output_name=declaration["output_name"],
        )
    if name == "tool":
        raw["source"].update(output_name=declaration["output_name"])
    return TypeAdapter(FactRecord).validate_json(json.dumps(raw))


def condition_use(data, declaration):
    result = dict(
        use_role="producer_activation",
        producer_catalogue_ref=data["registration"]["ref"],
        declaration_id=declaration["declaration_id"],
        producer_kind=declaration["producer_kind"],
        producer_scope=declaration["scope"],
        context_registrations=[],
        descriptors=[],
    )
    if declaration["scope"] == "object":
        result["profile_ref"] = declaration["profile_ref"]
    if declaration["producer_kind"] == "tool":
        result.update(
            tool_plan_ref=declaration["producer"]["tool_plan_ref"],
            step_id=declaration["producer"]["step_id"],
        )
    return result


def local_setup():
    from copy import deepcopy

    data = fixture()
    declarations = data["registration"]["definition"]["declarations"]
    consumer = declarations[1]
    ancestor = deepcopy(consumer)
    ancestor.update(declaration_id="tool.ancestor", fact_key="ancestor.state")
    ancestor["producer"]["step_id"] = "ancestor"
    ancestor["producer"]["tool_ref"]["artifact_id"] = "ancestor-tool"
    ancestor["producer_id"] = "ancestor-tool"
    ancestor["activation_condition"] = {"mode": "always"}
    declarations.append(ancestor)
    tool = deepcopy(data["context"]["tools"][0])
    tool["ref"]["artifact_id"] = "ancestor-tool"
    tool["definition"]["tool_id"] = "ancestor-tool"
    tool["definition"]["fact_outputs"][0]["fact_key"] = "ancestor.state"
    data["context"]["tools"].append(tool)
    steps = data["context"]["plans"][0]["definition"]["steps"]
    prior = deepcopy(steps[0])
    prior.update(step_id="ancestor", tool_ref=tool["ref"], depends_on=[])
    prior["fact_output_mappings"][0]["target_fact_key"] = "ancestor.state"
    policy = deepcopy(data["context"]["policies"][0])
    policy["ref"]["artifact_id"] = "ancestor-skip"
    policy["artifact"]["policy_id"] = "ancestor-skip"
    policy["artifact"]["policy"]["condition_ast"] = True
    data["context"]["policies"].append(policy)
    prior["skip_policy_ref"] = policy["ref"]
    steps[0]["depends_on"] = ["ancestor"]
    steps.insert(0, prior)
    expression = dict(
        kind="local_tool_fact_ref",
        selector=dict(
            scope="object",
            step_id="ancestor",
            tool_ref=tool["ref"],
            output_name="relation",
            fact_key="ancestor.state",
            fact_schema_ref=consumer["fact_schema_ref"],
            field_path=[],
        ),
    )
    consumer["activation_condition"] = {
        "mode": "tool_condition",
        "condition_ast": expression,
    }
    data["context"]["policies"][0]["artifact"]["policy"]["condition_ast"] = deepcopy(
        expression
    )
    data["context"]["condition_contexts"] = [condition_use(data, consumer)]
    return data
