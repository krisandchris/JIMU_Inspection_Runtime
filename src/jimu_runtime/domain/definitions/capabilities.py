"""Capability definition shapes; injected relation checks live above this layer."""

from typing import Annotated, Literal, Self

from pydantic import BeforeValidator, ConfigDict, Field, model_validator

from jimu_runtime.domain.base import ContractModel
from jimu_runtime.domain.definition_support import (
    AbsentOptional,
    AttributeSchemaContentRef,
    ContextFieldSelector,
    DefinitionContract,
    Description,
    OptionalContract,
    ShortText,
    VocabularyMemberRef,
    immutable_sequence,
    omit_default_from_schema,
    reference_identity,
    require_unique_references,
)
from jimu_runtime.domain.definitions.prompts import PromptTemplateContentRef
from jimu_runtime.domain.enums import ArtifactKind
from jimu_runtime.domain.fact_selectors import (
    FactSchemaContentRef,
    QuestionContentRef,
    ToolContentRef,
    ToolPlanContentRef,
)
from jimu_runtime.domain.primitives import ArtifactId, Version
from jimu_runtime.domain.references import ContentRef
from jimu_runtime.domain.tool_mappings import ContextInput, ImageInput, InputBinding
from jimu_runtime.domain.values import RecordValue


class FacetContentRef(ContentRef):
    artifact_kind: Literal[ArtifactKind.FACET]


class EntityContentRef(ContentRef):
    artifact_kind: Literal[ArtifactKind.ENTITY]


class ModelBindingContentRef(ContentRef):
    artifact_kind: Literal[ArtifactKind.MODEL_BINDING]


class PolicyContentRef(ContentRef):
    artifact_kind: Literal[ArtifactKind.POLICY]


class RuleSetContentRef(ContentRef):
    artifact_kind: Literal[ArtifactKind.RULE_SET]


class FacetMetadataEntry(ContractModel):
    namespace: ArtifactId
    attribute_schema_ref: AttributeSchemaContentRef
    value: RecordValue


type FacetRefs = Annotated[
    tuple[FacetContentRef, ...],
    Field(max_length=1024, json_schema_extra={"uniqueItems": True}),
    BeforeValidator(immutable_sequence),
]


class FacetDefinition(DefinitionContract):
    facet_id: ArtifactId
    version: Version
    name: ShortText
    description: Description
    fact_schema_refs: Annotated[
        tuple[FactSchemaContentRef, ...],
        Field(min_length=1, max_length=1024, json_schema_extra={"uniqueItems": True}),
        BeforeValidator(immutable_sequence),
    ]
    metadata: AbsentOptional[
        Annotated[
            tuple[FacetMetadataEntry, ...],
            Field(min_length=1, max_length=64, json_schema_extra={"uniqueItems": True}),
            BeforeValidator(immutable_sequence),
        ]
    ] = Field(
        default=None,
        validate_default=False,
        exclude_if=lambda value: value is None,
        json_schema_extra=omit_default_from_schema,
    )
    tags: AbsentOptional[
        Annotated[
            tuple[VocabularyMemberRef, ...],
            Field(max_length=1024, json_schema_extra={"uniqueItems": True}),
            BeforeValidator(immutable_sequence),
        ]
    ] = Field(
        default=None,
        validate_default=False,
        exclude_if=lambda value: value is None,
        json_schema_extra=omit_default_from_schema,
    )

    @model_validator(mode="after")
    def unique_members(self) -> Self:
        require_unique_references(self.fact_schema_refs)
        if self.metadata is not None:
            namespaces = [entry.namespace for entry in self.metadata]
            if len(namespaces) != len(set(namespaces)):
                raise ValueError("Facet metadata namespaces must be unique")
        if self.tags is not None:
            tags = [
                (reference_identity(tag.vocabulary_ref), tag.member)
                for tag in self.tags
            ]
            if len(tags) != len(set(tags)):
                raise ValueError(
                    "Facet tags must have unique exact vocabulary/member pairs"
                )
        return self


