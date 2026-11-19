"""Only a verified logical N05 step can cross its child NodeRun boundaries."""

from dataclasses import replace
from hashlib import sha256

import pytest
from fixtures.wp04.routing_support import (
    AT,
    DIGEST,
    LOCAL,
    OTHER,
    REMOTE,
    routing_fixture,
)

from jimu_runtime.domain.definition_enums import ModelCapability
from jimu_runtime.domain.enums import (
    ArtifactKind,
    AttemptStatus,
    NodeRunStatus,
    RuntimeRecordKind,
)
from jimu_runtime.domain.fact_selectors import ToolContentRef, ToolPlanContentRef
from jimu_runtime.domain.runtime.facts import NodeRunRef
from jimu_runtime.domain.runtime.invocations import InvocationError
from jimu_runtime.domain.validation.authority_inputs import NodeAttemptOwner
from jimu_runtime.domain.validation.invocations import (
    NestedModelStepFallbackWitness,
    NestedModelStepLineage,
    NestedModelStepOwner,
    validate_model_invocation,
)
from jimu_runtime.gateway.invocation import (
    DispatchRequest,
    TransportResult,
    project_dispatch,
)
from jimu_runtime.gateway.routing import (
    RecordedInvocation,
    RouteRejected,
    plan_fallback,
    plan_primary,
    plan_retry,
)


def _auxiliary_case():
    case = routing_fixture(edges=((REMOTE, LOCAL), (LOCAL, OTHER), (OTHER, LOCAL)))
    capability = ModelCapability.VISION_AUXILIARY
    registration = case.policy.binding
    binding = registration.definition.model_copy(update={"capability": capability})
    policy = case.policy.model_copy(
        update={"binding": registration.model_copy(update={"definition": binding})}
    )
    state = case.current.capabilities
    deployments = tuple(
        row.model_copy(
            update={
                "contracts": tuple(
                    contract.model_copy(update={"capability": capability})
                    for contract in row.contracts
                )
            }
        )
        for row in state.model_deployments
    )
    reviews = tuple(
        row.model_copy(
            update={
                "metadata": row.metadata.model_copy(
                    update={
                        "assessments": tuple(
                            check.model_copy(update={"capability": capability})
                            if check.assessment_kind == "fallback_transition"
                            else check
                            for check in row.metadata.assessments
                        )
                    }
                )
            }
        )
        for row in state.model_reviews
    )
    current = case.current.model_copy(
        update={
            "capabilities": state.model_copy(
                update={"model_deployments": deployments, "model_reviews": reviews}
            )
        }
    )
    operation = case.operation.model_copy(
        update={
            "node_run": case.operation.node_run.model_copy(
                update={
                    "node_id": "N05",
                    "node_run_ref": NodeRunRef(
                        record_kind=RuntimeRecordKind.NODE_RUN, record_id="n05"
                    ),
                }
            ),
            "purposes": (capability,),
            "contract_snapshot": None,
            "response_contract_digest": binding.output_schema_ref.digest,
            "nested_step_input_digest": DIGEST,
        }
    )
    return replace(case, policy=policy, operation=operation, current=current)


def _step_round(case, number):
    parent = case.operation.node_run
    child = parent.model_copy(
        update={
            "node_run_ref": NodeRunRef(
                record_kind=RuntimeRecordKind.NODE_RUN, record_id=f"step-child-{number}"
            )
        }
    )
    child_attempt = case.attempt_at(number * 2).model_copy(
        update={
            "attempt_number": 1,
            "owner": NodeAttemptOwner(
                owner_kind="node", node_run_ref=child.node_run_ref
            ),
        }
    )
    parent_attempt = case.attempt_at(number * 2 - 1).model_copy(
        update={
            "attempt_number": number,
            "owner": NodeAttemptOwner(
                owner_kind="node", node_run_ref=parent.node_run_ref
            ),
        }
    )
    operation = case.operation.model_copy(update={"node_run": child})
    lineage = NestedModelStepLineage(
        execution_id=parent.execution_id,
        parent_node_run_ref=parent.node_run_ref,
        tool_plan_ref=ToolPlanContentRef(
            artifact_kind=ArtifactKind.TOOL_PLAN,
            artifact_id="parking-plan",
            version="1.0.0",
            digest=DIGEST,
        ),
        step_id="parking-step",
        tool_ref=ToolContentRef(
            artifact_kind=ArtifactKind.TOOL,
            artifact_id="parking-tool",
            version="1.0.0",
            digest=DIGEST,
        ),
        profile_ref=operation.profile_refs[0],
        object_ref=operation.object_ref,
        binding_ref=operation.binding_ref,
        bound_input_digest=DIGEST,
    )
    return operation, NestedModelStepOwner(
        lineage, parent_attempt, child, child_attempt
    )


