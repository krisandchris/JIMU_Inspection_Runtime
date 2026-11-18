"""Independent invalid relations cannot be masked by incomplete external admission."""

from copy import deepcopy

import pytest
from fixtures.wp01b.v1.support import (
    codes,
    entry,
    fixture,
    prompt_with_variable,
    report,
    with_step,
)


@pytest.mark.parametrize("mutation", ["duplicate", "digest", "descriptor", "policy"])
def test_index_conflicts_are_rejected_before_losing_data(mutation):
    data = fixture()
    if mutation in ("duplicate", "digest"):
        copied = deepcopy(data["input"]["definitions"][0])
        if mutation == "digest":
            copied["ref"]["digest"] = "b" * 64
        data["input"]["definitions"].append(copied)
    else:
        key = "descriptors" if mutation == "descriptor" else "policies"
        copied = deepcopy(data["input"][key][0])
        data["input"][key].append(copied)
    assert "definitions.identity_conflict" in codes(report(data))


@pytest.mark.parametrize(
    "mutation,expected",
    [
        ("member", "definitions.vocabulary_member"),
        ("domain", "definitions.vocabulary_member"),
        ("digest", "definitions.digest_mismatch"),
    ],
)
def test_vocabulary_checks_actual_member_domain_and_exact_digest(mutation, expected):
    data = fixture()
    if mutation == "member":
        entry(data, "entity")["definition"]["entity_type"]["member"] = "foreign"
    elif mutation == "domain":
        entry(data, "vocabulary", "entity-type")["definition"]["semantic_domain"] = (
            "wrong.domain"
        )
    else:
        entry(data, "entity")["definition"]["entity_type"]["vocabulary_ref"][
            "digest"
        ] = "b" * 64
    assert expected in codes(report(data))


@pytest.mark.parametrize(
    "mutation,expected",
    [
        ("facet", "definitions.facet_intersection"),
        ("profile", "definitions.profile_facets"),
        ("scope", "definitions.question_schema"),
        ("producer", "definitions.question_schema"),
    ],
)
def test_schema_intersection_profile_facets_scope_and_producer(mutation, expected):
    data = fixture()
    if mutation == "facet":
        copied = deepcopy(data["fact_schemas"][0])
        copied["ref"]["artifact_id"] = "other"
        copied["definition"]["schema_id"] = "other"
        data["fact_schemas"].append(copied)
        entry(data, "facet")["definition"]["fact_schema_refs"] = [copied["ref"]]
    elif mutation == "profile":
        entry(data, "profile")["definition"]["facet_refs"] = []
    elif mutation == "scope":
        data["fact_schemas"][0]["definition"].update(
            scope="execution", allowed_producer_kinds=["system"]
        )
    else:
        data["fact_schemas"][0]["definition"]["allowed_producer_kinds"] = ["system"]
    assert expected in codes(report(data))


@pytest.mark.parametrize("mutation", ["raw", "missing", "wrong_enum"])
def test_metadata_uses_trusted_record_type(mutation):
    data = fixture()
    descriptor = next(
        d
        for d in data["input"]["descriptors"]
        if d["schema_ref"]["artifact_id"] == "facet-meta"
    )
    if mutation == "raw":
        descriptor["descriptor_kind"] = "raw_dto"
    elif mutation == "missing":
        entry(data, "facet")["definition"]["metadata"][0]["value"]["value"] = {}
    else:
        descriptor["type_definition"]["fields"][0]["type_definition"] = {
            "kind": "enum",
            "members": ["yes"],
        }
    result = report(data)
    assert result.aggregate_status == "invalid"


@pytest.mark.parametrize(
    "mutation,expected",
    [
        ("cycle", "definitions.prompt_cycle"),
        ("dangling", "definitions.target_missing"),
        ("unused", "definitions.prompt_registration"),
        ("conflict", "definitions.prompt_declaration_conflict"),
        ("allowlist", "definitions.question_context_allowlist"),
    ],
)
def test_partial_graph_and_inherited_allowlist(mutation, expected):
    data = fixture()
    root = entry(data, "prompt_template", "prompt-question")
    if mutation == "cycle":
        root["definition"]["partial_refs"] = [root["ref"]]
    elif mutation == "dangling":
        root["definition"]["partial_refs"] = [{**root["ref"], "artifact_id": "absent"}]
    else:
        prompt_with_variable(data)
        if mutation == "unused":
            root["definition"]["template_text"] = "literal"
        elif mutation == "allowlist":
            entry(data, "question")["definition"]["context_allowlist"] = []
        else:
            child = deepcopy(root)
            child["ref"]["artifact_id"] = "partial"
            child["definition"]["prompt_template_id"] = "partial"
            child["definition"]["variables"][0]["field_path"] = ["different"]
            data["input"]["definitions"].append(child)
            root["definition"]["partial_refs"] = [child["ref"]]
    assert expected in codes(report(data))