class QuestionDefinition(DefinitionContract):
    question_id: ArtifactId
    version: Version
    objective: Description
    prompt_template_ref: PromptTemplateContentRef
    facet_refs: Annotated[
        tuple[FacetContentRef, ...],
        Field(min_length=1, max_length=1024, json_schema_extra={"uniqueItems": True}),
        BeforeValidator(immutable_sequence),
    ]
    context_allowlist: Annotated[
        tuple[ContextFieldSelector, ...],
        Field(max_length=1024, json_schema_extra={"uniqueItems": True}),
        BeforeValidator(immutable_sequence),
    ]
    primary_fact_key: ArtifactId
    primary_fact_schema_ref: FactSchemaContentRef
    evidence_policy_ref: PolicyContentRef
    unknown_policy_ref: PolicyContentRef
    validation_policy_ref: PolicyContentRef

    @model_validator(mode="after")
    def unique_inputs(self) -> Self:
        require_unique_references(self.facet_refs)
        contexts = [
            (
                selector.source,
                reference_identity(selector.schema_ref),
                selector.field_path,
            )
            for selector in self.context_allowlist
        ]
        if len(contexts) != len(set(contexts)):
            raise ValueError(
                "Question context allowlist contains duplicate exact selectors"
            )
        return self


type GroundingInputSource = Annotated[
    ContextInput | ImageInput, Field(discriminator="source_kind")
]


class GroundingInputBinding(InputBinding):
    model_config = ConfigDict(
        json_schema_extra={
            "if": {
                "properties": {"required": {"const": True}},
                "required": ["required"],
            },
            "then": {"properties": {"on_missing": {"const": "block"}}},
        }
    )
    source: GroundingInputSource


type GroundingInputBindings = Annotated[
    tuple[GroundingInputBinding, ...],
    Field(max_length=64, json_schema_extra={"uniqueItems": True}),
    BeforeValidator(immutable_sequence),
]


def _unique_grounding_targets(
    bindings: tuple[GroundingInputBinding, ...] | None,
) -> None:
    if bindings is None:
        return
    targets = [
        (reference_identity(binding.target.schema_ref), binding.target.field_path)
        for binding in bindings
    ]
    for index, (schema, path) in enumerate(targets):
        for other_schema, other_path in targets[:index]:
            if schema == other_schema and (
                path[: len(other_path)] == other_path or other_path[: len(path)] == path
            ):
                raise ValueError("Grounding input target writes must not overlap")


class ModelGroundingBinding(OptionalContract):
    kind: Literal["model"]
    model_binding_ref: ModelBindingContentRef
    input_bindings: AbsentOptional[GroundingInputBindings] = Field(
        default=None,
        validate_default=False,
        exclude_if=lambda value: value is None,
        json_schema_extra=omit_default_from_schema,
    )

    @model_validator(mode="after")
    def unique_targets(self) -> Self:
        _unique_grounding_targets(self.input_bindings)
        return self


class ToolGroundingBinding(OptionalContract):
    kind: Literal["tool"]
    tool_ref: ToolContentRef
    input_bindings: AbsentOptional[GroundingInputBindings] = Field(
        default=None,
        validate_default=False,
        exclude_if=lambda value: value is None,
        json_schema_extra=omit_default_from_schema,
    )

    @model_validator(mode="after")
    def unique_targets(self) -> Self:
        _unique_grounding_targets(self.input_bindings)
        return self


type GroundingBinding = Annotated[
    ModelGroundingBinding | ToolGroundingBinding, Field(discriminator="kind")
]


class ProfileDefinition(DefinitionContract):
    profile_id: ArtifactId
    version: Version
    entity_ref: EntityContentRef
    discovery_binding_ref: ModelBindingContentRef
    grounding_binding: GroundingBinding
    observation_binding_ref: ModelBindingContentRef
    facet_refs: FacetRefs
    question_refs: Annotated[
        tuple[QuestionContentRef, ...],
        Field(max_length=1024, json_schema_extra={"uniqueItems": True}),
        BeforeValidator(immutable_sequence),
    ]
    question_selection_policy_ref: PolicyContentRef
    rule_set_refs: Annotated[
        tuple[RuleSetContentRef, ...],
        Field(max_length=1024, json_schema_extra={"uniqueItems": True}),
        BeforeValidator(immutable_sequence),
    ]
    tool_plan_ref: ToolPlanContentRef
    applicability_policy_ref: PolicyContentRef

    @model_validator(mode="after")
    def unique_refs(self) -> Self:
        require_unique_references(self.facet_refs)
        require_unique_references(self.question_refs)
        require_unique_references(self.rule_set_refs)
        return self
