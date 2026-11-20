"""Closed evidence contract for the member-aware parking Tool."""

from typing import Annotated, Literal, Self

from pydantic import Field, model_validator

from jimu_runtime.bundle.trust import TrustPin
from jimu_runtime.domain.base import ContractModel
from jimu_runtime.domain.primitives import Digest, RecordId
from jimu_runtime.domain.runtime.facts import ObjectRef
from jimu_runtime.domain.runtime.media import BBoxXYXY, PixelCoordinate
from jimu_runtime.domain.runtime.tool_executions import ToolArtifactRefs
from jimu_runtime.domain.tool_mappings import ProfileContentRef
from jimu_runtime.inspection.geometry.parking_members import (
    ParkingState,
    WheelContact,
    match_group,
    match_member,
    summarize_contacts,
)
from jimu_runtime.inspection.geometry.region import ParkingArea

GROUP_CAPABILITY = "live.parking.member_geometry"
GROUP_OUTPUT = "parking_group_match"
GROUP_REPORT = "parking_member_geometry"
type Crop = tuple[int, int, int, int]
type Extent = tuple[PixelCoordinate, PixelCoordinate, PixelCoordinate, PixelCoordinate]


class SegmentationEvidence(ContractModel):
    status: Literal[
        "observed",
        "segmentation_unavailable",
        "segmentation_budget_exhausted",
        "segmentation_reply_budget_exhausted",
        "member_crop_unavailable",
    ]
    request_sha256: Digest | None = None
    response_sha256: Digest | None = None
    model: Literal["WeVisOne-VL-4B-NF4"] | None = None
    model_forward_calls: Annotated[int, Field(gt=0)] | None = None
    mask_count: Annotated[int, Field(ge=0, le=16)] | None = None

    @model_validator(mode="after")
    def complete_observed(self) -> Self:
        if self.status == "observed" and any(
            field is None
            for field in (
                self.request_sha256,
                self.response_sha256,
                self.model,
                self.model_forward_calls,
                self.mask_count,
            )
        ):
            raise ValueError("Observed segmentation is missing its upstream receipt")
        return self


class ContactEvidence(ContractModel):
    source_mask_index: Annotated[int, Field(ge=1, le=16)]
    contact_extent: Extent | None
    wheel_extent: Extent | None
    shared_candidate_pixels: Annotated[int, Field(ge=0)]
    outside: bool | None
    tactile_overlap: bool | None

    @model_validator(mode="after")
    def contact_present_for_known_relation(self) -> Self:
        if self.contact_extent is None:
            if self.outside is not None or self.tactile_overlap is not None:
                raise ValueError("An unattributed wheel cannot establish a relation")
        else:
            left, top, right, bottom = self.contact_extent
            if (
                left > right
                or top != bottom
                or self.wheel_extent is None
                or not (
                    self.wheel_extent[0] < left <= right < self.wheel_extent[2]
                    and self.wheel_extent[1] < top < self.wheel_extent[3]
                )
            ):
                raise ValueError("Contact support must use a row within the wheel mask")
        return self


class MemberGeometryEvidence(ContractModel):
    contacts: Annotated[tuple[ContactEvidence, ...], Field(max_length=16)]
    outside: bool | None
    tactile_overlap: bool | None


class MemberEvidence(ContractModel):
    member_index: Annotated[int, Field(ge=1, le=1024)]
    bbox: Extent
    crop_xyxy: Crop | None
    parking_state: ParkingState
    visual_evidence: Annotated[str, Field(min_length=1, max_length=160)] | None
    geometry: MemberGeometryEvidence
    matches: bool | None
    segmentation: SegmentationEvidence

    @model_validator(mode="after")
    def same_member_match(self) -> Self:
        geometry = summarize_contacts(
            tuple(
                WheelContact(
                    contact.source_mask_index,
                    contact.contact_extent,
                    contact.wheel_extent,
                    contact.shared_candidate_pixels,
                    contact.outside,
                    contact.tactile_overlap,
                )
                for contact in self.geometry.contacts
            )
        )
        if (
            self.geometry.outside != geometry.outside
            or self.geometry.tactile_overlap != geometry.tactile_overlap
            or self.matches != match_member(self.parking_state, geometry)
            or (self.parking_state != "unknown" and not self.visual_evidence)
            or (
                self.segmentation.status == "observed"
                and (
                    self.crop_xyxy is None
                    or self.segmentation.mask_count != len(self.geometry.contacts)
                )
            )
            or (self.segmentation.status != "observed" and self.geometry.contacts)
            or any(
                contact.source_mask_index != index
                for index, contact in enumerate(self.geometry.contacts, start=1)
            )
        ):
            raise ValueError("Grouped evidence must retain the exact same-member join")
        return self


class ParkingMemberGeometryEvidence(ContractModel):
    schema_version: Literal["1.0.0"]
    kind: Literal["parking_member_geometry"]
    tool_execution_id: RecordId
    execution_id: RecordId
    attempt_id: RecordId
    object_ref: ObjectRef
    profile_ref: ProfileContentRef
    implementation: TrustPin
    input_refs: ToolArtifactRefs
    original_sha256: Digest
    request_sha256: Digest
    response_sha256: Digest
    parking_area: ParkingArea
    parking_inner: Extent
    tactile_segmentation: SegmentationEvidence
    members: Annotated[tuple[MemberEvidence, ...], Field(min_length=1, max_length=1024)]
    matches: bool | None

    @model_validator(mode="after")
    def validate_coverage(self) -> Self:
        width = self.parking_area.coordinate_system.dimensions.width
        height = self.parking_area.coordinate_system.dimensions.height
        x0, y0, x1, y1 = self.parking_area.bbox.xyxy
        left, top, right, bottom = self.parking_inner
        if (
            not (x0 <= left < right <= x1 and y0 <= top < bottom <= y1)
            or self.matches
            != match_group(tuple(member.matches for member in self.members))
            or (
                self.tactile_segmentation.status == "observed"
                and self.tactile_segmentation.request_sha256 != self.original_sha256
            )
        ):
            raise ValueError("Grouped evidence lost ROI, image or coverage consistency")
        for index, member in enumerate(self.members, start=1):
            if member.member_index != index:
                raise ValueError("Grouped evidence must retain all member positions")
            bbox = BBoxXYXY(xyxy=member.bbox)
            bbox.validate_bounds(self.parking_area.coordinate_system.dimensions)
            if member.crop_xyxy is not None:
                left, top, right, bottom = member.crop_xyxy
                if not (0 <= left < right <= width and 0 <= top < bottom <= height):
                    raise ValueError("Grouped evidence contains an invalid member crop")
                for contact in member.geometry.contacts:
                    if contact.wheel_extent is not None:
                        wx0, wy0, wx1, wy1 = contact.wheel_extent
                        if not (
                            left <= wx0 < wx1 <= right and top <= wy0 < wy1 <= bottom
                        ):
                            raise ValueError(
                                "Wheel mask cannot lie outside the member crop"
                            )
                    if contact.contact_extent is None:
                        continue
                    x_min, row, x_max, _ = contact.contact_extent
                    if not (left < x_min <= x_max < right and top < row < bottom):
                        raise ValueError(
                            "Ground contact cannot lie outside the member crop"
                        )
        return self
