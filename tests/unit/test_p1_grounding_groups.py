"""P1 grouping and full-frame marking use only pinned original coordinates."""

import asyncio
import json
from hashlib import sha256
from io import BytesIO
from itertools import permutations

import pytest
from PIL import Image
from pydantic import TypeAdapter, ValidationError

from jimu_runtime.domain.definitions.capabilities import GroundingGrouping
from jimu_runtime.domain.nodes.grounding import GroundingInput
from jimu_runtime.domain.runtime.grounding_groups import group_boxes, observation_groups
from jimu_runtime.domain.runtime.media import BBoxXYXY, ImageDimensions
from jimu_runtime.domain.runtime.perception import ObjectRecord
from jimu_runtime.gateway.http_wire import VerifiedImage
from jimu_runtime.inspection.media.group_frame import marked_group_image
from jimu_runtime.inspection.media.types import MediaError
from jimu_runtime.inspection.perception import grounding
from jimu_runtime.workflow.worker import AdapterRejected
from tests.fixtures.wp01c.nodes.cases import cases


def _box(x1: float, y1: float, x2: float, y2: float) -> BBoxXYXY:
    return BBoxXYXY(xyxy=(x1, y1, x2, y2))


def test_grouping_is_order_independent_and_uses_transitive_euclidean_gap() -> None:
    a, b, c, d = (
        _box(1, 1, 4, 5),
        _box(4, 5, 7, 9),
        _box(10, 13, 16, 16),
        _box(23, 17, 24, 18),
    )
    for order in permutations((a, b, c, d)):
        assert group_boxes(order, 5) == ((a, b, c), (d,))
    assert group_boxes((d, c, b, a), 4) == ((a, b), (c,), (d,))
    assert group_boxes((d, c, a, b), 0) == ((a, b), (c,), (d,))
    assert group_boxes((a, a, b), 0) == ((a, a, b),)
    assert group_boxes((), 0) == ()


@pytest.mark.parametrize("invalid", [-1, 8193, "5", 5.0, True, None])
def test_grouping_threshold_is_a_bounded_nonnegative_pixel_integer(
    invalid: object,
) -> None:
    with pytest.raises(ValidationError):
        TypeAdapter(GroundingGrouping).validate_python(
            {"mode": "connected_boxes", "max_gap_pixels": invalid}
        )


def test_per_box_keeps_intersections_and_duplicate_detections_separate() -> None:
    validator = TypeAdapter[GroundingGrouping](GroundingGrouping)
    grouping = validator.validate_python({"mode": "per_box"})
    with pytest.raises(ValidationError):
        validator.validate_python({"mode": "per_box", "max_gap_pixels": 0})

    same = _box(1, 1, 4, 4)
    touching = _box(4, 1, 6, 4)
    overlapping = _box(3, 1, 5, 4)
    boxes = (touching, same, overlapping, same)
    groups = observation_groups(boxes, grouping)
    assert groups == ((same,), (same,), (overlapping,), (touching,))
    assert observation_groups((same,), grouping) == ((same,),)
    assert observation_groups((), grouping) == ()
    wire = cases()  # type: ignore[no-untyped-call]
    inputs = GroundingInput.model_validate_json(json.dumps(wire["grounding_input"]))
    profile_ref = inputs.profiles[0].hit.profile_ref
    identities = tuple(
        grounding._grouped_identities(inputs.envelope, profile_ref, groups)
    )
    assert len({object_id for object_id, _ in identities}) == 4
    assert len({media_id for _, media_id in identities}) == 4
    for sequence in permutations(boxes):
        assert identities == tuple(
            grounding._grouped_identities(
                inputs.envelope, profile_ref, observation_groups(sequence, grouping)
            )
        )


def _source() -> VerifiedImage:
    with Image.new("RGB", (12, 10), (20, 30, 40)) as original, BytesIO() as output:
        original.save(output, format="PNG")
        raw = output.getvalue()
    return VerifiedImage(raw, "image/png", sha256(raw).hexdigest())


