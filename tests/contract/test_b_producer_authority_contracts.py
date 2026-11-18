"""Integrated reports remain finite diagnostics, not a verified authority API."""

from copy import deepcopy

import pytest
from fixtures.wp01b.x3.integration import check, codes, fixture
from jsonschema import Draft202012Validator

from jimu_runtime.domain.validation.producer_authority_inputs import (
    ExecutionDefinitionPin,
    ProducerAuthorityValidationInput,
)


def test_strict_injected_root_schemas():
    for model in (ExecutionDefinitionPin, ProducerAuthorityValidationInput):
        schema = model.model_json_schema()
        Draft202012Validator.check_schema(schema)
        assert schema["additionalProperties"] is False
        assert "schema_version" not in schema["properties"]


def test_report_never_exposes_consumer_authority():
    report = check(fixture())
    assert report.scope == "authority_admission"
    for name in ("authority_view", "selected_fact", "admitted_release"):
        assert not hasattr(report, name)
    assert any("external_provenance" in c.check_id for c in report.checks)
    assert any("activation_truth" in code for code in codes(report))


@pytest.mark.parametrize("bad_bytes", [False, True])
def test_diagnostic_overflow_preserves_known_invalidity_and_deferred_boundary(
    bad_bytes,
):
    raw = fixture("question")
    raw["execution_pins"] = []
    if bad_bytes:
        raw["history"]["candidates"]["entries"][0]["raw_bytes"] = "{}"
    template = fixture("system")["history"]["system_results"][0]
    for index in range(70):
        result = deepcopy(template)
        result["execution_id"] = f"extra-{index}"
        result["result_ref"]["record_id"] = f"result-{index}"
        raw["history"]["system_results"].append(result)
    report = check(raw)
    assert "x3.diagnostic_overflow" in codes(report), report
    assert report.aggregate_status == ("invalid" if bad_bytes else "incomplete"), report
    assert any(c.status == "deferred" for c in report.checks)
    assert len(report.checks) <= 128
    assert all(len(c.issues) <= 64 for c in report.checks)


def test_genuinely_absent_schema_registry_is_incomplete():
    from fixtures.wp01b.x3.integration import data, parse

    from jimu_runtime.domain.fact_schemas.registry import FactSchemaRegistryBuilder
    from jimu_runtime.domain.runtime.candidates import (
        CandidateParseLimits,
        CandidateSnapshotLimits,
    )
    from jimu_runtime.domain.validation.producer_authority import (
        validate_producer_authority_contracts,
    )

    limits = data()
    report = validate_producer_authority_contracts(
        parse(fixture()),
        fact_schemas=FactSchemaRegistryBuilder().freeze(),
        parse_limits=CandidateParseLimits.model_validate(limits["parse_limits"]),
        snapshot_limits=CandidateSnapshotLimits.model_validate(
            limits["snapshot_limits"]
        ),
    )
    assert report.aggregate_status == "incomplete", report
    assert "fact_schema_missing" in codes(report)
    assert any(c.status == "deferred" for c in report.checks)
