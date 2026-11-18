"""Independent X-I06/I07 regressions against the actual finite composition API."""

from copy import deepcopy

import pytest
from fixtures.wp01b.x2.support import fact as x2_fact
from fixtures.wp01b.x2.support import fixture as x2_fixture
from fixtures.wp01b.x3.integration import data, fixture, parse, registry, sync_fact

from jimu_runtime.domain.runtime.candidates import (
    CandidateParseLimits,
    CandidateSnapshotLimits,
)
from jimu_runtime.domain.validation.producer_authority import (
    validate_producer_authority_contracts,
)

_RESULT_FIELD = dict(
    question="observation_results", tool="tool_results", system="system_results"
)
_SOURCE_REF = dict(
    question="observation_batch_ref",
    tool="tool_execution_ref",
    system="system_output_ref",
)


def _check(raw):
    """Use real typed history, byte parser, registry and integrated checker."""
    limits = data()
    return validate_producer_authority_contracts(
        parse(raw),
        fact_schemas=registry(),
        parse_limits=CandidateParseLimits.model_validate(limits["parse_limits"]),
        snapshot_limits=CandidateSnapshotLimits.model_validate(
            limits["snapshot_limits"]
        ),
    )


def _codes(report):
    return {issue.code for check in report.checks for issue in check.issues}


def _unselected(
    name, emitted=False, arrival="late", attempt_status="failed", seed=None
):
    raw = deepcopy(fixture(name) if seed is None else seed)
    history = raw["history"]
    attempt = deepcopy(history["attempts"][0])
    attempt.update(
        attempt_ref=dict(record_kind="attempt", record_id="attempt-review"),
        attempt_number=2,
        status=attempt_status,
    )
    if name != "system":
        node = deepcopy(history["node_runs"][0])
        node.update(
            node_run_ref=dict(record_kind="node_run", record_id="node-review"),
            status="failed" if attempt_status == "failed" else "succeeded",
        )
        history["node_runs"].append(node)
        attempt["owner"]["node_run_ref"] = deepcopy(node["node_run_ref"])
    history["attempts"].append(attempt)
    result = deepcopy(history[_RESULT_FIELD[name]][0])
    result.update(
        attempt_ref=deepcopy(attempt["attempt_ref"]), arrival_qualification=arrival
    )
    result["result_ref"]["record_id"] += "-review"
    if name == "question":
        result["question_results"] = []
    elif name == "tool":
        result["mapped_outputs"] = []
    if emitted:
        if name == "system":
            fact = x2_fact(x2_fixture(), "system_execution").model_dump(mode="json")
        else:
            fact = deepcopy(history["candidates"]["entries"][0]["fact"])
        fact.update(
            fact_id="fact-review", attempt_id=attempt["attempt_ref"]["record_id"]
        )
        fact["source"][_SOURCE_REF[name]] = deepcopy(result["result_ref"])
        if name == "question":
            result["question_results"] = [
                dict(
                    question_ref=deepcopy(fact["source"]["question_ref"]),
                    fact_ref=dict(record_kind="fact", record_id="fact-review"),
                )
            ]
        else:
            output = dict(
                disposition="emitted",
                output_name=fact["source"]["output_name"],
                fact_status=fact["status"],
                fact_ref=dict(record_kind="fact", record_id="fact-review"),
            )
            if name == "tool":
                result["mapped_outputs"] = [output]
            else:
                result["output"] = output
        history["candidates"]["entries"].append(dict(fact=fact, raw_bytes=""))
        sync_fact(raw)
    history[_RESULT_FIELD[name]].append(result)
    return raw, result, attempt


def _emitted_ref(name, result):
    if name == "question":
        return result["question_results"][0]["fact_ref"]
    if name == "tool":
        return result["mapped_outputs"][0]["fact_ref"]
    return result["output"]["fact_ref"]


@pytest.mark.parametrize("name", ["question", "tool", "system"])
@pytest.mark.parametrize("emitted", [False, True])
@pytest.mark.parametrize(
    "arrival,status", [("late", "failed"), ("on_time", "failed"), ("late", "succeeded")]
)
def test_valid_unselected_failed_or_late_partial_results_are_retained(
    name, emitted, arrival, status
):
    raw, _, _ = _unselected(
        name, emitted=emitted, arrival=arrival, attempt_status=status
    )
    report = _check(raw)
    assert report.aggregate_status == "deferred", report.model_dump_json(indent=2)
    assert all(check.status in ("passed", "deferred") for check in report.checks)


@pytest.mark.parametrize("name", ["question", "tool", "system"])
def test_unselected_emitted_reference_missing_candidate_is_incomplete(name):
    raw, result, _ = _unselected(name, emitted=True)
    raw["history"]["candidates"]["entries"] = [
        item
        for item in raw["history"]["candidates"]["entries"]
        if item["fact"]["fact_id"] != "fact-review"
    ]
    report = _check(raw)
    assert report.aggregate_status == "incomplete", report.model_dump_json(indent=2)
    assert "x3.emitted_candidate_missing" in _codes(report)
    assert _emitted_ref(name, result)["record_id"] == "fact-review"


