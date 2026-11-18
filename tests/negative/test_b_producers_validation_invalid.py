"""Missing evidence differs from contradictory actual catalogues."""

from copy import deepcopy

import pytest
from fixtures.wp01b.x2.support import check, codes, fixture


@pytest.mark.parametrize(
    "directory", ["questions", "plans", "tools", "policies", "system_capabilities"]
)
def test_missing_actual_definition_is_incomplete(directory):
    data = fixture()
    data["context"][directory] = []
    report = check(data)
    assert report.aggregate_status == "incomplete", report.model_dump_json()


@pytest.mark.parametrize("directory", ["questions", "plans", "tools", "policies"])
def test_unused_directory_digest_rebinding_is_invalid(directory):
    data = fixture()
    other = deepcopy(data["context"][directory][0])
    other["ref"]["digest"] = "b" * 64
    data["context"][directory].append(other)
    report = check(data)
    assert report.aggregate_status == "invalid"
    assert "producer.definition_rebinding" in codes(report)


@pytest.mark.parametrize(
    "mutation,code",
    [
        ("question", "producer.primary_completeness"),
        ("tool", "producer.mapped_completeness"),
        ("host", "producer.host_requirement"),
        ("key", "producer.question_primary"),
        ("skip", "producer.activation_mismatch"),
        ("cardinality", "producer.tool_output_contract"),
    ],
)
def test_known_contradiction_is_invalid(mutation, code):
    data = fixture()
    declarations = data["registration"]["definition"]["declarations"]
    if mutation in ("question", "tool"):
        declarations[:] = [
            d
            for d in declarations
            if d["producer_kind"]
            != {"question": "vlm_question", "tool": "tool"}[mutation]
        ]
    elif mutation == "host":
        data["context"]["system_capabilities"][0][
            "available_implementation_versions"
        ] = ["9.0.0"]
    elif mutation == "key":
        declarations[0]["fact_key"] = "different"
    elif mutation == "skip":
        declarations[1]["activation_condition"] = {"mode": "always"}
    elif mutation == "cardinality":
        declarations[1]["cardinality"] = "zero_or_one"
    report = check(data)
    assert report.aggregate_status == "invalid", report.model_dump_json()
    assert code in codes(report)


@pytest.mark.parametrize("directory", ["questions", "plans", "tools", "policies"])
def test_known_actual_digest_mismatch_is_invalid(directory):
    data = fixture()
    data["context"][directory][0]["ref"]["digest"] = "b" * 64
    assert check(data).aggregate_status == "invalid"


@pytest.mark.parametrize(
    "change",
    [
        "self",
        "foreign_tool",
        "missing_context",
        "no_dependency",
        "wrong_key",
        "wrong_schema",
    ],
)
def test_local_reference_rejects_known_conflict_or_reports_missing_environment(change):
    from fixtures.wp01b.x2.support import local_setup

    data = local_setup()
    declaration = data["registration"]["definition"]["declarations"][1]
    selector = declaration["activation_condition"]["condition_ast"]["selector"]
    if change == "self":
        selector["step_id"] = "measure"
    elif change == "foreign_tool":
        selector["tool_ref"]["digest"] = "b" * 64
    elif change == "missing_context":
        data["context"]["condition_contexts"] = []
    elif change == "no_dependency":
        data["context"]["plans"][0]["definition"]["steps"][1]["depends_on"] = []
    elif change == "wrong_key":
        selector["fact_key"] = "wrong"
    elif change == "wrong_schema":
        selector["fact_schema_ref"]["digest"] = "b" * 64
    data["context"]["policies"][0]["artifact"]["policy"]["condition_ast"] = deepcopy(
        declaration["activation_condition"]["condition_ast"]
    )
    report = check(data)
    assert report.aggregate_status == (
        "incomplete" if change == "missing_context" else "invalid"
    ), report.model_dump_json()


@pytest.mark.parametrize(
    "field,value",
    [
        ("fact_key", "wrong"),
        ("producer_declaration_id", "missing"),
        ("producer_version", "2.0.0"),
    ],
)
def test_fact_identity_contradiction_rejected(field, value):
    from fixtures.wp01b.x2.support import fact, registry

    from jimu_runtime.domain.validation.producer_inputs import (
        ProducerCatalogueRegistration,
        ProducerValidationContext,
    )
    from jimu_runtime.domain.validation.producers import validate_fact_producer

    data = fixture()
    record = fact(data).model_copy(update={field: value})
    report = validate_fact_producer(
        record,
        ProducerCatalogueRegistration.model_validate(data["registration"]),
        ProducerValidationContext.model_validate(data["context"]),
        fact_schemas=registry(),
    )
    assert report.aggregate_status == "invalid"
    assert report.checks[-1].status == "invalid"


def test_host_same_identity_conflicting_descriptor_is_invalid():
    data = fixture()
    other = deepcopy(data["context"]["system_capabilities"][0])
    other["registry_id"] = "other"
    data["context"]["system_capabilities"].append(other)
    assert "producer.host_rebinding" in codes(check(data))


