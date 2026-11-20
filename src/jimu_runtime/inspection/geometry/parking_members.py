"""Deterministic, three-valued parking geometry for one grounded group."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from itertools import combinations
from typing import Literal

from jimu_runtime.inspection.geometry._cpu import Box

type ParkingState = Literal["parked", "unparked", "unknown"]


@dataclass(frozen=True, slots=True)
class MemberGeometry:
    contacts: tuple[WheelContact, ...]
    outside: bool | None
    tactile_overlap: bool | None

    @property
    def matches(self) -> bool | None:
        if self.outside is True or self.tactile_overlap is True:
            return True
        if self.outside is False and self.tactile_overlap is False:
            return False
        return None


def match_member(state: ParkingState, geometry: MemberGeometry) -> bool | None:
    spatial = geometry.matches
    if state == "unparked" or spatial is False:
        return False
    if state == "parked" and spatial is True:
        return True
    return None


def match_group(matches: Sequence[bool | None]) -> bool | None:
    if True in matches:
        return True
    if matches and all(match is False for match in matches):
        return False
    return None


@dataclass(frozen=True, slots=True)
class WheelContact:
    # The bottom foreground row is a wheel-to-ground proxy in this camera view.
    # Coordinates use original-image pixel centers.
    source_mask_index: int
    contact_extent: Box | None
    wheel_extent: Box | None
    shared_candidate_pixels: int
    outside: bool | None
    tactile_overlap: bool | None


def summarize_contacts(contacts: tuple[WheelContact, ...]) -> MemberGeometry:
    supports = [
        contact.wheel_extent
        for contact in contacts
        if contact.contact_extent is not None and contact.wheel_extent is not None
    ]
    independent_wheels = any(
        first[2] <= second[0]
        or second[2] <= first[0]
        or first[3] <= second[1]
        or second[3] <= first[1]
        for first, second in combinations(supports, 2)
    )
    outside: bool | None = None
    if any(contact.outside is False for contact in contacts):
        outside = False
    elif independent_wheels and all(
        contact.outside is True
        for contact in contacts
        if contact.contact_extent is not None
    ):
        # Completely shared contact supports cannot be attributed to this
        # member. Two distinct attributable supports establish its two wheels.
        outside = True
    if any(contact.tactile_overlap is True for contact in contacts):
        overlap = True
    elif independent_wheels and all(
        contact.tactile_overlap is False for contact in contacts
    ):
        overlap = False
    else:
        overlap = None
    return MemberGeometry(contacts, outside, overlap)


def associate_observations(
    observations: Sequence[Mapping[str, object]], member_count: int
) -> tuple[tuple[ParkingState, str | None], ...]:
    """Keep missing and conflicting member answers unknown."""
    by_index: dict[int, tuple[ParkingState, str | None]] = {}
    for observation in observations:
        index = observation.get("member_index")
        if type(index) is not int or not 1 <= index <= member_count:
            continue
        state = observation.get("state")
        evidence = observation.get("visual_evidence")
        if (
            index in by_index
            or state not in ("parked", "unparked")
            or not isinstance(evidence, str)
            or not evidence.strip()
        ):
            by_index[index] = ("unknown", None)
        elif state == "parked":
            by_index[index] = ("parked", evidence)
        else:
            by_index[index] = ("unparked", evidence)
    return tuple(
        by_index.get(index, ("unknown", None)) for index in range(1, member_count + 1)
    )