@pytest.mark.parametrize("name", ["question", "tool", "system"])
def test_unselected_result_cannot_borrow_fact_from_another_valid_result(name):
    raw, result, _ = _unselected(name, emitted=True)
    if name == "system":
        # Give the original selected result its own independently valid emitted Fact.
        original = raw["history"]["system_results"][0]
        old_fact = deepcopy(raw["history"]["candidates"]["entries"][-1]["fact"])
        old_fact.update(fact_id="fact-original", attempt_id="attempt-1")
        old_fact["source"]["system_output_ref"] = deepcopy(original["result_ref"])
        original["output"] = deepcopy(result["output"])
        original["output"]["fact_ref"]["record_id"] = "fact-original"
        raw["history"]["proposed_selections"][0]["selected_fact_refs"] = [
            dict(record_kind="fact", record_id="fact-original")
        ]
        raw["history"]["candidates"]["entries"].append(
            dict(fact=old_fact, raw_bytes="")
        )
        borrowed = "fact-original"
    else:
        borrowed = "fact-1"
    raw["history"]["candidates"]["entries"] = [
        item
        for item in raw["history"]["candidates"]["entries"]
        if item["fact"]["fact_id"] != "fact-review"
    ]
    _emitted_ref(name, result)["record_id"] = borrowed
    sync_fact(raw)
    report = _check(raw)
    assert report.aggregate_status == "invalid", report.model_dump_json(indent=2)
    assert "x3.emitted_candidate_owner" in _codes(report)


@pytest.mark.parametrize(
    "name,mutation",
    [
        (name, mutation)
        for name in ("question", "tool", "system")
        for mutation in ("execution", "attempt", "source_result", "status", "output")
        if name != "question" or mutation not in ("status", "output")
    ],
)
def test_unselected_emitted_fact_exact_links_are_not_selection_dependent(
    name, mutation
):
    raw, result, _ = _unselected(name, emitted=True)
    fact = next(
        item["fact"]
        for item in raw["history"]["candidates"]["entries"]
        if item["fact"]["fact_id"] == "fact-review"
    )
    if mutation == "execution":
        fact["execution_id"] = "foreign-execution"
    elif mutation == "attempt":
        fact["attempt_id"] = "attempt-1"
    elif mutation == "source_result":
        fact["source"][_SOURCE_REF[name]] = deepcopy(
            raw["history"][_RESULT_FIELD[name]][0]["result_ref"]
        )
    elif mutation == "status":
        output = result["mapped_outputs"][0] if name == "tool" else result["output"]
        output["fact_status"] = "unknown"
    else:
        fact["source"]["output_name"] = "foreign-output"
    sync_fact(raw)
    report = _check(raw)
    assert report.aggregate_status == "invalid", report.model_dump_json(indent=2)


@pytest.mark.parametrize("name", ["question", "tool"])
@pytest.mark.parametrize(
    "mutation,expected",
    [
        ("node", "x3.result_node"),
        ("owner", "x3.result_attempt_owner"),
        ("object_profile", "x3.result_object_profile"),
    ],
)
def test_zero_fact_unselected_node_results_still_bind_static_owners(
    name, mutation, expected
):
    raw, result, attempt = _unselected(name)
    if mutation == "node":
        raw["history"]["node_runs"][-1]["node_id"] = "N03"
    elif mutation == "owner":
        attempt["owner"] = dict(
            owner_kind="system",
            requirement=deepcopy(
                fixture("system")["history"]["system_results"][0]["requirement"]
            ),
        )
    else:
        result["profile_ref"] = deepcopy(result["profile_ref"])
        result["profile_ref"]["digest"] = "b" * 64
    report = _check(raw)
    assert report.aggregate_status == "invalid", report.model_dump_json(indent=2)
    assert expected in _codes(report)


@pytest.mark.parametrize("mutation", ["plan", "step", "tool"])
def test_zero_fact_unselected_tool_result_binds_actual_plan_step_tool(mutation):
    raw, result, _ = _unselected("tool")
    if mutation == "step":
        result["step_id"] = "foreign-step"
    else:
        result["tool_plan_ref" if mutation == "plan" else "tool_ref"]["artifact_id"] = (
            "foreign"
        )
    report = _check(raw)
    assert report.aggregate_status == "invalid", report.model_dump_json(indent=2)
    assert (
        "x3.result_plan" if mutation == "plan" else "x3.result_step_tool"
    ) in _codes(report)


