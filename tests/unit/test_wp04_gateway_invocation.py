"""Physical responses are auditable independently of usable model output."""

from dataclasses import replace

import pytest
from fixtures.wp04.routing_support import AT, routing_fixture

from jimu_runtime.bundle.integrity import sha256_digest
from jimu_runtime.domain.runtime.invocations import (
    FailedInvocation,
    InvocationError,
    ModelInvocation,
    RegistryVerifiedProviderIdentity,
    ReportedProviderIdentity,
    SucceededInvocation,
    UnavailableProviderIdentity,
    UndispatchedInvocation,
)
from jimu_runtime.gateway.invocation import (
    DispatchRequest,
    ProviderObservation,
    TransportResult,
    project_dispatch,
)

WIRE_BODY = b'{"choice":{"value":"synthetic"}}'
PARSED_OUTPUT = b'{"value":"synthetic"}'
REQUEST = b'{"model":"synthetic-model","payload":"synthetic"}'


def _project(result, *, trusted_version=True):
    route = routing_fixture(version_evidence=trusted_version).primary_route()
    request = DispatchRequest("dispatch-1", route, REQUEST)
    assert route.operation.contract_snapshot is not None
    return project_dispatch(
        request,
        result,
        started_at=AT,
        completed_at=AT,
        evidence_refs=(route.operation.contract_snapshot.response_schema_evidence_ref,),
    )


def _successful_result(observation=None):
    return TransportResult(
        dispatched=True,
        observation=observation,
        parsed_content=PARSED_OUTPUT,
        response_status=200,
        observed_response_bytes=len(WIRE_BODY),
        validated_response_contract_digest="b" * 64,
        response_digest=sha256_digest(WIRE_BODY),
    )


def test_success_uses_observed_identity_and_exact_wire_digest():
    observation = ProviderObservation(
        provider_request_id="provider-pid",
        model_id="synthetic-model",
        deployment_version="1.0.0",
        model_version="1.0.0",
    )
    result = _project(_successful_result(observation), trusted_version=False)
    assert result.request_digest == sha256_digest(REQUEST)
    assert result.response_digest == sha256_digest(WIRE_BODY)
    assert result.boundary == "acknowledged"
    assert result.output == PARSED_OUTPUT
    assert result.error is None
    invocation = result.invocation
    assert isinstance(invocation.actual_provider, ReportedProviderIdentity)
    assert isinstance(invocation.outcome, SucceededInvocation)
    assert (
        invocation.actual_provider.version_evidence == observation.provider_request_id
    )
    assert invocation.outcome.response_digest == result.response_digest
    assert invocation.model_invocation_id == "dispatch-1"
    assert ModelInvocation.model_validate_json(invocation.stable_json()) == invocation
    assert "payload" not in repr(
        DispatchRequest("d1", routing_fixture().primary_route(), REQUEST)
    )
    assert repr(PARSED_OUTPUT) not in repr(result)


def test_model_versions_may_be_resolved_only_by_exact_independent_host_proof():
    observed = ProviderObservation(model_id="synthetic-model")
    trusted = _project(_successful_result(observed), trusted_version=True)
    unresolved = _project(_successful_result(observed), trusted_version=False)
    assert isinstance(
        trusted.invocation.actual_provider, RegistryVerifiedProviderIdentity
    )
    assert trusted.output == PARSED_OUTPUT
    assert isinstance(
        unresolved.invocation.actual_provider, UnavailableProviderIdentity
    )
    assert isinstance(unresolved.invocation.outcome, FailedInvocation)
    assert unresolved.error.code == "model.actual_version_unavailable"
    assert unresolved.output is None
    assert unresolved.response_digest == trusted.response_digest


@pytest.mark.parametrize(
    ("observation", "code"),
    [
        (None, "model.actual_identity_unavailable"),
        (
            ProviderObservation(model_id="different-model"),
            "model.actual_identity_mismatch",
        ),
        (
            ProviderObservation(model_id="synthetic-model", model_version="2.0.0"),
            "model.actual_identity_mismatch",
        ),
    ],
)
def test_wire_identity_absence_or_contradiction_keeps_evidence_without_output(
    observation, code
):
    result = _project(
        _successful_result(observation), trusted_version=observation is not None
    )
    assert result.boundary == "acknowledged"
    assert result.response_digest == sha256_digest(WIRE_BODY)
    assert result.transport_result.observation == observation
    assert result.invocation is None
    assert result.output is None
    assert result.error.code == code


