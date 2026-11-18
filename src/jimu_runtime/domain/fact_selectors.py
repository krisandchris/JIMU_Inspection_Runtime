"""Static Fact identities; no runtime resolution or activation expressions."""

from typing import Annotated, Literal, Self

from pydantic import (
    BeforeValidator,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)

from jimu_runtime.domain.base import ContractModel
from jimu_runtime.domain.definition_enums import (
    CapabilityFamily,
    FactScope,
    ProducerKind,
)
from jimu_runtime.domain.definition_support import (
    ExternalCapabilityRequirement,
    FieldPath,
)
from jimu_runtime.domain.enums import ArtifactKind
from jimu_runtime.domain.primitives import ArtifactId
from jimu_runtime.domain.references import ContentRef


class QuestionContentRef(ContentRef):
    artifact_kind: Literal[ArtifactKind.QUESTION]


class ToolPlanContentRef(ContentRef):
    artifact_kind: Literal[ArtifactKind.TOOL_PLAN]


class ToolContentRef(ContentRef):
    artifact_kind: Literal[ArtifactKind.TOOL]


class FactSchemaContentRef(ContentRef):
    artifact_kind: Literal[ArtifactKind.FACT_SCHEMA]


class ProducerCatalogueContentRef(ContentRef):
    artifact_kind: Literal[ArtifactKind.FACT_PRODUCER_CATALOG]


class SystemProducerRequirement(ExternalCapabilityRequirement):
    family: Literal[CapabilityFamily.SYSTEM_FACT_PRODUCER]

    @field_validator("family", mode="before")
    @classmethod
    def decode_family(cls, value: object) -> CapabilityFamily:
        return CapabilityFamily._decode(value)


class _ProducerSelector(ContractModel):
    @model_validator(mode="before")
    @classmethod
    def decode_producer(cls, value: object) -> object:
        return _decode_selector(value)


class QuestionProducerSelector(_ProducerSelector):
    producer_kind: Literal[ProducerKind.VLM_QUESTION]
    question_ref: QuestionContentRef
    output_name: Literal["primary"]


class ToolProducerSelector(_ProducerSelector):
    producer_kind: Literal[ProducerKind.TOOL]
    tool_plan_ref: ToolPlanContentRef
    step_id: ArtifactId
    tool_ref: ToolContentRef
    output_name: ArtifactId


class SystemProducerSelector(_ProducerSelector):
    producer_kind: Literal[ProducerKind.SYSTEM]
    requirement: SystemProducerRequirement
    output_name: ArtifactId


def _decode_selector(value: object) -> object:
    if isinstance(value, dict) and "producer_kind" in value:
        return {**value, "producer_kind": ProducerKind._decode(value["producer_kind"])}
    return value


type ProducerSelector = Annotated[
    QuestionProducerSelector | ToolProducerSelector | SystemProducerSelector,
    Field(discriminator="producer_kind"),
    BeforeValidator(_decode_selector),
]


class FactSelector(ContractModel):
    model_config = ConfigDict(
        json_schema_extra={
            "allOf": [
                {
                    "if": {
                        "properties": {"scope": {"const": "execution"}},
                        "required": ["scope"],
                    },
                    "then": {
                        "properties": {
                            "producer_selector": {
                                "properties": {"producer_kind": {"const": "system"}}
                            }
                        }
                    },
                }
            ]
        }
    )
    scope: FactScope
    fact_key: ArtifactId
    fact_schema_ref: FactSchemaContentRef
    producer_selector: ProducerSelector
    field_path: FieldPath

    @model_validator(mode="after")
    def validate_scope(self) -> Self:
        if self.scope is FactScope.EXECUTION and not isinstance(
            self.producer_selector, SystemProducerSelector
        ):
            raise ValueError("execution Fact selectors require a system producer")
        return self
