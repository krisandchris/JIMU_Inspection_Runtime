"""Complete Producer definition data; no activation inference or runtime authority."""

from typing import Annotated, Literal, Self

from pydantic import (
    BeforeValidator,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)
from pydantic.json_schema import JsonSchemaValue

from jimu_runtime.domain.base import ContractModel
from jimu_runtime.domain.conditions import (
    ConditionExpr,
    condition_references,
    validate_condition_data,
)
from jimu_runtime.domain.definition_enums import (
    ConditionInputDomain,
    FactScope,
    ProducerCardinality,
    ProducerKind,
)
from jimu_runtime.domain.definition_support import (
    AbsentOptional,
    DefinitionContract,
    Description,
    OptionalContract,
    immutable_sequence,
    omit_default_from_schema,
    reference_identity,
)
from jimu_runtime.domain.fact_selectors import (
    FactSchemaContentRef,
    QuestionContentRef,
    SystemProducerRequirement,
    ToolContentRef,
    ToolPlanContentRef,
)
from jimu_runtime.domain.primitives import ArtifactId, Version
from jimu_runtime.domain.tool_mappings import ProfileContentRef


class AlwaysActivation(ContractModel):
    mode: Literal["always"]


class ToolConditionActivation(ContractModel):
    mode: Literal["tool_condition"]
    condition_ast: ConditionExpr

    @field_validator("condition_ast", mode="before")
    @classmethod
    def condition_data(cls, raw: object) -> ConditionExpr:
        return validate_condition_data(
            raw, input_domain=ConditionInputDomain.TOOL_CONDITION
        )


type ActivationCondition = Annotated[
    AlwaysActivation | ToolConditionActivation, Field(discriminator="mode")
]


def _decode_producer(raw: object) -> object:
    if isinstance(raw, dict) and "producer_kind" in raw:
        return {**raw, "producer_kind": ProducerKind._decode(raw["producer_kind"])}
    return raw


class _Producer(ContractModel):
    @model_validator(mode="before")
    @classmethod
    def decode_kind(cls, raw: object) -> object:
        return _decode_producer(raw)


class QuestionProducer(_Producer):
    producer_kind: Literal[ProducerKind.VLM_QUESTION]
    question_ref: QuestionContentRef


class ToolStepProducer(_Producer):
    producer_kind: Literal[ProducerKind.TOOL]
    tool_plan_ref: ToolPlanContentRef
    step_id: ArtifactId
    tool_ref: ToolContentRef


class SystemProducer(_Producer):
    producer_kind: Literal[ProducerKind.SYSTEM]
    requirement: SystemProducerRequirement


type Producer = Annotated[
    QuestionProducer | ToolStepProducer | SystemProducer,
    Field(discriminator="producer_kind"),
    BeforeValidator(_decode_producer),
]


def _declaration_schema(schema: JsonSchemaValue) -> None:
    constraints: list[JsonSchemaValue] = [
        {
            "if": {"properties": {"scope": {"const": "object"}}, "required": ["scope"]},
            "then": {"required": ["profile_ref"]},
        },
        {
            "if": {
                "properties": {"scope": {"const": "execution"}},
                "required": ["scope"],
            },
            "then": {"properties": {"profile_ref": False}},
        },
        {
            "if": {
                "properties": {"producer_kind": {"const": "vlm_question"}},
                "required": ["producer_kind"],
            },
            "then": {
                "properties": {
                    "scope": {"const": "object"},
                    "output_name": {"const": "primary"},
                    "cardinality": {"const": "exactly_one"},
                }
            },
        },
        {
            "if": {
                "properties": {"producer_kind": {"const": "tool"}},
                "required": ["producer_kind"],
            },
            "then": {"properties": {"scope": {"const": "object"}}},
        },
    ]
    for kind in ProducerKind:
        constraints.append(
            {
                "if": {
                    "properties": {"producer_kind": {"const": kind.value}},
                    "required": ["producer_kind"],
                },
                "then": {
                    "properties": {
                        "producer": {
                            "properties": {"producer_kind": {"const": kind.value}}
                        }
                    }
                },
            }
        )
    schema["allOf"] = constraints


