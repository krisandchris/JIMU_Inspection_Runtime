"""Immutable storage metadata and V1 image-space coordinates."""

import re
from typing import Annotated, Literal, Self

from pydantic import BeforeValidator, Field, StringConstraints, model_validator

from jimu_runtime.domain.base import ContractModel
from jimu_runtime.domain.definition_support import (
    AssetDimensions,
    PositiveBudget,
    ShortText,
    UTCDateTime,
    immutable_sequence,
)
from jimu_runtime.domain.primitives import ArtifactId, Digest, RecordId, Version

ImageDimensions = AssetDimensions

type ImageMimeType = Annotated[
    str,
    StringConstraints(
        strict=True, pattern=r"^image/[a-z0-9][a-z0-9.+-]*$", max_length=128
    ),
]

type ObjectKey = Annotated[
    str,
    StringConstraints(
        strict=True,
        min_length=1,
        max_length=1024,
        # A relative logical object key, never a URL or filesystem traversal.
        pattern=re.compile(
            r"^(?!.*(?:^|/)[.]{1,2}(?:/|$))[A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)*(?![\s\S])"
        ),
    ),
]


class StorageReference(ContractModel):
    store_id: ArtifactId
    object_key: ObjectKey


class StagedAsset(ContractModel):
    stage: Literal["staged"]
    staged_at: UTCDateTime


class FinalizedAsset(ContractModel):
    stage: Literal["finalized"]
    staged_at: UTCDateTime
    verified_at: UTCDateTime
    verified_sha256: Digest
    object_version: ShortText

    @model_validator(mode="after")
    def verify_chronology(self) -> Self:
        if self.verified_at < self.staged_at:
            raise ValueError("asset verification cannot precede staging")
        return self


type AssetStage = Annotated[StagedAsset | FinalizedAsset, Field(discriminator="stage")]


class MediaRef(ContractModel):
    media_id: RecordId
    sha256: Digest


class MediaAsset(ContractModel):
    schema_version: Literal["1.0.0"]
    media_ref: MediaRef
    storage: StorageReference
    stage: AssetStage
    mime_type: ImageMimeType
    byte_size: PositiveBudget
    dimensions: ImageDimensions
    color_space: Literal["RGB"]

    @model_validator(mode="after")
    def match_verified_hash(self) -> Self:
        if isinstance(self.stage, FinalizedAsset) and (
            self.stage.verified_sha256 != self.media_ref.sha256
        ):
            raise ValueError("media digest must match the verified object digest")
        return self


class CoordinateSystem(ContractModel):
    """Image-plane pixel edges with origin at the top left and axes right/down."""

    coordinate_system_id: RecordId
    frame: Literal["original_image", "derived_image"]
    dimensions: ImageDimensions


# Binary64 can represent every integer pixel index through this bound.
_MAX_EXACT_PIXEL_INDEX = 2**53 - 1

type PixelCoordinate = Annotated[
    float, Field(ge=0, le=_MAX_EXACT_PIXEL_INDEX, allow_inf_nan=False)
]


class BBoxXYXY(ContractModel):
    xyxy: Annotated[
        tuple[PixelCoordinate, PixelCoordinate, PixelCoordinate, PixelCoordinate],
        BeforeValidator(immutable_sequence),
    ]

    @model_validator(mode="after")
    def positive_area(self) -> Self:
        x_min, y_min, x_max, y_max = self.xyxy
        if x_min >= x_max or y_min >= y_max:
            raise ValueError("box requires ordered corners and positive area")
        return self

    @classmethod
    def enclosing(cls, boxes: tuple[Self, ...]) -> Self:
        if not boxes:
            raise ValueError("an enclosing box requires at least one member")
        return cls(
            xyxy=(
                min(box.xyxy[0] for box in boxes),
                min(box.xyxy[1] for box in boxes),
                max(box.xyxy[2] for box in boxes),
                max(box.xyxy[3] for box in boxes),
            )
        )

    def validate_bounds(self, dimensions: ImageDimensions) -> None:
        """Validate against the owning original-image dimensions."""
        _, _, x_max, y_max = self.xyxy
        if x_max > dimensions.width or y_max > dimensions.height:
            raise ValueError("box exceeds image dimensions")


type AffineCoefficient = Annotated[
    float,
    Field(ge=-_MAX_EXACT_PIXEL_INDEX, le=_MAX_EXACT_PIXEL_INDEX, allow_inf_nan=False),
]


class TransformRecord(ContractModel):
    """Forward affine mapping from source to target pixel coordinates.

    The six coefficients are [a, b, c, d, e, f], mapping to
    x' = a*x + b*y + c and y' = d*x + e*y + f.
    """

    source: CoordinateSystem
    target: CoordinateSystem
    implementation_id: ArtifactId
    implementation_version: Version
    affine: Annotated[
        tuple[
            AffineCoefficient,
            AffineCoefficient,
            AffineCoefficient,
            AffineCoefficient,
            AffineCoefficient,
            AffineCoefficient,
        ],
        BeforeValidator(immutable_sequence),
    ]

    @model_validator(mode="after")
    def validate_mapping(self) -> Self:
        if self.target.frame != "derived_image" or (
            self.source.coordinate_system_id == self.target.coordinate_system_id
        ):
            raise ValueError("transform requires a distinct derived target frame")
        a, b, _, d, e, _ = self.affine
        if a * e - b * d == 0:
            raise ValueError("affine transform must be invertible")
        return self
