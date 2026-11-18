"""Ontology definition shapes; catalogue membership is checked separately."""

from typing import Annotated, Self

from pydantic import BeforeValidator, Field, model_validator

from jimu_runtime.domain.definition_support import (
    AbsentOptional,
    AttributeSchemaContentRef,
    ContextSchemaContentRef,
    DefinitionContract,
    Description,
    GeometryResourceContentRef,
    ShortText,
    StaticAssetContentRef,
    VocabularyMemberRef,
    immutable_sequence,
    omit_default_from_schema,
    require_unique_references,
)
from jimu_runtime.domain.primitives import ArtifactId, Version


class EntityDefinition(DefinitionContract):
    entity_id: ArtifactId
    version: Version
    name: ShortText
    entity_type: VocabularyMemberRef
    description: Description
    attribute_schema_refs: Annotated[
        tuple[AttributeSchemaContentRef, ...],
        Field(max_length=1024, json_schema_extra={"uniqueItems": True}),
        BeforeValidator(immutable_sequence),
    ]
    lifecycle: VocabularyMemberRef

    @model_validator(mode="after")
    def unique_refs(self) -> Self:
        require_unique_references(self.attribute_schema_refs)
        return self


class RegionDefinition(DefinitionContract):
    region_id: ArtifactId
    version: Version
    name: ShortText
    region_type: VocabularyMemberRef
    scene_type: VocabularyMemberRef
    asset_refs: Annotated[
        tuple[StaticAssetContentRef, ...],
        Field(max_length=1024, json_schema_extra={"uniqueItems": True}),
        BeforeValidator(immutable_sequence),
    ]
    context_schema_ref: ContextSchemaContentRef
    geometry_ref: AbsentOptional[GeometryResourceContentRef] = Field(
        default=None,
        validate_default=False,
        exclude_if=lambda value: value is None,
        json_schema_extra=omit_default_from_schema,
    )

    @model_validator(mode="after")
    def unique_refs(self) -> Self:
        require_unique_references(self.asset_refs)
        return self
