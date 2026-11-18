"""Closed Fact Schema contracts and bounded normalization before A defaults."""

from typing import Annotated, Literal, Self, cast

from pydantic import (
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    StrictInt,
    TypeAdapter,
    model_validator,
)

from jimu_runtime.domain.base import ContractModel
from jimu_runtime.domain.definition_enums import FactScope, ProducerKind
from jimu_runtime.domain.definition_support import (
    DefinitionContract,
    FieldPath,
    VocabularyMemberRef,
    immutable_sequence,
)
from jimu_runtime.domain.enums import ArtifactKind, FactStatus
from jimu_runtime.domain.primitives import ArtifactId, Digest, Version
from jimu_runtime.domain.references import ContentRef
from jimu_runtime.domain.values import (
    RecordTypeDefinition,
    TypeDefinition,
)

type ProjectionErrorCode = Literal[
    "invalid_type",
    "cyclic_type",
    "type_limit",
    "invalid_unicode",
    "canonicalizer_integer_domain",
    "invalid_number",
    "canonicalization_failed",
    "child_reference_mismatch",
]
type ErrorPath = tuple[str | int, ...]


class ProjectionValidationError(ValueError):
    """Stable technical error code and path; never a provider exception shape."""

    def __init__(
        self, code: ProjectionErrorCode, path: ErrorPath, message: str
    ) -> None:
        self.code = code
        self.path = path
        self.message = message
        super().__init__(f"{code} at {path}: {message}")


class GlobalTypeLimits(ContractModel):
    string_max_length: Annotated[StrictInt, Field(ge=1, le=4096)]
    list_max_items: Annotated[StrictInt, Field(ge=1, le=256)]
    record_max_fields: Annotated[StrictInt, Field(ge=1, le=64)]
    max_depth: Annotated[StrictInt, Field(ge=1, le=8)]


class ValueSchemaGenerationSpec(ContractModel):
    projection_profile_version: Literal["1.0.0"]
    generator_id: Literal["jimu.value-schema"]
    generator_version: Literal["1.0.0"]
    schema_dialect: Literal["https://json-schema.org/draft/2020-12/schema"]
    canonicalization_id: Literal["JCS"]
    global_limits: GlobalTypeLimits


def _payload(value: object) -> object:
    """Preserve all copy/construct-injected fields during model revalidation."""
    if isinstance(value, BaseModel):
        result: dict[str, object] = dict(value.__dict__)
        if value.__pydantic_extra__:
            for key, item in value.__pydantic_extra__.items():
                if key in result:
                    raise ValueError("instance fields conflict with extra fields")
                result[key] = item
        return result
    return value


def _unicode(text: str, path: ErrorPath) -> None:
    try:
        text.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise ProjectionValidationError(
            "invalid_unicode", path, "invalid Unicode scalar"
        ) from exc


