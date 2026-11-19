"""Current evidence may decline, but cannot replace, an immutable route."""

import pytest
from fixtures.wp04.routing_support import (
    AT,
    DIGEST,
    LOCAL,
    OTHER,
    REMOTE,
    finish_call,
    routing_fixture,
)

from jimu_runtime.domain.enums import AttemptStatus
from jimu_runtime.domain.runtime.invocations import FallbackProviderRoute
from jimu_runtime.gateway.routing import (
    RouteRejected,
    plan_fallback,
    plan_primary,
    plan_retry,
)


def _next(
    case, history, *, at=AT, current=None, budget=None, operation=None, attempt=None
):
    return plan_fallback(
        case.policy,
        operation or case.operation,
        attempt or case.attempt_at(len(history) + 1),
        current or case.current,
        budget or case.budget,
        at,
        history=history,
    )


def test_first_call_selects_explicit_remote_primary_and_rejects_changed_operation():
    case = routing_fixture(edges=())
    route = case.primary_route()
    assert route.selected.deployment_id == REMOTE
    assert route.configured.primary_deployment_id == REMOTE
    assert route.trace.route_kind == "primary"
    assert route.binding == case.policy.binding.definition
    assert route.resource_requirements.input_byte_limit == 65536
    altered = case.operation.model_copy(update={"execution_input_digest": "f" * 64})
    with pytest.raises(RouteRejected) as rejected:
        plan_primary(
            case.policy, altered, case.attempt_at(1), case.current, case.budget, AT
        )
    assert rejected.value.code == "model.operation_pin_mismatch"


@pytest.mark.parametrize(
    "fault", ["missing", "revoked", "endpoint_changed", "owner_changed"]
)
def test_primary_checks_authoritative_ownership_at_dispatch(fault):
    case = routing_fixture()
    primary, *rest = case.current.ownership
    if fault == "missing":
        rows = tuple(rest)
    elif fault == "revoked":
        rows = (primary.model_copy(update={"revoked_at": AT}), *rest)
    else:
        field = (
            "endpoint_descriptor_ref" if fault == "endpoint_changed" else "owner_ref"
        )
        changed = getattr(primary.ownership, field).model_copy(
            update={"sha256": "a" * 64}
        )
        proof = primary.ownership.model_copy(update={field: changed})
        rows = (primary.model_copy(update={"ownership": proof}), *rest)
    current = case.current.model_copy(update={"ownership": rows})
    with pytest.raises(RouteRejected, match="ownership"):
        plan_primary(
            case.policy, case.operation, case.attempt_at(1), current, case.budget, AT
        )


@pytest.mark.parametrize(
    "fault", ["version", "contract", "feature", "revoked", "snapshot"]
)
def test_current_registration_may_not_replace_pinned_deployment(fault):
    case = routing_fixture()
    primary, *rest = case.current.capabilities.model_deployments
    if fault == "version":
        entries = {
            "identity": primary.identity.model_copy(update={"model_version": "2.0.0"})
        }
    elif fault == "contract":
        (contract,) = primary.contracts
        entries = {
            "contracts": (
                contract.model_copy(
                    update={"output_schema_ref": contract.input_schema_ref}
                ),
            )
        }
    elif fault == "feature":
        entries = {"supported_features": ()}
    elif fault == "revoked":
        entries = {"revoked_at": AT}
    else:
        entries = {}
    update = {"model_deployments": (primary.model_copy(update=entries), *rest)}
    if fault == "snapshot":
        update["valid_until"] = AT
    current = case.current.model_copy(
        update={"capabilities": case.current.capabilities.model_copy(update=update)}
    )
    with pytest.raises(RouteRejected):
        plan_primary(
            case.policy, case.operation, case.attempt_at(1), current, case.budget, AT
        )


def test_local_primary_contains_separate_valid_exception_proof():
    case = routing_fixture(local_primary=True)
    route = case.primary_route()
    assert route.selected.deployment_id == LOCAL
    assert route.trace.local_primary_exception is not None
    assert route.trace.routing_mode == "local_primary_exception"
    assert (
        finish_call(route).invocation.actual_provider.version_resolution
        == "registry_verified"
    )


