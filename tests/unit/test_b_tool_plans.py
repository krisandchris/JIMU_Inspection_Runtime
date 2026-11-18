"""Local DAG and finite IO tests, not execution or full Bundle admission."""

import json
from copy import deepcopy
from pathlib import Path
from typing import cast

import pytest
from pydantic import TypeAdapter, ValidationError

from jimu_runtime.domain.definitions.tools import (
    ToolDefinition,
    ToolPlanDefinition,
    plan_ancestors,
)
from jimu_runtime.domain.schema_types import SchemaDescriptor
from jimu_runtime.domain.tool_mappings import MappingTypeContext
from jimu_runtime.domain.validation.issues import ValidationReport
from jimu_runtime.domain.validation.tool_plans import (
    ToolRegistration,
    validate_tool_definition,
    validate_tool_plan,
)


def fixture() -> dict[str, object]:
    return cast(
        dict[str, object],
        json.loads(
            (Path(__file__).parents[1] / "fixtures/wp01b/m1/contracts.json").read_text()
        ),
    )


def validate_fixture_plan(
    plan: ToolPlanDefinition,
    tools: tuple[ToolRegistration, ...],
    contexts: tuple[MappingTypeContext, ...] = (),
) -> ValidationReport:
    from jimu_runtime.domain.fact_selectors import ToolPlanContentRef

    context = cast(dict[str, object], fixture()["context"])
    return validate_tool_plan(
        plan,
        tools,
        contexts,
        plan_ref=ToolPlanContentRef.model_validate(context["tool_plan_ref"]),
    )


def test_empty_single_fork_join_and_unordered_array_dags() -> None:
    data = cast(dict[str, object], fixture()["plan"])
    first = cast(list[dict[str, object]], data["steps"])[0]
    for edges in [
        [],
        [("one", [])],
        [
            ("one", []),
            ("left", ["one"]),
            ("right", ["one"]),
            ("join", ["left", "right"]),
        ],
    ]:
        steps = []
        for identity, deps in reversed(edges):
            step = deepcopy(first)
            step["step_id"] = identity
            step["depends_on"] = deps
            steps.append(step)
        data["steps"] = steps
        plan = ToolPlanDefinition.model_validate(data)
        assert tuple(step.step_id for step in plan.steps) == tuple(
            step["step_id"] for step in steps
        )
        ancestors = plan_ancestors(plan.steps)
        if "join" in ancestors:
            assert ancestors["join"] == frozenset({"one", "left", "right"})


@pytest.mark.parametrize(
    "edges",
    [
        [("a", ["a"])],
        [("a", ["missing"])],
        [("a", ["b"]), ("b", ["a"])],
        [("a", ["b"]), ("b", ["c"]), ("c", ["a"])],
        [("a", []), ("a", [])],
        [("a", []), ("b", ["a", "a"])],
    ],
)
def test_invalid_dags_rejected(edges: list[tuple[str, list[str]]]) -> None:
    data = cast(dict[str, object], fixture()["plan"])
    first = cast(list[dict[str, object]], data["steps"])[0]
    steps = []
    for identity, deps in edges:
        step = deepcopy(first)
        step["step_id"] = identity
        step["depends_on"] = deps
        steps.append(step)
    data["steps"] = steps
    with pytest.raises(ValidationError):
        ToolPlanDefinition.model_validate(data)


def test_step_output_requires_real_ancestor_not_array_order() -> None:
    data = cast(dict[str, object], fixture()["plan"])
    first = cast(list[dict[str, object]], data["steps"])[0]
    second = deepcopy(first)
    second["step_id"] = "two"
    binding = cast(list[dict[str, object]], second["input_bindings"])[0]
    tool = cast(dict[str, object], fixture()["tool"])
    binding["source"] = {
        "source_kind": "step_output",
        "step_id": "one",
        "selector": {"schema_ref": tool["output_schema_ref"], "field_path": []},
    }
    data["steps"] = [first, second]
    with pytest.raises(ValidationError):
        ToolPlanDefinition.model_validate(data)
    second["depends_on"] = ["one"]
    assert ToolPlanDefinition.model_validate(data).steps[1].step_id == "two"