def test_context_owner_ambiguity_never_picks_first():
    data = fixture()
    prompt_with_variable(data)
    region = deepcopy(data["input"]["use_contexts"][0]["context_registrations"][1])
    region["schema_ref"] = deepcopy(
        data["input"]["use_contexts"][0]["context_registrations"][0]["schema_ref"]
    )
    data["input"]["use_contexts"][0]["context_registrations"][1] = region
    assert "definitions.context_owner_ambiguous" in codes(report(data))


def test_model_joint_prompt_does_not_guess_question_metadata_owner():
    data = fixture()
    second = deepcopy(entry(data, "question"))
    second["ref"]["artifact_id"] = second["definition"]["question_id"] = (
        "second-question"
    )
    second["definition"]["primary_fact_key"] = "second-primary"
    data["input"]["definitions"].append(second)
    entry(data, "profile")["definition"]["question_refs"].append(second["ref"])
    selection = next(
        item
        for item in data["input"]["policies"]
        if item["artifact"]["policy_id"] == "question_selection"
    )
    selection["artifact"]["policy"]["mandatory_question_refs"].append(second["ref"])
    prompt_with_variable(
        data,
        "prompt-observation",
        source="question_metadata",
        kind="attribute_schema",
        schema_id="question-meta",
        path=["flag"],
    )
    assert "definitions.question_owner_unavailable" in codes(report(data))


@pytest.mark.parametrize("name", ["prompt-discovery", "prompt-grounding"])
def test_preobject_model_prompt_geometry_view_does_not_confer_object_owner(name):
    data = fixture()
    if name == "prompt-grounding":
        profile = entry(data, "profile")["definition"]
        profile["grounding_binding"] = {
            "kind": "model",
            "model_binding_ref": entry(data, "model_binding", "grounding")["ref"],
            "input_bindings": profile["grounding_binding"]["input_bindings"],
        }
    prompt_with_variable(
        data,
        name,
        source="object_geometry",
        kind="object_schema",
        schema_id="geometry",
        path=["flag"],
    )
    assert "definitions.object_owner_unavailable" in codes(report(data))


@pytest.mark.parametrize("mutation", ["missing", "wrong_target", "missing_image"])
def test_grounding_input_mapping_is_not_guessed(mutation):
    data = fixture()
    binding = entry(data, "profile")["definition"]["grounding_binding"]
    if mutation == "missing":
        binding.pop("input_bindings")
    elif mutation == "wrong_target":
        binding["input_bindings"][0]["target"]["schema_ref"]["digest"] = "b" * 64
    else:
        data["input"]["use_contexts"][0].pop("image_reference_schema_ref")
    result = report(data)
    assert any(
        check.check_id.startswith("definitions.grounding_io.")
        and check.status in ("invalid", "incomplete")
        for check in result.checks
    )


def test_missing_policy_content_is_incomplete_not_reference_shape_pass():
    data = fixture()
    data["input"]["policies"] = []
    result = report(data)
    assert any(
        check.check_id == "definitions.policies.incomplete" for check in result.checks
    )


def test_diagnostic_capacity_fails_explicitly_without_successful_truncation():
    data = fixture()
    base = entry(data, "entity")
    for index in range(70):
        item = deepcopy(base)
        item["ref"]["artifact_id"] = f"missing.e{index}"
        item["definition"]["entity_id"] = f"missing.e{index}"
        item["definition"]["attribute_schema_refs"][0]["artifact_id"] = "absent"
        data["input"]["definitions"].append(item)
    result = report(data)
    assert result.aggregate_status == "incomplete"
    assert "definitions.diagnostic_capacity" in codes(result)
    assert "definitions.deferred.producer_authority_admission" in codes(result)
    assert any(
        check.status == "incomplete" and len(check.issues) == 64
        for check in result.checks
    )


def test_overflow_preserves_known_invalidity_and_all_deferred_boundaries():
    data = fixture()
    entry(data, "entity")["definition"]["entity_type"]["member"] = "foreign"
    base = entry(data, "entity")
    for index in range(70):
        item = deepcopy(base)
        item["ref"]["artifact_id"] = item["definition"]["entity_id"] = f"e{index}"
        data["input"]["definitions"].append(item)
    result = report(data)
    assert result.aggregate_status == "invalid"
    assert "definitions.diagnostic_capacity" in codes(result)
    assert "definitions.vocabulary_member" in codes(result)
    assert "definitions.deferred.bundle_closure" in codes(result)