@pytest.mark.parametrize("fault", ["revocation", "expiry", "scope", "version"])
def test_local_primary_deploys_only_under_exact_current_review(fault):
    case = routing_fixture(local_primary=True)
    (row,) = case.current.capabilities.model_reviews
    approval, *fallbacks = row.metadata.assessments
    if fault == "revocation":
        row = row.model_copy(update={"revoked_at": AT})
    elif fault == "expiry":
        row = row.model_copy(
            update={"metadata": row.metadata.model_copy(update={"valid_until": AT})}
        )
    else:
        if fault == "version":
            updated = {
                "primary": approval.primary.model_copy(
                    update={"model_version": "2.0.0"}
                )
            }
        else:
            wrong = approval.scope.binding_refs[0].model_copy(
                update={"digest": "f" * 64}
            )
            updated = {
                "scope": approval.scope.model_copy(update={"binding_refs": (wrong,)})
            }
        row = row.model_copy(
            update={
                "metadata": row.metadata.model_copy(
                    update={
                        "assessments": (approval.model_copy(update=updated), *fallbacks)
                    }
                )
            }
        )
    current = case.current.model_copy(
        update={
            "capabilities": case.current.capabilities.model_copy(
                update={"model_reviews": (row,)}
            )
        }
    )
    with pytest.raises(RouteRejected, match="Local primary"):
        plan_primary(
            case.policy, case.operation, case.attempt_at(1), current, case.budget, AT
        )


def test_timeout_can_hop_across_exact_independently_evaluated_deployments():
    case = routing_fixture(
        edges=((REMOTE, LOCAL), (LOCAL, OTHER), (OTHER, LOCAL), (OTHER, REMOTE))
    )
    first = finish_call(case.primary_route())
    second_route = _next(case, (first,))
    assert isinstance(second_route.trace, FallbackProviderRoute)
    assert second_route.selected.deployment_id == LOCAL
    assert (
        second_route.selected.model_version
        == first.invocation.actual_provider.model_version
    )
    assert second_route.trace.from_deployment_id == REMOTE
    assert (
        second_route.trace.trigger_invocation_ref.record_id
        == first.invocation.model_invocation_id
    )
    second = finish_call(second_route)
    third = _next(case, (first, second))
    assert third.selected.deployment_id == OTHER
    assert third.trace.from_deployment_id == LOCAL
    assert (
        third.trace.trigger_invocation_ref.record_id
        == second.invocation.model_invocation_id
    )
    with pytest.raises(RouteRejected) as error:
        _next(case, (first, second, finish_call(third)))
    assert error.value.code == "model.fallback_unavailable"


@pytest.mark.parametrize(
    "fault",
    [
        "same_attempt",
        "operation_id",
        "input_pin",
        "untrusted_attempt",
        "foreign_execution",
    ],
)
def test_fallback_requires_earlier_terminal_attempt_of_exact_operation(fault):
    case = routing_fixture()
    first = finish_call(case.primary_route())
    attempt = case.attempt_at(2)
    operation = case.operation
    if fault == "same_attempt":
        attempt = case.attempt_at(1)
    elif fault == "operation_id":
        operation = operation.model_copy(update={"operation_id": "other-operation"})
    elif fault == "input_pin":
        operation = operation.model_copy(update={"input_digest": DIGEST})
    elif fault == "untrusted_attempt":
        first = type(first)(
            operation,
            first.attempt.model_copy(update={"status": AttemptStatus.SUPERSEDED}),
            first.invocation,
        )
    else:
        first = type(first)(
            operation,
            first.attempt,
            first.invocation.model_copy(update={"execution_id": "execution-elsewhere"}),
        )
    with pytest.raises(RouteRejected) as error:
        _next(case, (first,), attempt=attempt, operation=operation)
    assert error.value.code == "model.fallback_history_invalid"


