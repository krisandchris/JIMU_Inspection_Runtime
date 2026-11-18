"""Finite caller-pinned definition views, not an authenticated Bundle inventory."""

from typing import Annotated, ClassVar, Literal, Self

from pydantic import BeforeValidator, Field, field_validator, model_validator

from jimu_runtime.domain.base import ContractModel
from jimu_runtime.domain.definition_enums import ModelCapability
from jimu_runtime.domain.definition_support import (
    AbsentOptional,
    DefinitionContract,
    OptionalContract,
    StaticAssetContentRef,
    UTCDateTime,
    VocabularyContentRef,
    immutable_sequence,
    omit_default_from_schema,
    reference_identity,
)
from jimu_runtime.domain.definitions.capabilities import (
    EntityContentRef,
    FacetContentRef,
    FacetDefinition,
    ProfileDefinition,
    QuestionDefinition,
)
from jimu_runtime.domain.definitions.models import ModelBindingContentRef
from jimu_runtime.domain.definitions.ontology import EntityDefinition, RegionDefinition
from jimu_runtime.domain.definitions.prompts import PromptPurpose
from jimu_runtime.domain.definitions.resources import (
    StaticAssetDescriptor,
    VocabularyDefinition,
)
from jimu_runtime.domain.definitions.tools import ToolPlanDefinition
from jimu_runtime.domain.enums import ArtifactKind
from jimu_runtime.domain.fact_selectors import (
    FactSchemaContentRef,
    QuestionContentRef,
    ToolPlanContentRef,
)
from jimu_runtime.domain.references import ContentRef, SchemaRef
from jimu_runtime.domain.schema_types import ObjectSchemaRef, SchemaDescriptor
from jimu_runtime.domain.tool_mappings import (
    ContextTypeRegistration,
    MappingTypeContext,
    ProfileContentRef,
    RegionContentRef,
)
from jimu_runtime.domain.validation.policies import (
    ModelBindingRegistration,
    PolicyRegistration,
    PromptRegistration,
)
from jimu_runtime.domain.validation.review_inputs import ReviewMetadata
from jimu_runtime.domain.validation.tool_plans import ToolRegistration


class _Registration[T: DefinitionContract](ContractModel):
    ref: ContentRef
    definition: T
    _id_field: ClassVar[str]

    @model_validator(mode="after")
    def identity_matches(self) -> Self:
        if (self.ref.artifact_id, self.ref.version) != (
            getattr(self.definition, self._id_field),
            self.definition.__dict__.get("version"),
        ):
            raise ValueError("Registration ref ID/version differs from definition")
        return self


class EntityRegistration(_Registration[EntityDefinition]):
    registration_kind: Literal["entity"]
    ref: EntityContentRef
    _id_field = "entity_id"


class RegionRegistration(_Registration[RegionDefinition]):
    registration_kind: Literal["region"]
    ref: RegionContentRef
    _id_field = "region_id"


class FacetRegistration(_Registration[FacetDefinition]):
    registration_kind: Literal["facet"]
    ref: FacetContentRef
    _id_field = "facet_id"


class QuestionRegistration(_Registration[QuestionDefinition]):
    registration_kind: Literal["question"]
    ref: QuestionContentRef
    _id_field = "question_id"


class ProfileRegistration(_Registration[ProfileDefinition]):
    registration_kind: Literal["profile"]
    ref: ProfileContentRef
    _id_field = "profile_id"


class RegisteredPrompt(PromptRegistration):
    registration_kind: Literal["prompt_template"]


class RegisteredModel(ModelBindingRegistration):
    registration_kind: Literal["model_binding"]


class RegisteredTool(ToolRegistration):
    registration_kind: Literal["tool"]


class ToolPlanRegistration(_Registration[ToolPlanDefinition]):
    registration_kind: Literal["tool_plan"]
    ref: ToolPlanContentRef
    _id_field = "tool_plan_id"


class VocabularyRegistration(_Registration[VocabularyDefinition]):
    registration_kind: Literal["vocabulary"]
    ref: VocabularyContentRef
    _id_field = "vocabulary_id"


class StaticAssetRegistration(_Registration[StaticAssetDescriptor]):
    registration_kind: Literal["static_asset"]
    ref: StaticAssetContentRef
    _id_field = "asset_id"


