"""Regression coverage for same-member parking evidence and unknown outcomes."""

from base64 import b64encode
from hashlib import sha256
from types import SimpleNamespace

import pytest
from PIL import Image

from examples.live_inspection.parking import tool
from examples.live_inspection.parking.contact import member_geometry
from examples.live_inspection.parking.scene import CAPABILITY, TOOL, VERSION
from examples.live_inspection.parking.wevis_masks import PARKING_OUTER
from jimu_runtime.domain.runtime.media import (
    BBoxXYXY,
    CoordinateSystem,
    ImageDimensions,
    MediaRef,
)
from jimu_runtime.inspection.geometry.parking_members import (
    MemberGeometry,
    associate_observations,
    match_group,
    match_member,
)


def test_parking_tool_validates_original_bytes_and_ordered_n03_boxes(monkeypatch):
    image = b"verified RGB original bytes"
    digest = sha256(image).hexdigest()
    monkeypatch.setattr(tool, "APPROVED_IMAGE_SHA256", digest)
    original = MediaRef(media_id="original", sha256=digest)
    coordinates = CoordinateSystem(
        coordinate_system_id=original.media_id,
        frame="original_image",
        dimensions=ImageDimensions(width=1536, height=1024),
    )
    members = (
        BBoxXYXY(xyxy=(16.0, 64.0, 128.0, 256.0)),
        BBoxXYXY(xyxy=(256.0, 128.0, 384.0, 256.0)),
    )
    area = tool.ParkingArea(
        region_id="parking",
        region_version=VERSION,
        region_sha256=digest,
        geometry_resource_id="parking-geometry",
        geometry_version=VERSION,
        geometry_sha256=digest,
        source_asset_sha256=digest,
        coordinate_system=coordinates,
        units="pixel",
        bbox=BBoxXYXY(xyxy=tuple(float(value) for value in PARKING_OUTER)),
    )
    dispatch = SimpleNamespace(
        tool_ref=SimpleNamespace(artifact_id=TOOL),
        capability_id=CAPABILITY,
        contract_version=VERSION,
        implementation_version=VERSION,
        invoke_model=None,
        object_ref=SimpleNamespace(record_id="parking-object"),
        original_media_ref=original,
        original_coordinates=coordinates,
        object_bbox=BBoxXYXY.enclosing(members),
        object_member_boxes=members,
        request=tool._wire(
            {
                "image": original.model_dump(mode="json"),
                "parking_area": area.model_dump(mode="json"),
            }
        ),
    )
    physical = {
        "dimensions": [1536, 1024],
        "image": b64encode(image).decode("ascii"),
        "member_boxes": [box.xyxy for box in members],
    }
    tool.validate_input(dispatch, tool._wire(physical))
    with pytest.raises(ValueError, match="authoritative ordered N03 boxes"):
        tool.validate_input(
            dispatch,
            tool._wire(
                {**physical, "member_boxes": list(reversed(physical["member_boxes"]))}
            ),
        )
    with pytest.raises(ValueError, match="changed original image"):
        tool.validate_input(
            dispatch,
            tool._wire(
                {**physical, "image": b64encode(b"marked bytes").decode("ascii")}
            ),
        )
    with pytest.raises(ValueError, match="changed original image"):
        tool.validate_input(
            dispatch, tool._wire({**physical, "marked_frame": "overlay"})
        )


@pytest.mark.parametrize(
    ("rows", "expected"),
    [
        ([("parked", True), ("unknown", None)], True),
        ([("parked", False), ("unparked", True)], False),
        ([("parked", False), ("unknown", True)], None),
        ([("parked", None), ("unparked", True)], None),
        ([("unparked", None), ("unknown", False)], False),
        ([], None),
    ],
)
def test_group_existential_requires_conditions_on_the_same_member(rows, expected):
    assert (
        match_group(
            [
                match_member(state, MemberGeometry((), spatial, spatial))
                for state, spatial in rows
            ]
        )
        is expected
    )


def test_observation_association_uses_grounding_ordinal_and_conserves_missing_members():
    rows = [
        {"member_index": 3, "state": "parked", "visual_evidence": "Blue bike on stand"},
        {"member_index": 1, "state": "unparked", "visual_evidence": "Rider seated"},
    ]
    assert associate_observations(rows, 4) == (
        ("unparked", "Rider seated"),
        ("unknown", None),
        ("parked", "Blue bike on stand"),
        ("unknown", None),
    )
    duplicate = {
        "member_index": 3,
        "state": "unparked",
        "visual_evidence": "Rider seated",
    }
    assert associate_observations([*rows, duplicate], 4)[2] == ("unknown", None)
    assert associate_observations(rows, 2) == (
        ("unparked", "Rider seated"),
        ("unknown", None),
    )


def _mask(*points):
    data = Image.new("L", (12, 12))
    for point in points:
        data.putpixel(point, 255)
    return data