def _primary(case):
    operation, owner = _step_round(case, 1)
    return plan_primary(
        case.policy,
        operation,
        owner.child_attempt,
        case.current,
        case.budget,
        AT,
        nested_step_owner=owner,
    )


def _subsequent(case, history, number, *, current=None, retry=False):
    operation, owner = _step_round(case, number)
    args = (
        case.policy,
        operation,
        owner.child_attempt,
        current or case.current,
        case.budget,
        AT,
    )
    if retry:
        return plan_retry(
            *args,
            history=history,
            nested_step_owner=owner,
            retryable_errors=("timeout",),
        )
    return plan_fallback(*args, history=history, nested_step_owner=owner)


def _failed(route):
    owner = route.nested_step_owner
    dispatch = DispatchRequest(
        f"nested-dispatch-{owner.parent_attempt.attempt_number}", route, b"request"
    )
    outcome = project_dispatch(
        dispatch,
        TransportResult(
            True,
            error=InvocationError(
                error_class="timeout", code="timeout", diagnostic_refs=()
            ),
        ),
        started_at=AT,
        completed_at=AT,
    )
    assert outcome.invocation is not None
    archived = replace(
        owner,
        parent_attempt=owner.parent_attempt.model_copy(
            update={"status": AttemptStatus.FAILED}
        ),
        child_attempt=owner.child_attempt.model_copy(
            update={"status": AttemptStatus.TIMED_OUT}
        ),
        child_node=owner.child_node.model_copy(update={"status": NodeRunStatus.FAILED}),
    )
    return RecordedInvocation(
        route.operation.model_copy(update={"node_run": archived.child_node}),
        archived.child_attempt,
        outcome.invocation,
        nested_step_owner=archived,
    )


def _succeeded(route):
    output = b'{"result":"synthetic"}'
    return project_dispatch(
        DispatchRequest("nested-success", route, b"request"),
        TransportResult(
            True,
            response_status=200,
            response_digest=sha256(output).hexdigest(),
            parsed_content=output,
            validated_response_contract_digest=route.operation.response_contract_digest,
        ),
        started_at=AT,
        completed_at=AT,
    )


def test_nested_chain_preserves_distinct_child_owners_and_original_trigger():
    case = _auxiliary_case()
    first = _failed(_primary(case))
    alternative = _subsequent(case, (first,), 2)
    second = _failed(alternative)
    assert second.invocation.attempt_ref != first.invocation.attempt_ref
    assert first.attempt.attempt_number == second.attempt.attempt_number == 1
    third = _subsequent(case, (first, second), 3)
    assert third.selected.deployment_id == OTHER
    assert (
        third.trace.trigger_invocation_ref.record_id
        == second.invocation.model_invocation_id
    )
    outcome = _succeeded(third)
    assert outcome.output == b'{"result":"synthetic"}'
    assert (
        outcome.invocation.node_run_ref
        == third.nested_step_owner.child_node.node_run_ref
    )
    with pytest.raises(RouteRejected, match="begin at the pinned primary"):
        _subsequent(case, (second, first), 4)
    with pytest.raises(RouteRejected, match="No legal"):
        _subsequent(case, (first, second, _failed(third)), 4)


