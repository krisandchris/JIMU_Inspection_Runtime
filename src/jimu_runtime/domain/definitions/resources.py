"""Static resource descriptors; no filesystem or executable-resource loading."""

import re
from typing import Annotated, Self

from pydantic import (
    BeforeValidator,
    Field,
    StringConstraints,
    field_validator,
    model_validator,
)

from jimu_runtime.domain.definition_support import (
    AbsentOptional,
    AssetDimensions,
    DefinitionContract,
    NonNegativeBudget,
    ShortText,
    immutable_sequence,
    omit_default_from_schema,
)
from jimu_runtime.domain.primitives import ArtifactId, Digest, Version


class VocabularyDefinition(DefinitionContract):
    vocabulary_id: ArtifactId
    version: Version
    semantic_domain: ArtifactId
    members: Annotated[
        tuple[ShortText, ...],
        Field(min_length=1, max_length=1024, json_schema_extra={"uniqueItems": True}),
        BeforeValidator(immutable_sequence),
    ]

    @model_validator(mode="after")
    def unique_members(self) -> Self:
        if len(set(self.members)) != len(self.members):
            raise ValueError("vocabulary members must be unique")
        return self


# ASCII MIME token syntax (RFC 2045 token excludes tspecials/control/space).
_MIME_PATTERN = r"^[A-Za-z0-9!#$%&'*+.^_`|~-]+/[A-Za-z0-9!#$%&'*+.^_`|~-]+$(?![\s\S])"
_PATH_PATTERN = (
    r"^(?!.*(?:^|/)[.]{1,2}(?:/|$))resources/assets/data/"
    r"[^\\\x00-\x1f\x7f-\x9f:?#/]+(?:/[^\\\x00-\x1f\x7f-\x9f:?#/]+)*$(?![\s\S])"
)


type AssetDataPath = Annotated[
    str,
    StringConstraints(
        strict=True, min_length=1, max_length=512, pattern=re.compile(_PATH_PATTERN)
    ),
]
type MimeType = Annotated[
    ShortText, Field(json_schema_extra={"pattern": _MIME_PATTERN})
]


class StaticAssetDescriptor(DefinitionContract):
    asset_id: ArtifactId
    version: Version
    data_path: AssetDataPath
    sha256: Digest
    mime_type: MimeType
    byte_size: NonNegativeBudget
    dimensions: AbsentOptional[AssetDimensions] = Field(
        default=None,
        validate_default=False,
        exclude_if=lambda value: value is None,
        json_schema_extra=omit_default_from_schema,
    )

    @field_validator("mime_type")
    @classmethod
    def mime_token(cls, value: str) -> str:
        if re.fullmatch(_MIME_PATTERN, value, flags=re.ASCII) is None:
            raise ValueError("MIME must be ASCII type/subtype without parameters")
        return value