def test_marked_frame_covers_original_grid_and_has_exactly_one_red_outline() -> None:
    source = _source()
    dimensions = ImageDimensions(width=12, height=10)
    group = (_box(0.5, 1, 3, 5), _box(2, 4, 8.7, 10))
    target = BBoxXYXY.enclosing(group)
    result = asyncio.run(marked_group_image(source, dimensions, target))
    assert result == asyncio.run(marked_group_image(source, dimensions, target))
    assert result.media_type == "image/png"
    assert result.sha256 == sha256(result.content).hexdigest() != source.sha256
    with Image.open(BytesIO(result.content)) as marked:
        assert marked.mode == "RGB"
        assert marked.size == (12, 10)
        for x in range(12):
            for y in range(10):
                red_outline = 0 <= x <= 8 and y in (1, 9) or x in (0, 8) and 1 <= y <= 9
                assert marked.getpixel((x, y)) == (
                    (255, 0, 0) if red_outline else (20, 30, 40)
                )


def test_one_pixel_group_marks_only_its_own_pixel() -> None:
    result = asyncio.run(
        marked_group_image(
            _source(), ImageDimensions(width=12, height=10), _box(1.1, 1.2, 1.3, 1.4)
        )
    )
    with Image.open(BytesIO(result.content)) as marked:
        assert marked.getpixel((1, 1)) == (255, 0, 0)
        assert marked.getpixel((1, 2)) == (20, 30, 40)
        assert marked.getpixel((2, 1)) == (20, 30, 40)


def test_renderer_rejects_wrong_original_digest() -> None:
    source = _source()
    corrupt = VerifiedImage(source.content, source.media_type, "0" * 64)
    with pytest.raises(MediaError, match="marked_frame_original_digest_mismatch"):
        asyncio.run(
            marked_group_image(
                corrupt, ImageDimensions(width=12, height=10), _box(0, 0, 2, 2)
            )
        )


def test_marked_frame_budget_is_cumulative(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(grounding, "_MAX_MARKED_FRAME_PIXELS", 200)
    budget = grounding._MarkedFrameBudget()
    dimensions = ImageDimensions(width=10, height=10)
    budget.reserve_pixels(dimensions, 1)
    budget.reserve_pixels(dimensions, 1)
    with pytest.raises(
        AdapterRejected, match="perception.marked_frame_pixel_budget_exceeded"
    ):
        budget.reserve_pixels(dimensions, 1)

    source = _source()
    monkeypatch.setattr(grounding, "_MAX_MARKED_FRAME_BYTES", len(source.content) * 2)
    budget.reserve_bytes(source)
    budget.reserve_bytes(source)
    with pytest.raises(
        AdapterRejected, match="perception.marked_frame_byte_budget_exceeded"
    ):
        budget.reserve_bytes(source)


def test_grouped_projection_rejects_changed_members_order_and_media_id() -> None:
    wire = cases()  # type: ignore[no-untyped-call]
    envelope = GroundingInput.model_validate_json(
        json.dumps(wire["grounding_input"])
    ).envelope
    legacy = ObjectRecord.model_validate_json(json.dumps(wire["object_record"]))
    assert "grounding_member_boxes" not in legacy.model_dump()
    assert "marked_frame" not in legacy.model_dump()
    first = (_box(1, 1, 4, 4), _box(3, 3, 6, 6))
    second = (_box(10, 10, 12, 12),)
    groups = (first, second)

    def project(members: tuple[BBoxXYXY, ...]) -> ObjectRecord:
        ((object_id, media_id),) = tuple(
            grounding._grouped_identities(envelope, legacy.profile_ref, (members,))
        )
        return ObjectRecord.model_validate(
            legacy.model_dump()
            | {
                "object_id": object_id,
                "bbox": BBoxXYXY.enclosing(members),
                "grounding_member_boxes": members,
                "marked_frame": {
                    "media_ref": {"media_id": media_id, "sha256": "f" * 64},
                    "byte_size": 250,
                    "mime_type": "image/png",
                    "renderer_version": "1.0.0",
                },
            }
        )

    actual = tuple(project(members) for members in groups)
    grounding._verify_grouped_objects(envelope, legacy.profile_ref, actual, groups)
    wrong_members = project((_box(1, 1, 6, 6),))
    assert wrong_members.bbox == actual[0].bbox
    wrong_mark = actual[1].model_copy(update={"marked_frame": actual[0].marked_frame})
    for wrong in (actual[::-1], (wrong_members, actual[1]), (actual[0], wrong_mark)):
        with pytest.raises(
            AdapterRejected, match="perception.grouped_grounding_members_mismatch"
        ):
            grounding._verify_grouped_objects(
                envelope, legacy.profile_ref, wrong, groups
            )
