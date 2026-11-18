"""Finite caller-pinned condition use sites; never authenticated Bundle content."""

from typing import Annotated, Literal, Self

from pydantic import (
    BeforeValidator,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)

from jimu_runtime.domain.base import ContractModel
from jimu_runtime.domain.definition_enums import FactScope, ProducerKind
from jimu_runtime.domain.definition_support import (
    AbsentOptional,
    OptionalContract,
    immutable_sequence,
    omit_default_from_schema,
)
from jimu_runtime.domain.definitions.models import PolicyContentRef
from jimu_runtime.domain.fact_selectors import (
    FactSelector,
    ProducerCatalogueContentRef,
    QuestionContentRef,
    ToolPlanContentRef,
)
from jimu_runtime.domain.primitives import ArtifactId
from jimu_runtime.domain.schema_types import SchemaDescriptor
from jimu_runtime.domain.tool_mappings import (
    ContextTypeRegistration,
    ProfileContentRef,
    RegionContentRef,
)
from jimu_runtime.domain.validation.issues import ValidationReport


class _ConditionContext(OptionalContract):
    context_registrations: Annotated[
        tuple[ContextTypeRegistration, ...],
        Field(max_length=1024),
        BeforeValidator(immutable_sequence),
    ]
    descriptors: Annotated[
        tuple[SchemaDescriptor, ...],
        Field(max_length=1024),
        BeforeValidator(immutable_sequence),
    ]
    region_ref: AbsentOptional[RegionContentRef] = Field(
        default=None,
        validate_default=False,
        exclude_if=lambda value: value is None,
        json_schema_extra=omit_default_from_schema,
    )


class ApplicabilityConditionContext(_ConditionContext):
    use_role: Literal["applicability"]
    profile_ref: ProfileContentRef
    policy_ref: PolicyContentRef
    question_ref: AbsentOptional[QuestionContentRef] = Field(
        default=None,
        validate_default=False,
        exclude_if=lambda value: value is None,
        json_schema_extra=omit_default_from_schema,
    )


class ToolStepGuardConditionContext(_ConditionContext):
    use_role: Literal["tool_step_guard"]
    profile_ref: ProfileContentRef
    policy_ref: PolicyContentRef
    tool_plan_ref: ToolPlanContentRef
    step_id: ArtifactId


class ProducerActivationConditionContext(_ConditionContext):
    model_config = ConfigDict(
        json_schema_extra={
            "allOf": [
                {
                    "if": {"properties": {"producer_scope": {"const": "object"}}},
                    "then": {"required": ["profile_ref"]},
                    "else": {"not": {"required": ["profile_ref"]}},
                },
                {
                    "if": {"properties": {"producer_kind": {"const": "tool"}}},
                    "then": {"required": ["tool_plan_ref", "step_id"]},
                    "else": {
                        "not": {
                            "anyOf": [
                                {"required": ["tool_plan_ref"]},
                                {"required": ["step_id"]},
                            ]
                        }
                    },
                },
                {
                    "if": {
                        "properties": {
                            "producer_kind": {"enum": ["tool", "vlm_question"]}
                        }
                    },
                    "then": {"properties": {"producer_scope": {"const": "object"}}},
                },
            ]
        }
    )
    use_role: Literal["producer_activation"]
    producer_catalogue_ref: ProducerCatalogueContentRef
    declaration_id: ArtifactId
    producer_kind: ProducerKind
    producer_scope: FactScope
    profile_ref: AbsentOptional[ProfileContentRef] = Field(
        default=None,
        validate_default=False,
        exclude_if=lambda value: value is None,
        json_schema_extra=omit_default_from_schema,
    )
    tool_plan_ref: AbsentOptional[ToolPlanContentRef] = Field(
        default=None,
        validate_default=False,
        exclude_if=lambda value: value is None,
        json_schema_extra=omit_default_from_schema,
    )
    step_id: AbsentOptional[ArtifactId] = Field(
        default=None,
        validate_default=False,
        exclude_if=lambda value: value is None,
        json_schema_extra=omit_default_from_schema,
    )

    @field_validator("producer_kind", mode="before")
    @classmethod
    def decode_kind(cls, value: object) -> ProducerKind:
        return ProducerKind._decode(value)

    @field_validator("producer_scope", mode="before")
    @classmethod
    def decode_scope(cls, value: object) -> FactScope:
        return FactScope._decode(value)

    @model_validator(mode="after")
    def anchor_shape(self) -> Self:
        if (self.producer_scope is FactScope.OBJECT) != (self.profile_ref is not None):
            raise ValueError("object scope requires Profile; execution forbids Profile")
        if (
            self.producer_kind is not ProducerKind.SYSTEM
            and self.producer_scope is not FactScope.OBJECT
        ):
            raise ValueError("Question and Tool producers require object scope")
        tool = self.producer_kind is ProducerKind.TOOL
        if tool != (self.tool_plan_ref is not None) or tool != (
            self.step_id is not None
        ):
            raise ValueError("Tool requires Plan and step; other producers forbid both")
        return self


type ConditionReferenceContext = Annotated[
    ApplicabilityConditionContext
    | ToolStepGuardConditionContext
    | ProducerActivationConditionContext,
    Field(discriminator="use_role"),
]


class ConditionReferenceResult(ContractModel):
    report: ValidationReport
    selectors_for_matching: Annotated[
        tuple[FactSelector, ...],
        Field(max_length=512),
        BeforeValidator(immutable_sequence),
    ]

    @model_validator(mode="after")
    def no_failed_projection(self) -> Self:
        if (
            self.report.aggregate_status in ("invalid", "incomplete")
            and self.selectors_for_matching
        ):
            raise ValueError(
                "failed bindings cannot expose a partial selector projection"
            )
        return self
