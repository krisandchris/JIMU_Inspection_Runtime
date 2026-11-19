"""Deterministic connected components over one Profile's original-frame boxes."""

from jimu_runtime.domain.definitions.capabilities import GroundingGrouping
from jimu_runtime.domain.runtime.media import BBoxXYXY


def observation_groups(
    boxes: tuple[BBoxXYXY, ...], grouping: GroundingGrouping
) -> tuple[tuple[BBoxXYXY, ...], ...]:
    """Project already validated boxes under the pinned Profile grouping mode."""
    if grouping.mode == "per_box":
        return tuple((box,) for box in sorted(boxes, key=lambda box: box.xyxy))
    return group_boxes(boxes, grouping.max_gap_pixels)


def group_boxes(
    boxes: tuple[BBoxXYXY, ...], max_gap_pixels: int
) -> tuple[tuple[BBoxXYXY, ...], ...]:
    """Connect overlapping, touching, or nearby rectangles by single linkage.

    Distance is the shortest Euclidean distance between rectangles in original
    pixels, compared inclusively to the threshold. Member and component order are
    canonical, and repeated localizations remain visible as repeated members.
    """
    ordered = tuple(sorted(boxes, key=lambda box: box.xyxy))
    parent = list(range(len(ordered)))
    max_gap_squared = max_gap_pixels**2

    def root(index: int) -> int:
        while parent[index] != index:
            parent[index] = parent[parent[index]]
            index = parent[index]
        return index

    for index, left in enumerate(ordered):
        _, left_top, left_right, left_bottom = left.xyxy
        for other in range(index + 1, len(ordered)):
            right_left, right_top, _, right_bottom = ordered[other].xyxy
            gap_x = max(0, right_left - left_right)
            if gap_x > max_gap_pixels:
                break
            gap_y = max(0, right_top - left_bottom, left_top - right_bottom)
            if gap_x**2 + gap_y**2 <= max_gap_squared:
                left_root, right_root = root(index), root(other)
                if left_root != right_root:
                    earlier, later = sorted((left_root, right_root))
                    parent[later] = earlier

    groups: dict[int, list[BBoxXYXY]] = {}
    for index, box in enumerate(ordered):
        groups.setdefault(root(index), []).append(box)
    return tuple(tuple(members) for members in groups.values())
