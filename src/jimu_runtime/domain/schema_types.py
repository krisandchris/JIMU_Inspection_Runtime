"""Finite schema views: data only, never arbitrary JSON Schema or executable rules."""

from typing import Annotated, Literal, Self

from pydantic import BeforeValidator, Field, model_validator

from jimu_runtime.domain.base import ContractModel
from jimu_runtime.domain.definition_support import FieldPath, immutable_sequence
from jimu_runtime.domain.enums import ArtifactKind
from jimu_runtime.domain.primitives import ArtifactId
from jimu_runtime.domain.references import SchemaRef
from jimu_runtime.domain.values import FieldName, TypeDefinition

type Representation = Literal["raw_dto", "typed_value"]
type WirePath = Annotated[
    tuple[FieldName, ...], Field(max_length=16), BeforeValidator(immutable_sequence)
]


class RawSchemaRef(SchemaRef):
    artifact_kind: Literal[
        ArtifactKind.CONTEXT_SCHEMA,
        ArtifactKind.ATTRIBUTE_SCHEMA,
        ArtifactKind.RESPONSE_SCHEMA,
        ArtifactKind.TOOL_INPUT_SCHEMA,
        ArtifactKind.OBJECT_SCHEMA,
        ArtifactKind.DECISION_SCHEMA,
    ]


class TypedSchemaRef(SchemaRef):
    artifact_kind: Literal[
        ArtifactKind.VALUE_SCHEMA,
        ArtifactKind.ATTRIBUTE_SCHEMA,
        ArtifactKind.TOOL_INPUT_SCHEMA,
        ArtifactKind.RESPONSE_SCHEMA,
    ]


class ToolInputSchemaRef(SchemaRef):
    artifact_kind: Literal[ArtifactKind.TOOL_INPUT_SCHEMA]


class ToolOutputSchemaRef(SchemaRef):
    artifact_kind: Literal[ArtifactKind.TOOL_OUTPUT_SCHEMA]


class ObjectSchemaRef(SchemaRef):
    artifact_kind: Literal[ArtifactKind.OBJECT_SCHEMA]


class ImageSchemaRef(SchemaRef):
    artifact_kind: Literal[ArtifactKind.ATTRIBUTE_SCHEMA]


class RawDtoSchemaDescriptor(ContractModel):
    descriptor_kind: Literal["raw_dto"]
    schema_ref: RawSchemaRef
    type_definition: TypeDefinition


class TypedValueSchemaDescriptor(ContractModel):
    descriptor_kind: Literal["typed_value"]
    schema_ref: TypedSchemaRef
    type_definition: TypeDefinition


class EvidenceFieldSpec(ContractModel):
    name: FieldName
    required: bool
    multiple: bool


class ToolOutputDescriptor(ContractModel):
    output_name: ArtifactId
    value_path: FieldPath
    required: bool


class ToolResultSchemaDescriptor(ContractModel):
    descriptor_kind: Literal["tool_result"]
    schema_ref: ToolOutputSchemaRef
    payload_representation: Literal["typed_value"]
    payload_type: TypeDefinition
    evidence_fields: Annotated[
        tuple[EvidenceFieldSpec, ...],
        Field(max_length=64),
        BeforeValidator(immutable_sequence),
    ]
    output_declarations: Annotated[
        tuple[ToolOutputDescriptor, ...],
        Field(max_length=64),
        BeforeValidator(immutable_sequence),
    ]

    @model_validator(mode="after")
    def unique_names(self) -> Self:
        _unique(tuple(item.name for item in self.evidence_fields), "evidence slot")
        _unique(
            tuple(item.output_name for item in self.output_declarations), "output name"
        )
        return self


class ToolInputFieldSpec(ContractModel):
    name: FieldName
    required: bool
    representation: Representation
    type_definition: TypeDefinition


class ToolInputRecordDescriptor(ContractModel):
    descriptor_kind: Literal["tool_input_record"]
    schema_ref: ToolInputSchemaRef
    fields: Annotated[
        tuple[ToolInputFieldSpec, ...],
        Field(min_length=1, max_length=64),
        BeforeValidator(immutable_sequence),
    ]

    @model_validator(mode="after")
    def unique_names(self) -> Self:
        _unique(tuple(item.name for item in self.fields), "input field")
        return self


type SchemaDescriptor = Annotated[
    RawDtoSchemaDescriptor
    | TypedValueSchemaDescriptor
    | ToolResultSchemaDescriptor
    | ToolInputRecordDescriptor,
    Field(discriminator="descriptor_kind"),
]


class FieldSelector(ContractModel):
    schema_ref: SchemaRef
    field_path: FieldPath


class ResolvedFieldType(ContractModel):
    schema_ref: SchemaRef
    field_path: FieldPath
    representation: Representation
    type_definition: TypeDefinition
    optional_ancestor_paths: Annotated[
        tuple[FieldPath, ...],
        Field(max_length=9),
        BeforeValidator(immutable_sequence),
    ]
    wire_path: WirePath

    @model_validator(mode="after")
    def ordered_optional_paths(self) -> Self:
        paths = self.optional_ancestor_paths
        if len(paths) != len(set(paths)) or tuple(sorted(paths, key=len)) != paths:
            raise ValueError("optional ancestor paths must be unique and ordered")
        if any(self.field_path[: len(path)] != path for path in paths):
            raise ValueError("optional ancestor must be a prefix of selected path")
        return self


def _unique(names: tuple[str, ...], label: str) -> None:
    if len(set(names)) != len(names):
        raise ValueError(f"duplicate {label}")
