"""Reject implicit or invalid retries and ambiguous scheduling budgets."""

import pytest
from pydantic import ValidationError

from jimu_runtime.domain.runtime.executions import NodeId
from jimu_runtime.workflow.scheduling import (
    JobBudget,
    NodeBudgetOverride,
    SchedulingLimits,
    validated_milliseconds,
)
from tests.fixtures.wp03.jobs.support import budget


@pytest.mark.parametrize(
    "change",
    [
        {"max_attempts": 1025},
        {"timeout_ms": 2**31},
        {"retry_backoff_ms": ()},
        {"retry_backoff_ms": (0,)},
        {"retryable_error_classes": ("transport", "transport")},
        {"retryable_error_classes": ("resource_exhausted",)},
        {"retry_on_lease_loss": 1},
    ],
)
def test_rejects_invalid_budget(change):
    with pytest.raises(ValidationError):
        JobBudget.model_validate(budget().model_dump() | change)


@pytest.mark.parametrize("node", [None, *NodeId])
def test_validation_retry_requires_explicit_n04_scope(node):
    configured = JobBudget.model_validate(
        budget().model_dump() | {"retryable_error_classes": ("validation",)}
    )
    args = {
        "schema_version": "1.0.0",
        "version": "1.0.0",
        "default": configured if node is None else budget(),
        "overrides": ()
        if node is None
        else (NodeBudgetOverride(node_id=node, budget=configured),),
    }
    if node is NodeId.N04:
        limits = SchedulingLimits.model_validate(args)
        assert limits.for_node(NodeId.N04).retryable_error_classes == ("validation",)
        assert all(
            "validation" not in limits.for_node(other).retryable_error_classes
            for other in NodeId
            if other is not NodeId.N04
        )
    else:
        with pytest.raises(ValidationError, match="explicit N04"):
            SchedulingLimits.model_validate(args)


def test_requires_explicit_lease_loss_preference_and_rejects_duplicate_node_override():
    data = budget().model_dump()
    del data["retry_on_lease_loss"]
    with pytest.raises(ValidationError):
        JobBudget.model_validate(data)
    item = NodeBudgetOverride(node_id=NodeId.N01, budget=budget())
    with pytest.raises(ValidationError, match="only one"):
        SchedulingLimits(
            schema_version="1.0.0",
            version="1.0.0",
            default=budget(),
            overrides=(item, item),
        )


@pytest.mark.parametrize("value", [0, -1, True, 2**31, "250"])
def test_rejects_unbounded_or_coerced_claim_duration(value):
    with pytest.raises(ValidationError):
        validated_milliseconds(value)
