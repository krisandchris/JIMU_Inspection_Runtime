"""Finite static type-check results and path-addressed diagnostics."""

from typing import Annotated, Literal

from pydantic import BeforeValidator, Field

from jimu_runtime.domain.base import ContractModel
from jimu_runtime.domain.conditions import MAX_CONDITION_NODES
from jimu_runtime.domain.definition_enums import ConditionInputDomain
from jimu_runtime.domain.definition_support import immutable_sequence
from jimu_runtime.domain.validation.issues import ValidationIssue
from jimu_runtime.domain.values import BooleanTypeDefinition


class PassedTypeCheck(ContractModel):
    status: Literal["passed"]
    input_domain: ConditionInputDomain
    inferred_type: BooleanTypeDefinition


class FailedTypeCheck(ContractModel):
    status: Literal["invalid", "incomplete"]
    input_domain: ConditionInputDomain
    issues: Annotated[
        tuple[ValidationIssue, ...],
        Field(min_length=1, max_length=MAX_CONDITION_NODES),
        BeforeValidator(immutable_sequence),
    ]


type TypeCheckResult = Annotated[
    PassedTypeCheck | FailedTypeCheck,
    Field(discriminator="status"),
]
