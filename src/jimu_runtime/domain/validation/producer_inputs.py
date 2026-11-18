"""Finite supplied Producer context; no host discovery, registration or execution."""

from typing import Annotated, Literal, Self

from pydantic import BeforeValidator, Field, field_validator, model_validator

from jimu_runtime.domain.base import ContractModel
from jimu_runtime.domain.definition_enums import CapabilityFamily, CapabilityFeature
from jimu_runtime.domain.definition_support import (
    immutable_sequence,
    reference_identity,
    require_unique_references,
)
from jimu_runtime.domain.fact_selectors import ProducerCatalogueContentRef
from jimu_runtime.domain.primitives import ArtifactId, Version
from jimu_runtime.domain.producers import ProducerCatalogue
from jimu_runtime.domain.validation.authority_inputs import (
    AuthorityQuestionInput,
    AuthorityToolPlanInput,
)
from jimu_runtime.domain.validation.condition_inputs import (
    ProducerActivationConditionContext,
)
from jimu_runtime.domain.validation.definition_inputs import ProfileRegistration
from jimu_runtime.domain.validation.policies import PolicyRegistration
from jimu_runtime.domain.validation.tool_plans import ToolRegistration

type Directory[T] = Annotated[
    tuple[T, ...], Field(max_length=1024), BeforeValidator(immutable_sequence)
]


class ProducerCatalogueRegistration(ContractModel):
    ref: ProducerCatalogueContentRef
    definition: ProducerCatalogue

    @model_validator(mode="after")
    def identity_matches(self) -> Self:
        if (self.ref.artifact_id, self.ref.version) != (
            self.definition.catalogue_id,
            self.definition.version,
        ):
            raise ValueError("Catalogue ref ID/version differs from definition")
        return self


class SystemCapabilityRegistration(ContractModel):
    registry_id: ArtifactId
    family: Literal[CapabilityFamily.SYSTEM_FACT_PRODUCER]
    capability_id: ArtifactId
    contract_version: Version
    available_implementation_versions: Annotated[
        tuple[Version, ...],
        Field(min_length=1, max_length=1024, json_schema_extra={"uniqueItems": True}),
        BeforeValidator(immutable_sequence),
    ]
    supported_features: Annotated[
        tuple[CapabilityFeature, ...],
        Field(max_length=7, json_schema_extra={"uniqueItems": True}),
        BeforeValidator(immutable_sequence),
    ]

    @field_validator("family", mode="before")
    @classmethod
    def family_kind(cls, raw: object) -> CapabilityFamily:
        return CapabilityFamily._decode(raw)

    @model_validator(mode="after")
    def unique_host_data(self) -> Self:
        if len(set(self.available_implementation_versions)) != len(
            self.available_implementation_versions
        ) or len(set(self.supported_features)) != len(self.supported_features):
            raise ValueError("Host versions/features must be unique")
        return self


class ProducerValidationContext(ContractModel):
    profiles: Directory[ProfileRegistration]
    questions: Directory[AuthorityQuestionInput]
    plans: Directory[AuthorityToolPlanInput]
    tools: Directory[ToolRegistration]
    policies: Directory[PolicyRegistration]
    system_capabilities: Directory[SystemCapabilityRegistration]
    condition_contexts: Directory[ProducerActivationConditionContext]

    @model_validator(mode="after")
    def unique_configured_profiles_and_uses(self) -> Self:
        require_unique_references(tuple(item.ref for item in self.profiles))
        keys = []
        for use in self.condition_contexts:
            catalogue = reference_identity(use.producer_catalogue_ref)[:3]
            region = (
                reference_identity(use.region_ref)[:3]
                if use.region_ref is not None
                else None
            )
            keys.append((catalogue, use.declaration_id, region))
        if len(set(keys)) != len(keys):
            raise ValueError("Duplicate Producer condition use key")
        return self


class ProducerCatalogueContext(ContractModel):
    registration: ProducerCatalogueRegistration
    context: ProducerValidationContext
