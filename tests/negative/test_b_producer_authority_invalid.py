"""Finite missing/conflicting inputs never become runtime authority."""

from copy import deepcopy

import pytest
from fixtures.wp01b.x3.integration import check, codes, fixture, parse, sync_fact
from pydantic import ValidationError


@pytest.mark.parametrize("name", ["question", "tool", "system"])
@pytest.mark.parametrize("missing", ["execution_pins", "producer_catalogues"])
def test_missing_pins_even_empty_output(name, missing):
    raw = fixture(name)
    raw[missing] = []
    report = check(raw)
    assert report.aggregate_status == "incomplete", report
    assert any("missing" in code for code in codes(report))


@pytest.mark.parametrize("name", ["question", "tool"])
def test_unselected_failed_late_history_is_retained(name):
    raw = fixture(name)
    h = raw["history"]
    h["proposed_selections"] = []
    h["attempts"][0]["status"] = "failed"
    h["observation_results" if name == "question" else "tool_results"][0][
        "arrival_qualification"
    ] = "late"
    report = check(raw)
    assert report.aggregate_status == "deferred", report
    assert all(c.status in ("passed", "deferred") for c in report.checks)
    assert len(parse(raw).history.candidates.entries) == 1


@pytest.mark.parametrize("name", ["question", "tool"])
def test_selected_failed_attempt_is_invalid(name):
    raw = fixture(name)
    raw["history"]["attempts"][0]["status"] = "failed"
    report = check(raw)
    assert report.aggregate_status == "invalid", report
    assert any(c.status == "invalid" and "local" in c.check_id for c in report.checks)


@pytest.mark.parametrize("name", ["question", "tool"])
def test_all_history_results_require_actual_node_owner(name):
    raw = fixture(name)
    h = raw["history"]
    h["proposed_selections"] = []
    h["candidates"]["entries"] = []
    h["node_runs"][0]["node_id"] = "N03"
    result = h["observation_results" if name == "question" else "tool_results"][0]
    result["question_results" if name == "question" else "mapped_outputs"] = []
    report = check(raw)
    assert "x3.result_node" in codes(report), report


@pytest.mark.parametrize("name", ["question", "tool"])
def test_all_history_present_emitted_fact_must_exist(name):
    raw = fixture(name)
    raw["history"]["proposed_selections"] = []
    raw["history"]["candidates"]["entries"] = []
    report = check(raw)
    assert "x3.emitted_candidate_missing" in codes(report), report
    assert report.aggregate_status == "incomplete"


@pytest.mark.parametrize("name", ["question", "tool"])
def test_unselected_result_cannot_borrow_another_results_fact(name):
    raw = fixture(name)
    h = raw["history"]
    h["proposed_selections"] = []
    items = h["observation_results" if name == "question" else "tool_results"]
    extra = deepcopy(items[0])
    extra["result_ref"]["record_id"] += "-other"
    items.append(extra)
    report = check(raw)
    assert "x3.emitted_candidate_owner" in codes(report), report


@pytest.mark.parametrize("name", ["question", "tool"])
def test_empty_result_scope_still_requires_actual_object_profile(name):
    raw = fixture(name)
    h = raw["history"]
    h["proposed_selections"] = []
    h["candidates"]["entries"] = []
    result = h["observation_results" if name == "question" else "tool_results"][0]
    result["question_results" if name == "question" else "mapped_outputs"] = []
    result["object_id"] = "missing-object"
    assert "x3.result_object_missing" in codes(check(raw))


def test_zero_fact_tool_wrong_step_is_invalid():
    raw = fixture("tool")
    h = raw["history"]
    h["proposed_selections"] = []
    h["candidates"]["entries"] = []
    h["tool_results"][0]["mapped_outputs"] = []
    h["tool_results"][0]["step_id"] = "foreign"
    report = check(raw)
    assert report.aggregate_status == "invalid", report
    assert "x3.result_step_tool" in codes(report)