@pytest.mark.parametrize(
    "fault", ["ambiguous", "disabled", "error", "revoked", "budget", "bad_assessment"]
)
def test_fallback_fails_closed_without_one_current_legal_edge(fault):
    case = routing_fixture(
        edges=((REMOTE, LOCAL), (REMOTE, OTHER))
        if fault == "ambiguous"
        else ()
        if fault == "disabled"
        else ((REMOTE, LOCAL),)
    )
    first = finish_call(
        case.primary_route(),
        error_class="rate_limit" if fault == "error" else "timeout",
    )
    current = case.current
    budget = case.budget
    if fault in {"revoked", "bad_assessment"}:
        (row,) = current.capabilities.model_reviews
        if fault == "revoked":
            row = row.model_copy(update={"revoked_at": AT})
        else:
            approval, evaluation = row.metadata.assessments
            evaluation = evaluation.model_copy(
                update={"input_schema_ref": evaluation.output_schema_ref}
            )
            row = row.model_copy(
                update={
                    "metadata": row.metadata.model_copy(
                        update={"assessments": (approval, evaluation)}
                    )
                }
            )
        current = current.model_copy(
            update={
                "capabilities": current.capabilities.model_copy(
                    update={"model_reviews": (row,)}
                )
            }
        )
    elif fault == "budget":
        budget = budget.model_copy(update={"operation_remaining": 0})
    codes = {
        "ambiguous": "model.fallback_ambiguous",
        "disabled": "model.fallback_not_permitted",
        "error": "model.fallback_not_permitted",
        "revoked": "model.fallback_unavailable",
        "bad_assessment": "model.fallback_unavailable",
        "budget": "model.call_budget_exhausted",
    }
    with pytest.raises(RouteRejected) as error:
        _next(case, (first,), current=current, budget=budget)
    assert error.value.code == codes[fault]


def test_explicit_retry_preserves_selected_deployment_and_call_lineage():
    case = routing_fixture()
    first = finish_call(case.primary_route())
    alternative = _next(case, (first,))
    second = finish_call(alternative)
    args = (
        case.policy,
        case.operation,
        case.attempt_at(3),
        case.current,
        case.budget,
        AT,
    )
    retry = plan_retry(*args, history=(first, second), retryable_errors=("timeout",))
    assert retry.trace == alternative.trace
    assert retry.selected == alternative.selected
    assert retry.attempt != alternative.attempt
    retried = finish_call(retry)
    assert (
        retried.invocation.model_invocation_id != second.invocation.model_invocation_id
    )
    with pytest.raises(RouteRejected) as error:
        plan_retry(*args, history=(first, second), retryable_errors=("transport",))
    assert error.value.code == "model.retry_not_permitted"


def test_load_route_policy_from_a_compiled_admitted_execution(tmp_path):
    from jimu_runtime.bundle.trust import TrustPin
    from jimu_runtime.gateway.routing import DeploymentOwnership, RoutePolicy
    from tests.fixtures.wp03.pins.support import accepted_scenario

    execution = accepted_scenario(tmp_path).resolve()
    actual = execution.model_deployments[0]
    owner_ref = TrustPin(identity="fixture-owner", version="1.0.0", sha256=DIGEST)
    ownership = tuple(
        DeploymentOwnership(
            deployment=deployment,
            owner_ref=owner_ref,
            endpoint_descriptor_ref=TrustPin(
                identity=deployment.deployment_id, version="1.0.0", sha256=DIGEST
            ),
            model_id="synthetic-model",
            version_evidence=None,
        )
        for deployment in actual.deployments
    )
    created = RoutePolicy.from_execution(execution, actual.binding_ref, ownership)
    assert created.configured_pin in execution.pins.model_pins
    assert created.execution_input_digest == execution.pins.input_digest
    assert created.resolved == actual
    assert created.ownership == ownership
    assert created.resources.input_byte_limit > 0


def test_local_primary_can_use_explicitly_reviewed_remote_fallback_on_rate_limit():
    case = routing_fixture(
        local_primary=True, edges=((LOCAL, REMOTE),), triggers=("rate_limit",)
    )
    local = finish_call(case.primary_route(), error_class="rate_limit")
    remote = _next(case, (local,))
    assert remote.selected.provider_kind == "remote"
    assert (
        remote.trace.local_primary_exception
        == local.invocation.route_trace.local_primary_exception
    )
    assert remote.trace.trigger_error == "rate_limit"
    fallback = finish_call(remote, error_class="rate_limit")
    assert fallback.invocation.actual_provider.provider_kind == "remote"


def test_fallback_considers_only_available_edges_inside_deployment_budget():
    case = routing_fixture(edges=((REMOTE, LOCAL), (REMOTE, OTHER)))
    first = finish_call(case.primary_route())
    remaining = tuple(
        row.model_copy(update={"remaining": 0}) if row.deployment_id == LOCAL else row
        for row in case.budget.deployments
    )
    limited = case.budget.model_copy(update={"deployments": remaining})
    assert _next(case, (first,), budget=limited).selected.deployment_id == OTHER