def test_separate_wheel_contact_anchors_support_outside_even_with_tactile_overlap():
    front, back = _mask((9, 5), (9, 6)), _mask((10, 7), (10, 8))
    tactile = _mask((9, 6), (9, 7))
    result = member_geometry([front, back], tactile, (1, 1, 7, 10), (2, 2, 6, 9), ())
    assert result.outside is True
    assert result.tactile_overlap is True
    assert result.contacts[0].contact_extent == (9.5, 6.5, 9.5, 6.5)
    assert match_member("parked", result) is True


def test_shared_and_duplicate_wheels_cannot_form_a_positive_witness():
    front, back = _mask((9, 6)), _mask((10, 8))
    tactile = _mask((9, 6), (10, 8))
    result = member_geometry(
        [front, back], tactile, (1, 1, 7, 10), (2, 2, 6, 9), [(8, 4, 12, 10)]
    )
    assert all(contact.shared_candidate_pixels == 1 for contact in result.contacts)
    assert match_member("parked", result) is None
    duplicated = member_geometry([front, front], None, (1, 1, 7, 10), (2, 2, 6, 9), ())
    assert duplicated.outside is None


def test_interior_and_tactile_missing_are_different_from_confirmed_nonmatch():
    front, back = _mask((3, 5)), _mask((5, 7))
    tactile = _mask((10, 10))
    outer, inner = (1, 1, 8, 10), (2, 2, 7, 9)
    assert (
        match_member(
            "parked", member_geometry([front, back], tactile, outer, inner, ())
        )
        is False
    )
    assert (
        match_member("parked", member_geometry([front, back], None, outer, inner, ()))
        is None
    )
    touching = member_geometry(
        [_mask((7, 4)), _mask((7, 6))], tactile, outer, inner, ()
    )
    assert match_member("parked", touching) is None


def test_contact_across_boundary_requires_a_separate_tactile_match():
    front, back = _mask((3, 5)), _mask((10, 7))
    geometry = member_geometry(
        [front, back], _mask((10, 10)), (1, 1, 8, 10), (2, 2, 7, 9), ()
    )
    assert geometry.outside is False
    assert geometry.tactile_overlap is False
    assert match_member("parked", geometry) is False


def test_parking_sidecar_rejects_group_match_without_same_member_wheel_evidence():
    from pydantic import ValidationError

    from jimu_runtime.inspection.geometry.parking_members_evidence import MemberEvidence

    evidence = {
        "member_index": 1,
        "bbox": (1.0, 1.0, 3.0, 3.0),
        "crop_xyxy": (1, 1, 3, 3),
        "parking_state": "parked",
        "visual_evidence": "A kickstand supports the bike",
        "geometry": {"contacts": (), "outside": None, "tactile_overlap": None},
        "matches": None,
        "segmentation": {"status": "segmentation_unavailable"},
    }
    MemberEvidence.model_validate(evidence)
    evidence["geometry"]["outside"] = True
    evidence["matches"] = True
    with pytest.raises(ValidationError, match="exact same-member join"):
        MemberEvidence.model_validate(evidence)


def test_overlapping_near_duplicate_wheel_masks_are_not_two_wheels():
    one_wheel = _mask((8, 5), (8, 6), (9, 6))
    near_duplicate = _mask((8, 5), (8, 6))
    result = member_geometry(
        [one_wheel, near_duplicate], None, (1, 1, 7, 10), (2, 2, 6, 9), ()
    )
    assert result.contacts[0].contact_extent != result.contacts[1].contact_extent
    assert result.outside is None
    assert match_member("parked", result) is None


def test_extra_or_incomplete_observation_cannot_erase_an_independent_match():
    rows = [
        {"member_index": 1, "state": "parked", "visual_evidence": "On a kickstand"},
        {"member_index": 2, "state": "parked"},
        {"member_index": 3, "state": "parked", "visual_evidence": "On a kickstand"},
        {"state": "unparked"},
    ]
    assert associate_observations(rows, 2) == (
        ("parked", "On a kickstand"),
        ("unknown", None),
    )


def test_physical_request_rejects_reordered_grounding_members(monkeypatch):
    from hashlib import sha256
    from types import SimpleNamespace

    from examples.live_inspection.parking import tool
    from jimu_runtime.domain.runtime.media import BBoxXYXY, CoordinateSystem, MediaRef
    from jimu_runtime.inspection.geometry import ParkingArea, _content, _wire

    source = b"fixture-source"
    digest = sha256(source).hexdigest()
    monkeypatch.setattr(tool, "APPROVED_IMAGE_SHA256", digest)
    coordinates = CoordinateSystem.model_validate_json(
        b'{"coordinate_system_id":"test","frame":"original_image",'
        b'"dimensions":{"width":1536,"height":1024}}'
    )
    area = ParkingArea(
        region_id="test",
        region_version=tool.VERSION,
        region_sha256=digest,
        geometry_resource_id="test",
        geometry_version=tool.VERSION,
        geometry_sha256=digest,
        source_asset_sha256=digest,
        coordinate_system=coordinates,
        units="pixel",
        bbox=BBoxXYXY(xyxy=tool.PARKING_OUTER),
    )
    original = MediaRef(media_id="test", sha256=digest)
    member_a = BBoxXYXY(xyxy=(2, 2, 5, 5))
    member_b = BBoxXYXY(xyxy=(4, 4, 8, 8))
    dispatch = SimpleNamespace(
        tool_ref=SimpleNamespace(artifact_id=tool.TOOL),
        capability_id=tool.CAPABILITY,
        contract_version=tool.VERSION,
        implementation_version=tool.VERSION,
        object_ref=object(),
        invoke_model=None,
        request=_wire(
            {
                "image": original.model_dump(mode="json"),
                "parking_area": area.model_dump(mode="json"),
            }
        ),
        original_media_ref=original,
        original_coordinates=coordinates,
        object_bbox=BBoxXYXY.enclosing((member_a, member_b)),
        object_member_boxes=(member_a, member_b),
    )
    inputs = {"image": _content(source), "dimensions": (1536, 1024)}
    tool.validate_input(
        dispatch, _wire({**inputs, "member_boxes": (member_a.xyxy, member_b.xyxy)})
    )
    with pytest.raises(ValueError, match="authoritative ordered N03 boxes"):
        tool.validate_input(
            dispatch, _wire({**inputs, "member_boxes": (member_b.xyxy, member_a.xyxy)})
        )