type DefinitionRegistration = Annotated[
    EntityRegistration
    | RegionRegistration
    | FacetRegistration
    | QuestionRegistration
    | ProfileRegistration
    | RegisteredPrompt
    | RegisteredModel
    | RegisteredTool
    | ToolPlanRegistration
    | VocabularyRegistration
    | StaticAssetRegistration,
    Field(discriminator="registration_kind"),
]


class AttributeSchemaRef(SchemaRef):
    artifact_kind: Literal[ArtifactKind.ATTRIBUTE_SCHEMA]


class QuestionMetadataView(ContractModel):
    question_ref: QuestionContentRef
    schema_ref: AttributeSchemaRef


class ModelPromptUse(ContractModel):
    binding_ref: ModelBindingContentRef
    consuming_purpose: PromptPurpose

    @field_validator("consuming_purpose", mode="before")
    @classmethod
    def decode_purpose(cls, value: object) -> ModelCapability:
        return ModelCapability._decode(value)


def _unique_identity(refs: tuple[ContentRef, ...], label: str) -> None:
    identities = [reference_identity(ref)[:3] for ref in refs]
    if len(identities) != len(set(identities)):
        raise ValueError(f"Duplicate or conflicting {label} identity")


class ProfileUseContext(OptionalContract):
    profile_ref: ProfileContentRef
    context_registrations: Annotated[
        tuple[ContextTypeRegistration, ...],
        Field(max_length=1024),
        BeforeValidator(immutable_sequence),
    ]
    question_metadata_views: Annotated[
        tuple[QuestionMetadataView, ...],
        Field(max_length=1024),
        BeforeValidator(immutable_sequence),
    ]
    mapping_contexts: Annotated[
        tuple[MappingTypeContext, ...],
        Field(max_length=1024),
        BeforeValidator(immutable_sequence),
    ]
    model_prompt_uses: Annotated[
        tuple[ModelPromptUse, ...],
        Field(max_length=1024),
        BeforeValidator(immutable_sequence),
    ]
    region_ref: AbsentOptional[RegionContentRef] = Field(
        default=None,
        validate_default=False,
        exclude_if=lambda v: v is None,
        json_schema_extra=omit_default_from_schema,
    )
    profile_metadata_schema_ref: AbsentOptional[AttributeSchemaRef] = Field(
        default=None,
        validate_default=False,
        exclude_if=lambda v: v is None,
        json_schema_extra=omit_default_from_schema,
    )
    object_geometry_schema_ref: AbsentOptional[ObjectSchemaRef] = Field(
        default=None,
        validate_default=False,
        exclude_if=lambda v: v is None,
        json_schema_extra=omit_default_from_schema,
    )
    image_reference_schema_ref: AbsentOptional[AttributeSchemaRef] = Field(
        default=None,
        validate_default=False,
        exclude_if=lambda v: v is None,
        json_schema_extra=omit_default_from_schema,
    )

    @model_validator(mode="after")
    def unique_views(self) -> Self:
        _unique_identity(
            tuple(view.question_ref for view in self.question_metadata_views),
            "Question view",
        )
        _unique_identity(
            tuple(view.binding_ref for view in self.model_prompt_uses),
            "Model Prompt use",
        )
        return self


class DefinitionValidationInput(OptionalContract):
    definitions: Annotated[
        tuple[DefinitionRegistration, ...],
        Field(max_length=1024),
        BeforeValidator(immutable_sequence),
    ]
    descriptors: Annotated[
        tuple[SchemaDescriptor, ...],
        Field(max_length=1024),
        BeforeValidator(immutable_sequence),
    ]
    fact_schema_refs: Annotated[
        tuple[FactSchemaContentRef, ...],
        Field(max_length=1024),
        BeforeValidator(immutable_sequence),
    ]
    policies: Annotated[
        tuple[PolicyRegistration, ...],
        Field(max_length=1024),
        BeforeValidator(immutable_sequence),
    ]
    use_contexts: Annotated[
        tuple[ProfileUseContext, ...],
        Field(max_length=1024),
        BeforeValidator(immutable_sequence),
    ]
    reviews: Annotated[
        tuple[ReviewMetadata, ...],
        Field(max_length=1024),
        BeforeValidator(immutable_sequence),
    ]
    validation_at: AbsentOptional[UTCDateTime] = Field(
        default=None,
        validate_default=False,
        exclude_if=lambda v: v is None,
        json_schema_extra=omit_default_from_schema,
    )
