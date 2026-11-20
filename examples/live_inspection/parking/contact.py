"""Wheel-to-ground contact candidates in original image coordinates."""

from collections.abc import Sequence

from PIL import Image, ImageDraw

from jimu_runtime.inspection.geometry._cpu import Box
from jimu_runtime.inspection.geometry.parking_members import (
    MemberGeometry,
    WheelContact,
    summarize_contacts,
)


def _contact(
    mask: Image.Image,
    source_mask_index: int,
    tactile: Image.Image | None,
    outer: Box,
    inner: Box,
    other_boxes: Sequence[tuple[int, int, int, int]],
) -> WheelContact:
    mask_extent = mask.getbbox()
    if mask_extent is None:
        return WheelContact(source_mask_index, None, None, 0, None, None)
    left, top, right, bottom = mask_extent
    wheel_extent: Box = (float(left), float(top), float(right), float(bottom))
    row_y = bottom - 1
    candidates = mask.crop((left, row_y, right, bottom))
    exclusive = candidates.copy()
    draw = ImageDraw.Draw(exclusive)
    for other_left, top, other_right, other_bottom in other_boxes:
        if top <= row_y < other_bottom:
            draw.rectangle((other_left - left, 0, other_right - left - 1, 0), fill=0)
    shared = candidates.histogram()[255] - exclusive.histogram()[255]
    if exclusive.getbbox() is None:
        return WheelContact(source_mask_index, None, wheel_extent, shared, None, None)
    points = tuple(
        (left + index + 0.5, row_y + 0.5)
        for index, raw in enumerate(exclusive.tobytes())
        if raw == 255
    )
    contact_extent = (points[0][0], points[0][1], points[-1][0], points[-1][1])

    def in_box(point: tuple[float, float], box: Box) -> bool:
        return box[0] < point[0] < box[2] and box[1] < point[1] < box[3]

    def beyond_box(point: tuple[float, float], box: Box) -> bool:
        return (
            point[0] < box[0]
            or point[0] > box[2]
            or point[1] < box[1]
            or point[1] > box[3]
        )

    all_points = tuple(
        (left + index + 0.5, row_y + 0.5)
        for index, raw in enumerate(candidates.tobytes())
        if raw == 255
    )
    outside: bool | None = None
    if all(beyond_box(point, outer) for point in all_points):
        outside = True
    elif not shared and all(in_box(point, inner) for point in points):
        outside = False

    overlap: bool | None = None
    if tactile is not None and tactile.getbbox() is not None:
        hits = tuple(tactile.getpixel((int(x), int(y))) == 255 for x, y in points)
        if all(hits):
            overlap = True
        elif not any(hits) and not shared:
            overlap = False
    return WheelContact(
        source_mask_index, contact_extent, wheel_extent, shared, outside, overlap
    )


def member_geometry(
    wheels: Sequence[Image.Image],
    tactile: Image.Image | None,
    parking_outer: Box,
    parking_inner: Box,
    other_pixel_boxes: Sequence[tuple[int, int, int, int]],
) -> MemberGeometry:
    contacts = tuple(
        _contact(wheel, index, tactile, parking_outer, parking_inner, other_pixel_boxes)
        for index, wheel in enumerate(wheels, start=1)
    )
    return summarize_contacts(contacts)
