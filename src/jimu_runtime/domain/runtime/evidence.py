"""Versioned evidence metadata and provenance, independent of the storage adapter."""

import re
from typing import Annotated, Literal, Self

from pydantic import BeforeValidator, Field, StringConstraints, model_validator

from jimu_runtime.domain.base import ContractModel
from jimu_runtime.domain.definition_enums import EvidenceType
from jimu_runtime.domain.definition_support import (
    PositiveBudget,
    UTCDateTime,
    VocabularyMemberRef,
    immutable_sequence,
)
from jimu_runtime.domain.primitives import ArtifactId, Digest, RecordId, Version
from jimu_runtime.domain.runtime.facts import (
    ObjectRef,
    PolicyContentRef,
    ProfileContentRef,
)
from jimu_runtime.domain.runtime.media import (
    AssetStage as AssetStage,
)
from jimu_runtime.domain.runtime.media import (
    CoordinateSystem,
    FinalizedAsset,
    MediaAsset,
    MediaRef,
    TransformRecord,
)
from jimu_runtime.domain.runtime.media import (
    StorageReference as StorageReference,
)

# Shares the static resource MIME syntax while enforcing it on this standalone field.
type EvidenceMimeType = Annotated[
    str,
    StringConstraints(
        strict=True,
        max_length=128,
        pattern=re.compile(
            r"^[A-Za-z0-9!#$%&'*+.^_`|~-]+/[A-Za-z0-9!#$%&'*+.^_`|~-]+$(?![\s\S])"
        ),
    ),
]


class ExecutionEvidenceScope(ContractModel):
    scope: Literal["execution"]
    execution_id: RecordId


class ObjectEvidenceScope(ContractModel):
    scope: Literal["object"]
    execution_id: RecordId
    object_ref: ObjectRef
    profile_ref: ProfileContentRef


type EvidenceScope = Annotated[
    ExecutionEvidenceScope | ObjectEvidenceScope, Field(discriminator="scope")
]


class ProjectEvidenceAccess(ContractModel):
    scope: Literal["project"]
    project_id: RecordId


class TaskEvidenceAccess(ContractModel):
    scope: Literal["task"]
    project_id: RecordId
    task_id: RecordId


type EvidenceAccessScope = Annotated[
    ProjectEvidenceAccess | TaskEvidenceAccess, Field(discriminator="scope")
]


class RetentionPolicyRef(ContractModel):
    evidence_policy_ref: PolicyContentRef
    retention_class: VocabularyMemberRef


class OriginalImageEvidence(ContractModel):
    kind: Literal["original_image"]
    original_media_ref: MediaRef
    coordinate_system: CoordinateSystem

    @model_validator(mode="after")
    def original_coordinate_frame(self) -> Self:
        if self.coordinate_system.frame != "original_image":
            raise ValueError(
                "original image evidence requires the original coordinate frame"
            )
        return self


class DerivedImageEvidence(ContractModel):
    kind: Literal["derived_image"]
    original_media_ref: MediaRef
    transforms: Annotated[
        tuple[TransformRecord, ...],
        Field(min_length=1, max_length=64),
        BeforeValidator(immutable_sequence),
    ]

    @model_validator(mode="after")
    def validate_chain(self) -> Self:
        if self.transforms[0].source.frame != "original_image":
            raise ValueError("transform chain requires an original-image source")
        frame_ids = {self.transforms[0].source.coordinate_system_id}
        for index, transform in enumerate(self.transforms):
            if index and self.transforms[index - 1].target != transform.source:
                raise ValueError(
                    "transform chain must join matching coordinate systems"
                )
            target_id = transform.target.coordinate_system_id
            if target_id in frame_ids:
                raise ValueError("transform chain cannot reuse a coordinate system ID")
            frame_ids.add(target_id)
        return self

    @property
    def coordinate_system(self) -> CoordinateSystem:
        return self.transforms[-1].target


class NonSpatialEvidence(ContractModel):
    kind: Literal["non_spatial"]


type EvidenceSpatialProvenance = Annotated[
    OriginalImageEvidence | DerivedImageEvidence | NonSpatialEvidence,
    Field(discriminator="kind"),
]

_DERIVED_EVIDENCE_TYPES = frozenset(
    (
        EvidenceType.CROP,
        EvidenceType.BBOX_VISUALIZATION,
        EvidenceType.MASK,
        EvidenceType.GEOMETRY,
    )
)


class EvidenceAsset(ContractModel):
    schema_version: Literal["1.0.0"]
    evidence_id: RecordId
    evidence_type: EvidenceType
    scope: EvidenceScope
    storage: StorageReference
    sha256: Digest
    mime_type: EvidenceMimeType
    byte_size: PositiveBudget
    stage: AssetStage
    spatial: EvidenceSpatialProvenance
    producer_id: ArtifactId
    producer_version: Version
    access_scope: EvidenceAccessScope
    retention: RetentionPolicyRef
    created_at: UTCDateTime

    @model_validator(mode="after")
    def validate_asset_provenance(self) -> Self:
        if self.created_at > self.stage.staged_at:
            raise ValueError("asset cannot be staged before it is created")
        if (
            isinstance(self.stage, FinalizedAsset)
            and self.stage.verified_sha256 != self.sha256
        ):
            raise ValueError("evidence digest must match the verified object digest")
        if self.evidence_type is EvidenceType.ORIGINAL_IMAGE:
            if not isinstance(self.spatial, OriginalImageEvidence):
                raise ValueError(
                    "original image evidence requires original media provenance"
                )
            if self.spatial.original_media_ref.sha256 != self.sha256:
                raise ValueError(
                    "original image evidence must preserve the original media digest"
                )
        elif self.evidence_type in _DERIVED_EVIDENCE_TYPES:
            if not isinstance(self.spatial, DerivedImageEvidence):
                raise ValueError("derived spatial evidence requires a transform chain")
        elif not isinstance(self.spatial, NonSpatialEvidence):
            raise ValueError(
                "non-spatial evidence type requires non-spatial provenance"
            )
        if self.evidence_type in {
            EvidenceType.ORIGINAL_IMAGE,
            EvidenceType.CROP,
            EvidenceType.BBOX_VISUALIZATION,
        } and not self.mime_type.startswith("image/"):
            raise ValueError("image evidence requires an image MIME type")
        return self

    def validate_original_media(self, asset: MediaAsset) -> None:
        """Relate spatial provenance to the recorded immutable RGB input."""
        if isinstance(self.spatial, NonSpatialEvidence):
            raise ValueError("original media provenance applies to spatial evidence")
        asset = MediaAsset.model_validate(asset)
        if not isinstance(asset.stage, FinalizedAsset) or (
            self.spatial.original_media_ref != asset.media_ref
        ):
            raise ValueError(
                "spatial evidence requires the exact finalized original media"
            )
        if isinstance(self.spatial, OriginalImageEvidence):
            source = self.spatial.coordinate_system
            if self.mime_type != asset.mime_type or self.byte_size != asset.byte_size:
                raise ValueError(
                    "original image evidence must preserve media MIME type and size"
                )
        else:
            source = self.spatial.transforms[0].source
        if source.dimensions != asset.dimensions:
            raise ValueError(
                "source coordinate frame must match original media dimensions"
            )