def test_parking_sidecar_keeps_the_complete_authoritative_region():
    from types import SimpleNamespace

    from jimu_runtime.bundle.trust import TrustPin
    from jimu_runtime.domain.runtime.facts import ObjectRef
    from jimu_runtime.domain.runtime.media import BBoxXYXY, CoordinateSystem
    from jimu_runtime.domain.tool_mappings import ProfileContentRef
    from jimu_runtime.inspection.geometry import ParkingArea, _wire
    from jimu_runtime.inspection.geometry.parking_members_evidence import (
        GROUP_CAPABILITY,
        GROUP_REPORT,
    )
    from jimu_runtime.inspection.media.persistence import _geometry_document
    from jimu_runtime.inspection.media.types import MediaError

    digest = "a" * 64
    reference = ObjectRef.model_validate_json(
        '{"record_kind":"object","record_id":"obj"}'
    )
    profile = ProfileContentRef.model_validate_json(
        f'{{"artifact_kind":"profile","artifact_id":"profile","version":"1.0.0","digest":"{digest}"}}'
    )
    coordinates = CoordinateSystem.model_validate_json(
        b'{"coordinate_system_id":"test","frame":"original_image",'
        b'"dimensions":{"width":12,"height":12}}'
    )
    area = ParkingArea(
        region_id="area",
        region_version="1.0.0",
        region_sha256=digest,
        geometry_resource_id="geometry",
        geometry_version="1.0.0",
        geometry_sha256=digest,
        source_asset_sha256=digest,
        coordinate_system=coordinates,
        units="pixel",
        bbox=BBoxXYXY(xyxy=(1, 1, 8, 8)),
    )
    member_box = BBoxXYXY(xyxy=(2, 2, 4, 4))
    proof = TrustPin(identity=GROUP_CAPABILITY, version="1.0.0", sha256=digest)
    context = SimpleNamespace(
        dispatch=SimpleNamespace(
            intent=SimpleNamespace(dispatch_id="dispatch", request_digest=digest),
            attempt_id="attempt",
        ),
        target=SimpleNamespace(
            object_ref=reference,
            profile_ref=profile,
            capability_id=GROUP_CAPABILITY,
            implementation_proof_ref=proof,
            input_refs=(),
        ),
        binding=SimpleNamespace(execution_id="execution"),
        original=SimpleNamespace(
            coordinate_system=coordinates,
            asset=SimpleNamespace(media_ref=SimpleNamespace(sha256=digest)),
        ),
        member_boxes=(member_box,),
        parking_area=area,
    )
    report = {
        "schema_version": "1.0.0",
        "kind": GROUP_REPORT,
        "tool_execution_id": "dispatch",
        "execution_id": "execution",
        "attempt_id": "attempt",
        "object_ref": reference.model_dump(mode="json"),
        "profile_ref": profile.model_dump(mode="json"),
        "request_sha256": digest,
        "response_sha256": digest,
        "original_sha256": digest,
        "implementation": proof.model_dump(mode="json"),
        "input_refs": (),
        "parking_area": area.model_dump(mode="json"),
        "parking_inner": (2, 2, 6, 6),
        "members": [
            {
                "member_index": 1,
                "bbox": member_box.xyxy,
                "crop_xyxy": (2, 2, 4, 4),
                "parking_state": "unknown",
                "visual_evidence": None,
                "geometry": {"contacts": (), "outside": None, "tactile_overlap": None},
                "matches": None,
                "segmentation": {"status": "segmentation_unavailable"},
            }
        ],
        "tactile_segmentation": {"status": "segmentation_unavailable"},
        "matches": None,
    }
    assert _geometry_document(context, _wire(report))["matches"] is None

    report["parking_area"]["bbox"]["xyxy"] = (1, 1, 9, 9)
    with pytest.raises(MediaError, match="geometry_sidecar_dispatch_mismatch"):
        _geometry_document(context, _wire(report))
