"""Finite integration, retaining explicit runtime responsibilities."""

import pytest
from fixtures.wp01b.x3.integration import check, fixture


@pytest.mark.parametrize("name", ["question", "tool", "system"])
def test_complete_finite_history(name):
    report = check(fixture(name))
    assert report.aggregate_status == "deferred", report.model_dump_json(indent=2)
    assert all(c.status in ("passed", "deferred") for c in report.checks), report
    assert any(
        "result_contract" in c.check_id and c.status == "passed" for c in report.checks
    )
    assert not any(
        c.check_id.endswith("full_authority_admission.deferred") for c in report.checks
    )


@pytest.mark.parametrize("field", ["questions", "tools", "plans"])
@pytest.mark.parametrize("source", ["history", "context"])
def test_complete_source_can_fill_missing_duplicate_without_precedence(field, source):
    raw = fixture("tool")
    if source == "history":
        raw["producer_catalogues"][0]["context"][field] = []
    else:
        raw["history"][field] = []
    assert check(raw).aggregate_status == "deferred"


def test_inputs_and_old_local_reports_are_immutable():
    from fixtures.wp01b.x3.integration import data, parse, registry

    from jimu_runtime.domain.runtime.candidates import (
        CandidateParseLimits,
        CandidateSnapshotLimits,
    )
    from jimu_runtime.domain.validation.authority import (
        validate_local_authority_history,
    )
    from jimu_runtime.domain.validation.producer_authority import (
        validate_producer_authority_contracts,
    )

    raw = fixture()
    inputs = parse(raw)
    before = inputs.model_dump()
    limits = data()
    kwargs = dict(
        fact_schemas=registry(),
        parse_limits=CandidateParseLimits.model_validate(limits["parse_limits"]),
        snapshot_limits=CandidateSnapshotLimits.model_validate(
            limits["snapshot_limits"]
        ),
    )
    local_before = validate_local_authority_history(inputs.history, **kwargs)
    report = validate_producer_authority_contracts(inputs, **kwargs)
    assert inputs.model_dump() == before
    assert local_before == validate_local_authority_history(inputs.history, **kwargs)
    assert any(c.check_id == "full_authority_admission" for c in local_before.checks)
    assert not any("full_authority_admission" in c.check_id for c in report.checks)
    assert any("external_provenance" in c.check_id for c in report.checks)
    assert all(
        any(
            issue.code == "x3.deferred." + name
            for c in report.checks
            for issue in c.issues
        )
        for name in (
            "activation_truth",
            "typechecker",
            "release_admission",
            "runtime_trust",
        )
    )


def test_conditional_false_tool_still_requires_complete_selected_output():
    from fixtures.wp01b.x3.integration import codes

    raw = fixture("tool")
    catalogue = raw["producer_catalogues"][0]
    # The frozen expression wire comes from the actual X2 conditional declaration;
    # no Boolean interpretation or expression simplification occurs in X3.
    import json
    from pathlib import Path

    x2 = json.loads(
        (Path(__file__).parents[1] / "fixtures/wp01b/x2/contracts.json").read_text()
    )
    literal = x2["literal_false"]
    declaration = next(
        d
        for d in catalogue["registration"]["definition"]["declarations"]
        if d["producer_kind"] == "tool"
    )
    declaration["activation_condition"]["condition_ast"] = literal
    policy = next(
        p
        for p in catalogue["context"]["policies"]
        if p["artifact"]["policy"]["policy_kind"] == "tool_skip"
    )
    policy["artifact"]["policy"]["condition_ast"] = literal
    assert check(raw).aggregate_status == "deferred"
    raw["history"]["tool_results"][0]["mapped_outputs"] = []
    raw["history"]["candidates"]["entries"] = []
    raw["history"]["proposed_selections"][0]["selected_fact_refs"] = []
    report = check(raw)
    assert report.aggregate_status == "invalid", report
    assert "x3.complete_tool_outputs" in codes(report)


def test_system_constraint_set_order_does_not_change_capability_identity():
    raw = fixture("system")
    catalogue = raw["producer_catalogues"][0]
    catalogue["context"]["system_capabilities"][0][
        "available_implementation_versions"
    ].append("1.3.0")
    for declaration in catalogue["registration"]["definition"]["declarations"]:
        if declaration["producer_kind"] == "system":
            declaration["producer"]["requirement"][
                "allowed_implementation_versions"
            ].append("1.3.0")
    raw["history"]["system_results"][0]["requirement"][
        "allowed_implementation_versions"
    ] = ["1.3.0", "1.2.0"]
    raw["history"]["attempts"][0]["owner"]["requirement"][
        "allowed_implementation_versions"
    ] = ["1.2.0", "1.3.0"]
    raw["history"]["proposed_selections"][0]["requirement"][
        "allowed_implementation_versions"
    ] = ["1.3.0", "1.2.0"]
    assert check(raw).aggregate_status == "deferred"
