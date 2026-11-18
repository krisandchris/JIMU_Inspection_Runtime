"""Finite actual-directory checks do not certify conditional truth or host trust."""

from copy import deepcopy

import pytest
from fixtures.wp01b.x2.support import check, codes, fixture


def test_actual_catalogue_bindings_are_performed_with_runtime_work_deferred():
    report = check(fixture())
    assert report.aggregate_status == "deferred", report.model_dump_json()
    assert all(
        c.status == "passed"
        for c in report.checks
        if c.check_id
        in {
            "producer.input",
            "producer.identities",
            "producer.bindings",
            "producer.completeness",
            "producer.schema",
            "producer.conditions",
            "producer.selector_binding",
        }
    )
    assert "producer.expression_semantics" in codes(report)


@pytest.mark.parametrize("directory", ["questions", "plans", "tools", "policies"])
def test_identical_validated_content_can_be_reused_in_directory(directory):
    data = fixture()
    data["context"][directory].append(deepcopy(data["context"][directory][0]))
    assert check(data).aggregate_status == "deferred"


@pytest.mark.parametrize(
    "name", ["question", "tool", "system_object", "system_execution"]
)
def test_fact_source_and_cross_wrapper_profile_bindings(name):
    from fixtures.wp01b.x2.support import fact, registry

    from jimu_runtime.domain.validation.producer_inputs import (
        ProducerCatalogueRegistration,
        ProducerValidationContext,
    )
    from jimu_runtime.domain.validation.producers import validate_fact_producer

    data = fixture()
    result = validate_fact_producer(
        fact(data, name),
        ProducerCatalogueRegistration.model_validate(data["registration"]),
        ProducerValidationContext.model_validate(data["context"]),
        fact_schemas=registry(),
    )
    assert result.aggregate_status == "deferred", result.model_dump_json()
    assert result.checks[-1].status == "passed"


@pytest.mark.parametrize(
    "status", ["unknown", "error", "not_applicable", "not_evaluated"]
)
def test_nonknown_fact_remains_its_original_status(status):
    from fixtures.wp01b.x2.support import fact, registry

    from jimu_runtime.domain.validation.producer_inputs import (
        ProducerCatalogueRegistration,
        ProducerValidationContext,
    )
    from jimu_runtime.domain.validation.producers import validate_fact_producer

    data = fixture()
    value = fact(data, status=status)
    assert (
        validate_fact_producer(
            value,
            ProducerCatalogueRegistration.model_validate(data["registration"]),
            ProducerValidationContext.model_validate(data["context"]),
            fact_schemas=registry(),
        )
        .checks[-1]
        .status
        == "passed"
    )
    assert value.status.value == status


@pytest.mark.parametrize("name", ["question", "system_object", "system_execution"])
def test_question_and_system_exact_forward_reference_is_finitely_bound(name):
    from fixtures.wp01b.x2.support import condition_use

    data = fixture()
    declaration = next(
        x
        for x in data["registration"]["definition"]["declarations"]
        if x["declaration_id"] == data[name]["declaration_id"]
    )
    target = data["system_execution"]
    declaration["activation_condition"] = {
        "mode": "tool_condition",
        "condition_ast": {
            "kind": "fact_ref",
            "selector": dict(
                scope="execution",
                fact_key=target["fact_key"],
                fact_schema_ref=target["fact_schema_ref"],
                field_path=[],
                producer_selector={
                    "producer_kind": "system",
                    "requirement": target["producer"]["requirement"],
                    "output_name": target["output_name"],
                },
            ),
        },
    }
    data["context"]["condition_contexts"] = [condition_use(data, declaration)]
    report = check(data)
    assert report.aggregate_status == "deferred", report.model_dump_json()


def test_local_tool_reference_binds_actual_ancestor_without_changing_ast():
    from fixtures.wp01b.x2.support import local_setup

    data = local_setup()
    before = deepcopy(data)
    report = check(data)
    assert report.aggregate_status == "deferred", report.model_dump_json()
    assert data == before


@pytest.mark.parametrize(
    "skip_kind,activation,expected",
    [
        ("raw", "always", "deferred"),
        ("raw", "conditional", "invalid"),
        ("literal", "always", "deferred"),
        ("literal", "conditional", "deferred"),
        ("compound", "always", "invalid"),
        ("compound", "conditional", "deferred"),
    ],
)
def test_skip_activation_exact_data_correspondence(skip_kind, activation, expected):
    data = fixture()
    decl = data["registration"]["definition"]["declarations"][1]
    condition = (
        True
        if skip_kind == "raw"
        else data["literal_true"]
        if skip_kind == "literal"
        else decl["activation_condition"]["condition_ast"]
    )
    data["context"]["policies"][0]["artifact"]["policy"]["condition_ast"] = condition
    decl["activation_condition"] = (
        {"mode": "always"}
        if activation == "always"
        else {
            "mode": "tool_condition",
            "condition_ast": data["literal_true"] if condition is True else condition,
        }
    )
    assert check(data).aggregate_status == expected


def test_optional_tool_output_remains_declared_without_inference_of_absence():
    data = fixture()
    data["context"]["tools"][0]["definition"]["fact_outputs"][0]["required"] = False
    data["registration"]["definition"]["declarations"][1]["cardinality"] = "zero_or_one"
    report = check(data)
    assert report.aggregate_status == "deferred"
    assert not hasattr(report, "absent_facts")


def test_system_constraint_order_does_not_change_agreement_or_original_data():
    from jimu_runtime.domain.fact_selectors import SystemProducerRequirement
    from jimu_runtime.domain.validation.producers import requirements_agree

    data = fixture()
    left = deepcopy(data["system_object"]["producer"]["requirement"])
    left["allowed_implementation_versions"] = ["1.2.0", "1.3.0"]
    right = deepcopy(left)
    right["allowed_implementation_versions"].reverse()
    assert requirements_agree(
        SystemProducerRequirement.model_validate(left),
        SystemProducerRequirement.model_validate(right),
    )
    assert left["allowed_implementation_versions"] == ["1.2.0", "1.3.0"]


def test_actual_question_reuse_across_two_profiles_remains_complete():
    data = fixture()
    profile = deepcopy(data["context"]["profiles"][0])
    profile["ref"]["artifact_id"] = "second-profile"
    profile["definition"]["profile_id"] = "second-profile"
    data["context"]["profiles"].append(profile)
    declarations = data["registration"]["definition"]["declarations"]
    for original in list(declarations):
        if original["scope"] == "object":
            copied = deepcopy(original)
            copied["declaration_id"] += ".second"
            copied["profile_ref"] = deepcopy(profile["ref"])
            declarations.append(copied)
    report = check(data)
    assert report.aggregate_status == "deferred", report.model_dump_json()


def test_actual_one_step_multiple_mapped_outputs_requires_each_declaration():
    data = fixture()
    declaration = deepcopy(data["registration"]["definition"]["declarations"][1])
    declaration.update(
        declaration_id="tool.extra", output_name="extra", fact_key="extra.state"
    )
    data["registration"]["definition"]["declarations"].append(declaration)
    output = deepcopy(data["context"]["tools"][0]["definition"]["fact_outputs"][0])
    output.update(output_name="extra", fact_key="extra.state")
    data["context"]["tools"][0]["definition"]["fact_outputs"].append(output)
    data["context"]["plans"][0]["definition"]["steps"][0][
        "fact_output_mappings"
    ].append(dict(output_name="extra", target_fact_key="extra.state"))
    assert check(data).aggregate_status == "deferred"
    data["registration"]["definition"]["declarations"].pop()
    assert "producer.mapped_completeness" in codes(check(data))
