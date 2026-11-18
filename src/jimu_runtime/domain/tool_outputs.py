"""Declared Tool Fact outputs; no execution, aliasing or producer activation."""

from typing import Annotated, Literal, Self

from pydantic import BeforeValidator, Field, field_validator, model_validator

from jimu_runtime.domain.base import ContractModel
from jimu_runtime.domain.definition_enums import FactScope
from jimu_runtime.domain.definition_support import (
    AbsentOptional,
    FieldPath,
    OptionalContract,
    immutable_sequence,
    omit_default_from_schema,
)
from jimu_runtime.domain.fact_selectors import FactSchemaContentRef
from jimu_runtime.domain.primitives import ArtifactId
from jimu_runtime.domain.values import FieldName

type EvidencePath = Annotated[
    tuple[FieldName, ...],
    Field(min_length=1, max_length=1),
    BeforeValidator(immutable_sequence),
]


class ToolFactOutputDeclaration(OptionalContract):
    output_name: ArtifactId
    fact_key: ArtifactId
    fact_schema_ref: FactSchemaContentRef
    scope: Literal[FactScope.OBJECT]
    required: bool
    value_path: FieldPath
    evidence_paths: AbsentOptional[
        Annotated[
            tuple[EvidencePath, ...],
            Field(max_length=64),
            BeforeValidator(immutable_sequence),
        ]
    ] = Field(
        default=None,
        validate_default=False,
        exclude_if=lambda v: v is None,
        json_schema_extra=omit_default_from_schema,
    )
    confidence_path: AbsentOptional[FieldPath] = Field(
        default=None,
        validate_default=False,
        exclude_if=lambda v: v is None,
        json_schema_extra=omit_default_from_schema,
    )

    @field_validator("scope", mode="before")
    @classmethod
    def decode_scope(cls, value: object) -> FactScope:
        return FactScope._decode(value)

    @model_validator(mode="after")
    def unique_evidence_paths(self) -> Self:
        if self.evidence_paths is not None and len(set(self.evidence_paths)) != len(
            self.evidence_paths
        ):
            raise ValueError("duplicate evidence path")
        return self


class ToolFactOutputMapping(ContractModel):
    output_name: ArtifactId
    target_fact_key: ArtifactId