@pytest.mark.parametrize(
    ("error_class", "status", "dispatched", "boundary"),
    [
        ("timeout", None, True, "sent_unknown"),
        ("transport", None, True, "sent_unknown"),
        ("unavailable", None, False, "not_dispatched"),
        ("rate_limit", 429, True, "acknowledged"),
        ("validation", 200, True, "acknowledged"),
    ],
)
def test_physical_outcomes_have_distinct_boundaries_and_keep_original_error(
    error_class, status, dispatched, boundary
):
    error = InvocationError(
        error_class=error_class, code="wire-failure", diagnostic_refs=()
    )
    response = WIRE_BODY if status is not None else None
    result = _project(
        TransportResult(
            dispatched=dispatched,
            error=error,
            response_status=status,
            response_bytes=response,
        )
    )
    assert result.boundary == boundary
    assert result.error == error
    assert result.output is None
    assert result.response_digest == (
        None if response is None else sha256_digest(response)
    )
    if dispatched:
        assert isinstance(result.invocation.outcome, FailedInvocation)
        assert isinstance(
            result.invocation.actual_provider, RegistryVerifiedProviderIdentity
        )
    else:
        assert isinstance(result.invocation.outcome, UndispatchedInvocation)
        assert result.invocation.actual_provider is None
    assert result.invocation.outcome.error == error


def test_a_partial_response_records_observed_bytes_but_no_complete_digest():
    result = _project(
        TransportResult(
            dispatched=True,
            error=InvocationError(
                error_class="timeout", code="slow-body", diagnostic_refs=()
            ),
            response_status=200,
            observed_response_bytes=128,
        )
    )
    assert result.boundary == "acknowledged"
    assert result.transport_result.observed_response_bytes == 128
    assert result.response_digest is None
    assert isinstance(result.invocation.outcome, FailedInvocation)


def test_success_without_validated_contents_is_a_projection_failure():
    result = _project(replace(_successful_result(), parsed_content=None))
    assert result.error.code == "model.validated_output_missing"
    assert isinstance(result.invocation.outcome, FailedInvocation)
    assert result.output is None
    assert result.response_digest == sha256_digest(WIRE_BODY)


def test_undispatched_or_contradictory_physical_results_are_rejected():
    with pytest.raises(ValueError, match="pre-send"):
        TransportResult(dispatched=False)
    with pytest.raises(ValueError, match="pre-send"):
        TransportResult(
            dispatched=False,
            error=InvocationError(
                error_class="unavailable", code="not-sent", diagnostic_refs=()
            ),
            response_status=200,
        )
    with pytest.raises(ValueError, match="disagrees"):
        TransportResult(
            dispatched=True,
            response_bytes=WIRE_BODY,
            response_digest=sha256_digest(PARSED_OUTPUT),
        )


def test_omitted_wire_bytes_keep_digest_only_for_a_complete_response():
    kept = _project(
        replace(
            _successful_result(),
            response_bytes_omission_reason="policy_disabled",
        )
    )
    assert kept.response_digest == sha256_digest(WIRE_BODY)
    assert kept.transport_result.response_bytes is None
    assert kept.output == PARSED_OUTPUT

    with pytest.raises(ValueError, match="incomplete response"):
        replace(
            _successful_result(),
            parsed_content=None,
            response_bytes_omission_reason="incomplete",
        )
    with pytest.raises(ValueError, match="retained response"):
        replace(
            _successful_result(),
            response_bytes=WIRE_BODY,
            response_bytes_omission_reason="policy_disabled",
        )


def test_payload_budget_is_checked_before_transport():
    route = routing_fixture().primary_route()
    maximum = route.resource_requirements.input_byte_limit
    DispatchRequest("exact-limit", route, b"x" * maximum)
    with pytest.raises(ValueError, match="pinned input byte limit"):
        DispatchRequest("over-limit", route, b"x" * (maximum + 1))