def test_finite_mapping_checks_and_deferred_authority_are_separate() -> None:
    data = fixture()
    tool = ToolDefinition.model_validate(data["tool"])
    plan = ToolPlanDefinition.model_validate(data["plan"])
    context = MappingTypeContext.model_validate(data["context"])
    registration = ToolRegistration.model_validate(
        {"ref": plan.steps[0].tool_ref, "definition": tool}
    )
    report = validate_fixture_plan(plan, (registration,), (context,))
    assert (
        next(
            check for check in report.checks if check.check_id == "tool_plan.step.0"
        ).status
        == "passed"
    )
    assert report.aggregate_status == "deferred"
    assert validate_fixture_plan(plan, ()).aggregate_status == "incomplete"
    assert validate_fixture_plan(plan, (registration,)).aggregate_status == "incomplete"
    descriptors = TypeAdapter(tuple[SchemaDescriptor, ...]).validate_python(
        data["descriptors"]
    )
    checked = validate_tool_definition(tool, descriptors)
    assert (
        next(check for check in checked.checks if check.check_id == "tool.io").status
        == "passed"
    )
    assert checked.aggregate_status == "deferred"


def test_output_alias_is_rejected() -> None:
    data = fixture()
    rawtool = cast(dict[str, object], data["tool"])
    rawtool["fact_outputs"] = [data["output"]]
    tool = ToolDefinition.model_validate(rawtool)
    planraw = cast(dict[str, object], data["plan"])
    cast(list[dict[str, object]], planraw["steps"])[0]["fact_output_mappings"] = [
        {"output_name": "state", "target_fact_key": "alias"}
    ]
    plan = ToolPlanDefinition.model_validate(planraw)
    registration = ToolRegistration.model_validate(
        {"ref": plan.steps[0].tool_ref, "definition": tool}
    )
    report = validate_fixture_plan(plan, (registration,))
    assert report.aggregate_status == "invalid"
    assert report.checks[1].issues[0].code == "tool_plan.output_mapping"


def test_maximum_plan_reports_remain_bounded_without_losing_missing_steps() -> None:
    data = fixture()
    rawplan = cast(dict[str, object], data["plan"])
    first = cast(list[dict[str, object]], rawplan["steps"])[0]
    steps = []
    for index in range(1024):
        step = deepcopy(first)
        step["step_id"] = f"step-{index}"
        steps.append(step)
    rawplan["steps"] = steps
    plan = ToolPlanDefinition.model_validate(rawplan)
    report = validate_fixture_plan(plan, ())
    assert report.aggregate_status == "incomplete"
    assert len(report.checks) <= 128
    missing = [
        issue
        for check in report.checks
        for issue in check.issues
        if issue.code == "tool_plan.tool_unavailable"
    ]
    assert len(missing) == 1024


def test_tool_descriptor_mismatch_and_missing_fact_schema_are_distinct() -> None:
    data = fixture()
    rawtool = cast(dict[str, object], data["tool"])
    rawtool["fact_outputs"] = [data["output"]]
    tool = ToolDefinition.model_validate(rawtool)
    rawdescriptors = cast(list[dict[str, object]], data["descriptors"])
    output = rawdescriptors[1]
    output["output_declarations"] = [
        {"output_name": "state", "value_path": ["state"], "required": True}
    ]
    descriptors = TypeAdapter(tuple[SchemaDescriptor, ...]).validate_python(
        rawdescriptors
    )
    report = validate_tool_definition(tool, descriptors)
    assert report.aggregate_status == "incomplete"
    assert any(
        issue.code == "tool.fact_schema_unavailable"
        for check in report.checks
        for issue in check.issues
    )
    output["output_declarations"] = []
    descriptors = TypeAdapter(tuple[SchemaDescriptor, ...]).validate_python(
        rawdescriptors
    )
    assert validate_tool_definition(tool, descriptors).aggregate_status == "invalid"