def _normalize_node(
    raw: object, limits: GlobalTypeLimits, path: ErrorPath, depth: int, active: set[int]
) -> dict[str, object]:
    identity = id(raw)
    if identity in active:
        raise ProjectionValidationError("cyclic_type", path, "recursive type input")
    active.add(identity)
    try:
        candidate = _payload(raw)
        if type(candidate) is not dict:
            raise ProjectionValidationError(
                "invalid_type", path, "type definition must be an object"
            )
        node = dict(cast(dict[str, object], candidate))
        for key in node:
            if type(key) is not str:
                raise ProjectionValidationError(
                    "invalid_type", path, "field names must be strings"
                )
            _unicode(key, path + (key,))
        kind = node.get("kind")
        if kind in ("list", "record"):
            depth += 1
            if depth > limits.max_depth:
                raise ProjectionValidationError(
                    "type_limit", path, "container depth exceeds limit"
                )
        if kind == "string":
            node.setdefault("min_length", 0)
            node.setdefault("max_length", limits.string_max_length)
            maximum = node["max_length"]
            if type(maximum) is int and maximum > limits.string_max_length:
                raise ProjectionValidationError(
                    "type_limit",
                    path + ("max_length",),
                    "string exceeds generation limit",
                )
        elif kind == "list":
            node.setdefault("min_items", 0)
            node.setdefault("max_items", limits.list_max_items)
            maximum = node["max_items"]
            if type(maximum) is int and maximum > limits.list_max_items:
                raise ProjectionValidationError(
                    "type_limit", path + ("max_items",), "list exceeds generation limit"
                )
            node["item_type"] = _normalize_node(
                node.get("item_type"), limits, path + ("item_type",), depth, active
            )
        elif kind == "record":
            fields = node.get("fields")
            if type(fields) not in (list, tuple):
                raise ProjectionValidationError(
                    "invalid_type", path + ("fields",), "fields require array"
                )
            field_items = cast(list[object] | tuple[object, ...], fields)
            if len(field_items) > limits.record_max_fields:
                raise ProjectionValidationError(
                    "type_limit", path + ("fields",), "record exceeds generation limit"
                )
            normalized: list[dict[str, object]] = []
            for index, field in enumerate(field_items):
                data = _payload(field)
                field_path = path + ("fields", index)
                if type(data) is not dict:
                    raise ProjectionValidationError(
                        "invalid_type", field_path, "record field must be object"
                    )
                item = dict(cast(dict[str, object], data))
                name = item.get("name")
                if type(name) is not str:
                    raise ProjectionValidationError(
                        "invalid_type",
                        field_path + ("name",),
                        "field name requires string",
                    )
                _unicode(name, field_path + ("name",))
                item["type_definition"] = _normalize_node(
                    item.get("type_definition"),
                    limits,
                    field_path + ("type_definition",),
                    depth,
                    active,
                )
                normalized.append(item)
            node["fields"] = sorted(
                normalized, key=lambda item: cast(str, item["name"]).encode("utf-8")
            )
        elif kind == "enum":
            members = node.get("members")
            if type(members) not in (list, tuple):
                raise ProjectionValidationError(
                    "invalid_type", path + ("members",), "members require array"
                )
            member_items = cast(list[object] | tuple[object, ...], members)
            if len(member_items) > 256:
                raise ProjectionValidationError(
                    "type_limit", path + ("members",), "too many enum members"
                )
            strings: list[str] = []
            for index, member in enumerate(member_items):
                if type(member) is not str:
                    raise ProjectionValidationError(
                        "invalid_type",
                        path + ("members", index),
                        "member requires string",
                    )
                _unicode(member, path + ("members", index))
                if len(member) > limits.string_max_length:
                    raise ProjectionValidationError(
                        "type_limit",
                        path + ("members", index),
                        "enum member exceeds generation string limit",
                    )
                strings.append(member)
            node["members"] = sorted(strings, key=lambda member: member.encode("utf-8"))
        if kind in ("integer", "number"):
            for name in ("minimum", "maximum"):
                bound = node.get(name)
                if type(bound) is int and not -(2**53 - 1) <= bound <= 2**53 - 1:
                    raise ProjectionValidationError(
                        "canonicalizer_integer_domain",
                        path + (name,),
                        "integer exceeds chosen canonicalizer support domain",
                    )
        return node
    finally:
        active.remove(identity)


def normalize_type(raw: object, limits: GlobalTypeLimits) -> TypeDefinition:
    """Validate bounded raw/constructed A types, preserving explicit limits."""
    checked = GlobalTypeLimits.model_validate(limits)
    node = _normalize_node(raw, checked, (), 0, set())
    return TypeAdapter(TypeDefinition).validate_python(node)


class ValueSchemaGenerationInput(ContractModel):
    projection: TypeDefinition
    generation_spec: ValueSchemaGenerationSpec

    @model_validator(mode="before")
    @classmethod
    def normalize_projection(cls, raw: object) -> object:
        data = _payload(raw)
        if (
            isinstance(data, dict)
            and "generation_spec" in data
            and "projection" in data
        ):
            spec = ValueSchemaGenerationSpec.model_validate(data["generation_spec"])
            return {
                **data,
                "generation_spec": spec,
                "projection": normalize_type(data["projection"], spec.global_limits),
            }
        return data


class ValueSchemaContentRef(ContentRef):
    artifact_kind: Literal[ArtifactKind.VALUE_SCHEMA]


class PolicyContentRef(ContentRef):
    artifact_kind: Literal[ArtifactKind.POLICY]


class GeneratedValueSchema(ContractModel):
    """Internal output; generation entry points never accept it as schema authority."""

    generation_input: ValueSchemaGenerationInput
    canonical_input_bytes: bytes
    input_sha256: Digest
    canonical_schema_bytes: bytes
    child_digest: Digest
    child_ref: ValueSchemaContentRef


class FactStatusPolicy(ContractModel):
    known_value: Literal["required"]
    non_known_value: Literal["forbidden"]
    non_known_reason: Literal["required"]
    error_diagnostics: Literal["required"]
    not_applicable_basis: Literal["required"]
    not_evaluated_reason: Literal["required"]


class UnitBinding(ContractModel):
    field_path: FieldPath
    unit: VocabularyMemberRef


class CoordinateBinding(ContractModel):
    field_path: FieldPath
    coordinate_system: VocabularyMemberRef


class NoUnitMetadata(ContractModel):
    mode: Literal["not_applicable"]


class DeclaredUnitMetadata(ContractModel):
    mode: Literal["declared"]
    bindings: Annotated[
        tuple[UnitBinding, ...],
        Field(min_length=1, max_length=64),
        BeforeValidator(immutable_sequence),
    ]

    @model_validator(mode="after")
    def unique_paths(self) -> Self:
        _check_annotation_paths(tuple(binding.field_path for binding in self.bindings))
        return self


class NoCoordinateMetadata(ContractModel):
    mode: Literal["not_applicable"]