def test_zero_fact_unselected_system_result_binds_attempt_requirement():
    raw, _, attempt = _unselected("system")
    attempt["owner"]["requirement"]["capability_id"] = "foreign-system"
    report = _check(raw)
    assert report.aggregate_status == "invalid", report.model_dump_json(indent=2)
    assert "x3.system_attempt_requirement" in _codes(report)


@pytest.mark.parametrize("name", ["question", "tool", "system"])
def test_unselected_missing_attempt_remains_incomplete(name):
    raw, _, _ = _unselected(name)
    raw["history"]["attempts"].pop()
    report = _check(raw)
    assert report.aggregate_status == "incomplete", report.model_dump_json(indent=2)
    assert "x3.attempt_missing" in _codes(report)


@pytest.mark.parametrize("name", ["question", "tool"])
def test_unselected_missing_node_remains_incomplete(name):
    raw, _, _ = _unselected(name)
    raw["history"]["node_runs"].pop()
    report = _check(raw)
    assert report.aggregate_status == "incomplete", report.model_dump_json(indent=2)
    assert "x3.result_node_missing" in _codes(report)


@pytest.mark.parametrize("name", ["question", "tool"])
def test_unselected_emitted_fact_wrong_object_is_invalid(name):
    raw, _, _ = _unselected(name, emitted=True)
    fact = next(
        item["fact"]
        for item in raw["history"]["candidates"]["entries"]
        if item["fact"]["fact_id"] == "fact-review"
    )
    fact["object_id"] = "foreign-object"
    sync_fact(raw)
    report = _check(raw)
    assert report.aggregate_status == "invalid", report.model_dump_json(indent=2)
    assert "x3.emitted_candidate_owner" in _codes(report)


def test_unselected_n04_zero_fact_result_still_binds_effective_question_anchor():
    raw, result, _ = _unselected("question")
    result["effective_question_refs"][0]["artifact_id"] = "foreign-question"
    report = _check(raw)
    assert report.aggregate_status == "invalid", report.model_dump_json(indent=2)
    assert "x3.result_question_anchor" in _codes(report)


def _all_optional_questions(include_optional):
    raw = deepcopy(fixture("question"))
    catalogue = raw["producer_catalogues"][0]
    context = catalogue["context"]
    question = deepcopy(context["questions"][0])
    question["ref"]["artifact_id"] = "optional-question"
    question["definition"].update(
        question_id="optional-question", primary_fact_key="optional.parked"
    )
    context["questions"].append(question)
    raw["history"]["questions"].append(deepcopy(question))
    context["profiles"][0]["definition"]["question_refs"].append(
        deepcopy(question["ref"])
    )
    policy = next(
        item["artifact"]["policy"]
        for item in context["policies"]
        if item["artifact"]["policy"]["policy_kind"] == "question_selection"
    )
    policy.update(
        mode="mandatory_plus_optional",
        optional_source="all",
        optional_question_refs=[deepcopy(question["ref"])],
    )
    declaration = deepcopy(
        next(
            item
            for item in catalogue["registration"]["definition"]["declarations"]
            if item["producer_kind"] == "vlm_question"
        )
    )
    declaration.update(
        declaration_id="optional.primary",
        producer_id="optional-question",
        fact_key="optional.parked",
    )
    declaration["producer"]["question_ref"] = deepcopy(question["ref"])
    catalogue["registration"]["definition"]["declarations"].append(declaration)
    if include_optional:
        history = raw["history"]
        for anchor in (
            history["question_selections"][0],
            history["observation_results"][0],
        ):
            anchor["effective_question_refs"].append(deepcopy(question["ref"]))
        fact = deepcopy(history["candidates"]["entries"][0]["fact"])
        fact.update(
            fact_id="optional-fact",
            producer_id="optional-question",
            producer_declaration_id="optional.primary",
            fact_key="optional.parked",
        )
        fact["source"]["question_ref"] = deepcopy(question["ref"])
        history["candidates"]["entries"].append(dict(fact=fact, raw_bytes=""))
        ref = dict(record_kind="fact", record_id="optional-fact")
        history["observation_results"][0]["question_results"].append(
            dict(question_ref=deepcopy(question["ref"]), fact_ref=ref)
        )
        history["proposed_selections"][0]["selected_fact_refs"].append(deepcopy(ref))
        sync_fact(raw)
    return raw


def test_mandatory_plus_optional_source_all_cannot_omit_configured_question():
    report = _check(_all_optional_questions(False))
    assert report.aggregate_status == "invalid", report.model_dump_json(indent=2)
    assert "x3.all_questions" in _codes(report)


def test_mandatory_plus_optional_source_all_accepts_complete_configured_sequence():
    report = _check(_all_optional_questions(True))
    assert report.aggregate_status == "deferred", report.model_dump_json(indent=2)
    assert all(check.status in ("passed", "deferred") for check in report.checks)