@pytest.mark.parametrize("omission", ["unvalidated", "credential_echo"])
def test_unusable_content_cannot_escape_the_transport_contract(omission):
    with pytest.raises(ValueError, match="cannot be retained as parsed output"):
        replace(
            _successful_result(),
            parsed_content=b"unusable",
            response_bytes_omission_reason=omission,
        )
    rejected = _project(
        replace(
            _successful_result(),
            parsed_content=None,
            validated_response_contract_digest=None,
            response_bytes_omission_reason=omission,
        )
    )
    assert rejected.output is rejected.transport_result.parsed_content is None
    assert rejected.response_digest == sha256_digest(WIRE_BODY)


@pytest.mark.parametrize("contract_digest", [None, "f" * 64])
def test_validated_content_must_be_bound_to_exact_response_schema(contract_digest):
    outcome = _project(
        replace(
            _successful_result(), validated_response_contract_digest=contract_digest
        )
    )
    assert outcome.error.code == "model.response_contract_unverified"
    assert outcome.output is outcome.transport_result.parsed_content is None
    assert isinstance(outcome.invocation.outcome, FailedInvocation)


def test_http_error_status_does_not_qualify_as_success():
    with pytest.raises(ValueError, match="requires a normalized error"):
        replace(_successful_result(), response_status=429)
    error = InvocationError(
        error_class="rate_limit", code="rate_limited", diagnostic_refs=()
    )
    outcome = _project(
        replace(
            _successful_result(),
            response_status=429,
            error=error,
        )
    )
    assert outcome.error == error
    assert outcome.output is outcome.transport_result.parsed_content is None
    assert outcome.response_digest == sha256_digest(WIRE_BODY)


def test_received_prefix_cannot_be_claimed_as_complete_body():
    with pytest.raises(ValueError, match="observed response length"):
        TransportResult(
            dispatched=True,
            response_bytes=b"prefix",
            observed_response_bytes=21,
        )
    result = _project(
        TransportResult(
            dispatched=True,
            error=InvocationError(
                error_class="timeout", code="late", diagnostic_refs=()
            ),
            observed_response_bytes=21,
            response_bytes_omission_reason="incomplete",
        )
    )
    assert result.boundary == "acknowledged"
    assert result.response_digest is None


def test_route_authorization_deadline_retains_physical_evidence_and_rejects_output():
    from datetime import timedelta

    from jimu_runtime.gateway.routing import plan_primary

    case = routing_fixture()
    ownership, *others = case.current.ownership
    expiry = AT + timedelta(seconds=1)
    registry = case.current.model_copy(
        update={
            "ownership": (ownership.model_copy(update={"revoked_at": expiry}), *others)
        }
    )
    route = plan_primary(
        case.policy, case.operation, case.attempt_at(1), registry, case.budget, AT
    )
    assert route.authorization_valid_until == expiry
    request = DispatchRequest("expired-start", route, REQUEST)
    result = project_dispatch(
        request,
        _successful_result(),
        started_at=expiry,
        completed_at=expiry,
        evidence_refs=(route.operation.contract_snapshot.response_schema_evidence_ref,),
    )
    assert result.boundary == "acknowledged"
    assert result.error.code == "model.route_authorization_expired"
    assert result.output is result.transport_result.parsed_content is None
    assert result.response_digest == sha256_digest(WIRE_BODY)


def test_partial_question_diagnostics_cannot_name_an_unpinned_slot():
    accepted = _project(
        replace(
            _successful_result(),
            missing_question_slots=("question",),
            invalid_question_slots=("vehicle-type",),
        )
    )
    assert accepted.output
    assert accepted.transport_result.invalid_question_slots == ("vehicle-type",)
    rejected = _project(
        replace(
            _successful_result(),
            missing_question_slots=("foreign-question",),
        )
    )
    assert rejected.error.code == "model.question_slot_scope_invalid"
    assert rejected.output is None


@pytest.mark.parametrize(
    "bad_field",
    [
        {"observed_response_bytes": True},
        {"observed_response_bytes": 5.5},
        {"response_status": 200.5},
        {"retry_after_seconds": True},
    ],
)
def test_generic_receipt_requires_exact_numeric_types(bad_field):
    with pytest.raises(ValueError):
        TransportResult(dispatched=True, **bad_field)
