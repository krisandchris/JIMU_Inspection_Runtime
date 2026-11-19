"""Persisted Rule and RuleSet definitions using the shared condition wire format."""

from typing import Annotated, Literal, Self

from pydantic import BeforeValidator, Field, field_validator, model_validator

from jimu_runtime.domain.conditions import (
    MAX_CONDITION_NODES,
    ConditionExpr,
    condition_references,
    validate_condition_data,
)
from jimu_runtime.domain.definition_enums import ConditionInputDomain
from jimu_runtime.domain.definition_support import (
    AbsentOptional,
    DefinitionContract,
    immutable_sequence,
    omit_default_from_schema,
    require_unique_references,
)
from jimu_runtime.domain.definitions.capabilities import RuleSetContentRef
from jimu_runtime.domain.definitions.models import (
    ModelBindingContentRef,
    PolicyContentRef,
)
from jimu_runtime.domain.enums import ArtifactKind
from jimu_runtime.domain.fact_selectors import FactSelector
from jimu_runtime.domain.primitives import ArtifactId, Version
from jimu_runtime.domain.references import ContentRef

__all__ = ["RuleContentRef", "RuleDefinition", "RuleSetContentRef", "RuleSetDefinition"]


class RuleContentRef(ContentRef):
    artifact_kind: Literal[ArtifactKind.RULE]


type RuleFactSelectors = Annotated[
    tuple[FactSelector, ...],
    Field(max_length=MAX_CONDITION_NODES, json_schema_extra={"uniqueItems": True}),
    BeforeValidator(immutable_sequence),
]


class RuleDefinition(DefinitionContract):
    """A predicate and an exact, disjoint partition of its Fact dependencies."""

    rule_id: ArtifactId
    version: Version
    violation_category: ArtifactId
    applicability_policy_ref: PolicyContentRef
    required_fact_selectors: RuleFactSelectors
    optional_fact_selectors: RuleFactSelectors
    condition_ast: ConditionExpr
    rule_policy_ref: PolicyContentRef
    evidence_policy_ref: PolicyContentRef

    @field_validator("condition_ast", mode="before")
    @classmethod
    def rule_condition(cls, value: object) -> ConditionExpr:
        return validate_condition_data(
            value, input_domain=ConditionInputDomain.RULE_CONDITION
        )

    @model_validator(mode="after")
    def dependency_partition(self) -> Self:
        require_unique_references(
            (
                self.applicability_policy_ref,
                self.rule_policy_ref,
                self.evidence_policy_ref,
            )
        )
        required = set(self.required_fact_selectors)
        optional = set(self.optional_fact_selectors)
        if len(required) != len(self.required_fact_selectors) or len(optional) != len(
            self.optional_fact_selectors
        ):
            raise ValueError("Rule Fact dependency lists must not contain duplicates")
        if required & optional:
            raise ValueError(
                "required and optional Rule Fact dependencies must be disjoint"
            )
        referenced = set(condition_references(self.condition_ast).fact_selectors)
        if required | optional != referenced:
            raise ValueError(
                "declared Rule dependencies must exactly match condition Fact selectors"
            )
        return self


class RuleSetDefinition(DefinitionContract):
    """Exact Rule membership; applicability and collect-all behavior pin Policies."""

    rule_set_id: ArtifactId
    version: Version
    rule_refs: Annotated[
        tuple[RuleContentRef, ...],
        Field(min_length=1, max_length=1024, json_schema_extra={"uniqueItems": True}),
        BeforeValidator(immutable_sequence),
    ]
    applicability_policy_ref: PolicyContentRef
    conflict_policy_ref: PolicyContentRef
    candidate_binding_ref: AbsentOptional[ModelBindingContentRef] = Field(
        default=None,
        validate_default=False,
        exclude_if=lambda value: value is None,
        json_schema_extra=omit_default_from_schema,
    )

    @model_validator(mode="after")
    def unique_refs(self) -> Self:
        require_unique_references(self.rule_refs)
        require_unique_references(
            (self.applicability_policy_ref, self.conflict_policy_ref)
        )
        return self
