"""Wrong identities, unbound owners and invalid local references cannot yield selectors."""

from copy import deepcopy

import pytest
from fixtures.wp01b.v1.support import entry
from fixtures.wp01b.x4.support import (
    applicability,
    bind,
    codes,
    context,
    literal,
    setup,
)

from jimu_runtime.domain.definition_support import ContextFieldSelector


@pytest.mark.parametrize(
    "mutation,expected",
    [
        ("self", "condition.local_not_ancestor"),
        ("future", "condition.local_not_ancestor"),
        ("sibling", "condition.local_not_ancestor"),
        ("unknown_step", "condition.local_not_ancestor"),
        ("unmapped", "condition.tool_output_identity"),
        ("output", "condition.tool_output_identity"),
        ("key", "condition.tool_output_identity"),
        ("tool_digest", "condition.digest_conflict"),
        ("schema_digest", "registry.digest_mismatch"),
        ("path", "path_not_record"),
        ("other_plan", "condition.foreign_plan"),
        ("foreign_profile", "condition.owner_unavailable"),
    ],
)
def test_bad_local_binding_never_returns_partial_projection(mutation, expected):
    data, ctx, expr = setup()
    steps = entry(data, "tool_plan")["definition"]["steps"]
    if mutation == "self":
        expr["selector"]["step_id"] = "third"
    elif mutation == "future":
        ctx["step_id"] = "first"
        expr["selector"]["step_id"] = "third"
    elif mutation == "sibling":
        steps[2]["depends_on"] = []
    elif mutation == "unknown_step":
        expr["selector"]["step_id"] = "absent"
    elif mutation == "unmapped":
        steps[0]["fact_output_mappings"] = []
    elif mutation == "output":
        expr["selector"]["output_name"] = "absent"
    elif mutation == "key":
        expr["selector"]["fact_key"] = "wrong"
    elif mutation == "tool_digest":
        expr["selector"]["tool_ref"]["digest"] = "b" * 64
    elif mutation == "schema_digest":
        expr["selector"]["fact_schema_ref"]["digest"] = "b" * 64
    elif mutation == "path":
        expr["selector"]["field_path"] = ["absent"]
    elif mutation == "other_plan":
        entry(data, "profile")["definition"]["tool_plan_ref"]["artifact_id"] = "other"
    elif mutation == "foreign_profile":
        ctx["profile_ref"]["artifact_id"] = "absent"
    result = bind(data, ctx, expr)
    assert result.report.aggregate_status in ("invalid", "incomplete")
    assert result.selectors_for_matching == ()
    assert any(expected in code for code in codes(result)), codes(result)


@pytest.mark.parametrize("value", [True, False])
@pytest.mark.parametrize(
    "mutation,expected",
    [
        ("digest", "condition.digest_conflict"),
        ("path", "condition.field_unknown"),
        ("missing_descriptor", "condition.descriptor_unavailable"),
        ("missing_view", "condition.context_unavailable"),
    ],
)
def test_unused_allowlist_is_checked_even_with_constant_tree(value, mutation, expected):
    data, ctx, _ = setup()
    ctx = applicability(ctx)
    raw = {
        "source": "context_snapshot",
        "schema_ref": deepcopy(ctx["context_registrations"][0]["schema_ref"]),
        "field_path": ["scene"],
    }
    if mutation == "digest":
        raw["schema_ref"]["digest"] = "b" * 64
    elif mutation == "path":
        raw["field_path"] = ["absent"]
    elif mutation == "missing_descriptor":
        ctx["descriptors"] = [
            d for d in ctx["descriptors"] if d["schema_ref"]["artifact_id"] != "context"
        ]
    else:
        ctx["context_registrations"] = ctx["context_registrations"][1:]
    result = bind(
        data, ctx, literal(value), (ContextFieldSelector.model_validate(raw),)
    )
    assert expected in codes(result)
    assert result.selectors_for_matching == ()