def test_missing_time_cannot_hide_known_model_prompt_response_mismatch():
    data = fixture()
    data["input"].pop("validation_at")
    entry(data, "prompt_template", "prompt-observation")["definition"][
        "response_schema_ref"
    ]["digest"] = "b" * 64
    result = report(data)
    assert "definitions.model_response_schema" in codes(result)
    assert "definitions.validation_at_missing" in codes(result)
    assert result.aggregate_status == "invalid"


@pytest.mark.parametrize("grounding", [False, True])
def test_known_source_schema_digest_conflict_precedes_owner_missing(grounding):
    data = fixture()
    if grounding:
        mapping = entry(data, "profile")["definition"]["grounding_binding"][
            "input_bindings"
        ][0]
        mapping["source"] = {
            "source_kind": "context",
            "selector": deepcopy(
                entry(data, "question")["definition"]["context_allowlist"][0]
            ),
        }
        mapping["source"]["selector"]["schema_ref"]["digest"] = "b" * 64
    else:
        prompt = prompt_with_variable(data)
        prompt["variables"][0]["schema_ref"]["digest"] = "b" * 64
    assert "schema_digest_mismatch" in codes(report(data))


@pytest.mark.parametrize(
    "mutation,expected",
    [
        ("body", "definitions.nested_descriptor_conflict"),
        ("missing", "descriptor_unavailable"),
        ("profile", "definitions.mapping_owner"),
        ("plan", "definitions.mapping_owner"),
        ("step", "definitions.mapping_step"),
        ("source", "definitions.nested_context_conflict"),
        ("image", "definitions.outer_view_missing"),
    ],
)
def test_nested_mapping_views_are_restricted_even_when_descriptor_unused(
    mutation, expected
):
    data = with_step(fixture())
    use = data["input"]["use_contexts"][0]
    context = use["mapping_contexts"][0]
    if mutation == "body":
        unused = next(
            d
            for d in context["descriptors"]
            if d["schema_ref"]["artifact_id"] == "facet-meta"
        )
        unused["type_definition"]["fields"][0]["required"] = False
    elif mutation == "missing":
        context["descriptors"][0]["schema_ref"]["artifact_id"] = "foreign"
    elif mutation == "profile":
        context["profile_ref"]["artifact_id"] = "foreign"
    elif mutation == "plan":
        context["tool_plan_ref"]["artifact_id"] = "foreign"
    elif mutation == "step":
        context["current_step_id"] = "foreign"
    elif mutation == "source":
        context["context_registrations"][0]["schema_ref"]["artifact_id"] = "foreign"
    else:
        use.pop("image_reference_schema_ref")
    result = report(data)
    assert expected in codes(result), result.model_dump_json(indent=2)
    assert any(
        check.check_id == "definitions.questions.passed" for check in result.checks
    )


def test_used_region_metadata_needs_exact_current_region():
    data = with_step(fixture())
    use = data["input"]["use_contexts"][0]
    use.pop("region_ref")
    registration = use["context_registrations"][1]
    entry(data, "tool_plan")["definition"]["steps"][0]["input_bindings"][0][
        "source"
    ] = {
        "source_kind": "context",
        "selector": {
            "source": "region_metadata",
            "schema_ref": registration["schema_ref"],
            "field_path": ["scene"],
        },
    }
    assert "definitions.region_unavailable" in codes(report(data))


def test_missing_profile_use_is_not_silent_io_approval():
    data = fixture()
    data["input"]["use_contexts"] = []
    assert "definitions.profile_use_missing" in codes(report(data))


@pytest.mark.parametrize(
    "annotation,domain", [("units", "unit"), ("coordinate_system", "coordinate")]
)
def test_fact_schema_annotation_vocabulary_members_are_validated(annotation, domain):
    data = fixture()
    key = "unit" if annotation == "units" else "coordinate_system"
    data["fact_schemas"][0]["definition"][annotation] = {
        "mode": "declared",
        "bindings": [
            {
                "field_path": [],
                key: {
                    "vocabulary_ref": entry(data, "vocabulary", domain)["ref"],
                    "member": "foreign",
                },
            }
        ],
    }
    assert "definitions.vocabulary_member" in codes(report(data))


def test_unused_question_context_allowlist_path_is_still_checked():
    data = fixture()
    entry(data, "question")["definition"]["context_allowlist"][0]["field_path"] = [
        "missing"
    ]
    assert "field_unknown" in codes(report(data))


