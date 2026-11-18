"""Finite synthetic owner directories; no authenticated content or predicate truth."""

import json
from copy import deepcopy

from fixtures.wp01b.v1.support import entry, fixture, inputs, registry, with_step
from pydantic import TypeAdapter

from jimu_runtime.domain.conditions import validate_condition_data
from jimu_runtime.domain.definition_enums import ConditionInputDomain
from jimu_runtime.domain.validation.authority_inputs import (
    AuthorityQuestionInput,
    AuthorityToolPlanInput,
)
from jimu_runtime.domain.validation.condition_inputs import ConditionReferenceContext
from jimu_runtime.domain.validation.conditions import validate_condition_references
from jimu_runtime.domain.validation.definition_inputs import ProfileRegistration
from jimu_runtime.domain.validation.tool_plans import ToolRegistration


def setup():
    data = with_step(fixture())
    data["fact_schemas"][0]["definition"]["allowed_producer_kinds"] = [
        "vlm_question",
        "tool",
    ]
    tool = entry(data, "tool")["definition"]
    schema = data["fact_schemas"][0]["ref"]
    tool["fact_outputs"] = [
        dict(
            output_name="signal",
            fact_key="primary",
            fact_schema_ref=deepcopy(schema),
            scope="object",
            required=True,
            value_path=[],
        )
    ]
    steps = entry(data, "tool_plan")["definition"]["steps"]
    first = steps[0]
    first["step_id"] = "first"
    first["fact_output_mappings"] = [
        dict(output_name="signal", target_fact_key="primary")
    ]
    second = deepcopy(first)
    second["step_id"] = "second"
    second["depends_on"] = ["first"]
    third = deepcopy(first)
    third["step_id"] = "third"
    third["depends_on"] = ["second"]
    steps[:] = [first, second, third]
    use = data["input"]["use_contexts"][0]
    context = dict(
        use_role="tool_step_guard",
        profile_ref=use["profile_ref"],
        policy_ref=first["skip_policy_ref"],
        tool_plan_ref=entry(data, "tool_plan")["ref"],
        step_id="third",
        context_registrations=use["context_registrations"],
        descriptors=data["input"]["descriptors"],
        region_ref=use["region_ref"],
    )
    local = dict(
        kind="local_tool_fact_ref",
        selector=dict(
            scope="object",
            step_id="first",
            tool_ref=entry(data, "tool")["ref"],
            output_name="signal",
            fact_key="primary",
            fact_schema_ref=schema,
            field_path=[],
        ),
    )
    return data, deepcopy(context), deepcopy(local)


def context(raw):
    return TypeAdapter(ConditionReferenceContext).validate_json(json.dumps(raw))


def bind(data, raw, condition, allowlist=None):
    model = inputs(data)
    role = raw.use_role if not isinstance(raw, dict) else raw["use_role"]
    domain = (
        ConditionInputDomain.RULE_APPLICABILITY
        if role == "applicability"
        else ConditionInputDomain.TOOL_CONDITION
    )
    tree = (
        True
        if condition is True
        else validate_condition_data(condition, input_domain=domain)
    )
    return validate_condition_references(
        tree,
        context(raw) if isinstance(raw, dict) else raw,
        input_domain=domain,
        profiles=tuple(
            x for x in model.definitions if isinstance(x, ProfileRegistration)
        ),
        questions=tuple(
            AuthorityQuestionInput(ref=x.ref, definition=x.definition)
            for x in model.definitions
            if x.registration_kind == "question"
        ),
        tools=tuple(
            ToolRegistration(ref=x.ref, definition=x.definition)
            for x in model.definitions
            if x.registration_kind == "tool"
        ),
        plans=tuple(
            AuthorityToolPlanInput(ref=x.ref, definition=x.definition)
            for x in model.definitions
            if x.registration_kind == "tool_plan"
        ),
        fact_schemas=registry(data),
        context_allowlist=allowlist,
    )


def codes(result):
    return {x.code for check in result.report.checks for x in check.issues}


def literal(value=True):
    return dict(
        kind="literal",
        constant=dict(
            type_definition=dict(kind="bool"), value=dict(kind="bool", value=value)
        ),
    )


def applicability(raw):
    raw = deepcopy(raw)
    raw["use_role"] = "applicability"
    raw.pop("tool_plan_ref")
    raw.pop("step_id")
    return raw