def test_step_may_select_subset_of_required_tool_declarations() -> None:
    data = fixture()
    rawtool = cast(dict[str, object], data["tool"])
    rawtool["fact_outputs"] = [data["output"]]
    tool = ToolDefinition.model_validate(rawtool)
    plan = ToolPlanDefinition.model_validate(data["plan"])
    registration = ToolRegistration.model_validate(
        {"ref": plan.steps[0].tool_ref, "definition": tool}
    )
    report = validate_fixture_plan(
        plan, (registration,), (MappingTypeContext.model_validate(data["context"]),)
    )
    assert (
        next(
            check for check in report.checks if check.check_id == "tool_plan.step.0"
        ).status
        == "passed"
    )


def test_plan_pin_checks_digest_and_single_exact_profile() -> None:
    from jimu_runtime.domain.fact_selectors import ToolPlanContentRef

    data = fixture()
    plan = ToolPlanDefinition.model_validate(data["plan"])
    context = MappingTypeContext.model_validate(data["context"])
    tool = ToolDefinition.model_validate(data["tool"])
    registration = ToolRegistration.model_validate(
        {"ref": plan.steps[0].tool_ref, "definition": tool}
    )
    wrong = context.model_copy(
        update={
            "tool_plan_ref": context.tool_plan_ref.model_copy(
                update={"digest": "b" * 64}
            )
        }
    )
    report = validate_tool_plan(
        plan, (registration,), (wrong,), plan_ref=context.tool_plan_ref
    )
    assert any(
        issue.code == "tool_plan.context_owner"
        for check in report.checks
        for issue in check.issues
    )
    wrong_profile = context.model_copy(
        update={
            "current_step_id": "another",
            "profile_ref": context.profile_ref.model_copy(update={"digest": "b" * 64}),
        }
    )
    # Drop profile registration to isolate cross-context owner consistency.
    wrong_profile = wrong_profile.model_copy(update={"context_registrations": ()})
    report = validate_tool_plan(
        plan, (registration,), (context, wrong_profile), plan_ref=context.tool_plan_ref
    )
    assert report.checks[0].issues[0].code == "tool_plan.profile_scope"
    wrong_ref = ToolPlanContentRef.model_validate(
        {**context.tool_plan_ref.model_dump(mode="json"), "artifact_id": "other"}
    )
    assert (
        validate_tool_plan(plan, (), plan_ref=wrong_ref).aggregate_status == "invalid"
    )


def test_known_tool_identity_with_wrong_digest_is_invalid() -> None:
    data = fixture()
    tool = ToolDefinition.model_validate(data["tool"])
    plan = ToolPlanDefinition.model_validate(data["plan"])
    registration = ToolRegistration.model_validate(
        {
            "ref": plan.steps[0].tool_ref.model_copy(update={"digest": "b" * 64}),
            "definition": tool,
        }
    )
    report = validate_fixture_plan(plan, (registration,))
    assert report.aggregate_status == "invalid"
    assert report.checks[1].issues[0].code == "tool_plan.tool_digest_mismatch"


