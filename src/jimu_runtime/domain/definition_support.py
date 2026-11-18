"""Strict shared data contracts, without content resolution or runtime behavior."""

import json
import re
from collections.abc import Sequence
from datetime import datetime, timedelta
from typing import Annotated, Literal, Self, cast

from pydantic import (
    AfterValidator,
    BeforeValidator,
    Field,
    GetJsonSchemaHandler,
    ModelWrapValidatorHandler,
    StrictInt,
    StringConstraints,
    TypeAdapter,
    ValidationInfo,
    model_validator,
)
from pydantic.json_schema import JsonSchemaValue
from pydantic_core import CoreSchema

from jimu_runtime.domain.base import ContractModel
from jimu_runtime.domain.definition_enums import (
    CapabilityFamily,
    CapabilityFeature,
    ContextSource,
)
from jimu_runtime.domain.enums import ArtifactKind
from jimu_runtime.domain.primitives import ArtifactId, Digest, Version
from jimu_runtime.domain.references import ContentRef, SchemaRef
from jimu_runtime.domain.values import FieldName


def _nonblank(value: str) -> str:
    if not value.strip():
        raise ValueError("text must contain a non-whitespace character")
    return value


type ShortText = Annotated[
    str,
    StringConstraints(strict=True, min_length=1, max_length=128),
    AfterValidator(_nonblank),
    Field(json_schema_extra={"pattern": r"\S"}),
]
type Description = Annotated[
    str,
    StringConstraints(strict=True, min_length=1, max_length=4096),
    AfterValidator(_nonblank),
    Field(json_schema_extra={"pattern": r"\S"}),
]
type Namespace = ArtifactId
type SemanticKey = ArtifactId
type PositiveBudget = Annotated[StrictInt, Field(ge=1, le=2**63 - 1)]
type NonNegativeBudget = Annotated[StrictInt, Field(ge=0, le=2**63 - 1)]


def immutable_sequence(value: object) -> object:
    """Copy JSON arrays/Python list or tuple, rejecting other iterables."""
    if type(value) not in (list, tuple):
        raise ValueError("expected an array (list/tuple)")
    return tuple(cast(Sequence[object], value))


type FieldPath = Annotated[
    tuple[FieldName, ...], Field(max_length=8), BeforeValidator(immutable_sequence)
]


def _utc_datetime(value: datetime) -> datetime:
    if value.utcoffset() is None or value.utcoffset() != timedelta(0):
        raise ValueError("datetime must be aware with zero UTC offset")
    return value


UTC_DATETIME_PATTERN = (
    r"^[0-9]{4}-(?:0[1-9]|1[0-2])-(?:0[1-9]|[12][0-9]|3[01])[Tt]"
    r"(?:[01][0-9]|2[0-3]):[0-5][0-9]:[0-5][0-9](?:\.[0-9]+)?"
    r"(?:[Zz]|\+00:00)$(?![\s\S])"
)


def _json_utc_datetime(value: object, info: ValidationInfo) -> object:
    """Parse only RFC3339 JSON strings; Python values keep strict datetime typing."""
    if isinstance(value, datetime) or info.mode != "json":
        return value
    if type(value) is not str or re.fullmatch(UTC_DATETIME_PATTERN, value) is None:
        raise ValueError("JSON timestamp requires RFC3339 date-time and explicit UTC")
    # -00:00 preserves unknown local-offset provenance. This normalized internal
    # timestamp domain deliberately excludes it rather than discarding that fact.
    # Parse the underlying datetime, not this alias: no recursive TypeAdapter.
    # Calendar validity remains Pydantic's responsibility after lexical checks.
    return TypeAdapter(datetime).validate_json(json.dumps(value), strict=True)


type UTCDateTime = Annotated[
    datetime,
    Field(strict=True, json_schema_extra={"pattern": UTC_DATETIME_PATTERN}),
    AfterValidator(_utc_datetime),
    BeforeValidator(_json_utc_datetime),
]


def _reject_null(value: object) -> object:
    if value is None:
        raise ValueError("optional field must be absent rather than null")
    return value


class _AbsentOptionalSchema:
    """Keep optional non-null fields optional in generated JSON Schema too."""

    def __get_pydantic_json_schema__(
        self, core_schema: CoreSchema, handler: GetJsonSchemaHandler
    ) -> JsonSchemaValue:
        schema = handler(core_schema)
        branches = schema.get("anyOf")
        if isinstance(branches, list):
            non_null = [branch for branch in branches if branch != {"type": "null"}]
            if len(non_null) == 1:
                schema = cast(JsonSchemaValue, non_null[0]).copy()
        return schema


# The None default represents absence inside Python only. The before-validator
# rejects supplied None; default validation is explicitly disabled for absence.
# Serialization must omit absent optionals, so no wire null is ever fabricated.
type AbsentOptional[T] = Annotated[
    T | None,
    BeforeValidator(_reject_null),
    _AbsentOptionalSchema(),
]


def omit_default_from_schema(schema: JsonSchemaValue) -> None:
    schema.pop("default", None)