@pytest.mark.parametrize("name", ["tool", "system"])
def test_actual_result_version_not_copied_from_allowed_version(name):
    raw = fixture(name)
    raw["history"]["tool_results" if name == "tool" else "system_results"][0][
        "actual_implementation_version"
    ] = "9.0.0"
    if name == "system":
        catalogue = raw["producer_catalogues"][0]
        for declaration in catalogue["registration"]["definition"]["declarations"]:
            if declaration["producer_kind"] == "system":
                declaration["producer"]["requirement"][
                    "allowed_implementation_versions"
                ].append("9.0.0")
        raw["history"]["system_results"][0]["requirement"][
            "allowed_implementation_versions"
        ].append("9.0.0")
        raw["history"]["attempts"][0]["owner"]["requirement"][
            "allowed_implementation_versions"
        ].append("9.0.0")
        raw["history"]["proposed_selections"][0]["requirement"][
            "allowed_implementation_versions"
        ].append("9.0.0")
    assert f"x3.{name}_actual_version" in codes(check(raw))


def test_system_required_output_cannot_disappear_under_condition():
    raw = fixture("system")
    declarations = raw["producer_catalogues"][0]["registration"]["definition"][
        "declarations"
    ]
    next(d for d in declarations if d["declaration_id"] == "system.execution")[
        "cardinality"
    ] = "exactly_one"
    assert "x3.required_system_absence" in codes(check(raw))


def test_catalogue_mismatch_with_same_declaration_id():
    raw = fixture()
    raw["history"]["candidates"]["entries"][0]["fact"]["producer_catalogue_ref"][
        "artifact_id"
    ] = "other"
    sync_fact(raw)
    assert check(raw).aggregate_status == "invalid"


def test_original_byte_failure_preserved():
    raw = fixture()
    raw["history"]["candidates"]["entries"][0]["raw_bytes"] = "{}"
    report = check(raw)
    assert report.aggregate_status == "invalid"
    assert any(c.status == "invalid" and "local" in c.check_id for c in report.checks)
    assert any(c.status == "deferred" for c in report.checks)


@pytest.mark.parametrize("field", ["questions", "tools", "plans"])
def test_conflicting_actual_definition_sources_rejected_before_index(field):
    raw = fixture()
    raw["history"][field][0]["ref"]["digest"] = "b" * 64
    with pytest.raises(ValidationError):
        parse(raw)


def test_known_schema_wrong_digest_is_invalid():
    raw = fixture()
    raw["history"]["candidates"]["entries"][0]["fact"]["fact_schema_ref"]["digest"] = (
        "b" * 64
    )
    sync_fact(raw)
    assert check(raw).aggregate_status == "invalid"


@pytest.mark.parametrize("kind", ["profile", "policy", "host"])
def test_unused_catalogue_context_cannot_contradict_shared_directory(kind):
    raw = fixture()
    extra = deepcopy(raw["producer_catalogues"][0])
    extra["registration"]["ref"]["artifact_id"] = "unused"
    extra["registration"]["definition"]["catalogue_id"] = "unused"
    if kind == "profile":
        extra["context"]["profiles"][0]["ref"]["digest"] = "b" * 64
    elif kind == "policy":
        extra["context"]["policies"][0]["ref"]["digest"] = "b" * 64
    else:
        extra["context"]["system_capabilities"][0]["registry_id"] = "contradiction"
    raw["producer_catalogues"].append(extra)
    report = check(raw)
    assert report.aggregate_status == "invalid", report
    assert any(
        c.status == "invalid" and "directories" in c.check_id for c in report.checks
    )


def test_unused_catalogue_is_actually_validated():
    raw = fixture()
    extra = deepcopy(raw["producer_catalogues"][0])
    extra["registration"]["ref"]["artifact_id"] = "unused"
    extra["registration"]["definition"]["catalogue_id"] = "unused"
    extra["registration"]["definition"]["declarations"][0]["fact_key"] = "wrong"
    raw["producer_catalogues"].append(extra)
    report = check(raw)
    assert report.aggregate_status == "invalid", report
    assert any(
        c.status == "invalid" and "catalogue" in c.check_id for c in report.checks
    )