def test_unowned_plan_still_checks_referenced_tools_and_policies():
    data = with_step(fixture())
    plan = deepcopy(entry(data, "tool_plan"))
    plan["ref"]["artifact_id"] = plan["definition"]["tool_plan_id"] = "unowned"
    plan["definition"]["steps"][0]["tool_ref"]["artifact_id"] = "missing"
    data["input"]["definitions"].append(plan)
    assert "tool_plan.tool_unavailable" in codes(report(data))


@pytest.mark.parametrize("where", ["profile", "step"])
def test_known_nonexpression_policy_cannot_replace_blocked_content(where):
    data = with_step(fixture())
    ref = {
        "artifact_kind": "policy",
        "artifact_id": "evidence",
        "version": "1.0.0",
        "digest": "a" * 64,
    }
    if where == "profile":
        entry(data, "profile")["definition"]["applicability_policy_ref"] = ref
    else:
        entry(data, "tool_plan")["definition"]["steps"][0]["skip_policy_ref"] = ref
    assert "definitions.policy_subtype" in codes(report(data))


def test_unowned_execution_policy_uses_actual_subtype_validator():
    import json
    from pathlib import Path

    data = fixture()
    source = json.loads(
        (Path(__file__).parents[1] / "fixtures/wp01b/p1/contracts.json").read_text()
    )
    workflow = deepcopy(
        next(
            p
            for p in source["registrations"]
            if p["artifact"]["policy_id"] == "workflow"
        )
    )
    workflow["artifact"]["policy"]["coverage_policy_ref"]["artifact_id"] = "evidence"
    provenance = deepcopy(
        next(
            p
            for p in source["registrations"]
            if p["artifact"]["policy_id"] == "context_provenance"
        )
    )
    provenance["artifact"]["policy"]["fields"][0]["field_path"] = ["scene"]
    data["input"]["policies"].append(provenance)
    data["input"]["policies"].append(workflow)
    assert "execution.policy_subtypes" in codes(report(data))


@pytest.mark.parametrize("location", ["question", "partial", "orphan"])
@pytest.mark.parametrize(
    "mutation,expected",
    [("digest", "schema_digest_mismatch"), ("missing", "descriptor_unavailable")],
)
def test_every_prompt_response_reference_is_resolved_without_business_compilation(
    location, mutation, expected
):
    data = fixture()
    prompt = entry(data, "prompt_template", "prompt-question")
    if location != "question":
        child = deepcopy(prompt)
        child["ref"]["artifact_id"] = child["definition"]["prompt_template_id"] = (
            "extra-prompt"
        )
        data["input"]["definitions"].append(child)
        if location == "partial":
            prompt["definition"]["partial_refs"] = [child["ref"]]
        prompt = child
    response = prompt["definition"]["response_schema_ref"]
    response["digest" if mutation == "digest" else "artifact_id"] = (
        "b" * 64 if mutation == "digest" else "absent"
    )
    result = report(data)
    assert expected in codes(result)
    assert result.aggregate_status == (
        "invalid" if mutation == "digest" else "incomplete"
    )


@pytest.mark.parametrize(
    "field",
    [
        "image_reference_schema_ref",
        "profile_metadata_schema_ref",
        "object_geometry_schema_ref",
    ],
)
def test_unused_outer_views_cannot_conflict_with_known_schema_identity(field):
    data = fixture()
    data["input"]["use_contexts"][0][field]["digest"] = "b" * 64
    assert "definitions.view_identity_conflict" in codes(report(data))


@pytest.mark.parametrize(
    "mutation", ["discovery", "model_grounding", "tool_grounding", "nested_tool"]
)
def test_known_capability_labels_are_verified_without_host_quality_claims(mutation):
    data = fixture()
    profile = entry(data, "profile")["definition"]
    if mutation == "discovery":
        profile["discovery_binding_ref"] = entry(data, "model_binding", "observation")[
            "ref"
        ]
        expected = "definitions.model_capability"
    elif mutation == "model_grounding":
        profile["grounding_binding"] = {
            "kind": "model",
            "model_binding_ref": entry(data, "model_binding", "observation")["ref"],
        }
        expected = "definitions.grounding_capability"
    elif mutation == "tool_grounding":
        entry(data, "tool")["definition"]["capability_tags"] = []
        expected = "definitions.grounding_capability"
    else:
        entry(data, "tool")["definition"]["model_binding_ref"] = entry(
            data, "model_binding", "observation"
        )["ref"]
        expected = "definitions.nested_model_capability"
    assert expected in codes(report(data))
