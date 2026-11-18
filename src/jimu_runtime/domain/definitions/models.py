"""Logical model capability contracts; deployment routing is separately configured."""

from typing import Literal, Self

from pydantic import ConfigDict, Field, model_validator

from jimu_runtime.domain.definition_enums import ModelCapability
from jimu_runtime.domain.definition_support import (
    AbsentOptional,
    DefinitionContract,
    omit_default_from_schema,
)
from jimu_runtime.domain.definitions.prompts import PromptTemplateContentRef
from jimu_runtime.domain.enums import ArtifactKind
from jimu_runtime.domain.primitives import ArtifactId, Version
from jimu_runtime.domain.references import ContentRef, SchemaRef


class PolicyContentRef(ContentRef):
    artifact_kind: Literal[ArtifactKind.POLICY]


class ModelBindingContentRef(ContentRef):
    artifact_kind: Literal[ArtifactKind.MODEL_BINDING]


class ModelBinding(DefinitionContract):
    model_config = ConfigDict(
        json_schema_extra={
            "allOf": [
                {
                    "if": {
                        "properties": {
                            "capability": {"not": {"const": "vision_auxiliary"}}
                        },
                        "required": ["capability"],
                    },
                    "then": {"required": ["prompt_template_ref"]},
                }
            ]
        }
    )
    model_binding_id: ArtifactId
    version: Version
    capability: ModelCapability
    logical_model_id: ArtifactId
    provider_policy_ref: PolicyContentRef
    resource_requirements_ref: PolicyContentRef
    fallback_policy_ref: PolicyContentRef
    input_schema_ref: SchemaRef
    output_schema_ref: SchemaRef
    prompt_template_ref: AbsentOptional[PromptTemplateContentRef] = Field(
        default=None,
        validate_default=False,
        exclude_if=lambda v: v is None,
        json_schema_extra=omit_default_from_schema,
    )

    @model_validator(mode="after")
    def prompt_required(self) -> Self:
        if (
            self.capability is not ModelCapability.VISION_AUXILIARY
            and self.prompt_template_ref is None
        ):
            raise ValueError("language/VLM capability requires a prompt template")
        return self
