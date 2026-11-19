"""Invalid audit state, strict dispatch fields and failure/Fact boundaries."""

import json
from pathlib import Path

import pytest
from pydantic import TypeAdapter, ValidationError

from jimu_runtime.domain.runtime.invocations import ModelInvocation
from jimu_runtime.domain.runtime.selection import QuestionSelectionRecord
from jimu_runtime.domain.runtime.tool_executions import ToolExecution, ToolResult

_FIXTURES = (
    Path(__file__).parents[1] / "fixtures" / "wp01c" / "invocations" / "contracts.json"
)


def dump(family, case):
    return json.loads(_FIXTURES.read_text())[family][case]


@pytest.mark.parametrize(
    ("key", "rewrites"),
    [
        ("observation", {"provider_request_id": "another"}),
        ("observation", {"usage": {"input_tokens": True}}),
        ("observation", {"usage": {"input_tokens": -1}}),
        ("observation", {"contract_snapshot": None}),
        ("observation", {"purposes": ["object_observation", "object_grounding"]}),
        ("observation", {"completed_at": "2026-04-01T11:59:00Z"}),
        ("observation", {"evidence_refs": []}),
        ("shared", {"purposes": ["profile_discovery", "profile_discovery"]}),
        (
            "additional",
            {
                "source_discovery_invocation_ref": {
                    "record_kind": "model_invocation",
                    "record_id": "proposal-1",
                }
            },
        ),
        ("undispatched", {"provider_request_id": "fake"}),
    ],
)
def test_model_invocation_rejects_unverifiable_wire_combinations(key, rewrites):
    with pytest.raises(ValidationError):
        ModelInvocation.model_validate_json(
            json.dumps({**dump("models", key), **rewrites})
        )


def test_unavailable_version_does_not_copy_configured_versions():
    payload = dump("models", "unavailable")
    payload["actual_provider"]["model_version"] = "1.0.0"
    with pytest.raises(ValidationError):
        ModelInvocation.model_validate_json(json.dumps(payload))


def test_timeout_alone_does_not_authorize_fallback():
    payload = dump("models", "fallback")
    payload["route_trace"]["evaluation_refs"] = []
    with pytest.raises(ValidationError):
        ModelInvocation.model_validate_json(json.dumps(payload))


@pytest.mark.parametrize(
    ("key", "rewrites"),
    [
        ("all", {"fallback_applied": 0}),
        (
            "all",
            {
                "model_invocation_ref": {
                    "record_kind": "model_invocation",
                    "record_id": "fake",
                }
            },
        ),
        ("context", {"context_selection_rules": []}),
        ("context", {"excluded_question_reasons": []}),
        ("additional", {"selection_invocation_mode": "shared_n02"}),
        ("shared", {"selected_optional_question_refs": []}),
        ("fallback_shared", {"selection_call_policy": {"mode": "explicit_additional"}}),
    ],
)
def test_question_selection_prohibits_ambiguous_or_unsubstantiated_variants(
    key, rewrites
):
    payload = dump("selections", key)
    with pytest.raises(ValidationError):
        TypeAdapter(QuestionSelectionRecord).validate_json(
            json.dumps({**payload, **rewrites})
        )


@pytest.mark.parametrize("dispatched", [0, 1, "false", None])
def test_fallback_dispatch_requires_a_real_boolean(dispatched):
    payload = dump("selections", "fallback_context")
    payload["fallback_evidence"]["dispatched"] = dispatched
    with pytest.raises(ValidationError):
        TypeAdapter(QuestionSelectionRecord).validate_json(json.dumps(payload))


def test_undispatched_selection_forbids_trigger_model_call():
    payload = dump("selections", "fallback_context")
    payload["fallback_evidence"]["trigger_invocation_ref"] = payload[
        "discovery_model_invocation_ref"
    ]
    with pytest.raises(ValidationError):
        TypeAdapter(QuestionSelectionRecord).validate_json(json.dumps(payload))


@pytest.mark.parametrize(
    ("key", "rewrites"),
    [
        (
            "grounding",
            {"generated_fact_refs": [{"record_kind": "fact", "record_id": "bad"}]},
        ),
        ("grounding", {"object_ref": {"record_kind": "object", "record_id": "bad"}}),
        ("step", {"scope": "execution"}),
        ("step", {"status": "failed"}),
        ("step", {"implementation_version_unavailable_reason": "spurious"}),
        ("failure", {"actual_implementation_version": "1.0.0"}),
        (
            "failure",
            {
                "output_refs": [
                    {
                        "artifact_ref": {"record_kind": "fact", "record_id": "bad"},
                        "digest": "a" * 64,
                    }
                ]
            },
        ),
        ("undispatched", {"resource_usage": {"cpu_time_ms": 0}}),
    ],
)
def test_tool_rejects_invalid_scope_or_dispatch_evidence(key, rewrites):
    with pytest.raises(ValidationError):
        ToolExecution.model_validate_json(
            json.dumps({**dump("tools", key), **rewrites})
        )


@pytest.mark.parametrize("status", ["failed", "partial", "succeeded"])
def test_tool_result_cannot_claim_known_without_available_payload(status):
    payload = dump("tools", "step")["attempt_result"]["result"]
    payload["status"] = status
    payload["output_states"] = [
        {"output_name": "state", "fact_status": "known", "evidence_refs": []}
    ]
    if status == "failed":
        payload["error"] = {
            "error_class": "timeout",
            "code": "timed-out",
            "diagnostic_refs": [],
        }
    with pytest.raises(ValidationError):
        TypeAdapter(ToolResult).validate_json(json.dumps(payload))


def test_non_known_result_requires_reason_and_forbids_a_value():
    payload = dump("tools", "step")["attempt_result"]["result"]
    payload["output_states"] = [
        {"output_name": "state", "fact_status": "unknown", "evidence_refs": []}
    ]
    with pytest.raises(ValidationError):
        TypeAdapter(ToolResult).validate_json(json.dumps(payload))
    payload["output_states"][0].update(
        reason={
            "reason_code": "occluded",
            "message": "Synthetic unknown",
            "diagnostic_refs": [],
        },
        value={"kind": "boolean", "value": False},
    )
    with pytest.raises(ValidationError):
        TypeAdapter(ToolResult).validate_json(json.dumps(payload))