def _domain_audit_args(route):
    return {
        "node_run": route.operation.node_run,
        "attempt": route.attempt,
        "binding": route.policy.binding,
        "provider_policy": route.policy.provider_registration,
        "fallback_policy": route.policy.fallback_registration,
        "prior_invocations": tuple(row.invocation for row in route.history),
        "reviews": route.reviews,
        "region_ref": route.operation.region_ref,
    }


def test_local_exception_fallback_preserves_exact_primary_review_with_multiple_allowed_versions():
    from jimu_runtime.domain.validation.invocations import validate_model_invocation

    case = routing_fixture(
        local_primary=True,
        edges=((LOCAL, REMOTE),),
        multi_version_deployments=(LOCAL,),
    )
    prior = finish_call(case.primary_route())
    route = _next(case, (prior,))
    fallback = finish_call(route).invocation
    assert fallback.actual_provider.provider_kind == "remote"
    assert fallback.route_trace.local_primary_exception is not None
    args = _domain_audit_args(route)
    validate_model_invocation(
        fallback, **args, resolved_deployments=case.policy.resolved.deployments
    )
    with pytest.raises(ValueError, match="local primary review does not cover"):
        validate_model_invocation(fallback, **args)


def test_undispatched_fallback_witnesses_never_fabricate_an_actual_provider():
    from jimu_runtime.domain.runtime.invocations import InvocationError
    from jimu_runtime.domain.validation.invocations import validate_model_invocation
    from jimu_runtime.gateway.invocation import (
        DispatchRequest,
        TransportResult,
        project_dispatch,
    )
    from jimu_runtime.gateway.routing import RecordedInvocation

    case = routing_fixture(
        triggers=("unavailable",),
        multi_version_deployments=(REMOTE, LOCAL),
        version_evidence=False,
    )
    evidence = (case.operation.contract_snapshot.response_schema_evidence_ref,)
    result = TransportResult(
        False,
        error=InvocationError(
            error_class="unavailable", code="test-unavailable", diagnostic_refs=()
        ),
    )
    primary = case.primary_route()
    first = project_dispatch(
        DispatchRequest("first-unsent", primary, b"request"),
        result,
        started_at=AT,
        completed_at=AT,
        evidence_refs=evidence,
    )
    assert first.invocation.outcome.status == "not_dispatched"
    prior = RecordedInvocation(
        case.operation,
        primary.attempt.model_copy(update={"status": AttemptStatus.FAILED}),
        first.invocation,
    )
    fallback = _next(case, (prior,))
    second = project_dispatch(
        DispatchRequest("second-unsent", fallback, b"request"),
        result,
        started_at=AT,
        completed_at=AT,
        evidence_refs=evidence,
    )
    assert second.invocation.outcome.status == "not_dispatched"
    assert second.invocation.actual_provider is prior.invocation.actual_provider is None
    assert second.output is None
    with pytest.raises(ValueError, match="fallback review does not cover"):
        validate_model_invocation(second.invocation, **_domain_audit_args(fallback))


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("provider_id", "wrong-owner"),
        ("provider_kind", "local"),
        ("deployment_id", "wrong-deployment"),
        ("logical_model_id", "wrong-logical-model"),
        ("deployment_version", "2.0.0"),
        ("model_version", "3.0.0"),
    ],
)
def test_optional_execution_review_witness_rejects_mismatched_deployment_identity(
    field, value
):
    from jimu_runtime.domain.validation.invocations import validate_model_invocation

    case = routing_fixture(multi_version_deployments=(REMOTE,))
    route = case.primary_route()
    invocation = finish_call(route).invocation
    deployment, *other = case.policy.resolved.deployments
    altered = deployment.model_copy(update={field: value})
    with pytest.raises(ValueError, match="resolved deployment pins must match"):
        validate_model_invocation(
            invocation,
            **_domain_audit_args(route),
            resolved_deployments=(altered, *other),
        )


def test_optional_exact_execution_pin_does_not_override_observed_permitted_version():
    from jimu_runtime.domain.validation.invocations import validate_model_invocation

    case = routing_fixture(multi_version_deployments=(REMOTE,))
    route = case.primary_route()
    invocation = finish_call(route).invocation
    wrong_actual = invocation.actual_provider.model_copy(
        update={"model_version": "2.0.0"}
    )
    contradicted = invocation.model_copy(update={"actual_provider": wrong_actual})
    with pytest.raises(ValueError, match="observed provider identity must agree"):
        validate_model_invocation(
            contradicted,
            **_domain_audit_args(route),
            resolved_deployments=case.policy.resolved.deployments,
        )