class FactProducerDeclaration(OptionalContract):
    model_config = ConfigDict(json_schema_extra=_declaration_schema)
    declaration_id: ArtifactId
    scope: FactScope
    producer_kind: ProducerKind
    producer_id: ArtifactId
    producer_version: Version
    producer: Producer
    output_name: ArtifactId
    fact_key: ArtifactId
    fact_schema_ref: FactSchemaContentRef
    cardinality: ProducerCardinality
    activation_condition: ActivationCondition
    profile_ref: AbsentOptional[ProfileContentRef] = Field(
        default=None,
        validate_default=False,
        exclude_if=lambda value: value is None,
        json_schema_extra=omit_default_from_schema,
    )
    description: AbsentOptional[Description] = Field(
        default=None,
        validate_default=False,
        exclude_if=lambda value: value is None,
        json_schema_extra=omit_default_from_schema,
    )

    @model_validator(mode="after")
    def local_identity(self) -> Self:
        if (self.scope is FactScope.OBJECT) != (self.profile_ref is not None):
            raise ValueError("object iff profile_ref; execution forbids Profile")
        if self.producer_kind is not self.producer.producer_kind:
            raise ValueError("root and nested producer kinds must agree")
        if isinstance(self.producer, QuestionProducer):
            identity = (
                self.producer.question_ref.artifact_id,
                self.producer.question_ref.version,
            )
            if (
                self.scope is not FactScope.OBJECT
                or self.output_name != "primary"
                or self.cardinality is not ProducerCardinality.EXACTLY_ONE
            ):
                raise ValueError("Question requires object/exactly_one/primary")
        elif isinstance(self.producer, ToolStepProducer):
            identity = (
                self.producer.tool_ref.artifact_id,
                self.producer.tool_ref.version,
            )
            if self.scope is not FactScope.OBJECT:
                raise ValueError("Tool Fact production is object-scoped")
        else:
            identity = (
                self.producer.requirement.capability_id,
                self.producer.requirement.contract_version,
            )
        if (self.producer_id, self.producer_version) != identity:
            raise ValueError(
                "configured producer identity must equal exact definition identity"
            )
        if (
            not isinstance(self.producer, ToolStepProducer)
            and isinstance(self.activation_condition, ToolConditionActivation)
            and condition_references(
                self.activation_condition.condition_ast
            ).local_tool_selectors
        ):
            raise ValueError(
                "non-Tool producer cannot bind local-tool condition selectors"
            )
        return self


def declaration_collision_key(
    declaration: FactProducerDeclaration,
) -> tuple[object, ...]:
    return (
        declaration.scope,
        reference_identity(declaration.profile_ref)
        if declaration.profile_ref is not None
        else None,
        declaration.fact_key,
    )


def producer_output_slot(declaration: FactProducerDeclaration) -> tuple[object, ...]:
    prefix: tuple[object, ...] = (
        declaration.scope,
        reference_identity(declaration.profile_ref)
        if declaration.profile_ref is not None
        else None,
        declaration.producer_kind,
    )
    producer = declaration.producer
    if isinstance(producer, QuestionProducer):
        return (
            *prefix,
            reference_identity(producer.question_ref),
            declaration.output_name,
        )
    if isinstance(producer, ToolStepProducer):
        return (
            *prefix,
            reference_identity(producer.tool_plan_ref),
            producer.step_id,
            reference_identity(producer.tool_ref),
            declaration.output_name,
        )
    requirement = producer.requirement
    return (
        *prefix,
        requirement.family,
        requirement.capability_id,
        requirement.contract_version,
        declaration.output_name,
    )


class ProducerCatalogue(DefinitionContract):
    catalogue_id: ArtifactId
    version: Version
    declarations: Annotated[
        tuple[FactProducerDeclaration, ...],
        Field(max_length=1024),
        BeforeValidator(immutable_sequence),
    ]

    @model_validator(mode="after")
    def unique_declarations(self) -> Self:
        ids: set[str] = set()
        keys: set[tuple[object, ...]] = set()
        slots: set[tuple[object, ...]] = set()
        for declaration in self.declarations:
            key = declaration_collision_key(declaration)
            slot = producer_output_slot(declaration)
            if declaration.declaration_id in ids:
                raise ValueError("duplicate declaration_id")
            if key in keys:
                raise ValueError(
                    "Fact key collision regardless of Schema or activation"
                )
            if slot in slots:
                raise ValueError(
                    "producer output-slot must resolve at most one declaration"
                )
            ids.add(declaration.declaration_id)
            keys.add(key)
            slots.add(slot)
        return self
