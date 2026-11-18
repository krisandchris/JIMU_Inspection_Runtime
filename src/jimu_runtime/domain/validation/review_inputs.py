"""Finite externally supplied review metadata, not an approval or routing service."""

from typing import Annotated, Literal, Self

from pydantic import BeforeValidator, Field, field_validator, model_validator

from jimu_runtime.domain.base import ContractModel
from jimu_runtime.domain.definition_enums import ModelCapability
from jimu_runtime.domain.definition_support import (
    ReviewReportRef,
    UTCDateTime,
    immutable_sequence,
    require_unique_references,
)
from jimu_runtime.domain.enums import ArtifactKind
from jimu_runtime.domain.primitives import ArtifactId, Version
from jimu_runtime.domain.references import ContentRef, SchemaRef


class ReviewBindingRef(ContentRef):
    artifact_kind: Literal[ArtifactKind.MODEL_BINDING]


class ReviewProfileRef(ContentRef):
    artifact_kind: Literal[ArtifactKind.PROFILE]


class ReviewRegionRef(ContentRef):
    artifact_kind: Literal[ArtifactKind.REGION]


type BindingRefs = Annotated[
    tuple[ReviewBindingRef, ...],
    Field(min_length=1, max_length=1024),
    BeforeValidator(immutable_sequence),
]
type ProfileRefs = Annotated[
    tuple[ReviewProfileRef, ...],
    Field(min_length=1, max_length=1024),
    BeforeValidator(immutable_sequence),
]
type RegionRefs = Annotated[
    tuple[ReviewRegionRef, ...],
    Field(min_length=1, max_length=1024),
    BeforeValidator(immutable_sequence),
]


class _BindingScope(ContractModel):
    binding_refs: BindingRefs

    @model_validator(mode="after")
    def unique_bindings(self) -> Self:
        require_unique_references(self.binding_refs)
        return self


class BindingReviewScope(_BindingScope):
    scope_kind: Literal["binding"]


class ProfileReviewScope(_BindingScope):
    scope_kind: Literal["profile"]
    profile_refs: ProfileRefs

    @model_validator(mode="after")
    def unique_profiles(self) -> Self:
        require_unique_references(self.profile_refs)
        return self


class RegionReviewScope(_BindingScope):
    scope_kind: Literal["region"]
    region_refs: RegionRefs

    @model_validator(mode="after")
    def unique_regions(self) -> Self:
        require_unique_references(self.region_refs)
        return self


class ProfileRegionReviewScope(ContractModel):
    scope_kind: Literal["profile_region"]
    binding_refs: BindingRefs
    profile_refs: ProfileRefs
    region_refs: RegionRefs

    @model_validator(mode="after")
    def unique_refs(self) -> Self:
        require_unique_references(self.binding_refs)
        require_unique_references(self.profile_refs)
        require_unique_references(self.region_refs)
        return self


type ReviewScope = Annotated[
    BindingReviewScope
    | ProfileReviewScope
    | RegionReviewScope
    | ProfileRegionReviewScope,
    Field(discriminator="scope_kind"),
]


class ReviewedDeployment(ContractModel):
    provider_id: ArtifactId
    deployment_id: ArtifactId
    logical_model_id: ArtifactId
    provider_kind: Literal["remote", "local"]
    deployment_version: Version
    model_version: Version


class ReviewedLocalDeployment(ReviewedDeployment):
    provider_kind: Literal["local"]


type TriggerError = Literal["transport", "timeout", "rate_limit", "unavailable"]


class FallbackTransitionAssessment(ContractModel):
    assessment_kind: Literal["fallback_transition"]
    source: ReviewedDeployment
    target: ReviewedDeployment
    capability: ModelCapability
    scope: ReviewScope
    trigger_errors: Annotated[
        tuple[TriggerError, ...],
        Field(min_length=1, max_length=4, json_schema_extra={"uniqueItems": True}),
        BeforeValidator(immutable_sequence),
    ]
    input_schema_ref: SchemaRef
    output_schema_ref: SchemaRef
    contract_compatibility: Literal["exact"]
    quality_evaluated: Literal[True]

    @field_validator("quality_evaluated", mode="before")
    @classmethod
    def explicit_true(cls, value: object) -> object:
        if type(value) is not bool:
            raise ValueError("quality_evaluated requires explicit boolean true")
        return value

    @model_validator(mode="after")
    def distinct_transition(self) -> Self:
        if self.source == self.target:
            raise ValueError("fallback endpoints must be distinct")
        if len(self.trigger_errors) != len(set(self.trigger_errors)):
            raise ValueError("duplicate fallback trigger")
        return self


class LocalExceptionAssessment(ContractModel):
    assessment_kind: Literal["local_primary_exception"]
    exception_id: ArtifactId
    exception_version: Version
    primary: ReviewedLocalDeployment
    applicable_capabilities: Annotated[
        tuple[ModelCapability, ...],
        Field(min_length=1, max_length=5, json_schema_extra={"uniqueItems": True}),
        BeforeValidator(immutable_sequence),
    ]
    scope: ReviewScope
    reason: Literal[
        "explicit_local_capability",
        "privacy_restriction",
        "unavailable_remote_capability",
    ]

    @model_validator(mode="after")
    def unique_capabilities(self) -> Self:
        if len(self.applicable_capabilities) != len(set(self.applicable_capabilities)):
            raise ValueError("duplicate applicable capability")
        return self


type ReviewAssessment = Annotated[
    FallbackTransitionAssessment | LocalExceptionAssessment,
    Field(discriminator="assessment_kind"),
]


class ReviewMetadata(ContractModel):
    report_ref: ReviewReportRef
    verdict: Literal["approved", "rejected"]
    valid_from: UTCDateTime
    valid_until: UTCDateTime
    assessments: Annotated[
        tuple[ReviewAssessment, ...],
        Field(min_length=1, max_length=1024),
        BeforeValidator(immutable_sequence),
    ]

    @model_validator(mode="after")
    def ordered_window(self) -> Self:
        if self.valid_from >= self.valid_until:
            raise ValueError("valid_from must precede valid_until")
        return self
