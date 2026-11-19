"""Conversion of explicit node ceilings and per-call time windows."""

from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from jimu_runtime.domain.runtime.invocations import InvocationError
from jimu_runtime.gateway.bridge import ModelCallFailure, require_model_output
from jimu_runtime.gateway.bridge_tools import _tool_deadline
from jimu_runtime.gateway.invocation import DispatchOutcome, TransportResult
from jimu_runtime.workflow.worker import RetryableNodeFailure


@pytest.mark.parametrize(
    "code",
    (
        "model.node_budget_exhausted",
        "model.operation_budget_exhausted",
        "model.deployment_budget_exhausted",
    ),
)
def test_hard_ceiling_is_not_retryable(code: str) -> None:
    error = InvocationError(
        error_class="resource_exhausted", code=code, diagnostic_refs=()
    )
    outcome = DispatchOutcome(
        "0" * 64,
        None,
        "not_dispatched",
        error,
        TransportResult(False, error=error),
        None,
    )
    with pytest.raises(ModelCallFailure, match=code):
        require_model_output(outcome)


def test_temporary_resource_failure_retains_retry_path() -> None:
    error = InvocationError(
        error_class="resource_exhausted",
        code="model.resource_unavailable",
        diagnostic_refs=(),
    )
    outcome = DispatchOutcome(
        "0" * 64,
        None,
        "not_dispatched",
        error,
        TransportResult(False, error=error),
        None,
    )
    with pytest.raises(RetryableNodeFailure) as rejected:
        require_model_output(outcome)
    assert rejected.value.error_class == "resource"


def test_grounding_tool_budget_begins_with_its_own_call_and_stops_at_parent() -> None:
    parent_start = datetime(2026, 1, 1, tzinfo=UTC)
    first_tool_start = parent_start + timedelta(seconds=2)
    later_tool_start = parent_start + timedelta(seconds=4)
    parent_deadline = parent_start + timedelta(seconds=4, milliseconds=500)
    assert _tool_deadline(
        first_tool_start, parent_deadline, 1000
    ) == parent_start + timedelta(seconds=3)
    assert _tool_deadline(later_tool_start, parent_deadline, 1000) == parent_deadline


def test_model_reservation_is_returned_after_cancelled_loop(tmp_path, monkeypatch):
    from asyncio import CancelledError, create_task, run, to_thread
    from threading import Event

    from jimu_runtime.domain.policies import RemoteResourceRequirements
    from jimu_runtime.gateway.bridge import CapabilityGateway, ModelCallLimits
    from jimu_runtime.gateway.routing import plan_primary
    from jimu_runtime.workers.resource_inventory import (
        HostInventory,
        RemoteCapacity,
        RemoteQuota,
        ResourceScope,
    )
    from jimu_runtime.workers.resources import ResourceAdmission
    from tests.fixtures.wp04.routing_support import AT, routing_fixture

    fixture = routing_fixture()
    route = plan_primary(
        fixture.policy,
        fixture.operation,
        fixture.attempt,
        fixture.current,
        fixture.budget,
        AT,
    )
    quota = "model-cancel-quota"
    requirement = RemoteResourceRequirements.model_validate(
        {
            **route.resource_requirements.model_dump(),
            "resource_class": "remote",
            "remote_quota_id": quota,
        }
    )
    original_resources = route.policy.resource_registration
    policy = route.policy.model_copy(
        update={
            "resource_registration": original_resources.model_copy(
                update={
                    "artifact": original_resources.artifact.model_copy(
                        update={"policy": requirement}
                    ),
                }
            ),
        }
    )
    route = replace(route, policy=policy)
    admission = ResourceAdmission.provision(
        tmp_path / "gateway-reservation.sqlite3",
        HostInventory(
            "model-cancel-test",
            5000,
            0,
            (ResourceScope("remote", "remote", 1, pool_or_quota_id=quota),),
            (RemoteQuota(quota, 1, RemoteCapacity.PROVIDER_INFLIGHT),),
        ),
    )
    gateway = CapabilityGateway(
        None,
        None,
        admission,
        None,
        None,
        ModelCallLimits(node=1, operation=1, deployment=1, retryable=()),
    )
    entered = Event()
    allow_commit = Event()
    original = admission.reserve

    def slow_reserve(*args, **kwargs):
        entered.set()
        assert allow_commit.wait(2)
        return original(*args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(admission, "reserve", slow_reserve)

        async def interrupted() -> None:
            job = create_task(gateway._reserve(route, "remote", "cancelled-owner"))
            assert await to_thread(entered.wait, 1)
            job.cancel()
            with pytest.raises(CancelledError):
                await job
            # The reservation can only commit once the cancelled waiter is gone.
            allow_commit.set()

        run(interrupted())

    next_lease = admission.reserve(requirement, "remote", owner_id="next-owner")
    assert admission.release_reserved(next_lease)