class OptionalContract(ContractModel):
    """Preserve absent optionals when revalidating immutable model instances."""

    @model_validator(mode="wrap")
    @classmethod
    def revalidate_absence(
        cls, value: object, handler: ModelWrapValidatorHandler[Self]
    ) -> Self:
        if isinstance(value, cls):
            # Keep the complete instance payload, including copy-injected or
            # subclass fields. Rebuilding from cls.model_fields would silently
            # drop unknown data before extra="forbid" can reject it.
            payload: dict[str, object] = dict(value.__dict__)
            if value.__pydantic_extra__:
                for name, extra in value.__pydantic_extra__.items():
                    if name in payload:
                        raise ValueError("instance field and extra data conflict")
                    payload[name] = extra
            # Core revalidation otherwise treats an omitted None default as an
            # explicit wire null. Remove only that proven absence; supplied null
            # and every other actual field remain subject to strict validation.
            for name, field in cls.model_fields.items():
                if (
                    field.default is None
                    and name not in value.model_fields_set
                    and name in payload
                    and payload[name] is None
                ):
                    del payload[name]
            value = payload
        return handler(value)


class DefinitionContract(OptionalContract):
    """Persisted B definition root; nested contracts remain unversioned helpers."""

    schema_version: Literal["1.0.0"]


class VocabularyContentRef(ContentRef):
    artifact_kind: Literal[ArtifactKind.VOCABULARY]


class AttributeSchemaContentRef(ContentRef):
    artifact_kind: Literal[ArtifactKind.ATTRIBUTE_SCHEMA]


class StaticAssetContentRef(ContentRef):
    artifact_kind: Literal[ArtifactKind.STATIC_ASSET]


class ContextSchemaContentRef(ContentRef):
    artifact_kind: Literal[ArtifactKind.CONTEXT_SCHEMA]


class GeometryResourceContentRef(ContentRef):
    artifact_kind: Literal[ArtifactKind.GEOMETRY_RESOURCE]


class ContextSchemaRef(SchemaRef):
    artifact_kind: Literal[ArtifactKind.CONTEXT_SCHEMA]


def reference_identity(ref: ContentRef) -> tuple[ArtifactKind, str, str, str]:
    """Return content identity; SchemaRef dialect requires separate validation."""
    return (ref.artifact_kind, ref.artifact_id, ref.version, ref.digest)


def require_unique_references(refs: tuple[ContentRef, ...]) -> None:
    """Reject duplicates or different digests for one kind/id/version."""
    keys = [(ref.artifact_kind, ref.artifact_id, ref.version) for ref in refs]
    if len(set(keys)) != len(keys):
        raise ValueError("references must have unique kind/id/version identities")


class ReviewReportRef(ContractModel):
    report_id: ArtifactId
    version: Version
    sha256: Digest
    review_registry_id: ArtifactId


class ExternalCapabilityRequirement(OptionalContract):
    family: CapabilityFamily
    capability_id: ArtifactId
    contract_version: Version
    allowed_implementation_versions: Annotated[
        tuple[Version, ...],
        Field(min_length=1, max_length=1024, json_schema_extra={"uniqueItems": True}),
        BeforeValidator(immutable_sequence),
    ]
    required_features: Annotated[
        tuple[CapabilityFeature, ...],
        Field(max_length=7, json_schema_extra={"uniqueItems": True}),
        BeforeValidator(immutable_sequence),
    ]
    evaluation_ref: AbsentOptional[ReviewReportRef] = Field(
        default=None,
        validate_default=False,
        exclude_if=lambda value: value is None,
        json_schema_extra=omit_default_from_schema,
    )

    @model_validator(mode="after")
    def unique_collections(self) -> Self:
        if len(set(self.allowed_implementation_versions)) != len(
            self.allowed_implementation_versions
        ):
            raise ValueError("implementation versions must be unique")
        if len(set(self.required_features)) != len(self.required_features):
            raise ValueError("required features must be unique")
        return self


class VocabularyMemberRef(ContractModel):
    vocabulary_ref: VocabularyContentRef
    member: ShortText


class VersionConstraint(ContractModel):
    allowed_deployment_versions: Annotated[
        tuple[Version, ...],
        Field(min_length=1, max_length=1024, json_schema_extra={"uniqueItems": True}),
        BeforeValidator(immutable_sequence),
    ]
    allowed_model_versions: Annotated[
        tuple[Version, ...],
        Field(min_length=1, max_length=1024, json_schema_extra={"uniqueItems": True}),
        BeforeValidator(immutable_sequence),
    ]

    @model_validator(mode="after")
    def unique_versions(self) -> Self:
        for values in (self.allowed_deployment_versions, self.allowed_model_versions):
            if len(set(values)) != len(values):
                raise ValueError("versions must be unique")
        return self


class ContextFieldSelector(ContractModel):
    source: ContextSource
    schema_ref: ContextSchemaRef
    field_path: Annotated[
        tuple[FieldName, ...],
        Field(min_length=1, max_length=8),
        BeforeValidator(immutable_sequence),
    ]


class AssetDimensions(ContractModel):
    width: PositiveBudget
    height: PositiveBudget