@pytest.mark.parametrize("taint", ["copy", "construct", "nested"])
def test_tainted_context_is_not_projected(taint):
    data, ctx, expr = setup()
    typed = context(ctx)
    if taint == "copy":
        typed = typed.model_copy(update={"injected": "reject"})
    elif taint == "construct":
        typed = type(typed).model_construct(**typed.__dict__)
        typed.__dict__["injected"] = "reject"
    else:
        typed = typed.model_copy(
            update={
                "context_registrations": (
                    typed.context_registrations[0].model_copy(
                        update={"injected": "reject"}
                    ),
                )
            }
        )
    result = bind(data, typed, expr)
    assert result.report.aggregate_status == "invalid"
    assert result.selectors_for_matching == ()


def test_all_selectors_checked_without_boolean_short_circuit():
    data, ctx, expr = setup()
    bad = deepcopy(expr)
    bad["selector"]["output_name"] = "missing"
    result = bind(data, ctx, dict(kind="all", arguments=[literal(False), expr, bad]))
    assert "condition.tool_output_identity" in codes(result)
    assert result.selectors_for_matching == ()


def test_exact_tool_reference_is_forbidden_in_attached_guard():
    data, ctx, expr = setup()
    local = expr["selector"]
    selector = dict(
        scope="object",
        fact_key=local["fact_key"],
        fact_schema_ref=local["fact_schema_ref"],
        field_path=[],
        producer_selector=dict(
            producer_kind="tool",
            tool_plan_ref=ctx["tool_plan_ref"],
            step_id="first",
            tool_ref=local["tool_ref"],
            output_name="signal",
        ),
    )
    result = bind(data, ctx, dict(kind="fact_ref", selector=selector))
    assert "condition.exact_tool_guard_forbidden" in codes(result)
    assert result.selectors_for_matching == ()


def test_1024_unused_allowlist_failures_are_reported_without_truncation():
    data, ctx, _ = setup()
    ctx = applicability(ctx)
    allowlist = tuple(
        ContextFieldSelector.model_validate(
            dict(
                source="context_snapshot",
                schema_ref=ctx["context_registrations"][0]["schema_ref"],
                field_path=[f"absent{i}"],
            )
        )
        for i in range(1024)
    )
    result = bind(data, ctx, literal(), allowlist)
    invalid = [
        issue
        for check in result.report.checks
        if check.status == "invalid"
        for issue in check.issues
    ]
    assert len(invalid) == 1024
    assert all(issue.code == "condition.field_unknown" for issue in invalid)
    assert result.selectors_for_matching == ()


def test_subclass_declared_taint_is_not_silently_discarded():
    data, ctx, expr = setup()
    typed = context(ctx)

    class Expanded(type(typed)):
        injected: str

    expanded = Expanded.model_validate(dict(typed.__dict__, injected="reject"))
    result = bind(data, expanded, expr)
    assert result.report.aggregate_status == "invalid"
    assert result.selectors_for_matching == ()


def test_invalid_consuming_step_reports_instead_of_indexing_failed_anchor():
    data, ctx, expr = setup()
    ctx["step_id"] = "absent"
    result = bind(data, ctx, expr)
    assert result.report.aggregate_status == "invalid"
    assert "condition.unknown_step" in codes(result)
    assert result.selectors_for_matching == ()


def test_unused_nested_directory_pin_conflict_is_rejected_before_lookup():
    data, ctx, _ = setup()
    # A literal never references this Tool output; the supplied pin still conflicts.
    entry(data, "tool")["definition"]["fact_outputs"][0]["fact_schema_ref"][
        "digest"
    ] = "b" * 64
    result = bind(data, ctx, True)
    assert result.report.aggregate_status == "invalid"
    assert "condition.digest_conflict" in codes(result)
    assert result.selectors_for_matching == ()


def test_known_foreign_plan_is_invalid_even_when_foreign_definition_is_absent():
    data, ctx, expr = setup()
    ctx["tool_plan_ref"]["artifact_id"] = "unknown-foreign-plan"
    result = bind(data, ctx, expr)
    assert result.report.aggregate_status == "invalid"
    assert "condition.foreign_plan" in codes(result)
    assert result.selectors_for_matching == ()