@pytest.mark.parametrize("empty", [False, True])
@pytest.mark.parametrize("reverse", [False, True])
@pytest.mark.parametrize("wrong_digest", [False, True])
def test_all_supplied_contexts_preflight_even_if_no_step_uses_them(
    empty: bool, reverse: bool, wrong_digest: bool
) -> None:
    data = fixture()
    plan = ToolPlanDefinition.model_validate(data["plan"])
    context = MappingTypeContext.model_validate(data["context"])
    tool = ToolDefinition.model_validate(data["tool"])
    registration = ToolRegistration.model_validate(
        {"ref": plan.steps[0].tool_ref, "definition": tool}
    )
    extra = context.model_copy(update={"current_step_id": "unused"})
    if wrong_digest:
        extra = extra.model_copy(
            update={
                "tool_plan_ref": extra.tool_plan_ref.model_copy(
                    update={"digest": "b" * 64}
                )
            }
        )
    if empty:
        plan = plan.model_copy(update={"steps": ()})
        contexts = (extra,)
    else:
        contexts = (context, extra)
    if reverse:
        contexts = contexts[::-1]
    report = validate_tool_plan(
        plan, (registration,), contexts, plan_ref=context.tool_plan_ref
    )
    assert report.aggregate_status == "invalid"
    assert any(
        issue.code in {"tool_plan.context_owner", "tool_plan.context_step_unknown"}
        for check in report.checks
        for issue in check.issues
    )


def test_empty_plan_without_context_does_not_invent_input_scope() -> None:
    data = fixture()
    plan = ToolPlanDefinition.model_validate(data["plan"]).model_copy(
        update={"steps": ()}
    )
    context = MappingTypeContext.model_validate(data["context"])
    assert (
        validate_tool_plan(
            plan, (), (), plan_ref=context.tool_plan_ref
        ).aggregate_status
        == "deferred"
    )


def registry_case_inputs(registry_digest: str | None):
    from jimu_runtime.domain.fact_schemas.models import (
        FactSchemaDefinition,
        ValueSchemaGenerationSpec,
    )
    from jimu_runtime.domain.fact_schemas.projection import generate_value_schema
    from jimu_runtime.domain.fact_schemas.registry import FactSchemaRegistryBuilder
    from jimu_runtime.domain.references import ContentRef

    data = fixture()
    s2 = cast(
        dict[str, object],
        json.loads(
            (Path(__file__).parents[1] / "fixtures/wp01b/s2/contracts.json").read_text()
        ),
    )
    r1 = cast(
        dict[str, object],
        json.loads(
            (Path(__file__).parents[1] / "fixtures/wp01b/r1/contracts.json").read_text()
        ),
    )
    spec = ValueSchemaGenerationSpec.model_validate(r1["spec"])
    generated = generate_value_schema(
        {"kind": "enum", "members": ["parked", "moving"]}, spec
    )
    rawfact = cast(dict[str, object], r1["fact_schema"])
    rawfact.update(
        schema_id="state",
        value_schema_generation=spec,
        type_definition=generated.generation_input.projection,
        allowed_producer_kinds=["tool", "vlm_question"],
        payload_schema_ref=generated.child_ref.model_dump(mode="json"),
    )
    definition = FactSchemaDefinition.model_validate(rawfact)
    requested = cast(
        dict[str, object], cast(dict[str, object], data["output"])["fact_schema_ref"]
    )
    builder = FactSchemaRegistryBuilder()
    if registry_digest is not None:
        builder.register(
            ContentRef.model_validate({**requested, "digest": registry_digest}),
            definition,
        )
    registry = builder.freeze()
    rawtool = cast(dict[str, object], data["tool"])
    rawtool["fact_outputs"] = [data["output"]]
    tool = ToolDefinition.model_validate(rawtool)
    descriptors = cast(list[object], data["descriptors"])
    descriptors[0] = s2["mixed"]
    descriptors[1] = s2["tool"]
    rawplan = cast(dict[str, object], data["plan"])
    cast(list[dict[str, object]], rawplan["steps"])[0]["input_bindings"] = s2[
        "bindings"
    ]
    # Selecting no Tool output is legal; the Fact registry lookup here is an input.
    evidence_tool = ToolDefinition.model_validate(data["tool"]).model_copy(
        update={"fact_outputs": ()}
    )
    plan = ToolPlanDefinition.model_validate(rawplan)
    rawcontext = cast(dict[str, object], data["context"])
    rawcontext["descriptors"] = descriptors
    context = MappingTypeContext.model_validate(rawcontext)
    registration = ToolRegistration.model_validate(
        {"ref": plan.steps[0].tool_ref, "definition": evidence_tool}
    )
    return (
        tool,
        TypeAdapter(tuple[SchemaDescriptor, ...]).validate_python(descriptors),
        plan,
        context,
        registration,
        registry,
    )