def test_supplied_unused_condition_anchor_is_not_ignored():
    from fixtures.wp01b.x2.support import condition_use

    data = fixture()
    use = condition_use(data, data["question"])
    use["producer_catalogue_ref"] = deepcopy(use["producer_catalogue_ref"])
    use["producer_catalogue_ref"]["digest"] = "b" * 64
    data["context"]["condition_contexts"] = [use]
    assert "producer.condition_anchor" in codes(check(data))


@pytest.mark.parametrize(
    "mutation,code",
    [
        ("unmapped", "producer.unmapped_output"),
        ("wrong_alias", "producer.output_alias"),
        ("wrong_scope", "producer.schema_permission"),
        ("payload", "producer.fact_schema"),
        ("confidence", "producer.fact_confidence"),
    ],
)
def test_actual_contract_mismatch(mutation, code):
    from fixtures.wp01b.x2.support import fact, registry

    from jimu_runtime.domain.validation.producer_inputs import (
        ProducerCatalogueRegistration,
        ProducerValidationContext,
    )
    from jimu_runtime.domain.validation.producers import validate_fact_producer

    data = fixture()
    if mutation in ("payload", "confidence"):
        from jimu_runtime.domain.values import StringValue

        record = fact(data).model_copy(
            update={"value": StringValue(kind="string", value="false")}
            if mutation == "payload"
            else {"confidence": 0.5}
        )
        report = validate_fact_producer(
            record,
            ProducerCatalogueRegistration.model_validate(data["registration"]),
            ProducerValidationContext.model_validate(data["context"]),
            fact_schemas=registry(),
        )
    else:
        if mutation == "unmapped":
            data["context"]["plans"][0]["definition"]["steps"][0][
                "fact_output_mappings"
            ] = []
        elif mutation == "wrong_alias":
            data["context"]["plans"][0]["definition"]["steps"][0][
                "fact_output_mappings"
            ][0]["target_fact_key"] = "different"
        else:
            data["registration"]["definition"]["declarations"][2]["fact_schema_ref"] = (
                deepcopy(data["system_execution"]["fact_schema_ref"])
            )
        report = check(data)
    assert report.aggregate_status == "invalid", report.model_dump_json()
    assert code in codes(report), report.model_dump_json()


def test_bounded_diagnostics_mark_nonexhaustive_overflow():
    data = fixture()
    template = data["system_execution"]
    declarations = []
    for i in range(70):
        declaration = deepcopy(template)
        declaration.update(
            declaration_id=f"system.{i}", output_name=f"output.{i}", fact_key=f"key.{i}"
        )
        declarations.append(declaration)
    data["registration"]["definition"]["declarations"] = declarations
    data["context"]["profiles"] = []
    data["context"]["system_capabilities"] = []
    report = check(data)
    assert "producer.diagnostic_overflow" in codes(report)
    assert all(len(c.issues) <= 64 for c in report.checks)
    assert report.aggregate_status == "invalid"


def test_model_copy_taint_is_revalidated_before_indexing():
    from fixtures.wp01b.x2.support import registry

    from jimu_runtime.domain.validation.producer_inputs import (
        ProducerCatalogueRegistration,
        ProducerValidationContext,
    )
    from jimu_runtime.domain.validation.producers import validate_producer_catalogue

    data = fixture()
    registration = ProducerCatalogueRegistration.model_validate(data["registration"])
    object.__setattr__(
        registration.definition.declarations[0].producer.question_ref, "extra", True
    )
    report = validate_producer_catalogue(
        registration,
        ProducerValidationContext.model_validate(data["context"]),
        fact_schemas=registry(),
    )
    assert report.aggregate_status == "invalid"
    assert "producer.invalid_input" in codes(report)


@pytest.mark.parametrize("change", ["output", "key", "requirements", "path"])
def test_system_selector_requires_exact_declared_output_contract(change):
    from fixtures.wp01b.x2.support import condition_use

    data = fixture()
    declaration = data["registration"]["definition"]["declarations"][0]
    target = data["system_execution"]
    selector = dict(
        scope="execution",
        fact_key=target["fact_key"],
        fact_schema_ref=deepcopy(target["fact_schema_ref"]),
        field_path=[],
        producer_selector=dict(
            producer_kind="system",
            requirement=deepcopy(target["producer"]["requirement"]),
            output_name=target["output_name"],
        ),
    )
    if change == "output":
        selector["producer_selector"]["output_name"] = "unknown"
    elif change == "key":
        selector["fact_key"] = "wrong"
    elif change == "requirements":
        selector["producer_selector"]["requirement"][
            "allowed_implementation_versions"
        ] = ["9.0.0"]
    else:
        selector["field_path"] = ["missing"]
    declaration["activation_condition"] = {
        "mode": "tool_condition",
        "condition_ast": dict(
            kind="record_field_ref" if change == "path" else "fact_ref",
            selector=selector,
        ),
    }
    data["context"]["condition_contexts"] = [condition_use(data, declaration)]
    report = check(data)
    assert report.aggregate_status == "invalid", report.model_dump_json()
