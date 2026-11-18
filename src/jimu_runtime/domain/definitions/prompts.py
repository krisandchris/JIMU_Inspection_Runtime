"""Literal Prompt declarations and syntax only; no rendering or catalogue closure."""

import re
from typing import Annotated, Literal, Self

from pydantic import (
    BeforeValidator,
    ConfigDict,
    Field,
    StrictBool,
    StringConstraints,
    TypeAdapter,
    field_validator,
    model_validator,
)
from pydantic.config import JsonDict, JsonValue

from jimu_runtime.domain.base import ContractModel
from jimu_runtime.domain.definition_enums import ModelCapability
from jimu_runtime.domain.definition_support import (
    AbsentOptional,
    DefinitionContract,
    FieldPath,
    immutable_sequence,
    omit_default_from_schema,
    require_unique_references,
)
from jimu_runtime.domain.enums import ArtifactKind
from jimu_runtime.domain.primitives import ArtifactId, Version
from jimu_runtime.domain.references import ContentRef, SchemaRef

_NAME_PATTERN = r"^[A-Za-z_][A-Za-z0-9_]{0,127}$(?![\s\S])"
_TEMPLATE_PATTERN = r"^(?:[^{}]|\{\{|\}\}|\{[A-Za-z_][A-Za-z0-9_]{0,127}\})*$(?![\s\S])"

type PromptVariableName = Annotated[
    str,
    StringConstraints(
        strict=True, min_length=1, max_length=128, pattern=re.compile(_NAME_PATTERN)
    ),
]
type TemplateText = Annotated[
    str,
    StringConstraints(
        strict=True,
        min_length=1,
        max_length=65536,
        pattern=re.compile(_TEMPLATE_PATTERN),
    ),
]
type PromptPurpose = Literal[
    ModelCapability.PROFILE_DISCOVERY,
    ModelCapability.OBJECT_GROUNDING,
    ModelCapability.OBJECT_OBSERVATION,
    ModelCapability.CANDIDATE_PREDICTION,
]
type PromptSource = Literal[
    "context",
    "profile_metadata",
    "question_metadata",
    "object_geometry",
    "image_reference",
]
_SOURCE_SCHEMA_KINDS: tuple[tuple[PromptSource, ArtifactKind], ...] = (
    ("context", ArtifactKind.CONTEXT_SCHEMA),
    ("profile_metadata", ArtifactKind.ATTRIBUTE_SCHEMA),
    ("question_metadata", ArtifactKind.ATTRIBUTE_SCHEMA),
    ("object_geometry", ArtifactKind.OBJECT_SCHEMA),
    ("image_reference", ArtifactKind.ATTRIBUTE_SCHEMA),
)


class PromptTemplateContentRef(ContentRef):
    artifact_kind: Literal[ArtifactKind.PROMPT_TEMPLATE]


class ResponseSchemaContentRef(ContentRef):
    artifact_kind: Literal[ArtifactKind.RESPONSE_SCHEMA]


def _prompt_variable_schema(schema: JsonDict) -> None:
    clauses: list[JsonValue] = []
    for source, kind in _SOURCE_SCHEMA_KINDS:
        clauses.append(
            {
                "if": {
                    "properties": {"source": {"const": source}},
                    "required": ["source"],
                },
                "then": {
                    "properties": {
                        "schema_ref": {
                            "properties": {"artifact_kind": {"const": kind.value}}
                        }
                    }
                },
            }
        )
    clauses.append(
        {
            "if": {
                "properties": {"required": {"const": True}},
                "required": ["required"],
            },
            "then": {"properties": {"on_missing": {"const": "block"}}},
            "else": {"properties": {"on_missing": {"const": "omit"}}},
        }
    )
    schema["allOf"] = clauses


class PromptVariable(ContractModel):
    model_config = ConfigDict(json_schema_extra=_prompt_variable_schema)

    name: PromptVariableName
    source: PromptSource
    field_path: FieldPath
    schema_ref: SchemaRef
    required: StrictBool
    on_missing: Literal["block", "omit"]

    @model_validator(mode="after")
    def source_and_missing_contract(self) -> Self:
        expected_kind = dict(_SOURCE_SCHEMA_KINDS)[self.source]
        if self.schema_ref.artifact_kind is not expected_kind:
            raise ValueError("Prompt variable source requires its declared Schema kind")
        expected_missing = "block" if self.required else "omit"
        if self.on_missing != expected_missing:
            raise ValueError("required Prompt variables block; optional variables omit")
        return self


class PromptTemplateDefinition(DefinitionContract):
    prompt_template_id: ArtifactId
    version: Version
    purpose: PromptPurpose
    template_text: TemplateText
    variables: Annotated[
        tuple[PromptVariable, ...],
        Field(max_length=1024, json_schema_extra={"uniqueItems": True}),
        BeforeValidator(immutable_sequence),
    ]
    response_schema_ref: ResponseSchemaContentRef
    rendering_policy: Literal["literal_substitution"]
    partial_refs: AbsentOptional[
        Annotated[
            tuple[PromptTemplateContentRef, ...],
            Field(max_length=1024, json_schema_extra={"uniqueItems": True}),
            BeforeValidator(immutable_sequence),
        ]
    ] = Field(
        default=None,
        validate_default=False,
        exclude_if=lambda value: value is None,
        json_schema_extra=omit_default_from_schema,
    )

    @field_validator("purpose", mode="before")
    @classmethod
    def decode_purpose(cls, value: object) -> ModelCapability:
        return ModelCapability._decode(value)

    @model_validator(mode="after")
    def unique_declarations(self) -> Self:
        names = [variable.name for variable in self.variables]
        if len(names) != len(set(names)):
            raise ValueError("Prompt variables must have unique names")
        if self.partial_refs is not None:
            require_unique_references(self.partial_refs)
        return self


def placeholder_names(template_text: str) -> tuple[str, ...]:
    """Return named uses in source order, preserving repeats and skipping escapes.

    This only parses literal syntax. It does not resolve partials or establish
    that the complete graph declares each name, nor substitute any values.
    """
    text: str = TypeAdapter(TemplateText).validate_python(template_text)
    result: list[str] = []
    position = 0
    while position < len(text):
        if text.startswith(("{{", "}}"), position):
            position += 2
        elif text[position] == "{":
            end = text.index("}", position + 1)
            result.append(text[position + 1 : end])
            position = end + 1
        else:
            position += 1
    return tuple(result)
