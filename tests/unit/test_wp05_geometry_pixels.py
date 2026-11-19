"""Synthetic fixture semantics from source-pixel occupancy."""

import io

import pytest
from PIL import Image

from jimu_runtime.inspection.geometry._cpu import (
    WHEEL_COLOR,
    classify,
    load_image,
    measure,
    segment,
)


def mask(points):
    image = Image.new("L", (8, 8))
    for point in points:
        image.putpixel(point, 255)
    return image


@pytest.mark.parametrize(
    ("wheel", "tactile", "expected"),
    [
        ([(3, 3)], [(0, 0)], ("inside_parking_area", None)),
        ([(0, 0)], [(7, 7)], ("outside_parking_area", None)),
        ([(3, 3), (0, 0)], [(7, 7)], ("over_boundary", None)),
        ([(2, 2)], [(7, 7)], (None, "boundary_contact_unresolved")),
        ([(3, 3)], [(3, 3)], ("blocking_tactile_paving", None)),
        ([(0, 0)], [(0, 0)], (None, "overlapping_relations_unresolved")),
        ([(3, 3), (0, 0)], [(0, 0)], (None, "overlapping_relations_unresolved")),
        ([(3, 3)], [], (None, "empty_segmentation")),
        ([], [(7, 7)], (None, "empty_segmentation")),
    ],
)
def test_wheel_relation_uses_pixels_from_both_masks(wheel, tactile, expected):
    assert classify(measure(mask(wheel), mask(tactile), (2, 2, 6, 6))) == expected


def test_segmentation_reads_rgb_pixels_and_preserves_source_grid():
    source = Image.new("RGB", (8, 8))
    for point in [(1, 1), (2, 3), (4, 3)]:
        source.putpixel(point, WHEEL_COLOR)
    encoded, hits = segment(source, WHEEL_COLOR, (2.5, 1.0, 4.5, 5.0))
    image = load_image(encoded, (8, 8), mask=True)
    assert hits == 1
    assert image.tobytes() == mask([(2, 3)]).tobytes()


@pytest.mark.parametrize("variant", ["grayscale", "different-size", "truncated"])
def test_binary_mask_decoding_rejects_dirty_or_misaligned_content(variant):
    image = mask([(2, 2)])
    if variant == "grayscale":
        image.putpixel((1, 1), 12)
    output = io.BytesIO()
    image.save(output, format="PNG")
    content = output.getvalue()
    if variant == "truncated":
        content = content[:-18]
    dimensions = (8, 7) if variant == "different-size" else (8, 8)
    with pytest.raises(ValueError):
        load_image(content, dimensions, mask=True)