class DeclaredCoordinateMetadata(ContractModel):
    mode: Literal["declared"]
    bindings: Annotated[
        tuple[CoordinateBinding, ...],
        Field(min_length=1, max_length=64),
        BeforeValidator(immutable_sequence),
    ]

    @model_validator(mode="after")
    def unique_paths(self) -> Self:
        _check_annotation_paths(tuple(binding.field_path for binding in self.bindings))
        return self


def _check_annotation_paths(paths: tuple[tuple[str, ...], ...]) -> None:
    for index, left in enumerate(paths):
        for right in paths[index + 1 :]:
            if left[: len(right)] == right or right[: len(left)] == left:
                raise ValueError("annotation paths duplicate or overlap")


type UnitMetadata = Annotated[
    NoUnitMetadata | DeclaredUnitMetadata, Field(discriminator="mode")
]
type CoordinateMetadata = Annotated[
    NoCoordinateMetadata | DeclaredCoordinateMetadata, Field(discriminator="mode")
]


class ForbiddenConfidenceMetadataPolicy(ContractModel):
    mode: Literal["forbidden"]


class _AllowedConfidencePolicy(ContractModel):
    allowed_fact_statuses: Annotated[
        tuple[FactStatus, ...],
        Field(min_length=1, max_length=5, json_schema_extra={"uniqueItems": True}),
        BeforeValidator(immutable_sequence),
    ]
    interpretation: Literal["model_reported_not_calibrated_probability"]
    numeric_format: Literal["finite_number"]

    @model_validator(mode="after")
    def unique_statuses(self) -> Self:
        if len(self.allowed_fact_statuses) != len(set(self.allowed_fact_statuses)):
            raise ValueError("confidence statuses must be unique")
        return self


class OptionalConfidenceMetadataPolicy(_AllowedConfidencePolicy):
    mode: Literal["optional"]


class RequiredConfidenceMetadataPolicy(_AllowedConfidencePolicy):
    mode: Literal["required"]


type ConfidenceMetadataPolicy = Annotated[
    ForbiddenConfidenceMetadataPolicy
    | OptionalConfidenceMetadataPolicy
    | RequiredConfidenceMetadataPolicy,
    Field(discriminator="mode"),
]


class FactSchemaDefinition(DefinitionContract):
    model_config = ConfigDict(
        json_schema_extra={
            "allOf": [
                {
                    "if": {
                        "properties": {"scope": {"const": "execution"}},
                        "required": ["scope"],
                    },
                    "then": {
                        "properties": {"allowed_producer_kinds": {"const": ["system"]}}
                    },
                }
            ]
        }
    )

    schema_id: ArtifactId
    version: Version
    semantic_key: ArtifactId
    scope: FactScope
    type_definition: TypeDefinition
    payload_schema_ref: ValueSchemaContentRef
    value_schema_generation: ValueSchemaGenerationSpec
    allowed_producer_kinds: Annotated[
        tuple[ProducerKind, ...],
        Field(min_length=1, max_length=3, json_schema_extra={"uniqueItems": True}),
        BeforeValidator(immutable_sequence),
    ]
    status_policy: FactStatusPolicy
    units: UnitMetadata
    coordinate_system: CoordinateMetadata
    evidence_policy_ref: PolicyContentRef
    confidence_metadata_policy: ConfidenceMetadataPolicy

    @model_validator(mode="before")
    @classmethod
    def normalize_definition(cls, raw: object) -> object:
        data = _payload(raw)
        if (
            isinstance(data, dict)
            and "value_schema_generation" in data
            and "type_definition" in data
        ):
            spec = ValueSchemaGenerationSpec.model_validate(
                data["value_schema_generation"]
            )
            return {
                **data,
                "value_schema_generation": spec,
                "type_definition": normalize_type(
                    data["type_definition"], spec.global_limits
                ),
            }
        return data

    @model_validator(mode="after")
    def check_local_relationships(self) -> Self:
        if len(set(self.allowed_producer_kinds)) != len(self.allowed_producer_kinds):
            raise ValueError("producer kinds must be unique")
        if self.scope is FactScope.EXECUTION and self.allowed_producer_kinds != (
            ProducerKind.SYSTEM,
        ):
            raise ValueError("execution Facts require only system producers")
        for metadata in (self.units, self.coordinate_system):
            if isinstance(metadata, (DeclaredUnitMetadata, DeclaredCoordinateMetadata)):
                for binding in metadata.bindings:
                    _resolve_record_path(self.type_definition, binding.field_path)
        return self


def _resolve_record_path(
    definition: TypeDefinition, path: tuple[str, ...]
) -> TypeDefinition:
    current = definition
    for name in path:
        if not isinstance(current, RecordTypeDefinition):
            raise ValueError(
                "annotation paths may traverse only declared Record fields"
            )
        fields = {field.name: field.type_definition for field in current.fields}
        if name not in fields:
            raise ValueError("annotation path does not exist")
        current = fields[name]
    return current
