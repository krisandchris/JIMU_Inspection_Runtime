"""Finite type-check inputs for a pinned condition use."""

from typing import Annotated, Literal, Self

from pydantic import BeforeValidator, Field, model_validator

from jimu_runtime.domain.base import ContractModel
from jimu_runtime.domain.conditions import MAX_CONDITION_DEPTH
from jimu_runtime.domain.definition_support import (
    AbsentOptional,
    ContextFieldSelector,
    OptionalContract,
    VocabularyMemberRef,
    immutable_sequence,
    omit_default_from_schema,
)
from jimu_runtime.domain.primitives import ArtifactId
from jimu_runtime.domain.rules import (
    RuleContentRef,
    RuleSetContentRef,
    RuleSetDefinition,
)
from jimu_runtime.domain.schema_types import SchemaDescriptor
from jimu_runtime.domain.tool_mappings import (
    ContextTypeRegistration,
    ProfileContentRef,
    RegionContentRef,
)
from jimu_runtime.domain.validation.authority_inputs import (
    AuthorityQuestionInput,
    AuthorityToolPlanInput,
)
from jimu_runtime.domain.validation.condition_inputs import (
    ApplicabilityConditionContext,
    ProducerActivationConditionContext,
    ToolStepGuardConditionContext,
)
from jimu_runtime.domain.validation.definition_inputs import ProfileRegistration
from jimu_runtime.domain.validation.issues import IssueIndex
from jimu_runtime.domain.validation.policies import PolicyRegistration
from jimu_runtime.domain.validation.producer_inputs import (
    Directory,
    ProducerCatalogueRegistration,
)
from jimu_runtime.domain.validation.tool_plans import ToolRegistration
from jimu_runtime.domain.values import FieldName


class RuleSetTypeRegistration(ContractModel):
    ref: RuleSetContentRef
    definition: RuleSetDefinition

    @model_validator(mode="after")
    def identity_matches(self) -> Self:
        if (self.ref.artifact_id, self.ref.version) != (
            self.definition.rule_set_id,
            self.definition.version,
        ):
            raise ValueError("RuleSet registration ID/version differs from definition")
        return self


class RuleConditionContext(OptionalContract):
    use_role: Literal["rule_condition"]
    profile_ref: ProfileContentRef
    rule_ref: RuleContentRef
    rule_set: RuleSetTypeRegistration
    context_registrations: Directory[ContextTypeRegistration]
    descriptors: Directory[SchemaDescriptor]
    region_ref: AbsentOptional[RegionContentRef] = Field(
        default=None,
        validate_default=False,
        exclude_if=lambda value: value is None,
        json_schema_extra=omit_default_from_schema,
    )


type RuleTypeUseContext = Annotated[
    ApplicabilityConditionContext
    | RuleConditionContext
    | ToolStepGuardConditionContext
    | ProducerActivationConditionContext,
    Field(discriminator="use_role"),
]


class LiteralUnitBinding(ContractModel):
    """Unit declared for an occurrence in the AST, relative to the predicate root."""

    path: Annotated[
        tuple[FieldName | IssueIndex, ...],
        Field(max_length=2 * (MAX_CONDITION_DEPTH - 1)),
        BeforeValidator(immutable_sequence),
    ]
    unit: VocabularyMemberRef


class ContextUnitBinding(ContractModel):
    selector: ContextFieldSelector
    unit: VocabularyMemberRef


class RuleTypeBindings(OptionalContract):
    """Caller-supplied definitions and type metadata for one condition use."""

    profiles: Directory[ProfileRegistration]
    questions: Directory[AuthorityQuestionInput]
    plans: Directory[AuthorityToolPlanInput]
    tools: Directory[ToolRegistration]
    policies: Directory[PolicyRegistration] = ()
    producer_catalogue: AbsentOptional[ProducerCatalogueRegistration] = Field(
        default=None,
        validate_default=False,
        exclude_if=lambda value: value is None,
        json_schema_extra=omit_default_from_schema,
    )
    prior_producer_declaration_ids: Annotated[
        tuple[ArtifactId, ...],
        Field(max_length=1024),
        BeforeValidator(immutable_sequence),
    ] = ()
    literal_units: Directory[LiteralUnitBinding] = ()
    context_units: Directory[ContextUnitBinding] = ()

    @model_validator(mode="after")
    def unique_annotations(self) -> Self:
        for values in (
            self.prior_producer_declaration_ids,
            tuple(item.path for item in self.literal_units),
            tuple(item.selector for item in self.context_units),
        ):
            if len(set(values)) != len(values):
                raise ValueError("duplicate type binding")
        return self