@pytest.mark.parametrize("consumer", ["definition", "plan"])
@pytest.mark.parametrize(
    "digest,status,code",
    [
        (None, "incomplete", "registry.unknown_identity"),
        ("b" * 64, "invalid", "registry.digest_mismatch"),
    ],
)
def test_registry_unknown_vs_wrong_digest_has_cross_catalogue_status(
    consumer: str, digest: str | None, status: str, code: str
) -> None:
    tool, descriptors, plan, context, registration, registry = registry_case_inputs(
        digest
    )
    if consumer == "definition":
        report = validate_tool_definition(tool, descriptors, registry)
    else:
        report = validate_tool_plan(
            plan, (registration,), (context,), registry, plan_ref=context.tool_plan_ref
        )
    assert report.aggregate_status == status
    issues = [issue for check in report.checks for issue in check.issues]
    assert any(issue.code == code for issue in issues)
    assert all(not issue.message.startswith('{"scope"') for issue in issues)


def test_registry_path_failure_remains_invalid_with_typed_path() -> None:
    tool, descriptors, plan, context, registration, registry = registry_case_inputs(
        "a" * 64
    )
    raw = plan.model_dump(mode="json")
    steps = cast(list[dict[str, object]], raw["steps"])
    source = cast(
        dict[str, object],
        cast(list[dict[str, object]], steps[0]["input_bindings"])[1]["source"],
    )
    cast(dict[str, object], source["selector"])["field_path"] = ["unregistered"]
    changed = ToolPlanDefinition.model_validate(raw)
    report = validate_tool_plan(
        changed, (registration,), (context,), registry, plan_ref=context.tool_plan_ref
    )
    assert report.aggregate_status == "invalid"
    assert any(
        issue.code == "path_not_record" and issue.path == ("unregistered",)
        for check in report.checks
        for issue in check.issues
    )


@pytest.mark.parametrize("consumer", ["definition", "plan"])
def test_registry_long_typed_message_is_preserved_without_stringifying_report(
    consumer: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    from jimu_runtime.domain.fact_schemas.models import FactSchemaDefinition
    from jimu_runtime.domain.fact_schemas.registry import (
        FactSchemaRegistry,
        RegistryValidationError,
    )
    from jimu_runtime.domain.references import ContentRef
    from jimu_runtime.domain.validation.issues import CheckResult, ValidationIssue

    tool, descriptors, plan, context, registration, registry = registry_case_inputs(
        "a" * 64
    )
    issue = ValidationIssue(
        code="registry.digest_mismatch",
        path=("schema",),
        message="x" * 4096,
        related_refs=(tool.fact_outputs[0].fact_schema_ref,),
    )
    report = ValidationReport(
        scope="registry_core",
        checks=(
            CheckResult(
                check_id="registry.reference_shape", status="invalid", issues=(issue,)
            ),
        ),
    )

    def reject(self: FactSchemaRegistry, ref: ContentRef) -> FactSchemaDefinition:
        raise RegistryValidationError(report)

    monkeypatch.setattr(FactSchemaRegistry, "resolve", reject)
    checked = (
        validate_tool_definition(tool, descriptors, registry)
        if consumer == "definition"
        else validate_tool_plan(
            plan, (registration,), (context,), registry, plan_ref=context.tool_plan_ref
        )
    )
    assert checked.aggregate_status == "invalid"
    assert issue in tuple(issue for check in checked.checks for issue in check.issues)
