"""Versioned, host-supplied budgets pinned for each workflow Execution."""

from typing import Annotated, Literal, Self

from pydantic import BeforeValidator, Field, TypeAdapter, model_validator

from jimu_runtime.domain.base import ContractModel
from jimu_runtime.domain.definition_support import immutable_sequence
from jimu_runtime.domain.primitives import Version
from jimu_runtime.domain.runtime.executions import NodeId

type Milliseconds = Annotated[int, Field(ge=1, le=2**31 - 1)]
type RetryableNodeError = Literal["transport", "timeout", "resource", "validation"]

_ms: TypeAdapter[int] = TypeAdapter(Milliseconds)


def validated_milliseconds(value: int) -> int:
    return _ms.validate_python(value, strict=True)


class JobBudget(ContractModel):
    max_attempts: Annotated[int, Field(ge=1, le=1024)]
    timeout_ms: Milliseconds
    retry_backoff_ms: Annotated[
        tuple[Milliseconds, ...],
        Field(max_length=1023),
        BeforeValidator(immutable_sequence),
    ]
    retryable_error_classes: Annotated[
        tuple[RetryableNodeError, ...],
        Field(max_length=4),
        BeforeValidator(immutable_sequence),
    ]
    retry_on_lease_loss: bool

    @model_validator(mode="after")
    def validate_schedule(self) -> Self:
        if len(self.retry_backoff_ms) != self.max_attempts - 1:
            raise ValueError("every possible retry requires a backoff")
        if len(set(self.retryable_error_classes)) != len(self.retryable_error_classes):
            raise ValueError("retryable error classes must be unique")
        return self


class NodeBudgetOverride(ContractModel):
    node_id: NodeId
    budget: JobBudget


class SchedulingLimits(ContractModel):
    schema_version: Literal["1.0.0"]
    version: Version
    default: JobBudget
    overrides: Annotated[
        tuple[NodeBudgetOverride, ...],
        Field(max_length=6),
        BeforeValidator(immutable_sequence),
    ]

    @model_validator(mode="after")
    def validate_overrides(self) -> Self:
        if len({entry.node_id for entry in self.overrides}) != len(self.overrides):
            raise ValueError("a node can have only one budget override")
        if "validation" in self.default.retryable_error_classes or any(
            entry.node_id is not NodeId.N04
            and "validation" in entry.budget.retryable_error_classes
            for entry in self.overrides
        ):
            raise ValueError(
                "validation retry requires an explicit N04 budget override"
            )
        return self

    def for_node(self, node_id: NodeId) -> JobBudget:
        if not isinstance(node_id, NodeId):
            raise TypeError("node budget lookup requires a NodeId")
        return next(
            (entry.budget for entry in self.overrides if entry.node_id is node_id),
            self.default,
        )