def test_known_foreign_question_is_invalid_even_if_its_definition_is_missing():
    data, ctx, _ = setup()
    selector = dict(
        scope="object",
        fact_key="primary",
        fact_schema_ref=data["fact_schemas"][0]["ref"],
        field_path=[],
        producer_selector=dict(
            producer_kind="vlm_question",
            question_ref=dict(
                artifact_kind="question",
                artifact_id="foreign",
                version="1.0.0",
                digest="a" * 64,
            ),
            output_name="primary",
        ),
    )
    result = bind(data, ctx, dict(kind="fact_ref", selector=selector))
    assert result.report.aggregate_status == "invalid"
    assert "condition.foreign_question" in codes(result)
    assert result.selectors_for_matching == ()


@pytest.mark.parametrize(
    "source", ["context_snapshot", "profile_metadata", "region_metadata"]
)
@pytest.mark.parametrize("guard", ["local", "constant"])
def test_same_context_source_owner_cannot_register_two_schemas_even_unused(
    source, guard
):
    data, ctx, expr = setup()
    schema = deepcopy(ctx["context_registrations"][0]["schema_ref"])
    if source == "profile_metadata":
        first = dict(source=source, schema_ref=schema, profile_ref=ctx["profile_ref"])
    elif source == "region_metadata":
        first = deepcopy(ctx["context_registrations"][1])
    else:
        first = deepcopy(ctx["context_registrations"][0])
    second = deepcopy(first)
    second["schema_ref"]["artifact_id"] = "distinct-context-schema"
    descriptor = deepcopy(
        next(
            item
            for item in ctx["descriptors"]
            if item["schema_ref"] == first["schema_ref"]
        )
    )
    descriptor["schema_ref"] = deepcopy(second["schema_ref"])
    ctx["context_registrations"] = [first, second]
    ctx["descriptors"].append(descriptor)
    result = bind(data, ctx, expr if guard == "local" else True)
    assert result.report.aggregate_status == "invalid"
    assert "condition.duplicate_context" in codes(result)
    assert result.selectors_for_matching == ()


@pytest.mark.parametrize(
    "use_scope,fact_scope",
    [
        ("execution", "object"),
        ("object", "execution"),
        ("object", "object"),
        ("execution", "execution"),
    ],
)
def test_system_activation_scope_matrix(use_scope, fact_scope):
    data, ctx, _ = setup()
    schema = deepcopy(data["fact_schemas"][0])
    schema["ref"].update(artifact_id="system-fact", digest="c" * 64)
    schema["definition"].update(
        schema_id="system-fact", scope=fact_scope, allowed_producer_kinds=["system"]
    )
    data["fact_schemas"].append(schema)
    ctx.update(
        use_role="producer_activation",
        producer_catalogue_ref=dict(
            artifact_kind="fact_producer_catalog",
            artifact_id="catalogue",
            version="1.0.0",
            digest="a" * 64,
        ),
        declaration_id="system",
        producer_kind="system",
        producer_scope=use_scope,
    )
    for field in ("policy_ref", "tool_plan_ref", "step_id"):
        ctx.pop(field)
    if use_scope == "execution":
        ctx.pop("profile_ref")
    selector = dict(
        scope=fact_scope,
        fact_key="system-signal",
        fact_schema_ref=schema["ref"],
        field_path=[],
        producer_selector=dict(
            producer_kind="system",
            requirement=dict(
                family="system_fact_producer",
                capability_id="synthetic-system",
                contract_version="1.0.0",
                allowed_implementation_versions=["1.0.0"],
                required_features=[],
            ),
            output_name="signal",
        ),
    )
    result = bind(data, ctx, dict(kind="fact_ref", selector=selector))
    if use_scope == "execution" and fact_scope == "object":
        assert result.report.aggregate_status == "invalid"
        assert "condition.foreign_scope" in codes(result)
        assert result.selectors_for_matching == ()
    else:
        assert result.report.aggregate_status == "deferred", (
            result.report.model_dump_json()
        )
        assert len(result.selectors_for_matching) == 1
        assert any(
            check.check_id == "condition.references" and check.status == "passed"
            for check in result.report.checks
        )
