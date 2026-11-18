"""Bounded, nonpersistent diagnostics for checks actually performed."""

from typing import Annotated, Literal, Self

from pydantic import BeforeValidator, Field, StrictInt, model_validator

from jimu_runtime.domain.base import ContractModel
from jimu_runtime.domain.definition_support import (
    Description,
    immutable_sequence,
    reference_identity,
)
from jimu_runtime.domain.primitives import ArtifactId
from jimu_runtime.domain.references import ContentRef
from jimu_runtime.domain.values import FieldName

type CheckStatus = Literal["passed", "invalid", "incomplete", "deferred"]
type ReportScope = Literal[
    "registry_core", "definition_relations", "producer_admission", "authority_admission"
]
type IssueIndex = Annotated[StrictInt, Field(ge=0)]
type IssuePath = Annotated[
    tuple[FieldName | IssueIndex, ...],
    Field(max_length=32),
    BeforeValidator(immutable_sequence),
]


class ValidationIssue(ContractModel):
    code: ArtifactId
    path: IssuePath
    message: Description
    related_refs: Annotated[
        tuple[ContentRef, ...],
        Field(max_length=16),
        BeforeValidator(immutable_sequence),
    ]

    @model_validator(mode="after")
    def unique_references(self) -> Self:
        identities = [reference_identity(ref) for ref in self.related_refs]
        if len(identities) != len(set(identities)):
            raise ValueError("related_refs must contain unique exact references")
        return self


class CheckResult(ContractModel):
    check_id: ArtifactId
    status: CheckStatus
    issues: Annotated[
        tuple[ValidationIssue, ...],
        Field(max_length=64),
        BeforeValidator(immutable_sequence),
    ]

    @model_validator(mode="after")
    def status_matches_issues(self) -> Self:
        if (self.status == "passed") != (not self.issues):
            raise ValueError("passed requires no issues; other outcomes require issues")
        return self


class ValidationReport(ContractModel):
    scope: ReportScope
    checks: Annotated[
        tuple[CheckResult, ...],
        Field(min_length=1, max_length=128),
        BeforeValidator(immutable_sequence),
    ]

    @model_validator(mode="after")
    def unique_checks(self) -> Self:
        identities = [check.check_id for check in self.checks]
        if len(identities) != len(set(identities)):
            raise ValueError("checks must contain unique check_id values")
        return self

    @property
    def aggregate_status(self) -> CheckStatus:
        """Derive outcome without turning unperformed admission into approval."""
        statuses = {check.status for check in self.checks}
        for status in ("invalid", "incomplete", "deferred"):
            if status in statuses:
                return status
        return "passed"
