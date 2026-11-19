"""Local media host contracts. Byte payloads are excluded from representations."""

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Literal

from pydantic import TypeAdapter

from jimu_runtime.domain.base import ContractModel
from jimu_runtime.domain.enums import FactStatus
from jimu_runtime.domain.primitives import ArtifactId, Digest, RecordId
from jimu_runtime.domain.runtime.evidence import (
    DerivedImageEvidence,
    EvidenceAsset,
    RetentionPolicyRef,
)
from jimu_runtime.domain.runtime.media import (
    CoordinateSystem,
    ImageDimensions,
    MediaAsset,
)
from jimu_runtime.domain.values import (
    EnumValue,
    IntegerValue,
    RecordValue,
    StringValue,
    Value,
)
from jimu_runtime.gateway.journal_records import ModelDispatchResult

type ImageMime = Literal["image/jpeg", "image/png"]
type MaskSidecarSlot = Literal["wheel_mask", "tactile_paving_mask"]
type ToolSidecarSlot = MaskSidecarSlot | Literal["geometry"]

RECORD_ID = TypeAdapter[RecordId](RecordId)
STORE_ID = TypeAdapter[ArtifactId](ArtifactId)
IMAGE_MIME = TypeAdapter[ImageMime](ImageMime)
TOOL_SLOT = TypeAdapter[ToolSidecarSlot](ToolSidecarSlot)
MASK_SLOT = TypeAdapter[MaskSidecarSlot](MaskSidecarSlot)


class MediaError(ValueError):
    """A fixed diagnostic without paths, credentials, or payload data."""


@dataclass(frozen=True, slots=True)
class MediaLimits:
    max_original_bytes: int = 16 * 1024 * 1024
    max_response_bytes: int = 8 * 1024 * 1024
    max_sidecar_bytes: int = 16 * 1024 * 1024
    max_dimension: int = 8192
    max_pixels: int = 24_000_000
    max_parallel_decodes: int = 2

    def __post_init__(self) -> None:
        for value, ceiling in (
            (self.max_original_bytes, 64 * 1024 * 1024),
            (self.max_response_bytes, 64 * 1024 * 1024),
            (self.max_sidecar_bytes, 64 * 1024 * 1024),
            (self.max_dimension, 8192),
            (self.max_pixels, 24_000_000),
            (self.max_parallel_decodes, 8),
        ):
            if type(value) is not int or not 1 <= value <= ceiling:
                raise MediaError("media_limits_invalid")


@dataclass(frozen=True, slots=True)
class MediaCleanupPage:
    scanned: int
    removed: int
    next_key: str | None


@dataclass(frozen=True, slots=True)
class VerifiedOriginal:
    asset: MediaAsset
    content: bytes = field(repr=False)

    @property
    def coordinate_system(self) -> CoordinateSystem:
        return CoordinateSystem(
            coordinate_system_id=self.asset.media_ref.media_id,
            frame="original_image",
            dimensions=self.asset.dimensions,
        )


@dataclass(frozen=True, slots=True)
class StoredModelResponse:
    receipt: ModelDispatchResult
    late: bool
    raw: bytes | None = field(repr=False)
    normalized: bytes | None = field(repr=False)


class MaskSidecarRecord(ContractModel):
    """The four declared fields from a named mask output, after value projection."""

    coordinate_system: CoordinateSystem
    evidence_id: RecordId
    sha256: Digest
    source_media_sha256: Digest

    @classmethod
    def from_typed_output(cls, output: Value | None) -> "MaskSidecarRecord":
        fields = _record_fields(
            output,
            {"coordinate_system", "evidence_id", "sha256", "source_media_sha256"},
        )
        coordinates = _record_fields(
            fields["coordinate_system"], {"coordinate_system_id", "dimensions", "frame"}
        )
        dimensions = _record_fields(coordinates["dimensions"], {"width", "height"})
        frame = coordinates["frame"]
        width, height = dimensions["width"], dimensions["height"]
        if (
            not isinstance(frame, EnumValue)
            or frame.value != "original_image"
            or not isinstance(width, IntegerValue)
            or not isinstance(height, IntegerValue)
        ):
            raise MediaError("mask_sidecar_record_invalid")
        return cls(
            coordinate_system=CoordinateSystem(
                coordinate_system_id=_typed_string(coordinates["coordinate_system_id"]),
                frame="original_image",
                dimensions=ImageDimensions(width=width.value, height=height.value),
            ),
            evidence_id=_typed_string(fields["evidence_id"]),
            sha256=_typed_string(fields["sha256"]),
            source_media_sha256=_typed_string(fields["source_media_sha256"]),
        )

    def validate_evidence(self, evidence: EvidenceAsset) -> None:
        spatial = evidence.spatial
        if (
            not isinstance(spatial, DerivedImageEvidence)
            or self.coordinate_system != spatial.transforms[0].source
            or self.evidence_id != evidence.evidence_id
            or self.sha256 != evidence.sha256
            or self.source_media_sha256 != spatial.original_media_ref.sha256
        ):
            raise MediaError("mask_sidecar_record_mismatch")


@dataclass(frozen=True, slots=True)
class ToolSidecarSpec:
    evidence_id: RecordId
    spatial: DerivedImageEvidence
    retention: RetentionPolicyRef
    access: Literal["project", "task"]
    mask_record: MaskSidecarRecord | None = None
    geometry_digest: Digest | None = None


@dataclass(frozen=True, slots=True)
class VerifiedEvidence:
    asset: EvidenceAsset
    content: bytes = field(repr=False)


@dataclass(frozen=True, slots=True)
class AuditedMaskSidecar:
    """Physical evidence with the producing Tool's declared Fact status."""

    asset: EvidenceAsset
    fact_status: FactStatus
    content: bytes = field(repr=False)


def _record_fields(value: Value | None, names: set[str]) -> Mapping[str, Value]:
    if not isinstance(value, RecordValue) or value.value.keys() != names:
        raise MediaError("mask_sidecar_record_invalid")
    return value.value


def _typed_string(value: Value) -> str:
    if not isinstance(value, StringValue):
        raise MediaError("mask_sidecar_record_invalid")
    return value.value
