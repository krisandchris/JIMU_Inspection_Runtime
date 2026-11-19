"""Validation cases for the typed journal boundary."""

import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator
from pydantic import ConfigDict, TypeAdapter, ValidationError

from jimu_runtime.bundle.trust import TrustPin
from jimu_runtime.domain.runtime.invocations import InvocationError
from jimu_runtime.domain.runtime.tool_executions import ToolExecution
from jimu_runtime.gateway.journal import (
    DispatchIntent,
    DispatchResult,
    ModelDispatchResult,
    ToolDispatchResult,
    ToolDispatchTarget,
    ToolImplementationObservation,
    tool_receipt_digest,
)

_FIXTURE = Path(__file__).parents[1] / "fixtures/wp01c/invocations/contracts.json"
_RESULT = TypeAdapter(DispatchResult, config=ConfigDict(hide_input_in_errors=True))


def test_receipt_schema_rejects_arbitrary_wire_and_secret_fields():
    schema = _RESULT.json_schema()
    Draft202012Validator.check_schema(schema)
    error = InvocationError(error_class="timeout", code="timeout", diagnostic_refs=())
    result = ModelDispatchResult(boundary="sent_unknown", error=error)
    payload = result.model_dump(mode="json")
    Draft202012Validator(schema).validate(payload)
    assert _RESULT.validate_json(result.model_dump_json()) == result
    foreign_field = "authorization_header"
    foreign_value = "synthetic-marker-must-not-appear"
    denied = {**payload, foreign_field: foreign_value}
    assert not Draft202012Validator(schema).is_valid(denied)
    with pytest.raises(ValidationError) as caught:
        _RESULT.validate_python(denied)
    assert foreign_value not in str(caught.value)


def test_partial_http_response_retains_count_without_whole_body_digest():
    error = InvocationError(error_class="timeout", code="timeout", diagnostic_refs=())
    partial = ModelDispatchResult(
        boundary="acknowledged",
        acknowledgement="http_headers",
        response_status=200,
        observed_response_bytes=256,
        error=error,
    )
    assert partial.response_digest is None
    for body_claim in (
        {"boundary": "sent_unknown", "response_digest": "a" * 64},
        {"boundary": "not_dispatched", "observed_response_bytes": 1},
        {"boundary": "acknowledged", "response_status": 200},
    ):
        with pytest.raises(ValidationError):
            _RESULT.validate_python({"kind": "model", "error": error, **body_claim})


def test_tool_origin_and_trusted_implementation_proof_are_strict():
    raw = json.loads(_FIXTURE.read_bytes())["tools"]["grounding"]
    tool = ToolExecution.model_validate_json(json.dumps(raw))
    pin = TrustPin(identity="implementation", version="1.0.0", sha256="b" * 64)
    target = ToolDispatchTarget(
        tool_ref=tool.tool_ref,
        invocation_origin=tool.invocation_origin,
        profile_ref=tool.profile_ref,
        capability_id="tool",
        contract_version="1.0.0",
        implementation_version="1.2.0",
        implementation_proof_ref=pin,
    )
    intent = DispatchIntent(
        dispatch_id="dispatch",
        operation_id="run-tool",
        trace_id="trace",
        request_digest="c" * 64,
        input_digest="d" * 64,
        response_contract_digest="e" * 64,
        target=target,
    )
    assert DispatchIntent.model_validate_json(intent.model_dump_json()) == intent
    invalid = intent.model_dump(mode="json")
    invalid["target"]["parent_attempt_id"] = "foreign-attempt"
    with pytest.raises(ValidationError, match="both parent identities"):
        DispatchIntent.model_validate_json(json.dumps(invalid))
    assert (
        ToolImplementationObservation(
            implementation_version="1.2.0", proof_ref=pin
        ).proof_ref
        == pin
    )


def test_unrecognized_tool_diagnostic_text_cannot_enter_the_journal():
    payload = json.loads(_FIXTURE.read_bytes())["tools"]["grounding"]
    marker = "raw-worker-diagnostic-marker"
    payload["attempt_result"]["result"]["diagnostics"] = [
        {"code": "fixture", "message": marker, "path": [], "related_refs": []},
    ]
    execution = ToolExecution.model_validate_json(json.dumps(payload))
    with pytest.raises(ValidationError, match="unnormalized diagnostics") as caught:
        ToolDispatchResult(
            boundary="acknowledged",
            acknowledgement="tool_process",
            implementation=ToolImplementationObservation(
                implementation_version="1.0.0",
                proof_ref=TrustPin(identity="test", version="1.0.0", sha256="a" * 64),
            ),
            tool_execution_digest=tool_receipt_digest(execution),
            execution=execution,
        )
    assert marker not in str(caught.value)
