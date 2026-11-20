"""P1 grouping preserves canonical boxes in the pinned original coordinates."""

import json
from itertools import permutations

import pytest
from pydantic import TypeAdapter, ValidationError

from jimu_runtime.domain.definitions.capabilities import GroundingGrouping
from jimu_runtime.domain.nodes.grounding import GroundingInput
from jimu_runtime.domain.runtime.grounding_groups import group_boxes, observation_groups
from jimu_runtime.domain.runtime.media import BBoxXYXY
from jimu_runtime.domain.runtime.perception import ObjectRecord
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
    assert len(set(identities)) == 4
    for sequence in permutations(boxes):
        assert identities == tuple(
            grounding._grouped_identities(
                inputs.envelope, profile_ref, observation_groups(sequence, grouping)
            )
        )


def test_grouped_projection_validates_members_order_bounds_and_identity() -> None:
    wire = cases()  # type: ignore[no-untyped-call]
    envelope = GroundingInput.model_validate_json(
        json.dumps(wire["grounding_input"])
    ).envelope
    ungrouped = ObjectRecord.model_validate_json(json.dumps(wire["object_record"]))
    assert "grounding_member_boxes" not in ungrouped.model_dump()
    assert "marked_frame" not in ungrouped.model_dump()
    first = (_box(1, 1, 4, 4), _box(3, 3, 6, 6))
    second = (_box(10, 10, 12, 12),)
    groups = (first, second)

    def project(members: tuple[BBoxXYXY, ...]) -> ObjectRecord:
        (object_id,) = tuple(
            grounding._grouped_identities(envelope, ungrouped.profile_ref, (members,))
        )
        return ObjectRecord.model_validate(
            ungrouped.model_dump()
            | {
                "object_id": object_id,
                "bbox": BBoxXYXY.enclosing(members),
                "grounding_member_boxes": members,
            }
        )

    actual = tuple(project(members) for members in groups)
    assert all("marked_frame" not in obj.model_dump() for obj in actual)
    grounding._verify_grouped_objects(envelope, ungrouped.profile_ref, actual, groups)

    # A merged box alone does not recover the provider's member boxes.
    wrong_members = project((_box(1, 1, 6, 6),)).model_copy(
        update={"object_id": actual[0].object_id}
    )
    assert wrong_members.bbox == actual[0].bbox
    wrong_id = actual[1].model_copy(update={"object_id": ungrouped.object_id})
    for wrong in (actual[::-1], (wrong_members, actual[1]), (actual[0], wrong_id)):
        with pytest.raises(
            AdapterRejected, match="perception.grouped_grounding_members_mismatch"
        ):
            grounding._verify_grouped_objects(
                envelope, ungrouped.profile_ref, wrong, groups
            )

    with pytest.raises(ValidationError, match="requires canonical members"):
        ObjectRecord.model_validate(
            actual[0].model_dump() | {"grounding_member_boxes": first[::-1]}
        )
    with pytest.raises(ValidationError, match="requires canonical members"):
        ObjectRecord.model_validate(actual[0].model_dump() | {"bbox": first[0]})
    dimensions = actual[0].coordinate_system.dimensions
    with pytest.raises(ValidationError, match="box exceeds image dimensions"):
        project((_box(1, 1, dimensions.width + 1, dimensions.height),))


def test_historical_grouped_object_preserves_marked_frame_on_round_trip() -> None:
    wire = cases()  # type: ignore[no-untyped-call]
    ungrouped = ObjectRecord.model_validate_json(json.dumps(wire["object_record"]))
    historical = ungrouped.model_dump(mode="json") | {
        "grounding_member_boxes": [ungrouped.bbox.model_dump(mode="json")],
        "marked_frame": {
            "media_ref": {"media_id": "legacy-marked-frame", "sha256": "f" * 64},
            "byte_size": 250,
            "mime_type": "image/png",
            "renderer_version": "1.0.0",
        },
    }
    obj = ObjectRecord.model_validate_json(json.dumps(historical))
    assert obj.model_dump(mode="json") == historical