def test_unselected_n04_can_persist_one_of_two_effective_question_outputs():
    raw, result, _ = _unselected(
        "question", emitted=True, seed=_all_optional_questions(True)
    )
    assert len(result["effective_question_refs"]) == 2
    assert len(result["question_results"]) == 1
    assert len(raw["history"]["observation_results"][0]["question_results"]) == 2
    report = _check(raw)
    assert report.aggregate_status == "deferred", report.model_dump_json(indent=2)


def _complete_two_tool_outputs():
    raw = deepcopy(fixture("tool"))
    catalogue = raw["producer_catalogues"][0]
    context = catalogue["context"]
    output = deepcopy(context["tools"][0]["definition"]["fact_outputs"][0])
    output.update(output_name="second", fact_key="second.relation")
    context["tools"][0]["definition"]["fact_outputs"].append(output)
    context["plans"][0]["definition"]["steps"][0]["fact_output_mappings"].append(
        dict(output_name="second", target_fact_key="second.relation")
    )
    raw["history"]["tools"] = deepcopy(context["tools"])
    raw["history"]["plans"] = deepcopy(context["plans"])
    declaration = deepcopy(
        next(
            item
            for item in catalogue["registration"]["definition"]["declarations"]
            if item["producer_kind"] == "tool"
        )
    )
    declaration.update(
        declaration_id="tool.second", output_name="second", fact_key="second.relation"
    )
    catalogue["registration"]["definition"]["declarations"].append(declaration)
    fact = deepcopy(raw["history"]["candidates"]["entries"][0]["fact"])
    fact.update(
        fact_id="fact-second",
        fact_key="second.relation",
        producer_declaration_id="tool.second",
    )
    fact["source"]["output_name"] = "second"
    raw["history"]["candidates"]["entries"].append(dict(fact=fact, raw_bytes=""))
    ref = dict(record_kind="fact", record_id="fact-second")
    raw["history"]["tool_results"][0]["mapped_outputs"].append(
        dict(
            disposition="emitted",
            output_name="second",
            fact_status="known",
            fact_ref=ref,
        )
    )
    raw["history"]["proposed_selections"][0]["selected_fact_refs"].append(deepcopy(ref))
    sync_fact(raw)
    return raw


def test_unselected_n05_can_persist_one_of_two_mapped_required_outputs():
    raw, result, _ = _unselected(
        "tool", emitted=True, seed=_complete_two_tool_outputs()
    )
    assert len(result["mapped_outputs"]) == 1
    assert len(raw["history"]["tool_results"][0]["mapped_outputs"]) == 2
    report = _check(raw)
    assert report.aggregate_status == "deferred", report.model_dump_json(indent=2)


def _system_object_unselected(emitted=False):
    raw, result, attempt = _unselected("system")
    profile = raw["producer_catalogues"][0]["context"]["profiles"][0]["ref"]
    obj = deepcopy(fixture("question")["history"]["objects"][0])
    raw["history"]["objects"].append(obj)
    result.update(
        scope="object",
        object_id=obj["object_ref"]["record_id"],
        profile_ref=deepcopy(profile),
        output_name="context",
    )
    result["output"]["output_name"] = "context"
    if emitted:
        fact = x2_fact(x2_fixture(), "system_object").model_dump(mode="json")
        fact.update(
            fact_id="fact-review", attempt_id=attempt["attempt_ref"]["record_id"]
        )
        fact["source"]["system_output_ref"] = deepcopy(result["result_ref"])
        raw["history"]["candidates"]["entries"].append(dict(fact=fact, raw_bytes=""))
        result["output"] = dict(
            disposition="emitted",
            output_name="context",
            fact_status="known",
            fact_ref=dict(record_kind="fact", record_id="fact-review"),
        )
        sync_fact(raw)
    return raw, result


@pytest.mark.parametrize("emitted", [False, True])
def test_unselected_system_object_result_preserves_valid_zero_or_partial_fact(emitted):
    raw, _ = _system_object_unselected(emitted)
    report = _check(raw)
    assert report.aggregate_status == "deferred", report.model_dump_json(indent=2)


def test_zero_fact_unselected_system_object_result_binds_exact_profile():
    raw, result = _system_object_unselected()
    result["profile_ref"]["digest"] = "b" * 64
    report = _check(raw)
    assert report.aggregate_status == "invalid", report.model_dump_json(indent=2)
    assert "x3.result_object_profile" in _codes(report)


def test_unselected_system_object_fact_wrong_object_is_invalid():
    raw, _ = _system_object_unselected(True)
    raw["history"]["candidates"]["entries"][0]["fact"]["object_id"] = "foreign-object"
    sync_fact(raw)
    report = _check(raw)
    assert report.aggregate_status == "invalid", report.model_dump_json(indent=2)
    assert "x3.emitted_candidate_owner" in _codes(report)
