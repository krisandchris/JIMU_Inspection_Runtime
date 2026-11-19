"""Immutable V1 operator signatures for the static checker and future engine."""

from collections.abc import Mapping
from types import MappingProxyType
from typing import Annotated, Literal, Self

from pydantic import Field, StrictInt, model_validator

from jimu_runtime.domain.base import ContractModel
from jimu_runtime.domain.conditions import MAX_CONDITION_ARGUMENTS
from jimu_runtime.domain.definition_enums import ComparisonOperator
from jimu_runtime.domain.values import BooleanTypeDefinition

type OperatorId = ComparisonOperator | Literal["in", "all", "any", "not"]
type InputConstraint = Literal[
    "same_comparable_type",
    "same_numeric_type_unit",
    "scalar_and_nonempty_list",
    "boolean_arguments",
]
type NonKnownPropagation = Literal["unknown_operand", "strong_kleene"]
type ArgumentCount = Annotated[StrictInt, Field(ge=1, le=MAX_CONDITION_ARGUMENTS)]
type _Signature = tuple[int, int, InputConstraint, NonKnownPropagation]

_EQUALITY: _Signature = (2, 2, "same_comparable_type", "unknown_operand")
_ORDERING: _Signature = (2, 2, "same_numeric_type_unit", "unknown_operand")
_LOGICAL_GROUP: _Signature = (
    1,
    MAX_CONDITION_ARGUMENTS,
    "boolean_arguments",
    "strong_kleene",
)

_SIGNATURES: Mapping[OperatorId, _Signature] = MappingProxyType(
    {
        ComparisonOperator.EQ: _EQUALITY,
        ComparisonOperator.NE: _EQUALITY,
        ComparisonOperator.LT: _ORDERING,
        ComparisonOperator.LE: _ORDERING,
        ComparisonOperator.GT: _ORDERING,
        ComparisonOperator.GE: _ORDERING,
        "in": (2, 2, "scalar_and_nonempty_list", "unknown_operand"),
        "all": _LOGICAL_GROUP,
        "any": _LOGICAL_GROUP,
        "not": (1, 1, "boolean_arguments", "strong_kleene"),
    }
)


class OperatorArity(ContractModel):
    min_args: ArgumentCount
    max_args: ArgumentCount

    @model_validator(mode="after")
    def ordered_bounds(self) -> Self:
        if self.min_args > self.max_args:
            raise ValueError("operator minimum arity exceeds maximum")
        return self


class OperatorDescriptor(ContractModel):
    """A code-owned V1 signature with no dispatch hook or execution binding."""

    operator_id: OperatorId
    version: Literal["1.0.0"]
    arity: OperatorArity
    input_constraint: InputConstraint
    output_type: BooleanTypeDefinition
    nonknown_propagation: NonKnownPropagation

    @model_validator(mode="after")
    def registered_signature(self) -> Self:
        declared = (
            self.arity.min_args,
            self.arity.max_args,
            self.input_constraint,
            self.nonknown_propagation,
        )
        if declared != _SIGNATURES.get(self.operator_id):
            raise ValueError("signature does not match the registered V1 operator")
        return self


def _descriptor(operator_id: OperatorId, signature: _Signature) -> OperatorDescriptor:
    min_args, max_args, input_constraint, nonknown_propagation = signature
    return OperatorDescriptor(
        operator_id=operator_id,
        version="1.0.0",
        arity=OperatorArity(min_args=min_args, max_args=max_args),
        input_constraint=input_constraint,
        output_type=BooleanTypeDefinition(kind="bool"),
        nonknown_propagation=nonknown_propagation,
    )


OPERATOR_DESCRIPTORS: Mapping[str, OperatorDescriptor] = MappingProxyType(
    {
        operator.value
        if isinstance(operator, ComparisonOperator)
        else operator: _descriptor(operator, signature)
        for operator, signature in _SIGNATURES.items()
    }
)