def test_nested_retry_stays_on_the_current_deployment_and_original_edge():
    case = _auxiliary_case()
    first = _failed(_primary(case))
    alternative = _subsequent(case, (first,), 2)
    second = _failed(alternative)
    retry = _subsequent(case, (first, second), 3, retry=True)
    assert retry.selected == alternative.selected
    assert retry.trace == alternative.trace
    assert _succeeded(retry).invocation is not None


@pytest.mark.parametrize(
    "bad_scope",
    [
        "object",
        "plan",
        "bound_input",
        "model_input",
        "parent",
        "child",
        "superseded",
        "owner_missing",
    ],
)
def test_nested_history_rejects_mixed_identity_missing_or_non_authoritative_owner(
    bad_scope,
):
    case = _auxiliary_case()
    first = _failed(_primary(case))
    owner = first.nested_step_owner
    lineage = owner.lineage
    if bad_scope == "object":
        lineage = replace(
            lineage,
            object_ref=lineage.object_ref.model_copy(
                update={"record_id": "other-object"}
            ),
        )
    elif bad_scope == "plan":
        lineage = replace(
            lineage,
            tool_plan_ref=lineage.tool_plan_ref.model_copy(update={"digest": "f" * 64}),
        )
    elif bad_scope == "bound_input":
        lineage = replace(lineage, bound_input_digest="f" * 64)
    elif bad_scope == "model_input":
        first = replace(
            first,
            operation=first.operation.model_copy(update={"input_digest": "f" * 64}),
        )
    elif bad_scope == "parent":
        owner = replace(
            owner,
            parent_attempt=owner.parent_attempt.model_copy(
                update={"status": AttemptStatus.RUNNING}
            ),
        )
    elif bad_scope == "child":
        owner = replace(
            owner,
            child_attempt=owner.child_attempt.model_copy(update={"attempt_number": 2}),
        )
    elif bad_scope == "superseded":
        owner = replace(
            owner,
            child_attempt=owner.child_attempt.model_copy(
                update={"status": AttemptStatus.SUPERSEDED}
            ),
        )
    modified = None if bad_scope == "owner_missing" else replace(owner, lineage=lineage)
    with pytest.raises(RouteRejected):
        _subsequent(case, (replace(first, nested_step_owner=modified),), 2)


def test_nested_fallback_edge_cannot_bypass_current_review_revocation():
    case = _auxiliary_case()
    prior = _failed(_primary(case))
    state = case.current.capabilities
    current = case.current.model_copy(
        update={
            "capabilities": state.model_copy(
                update={
                    "model_reviews": tuple(
                        row.model_copy(update={"revoked_at": AT})
                        for row in state.model_reviews
                    )
                }
            )
        }
    )
    with pytest.raises(RouteRejected, match="No legal"):
        _subsequent(case, (prior,), 2, current=current)


def test_domain_rejects_cross_child_fallback_without_independent_lineage():
    case = _auxiliary_case()
    prior = _failed(_primary(case))
    route = _subsequent(case, (prior,), 2)
    record = _succeeded(route).invocation
    args = {
        "node_run": route.operation.node_run,
        "attempt": route.attempt,
        "binding": route.policy.binding,
        "provider_policy": route.policy.provider_registration,
        "fallback_policy": route.policy.fallback_registration,
        "prior_invocations": (prior.invocation,),
        "reviews": route.reviews,
    }
    with pytest.raises(ValueError, match="distinct earlier failed Attempt"):
        validate_model_invocation(record, **args)
    witness = NestedModelStepFallbackWitness(
        prior.nested_step_owner, route.nested_step_owner
    )
    validate_model_invocation(record, **args, nested_step_fallback=witness)
    wrong_node = witness.prior.child_node.model_copy(update={"node_id": "N04"})
    forged = replace(witness, prior=replace(witness.prior, child_node=wrong_node))
    with pytest.raises(ValueError, match="exact N05 parent and child"):
        validate_model_invocation(record, **args, nested_step_fallback=forged)
