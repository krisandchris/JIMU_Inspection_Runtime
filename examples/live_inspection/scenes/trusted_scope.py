"""Host-owned evidence for the two historical, generated trial captures.

The caller must use the same admitted test camera and Region when submitting
these values. Capture metadata is provisioned by the isolated host before HTTP
intake. It is not taken from a request body or a provider response.
"""

from dataclasses import dataclass
from math import isfinite

from jimu_runtime.bundle.integrity import canonical_json, sha256_digest
from jimu_runtime.domain.fact_schemas.canonicalization import JSONValue
from jimu_runtime.domain.values import BooleanValue, EnumValue, RecordValue

CAMERA = "synthetic.fixed.camera"
DEVICE = "synthetic.device"
MASK_REGION = "isolated_food_service"
SIDEWALK_REGION = "isolated_sidewalk"
MASK_SCOPE_FIELD = "synthetic_kitchen_employee_roster_attached"
SIDEWALK_SCOPE_FIELD = "synthetic_sidewalk_no_goods_policy"
SCOPE_EVIDENCE_SHA256 = "synthetic_scope_evidence_sha256"
SCOPE_IMAGE_SHA256 = "synthetic_scope_image_sha256"

type Box = tuple[float, float, float, float]
type Point = tuple[float, float]

CAPTURE_IMAGE_SIZE = (1536, 1024)
_MASK_IMAGE_SHA256 = "11a19d6d6ec44a249bd77e3350bcccf6ab741b7943de5ae6e36853f9fceb90f3"
_SIDEWALK_IMAGE_SHA256 = (
    "2270ce7d8acd45780bbed034e4598e68c38fddd333d16750d2761d4c506356e8"
)
_KITCHEN: Box = (0, 0, 1536, 1024)
SIDEWALK_ZONE: Box = (445, 410, 1320, 980)


@dataclass(frozen=True, slots=True)
class EmployeeAnnotation:
    """Independent, manual location of a fictional roster entry in the original."""

    roster_id: str
    visible_person: Box
    identity_anchor: Point


# The test fixture assigns a fictional employee to the annotated person.
# The annotation is made in original pixels and does not carry mask labels.
_FOOD_SERVICE_ROSTER = (
    EmployeeAnnotation("employee-01", (560, 0, 1295, 1024), (940, 187)),
)


def _contains(box: Box, point: Point) -> bool:
    return box[0] <= point[0] <= box[2] and box[1] <= point[1] <= box[3]


def _iou(first: Box, second: Box) -> float:
    width = max(0.0, min(first[2], second[2]) - max(first[0], second[0]))
    height = max(0.0, min(first[3], second[3]) - max(first[1], second[1]))
    intersection = width * height
    first_area = (first[2] - first[0]) * (first[3] - first[1])
    second_area = (second[2] - second[0]) * (second[3] - second[1])
    return intersection / (first_area + second_area - intersection)


@dataclass(frozen=True, slots=True)
class KitchenRoster:
    image_sha256: str
    metadata: RecordValue

    def match(self, image: bytes, boxes: tuple[Box, ...]) -> tuple[str, ...]:
        """Require a bijection and preserve the order of all provider boxes.

        An empty, missing, duplicate, ambiguous or drifted match fails the
        complete association. A failed association cannot authorize a subset
        or be reported as the confirmed absence of qualifying people.
        """
        if sha256_digest(image) != self.image_sha256:
            raise ValueError("synthetic roster does not apply to this frame")
        if len(boxes) != len(_FOOD_SERVICE_ROSTER):
            raise ValueError("expected a match for every provider box and roster entry")
        matched: list[str] = []
        for box in boxes:
            if len(box) != 4 or not all(isfinite(edge) for edge in box):
                raise ValueError("invalid roster association box")
            left, top, right, bottom = box
            width, height = CAPTURE_IMAGE_SIZE
            if not 0 <= left < right <= width or not 0 <= top < bottom <= height:
                raise ValueError("roster association box falls outside the frame")
            matches = [
                row.roster_id
                for row in _FOOD_SERVICE_ROSTER
                if _contains(_KITCHEN, row.identity_anchor)
                and _contains(box, row.identity_anchor)
                and _iou(row.visible_person, box) >= 0.65
            ]
            if len(matches) != 1 or matches[0] in matched:
                raise ValueError("missing or ambiguous synthetic roster association")
            matched.append(matches[0])
        return tuple(matched)


def scope_pins(region_id: str, scope_field: str) -> tuple[str, str]:
    """Return the exact image and synthetic evidence digest for a signed scene."""
    evidence: dict[str, JSONValue]
    match (region_id, scope_field):
        case _ if (region_id, scope_field) == (MASK_REGION, MASK_SCOPE_FIELD):
            image_digest = _MASK_IMAGE_SHA256
            evidence = {
                "kitchen": list(_KITCHEN),
                "employees": [
                    {
                        "roster_id": row.roster_id,
                        "visible_person": list(row.visible_person),
                        "identity_anchor": list(row.identity_anchor),
                    }
                    for row in _FOOD_SERVICE_ROSTER
                ],
            }
        case _ if (region_id, scope_field) == (SIDEWALK_REGION, SIDEWALK_SCOPE_FIELD):
            image_digest = _SIDEWALK_IMAGE_SHA256
            evidence = {
                "pedestrian_zone_original_pixels": list(SIDEWALK_ZONE),
                "placement_policy": "no_placed_cardboard_cartons",
            }
        case _:
            raise ValueError("scene has no registered synthetic scope declaration")
    revision = sha256_digest(
        canonical_json(
            {
                "schema_version": "1.0.0",
                "origin": "synthetic_fixture_manual_annotations",
                "device_id": DEVICE,
                "camera_id": CAMERA,
                "region_id": region_id,
                "image_sha256": image_digest,
                "dimensions": list(CAPTURE_IMAGE_SIZE),
                **evidence,
            },
            max_bytes=8192,
            max_depth=12,
        )
    )
    return image_digest, revision


def _capture_metadata(
    image: bytes,
    *,
    device_id: str,
    camera_id: str,
    region_id: str,
    scope_field: str,
) -> RecordValue:
    try:
        expected_digest, evidence_revision = scope_pins(region_id, scope_field)
    except ValueError:
        raise ValueError(
            "synthetic scope evidence does not apply to this capture"
        ) from None
    image_digest = sha256_digest(image)
    if (device_id, camera_id, image_digest) != (DEVICE, CAMERA, expected_digest):
        raise ValueError("synthetic scope evidence does not apply to this capture")
    return RecordValue(
        kind="record",
        value={
            scope_field: BooleanValue(kind="bool", value=True),
            SCOPE_EVIDENCE_SHA256: EnumValue(kind="enum", value=evidence_revision),
            SCOPE_IMAGE_SHA256: EnumValue(kind="enum", value=image_digest),
        },
    )


def kitchen_roster(
    image: bytes, *, camera_id: str, region_id: str, device_id: str = DEVICE
) -> KitchenRoster:
    """Load the independently labeled employee roster for the exact generated image."""
    metadata = _capture_metadata(
        image,
        camera_id=camera_id,
        region_id=region_id,
        device_id=device_id,
        scope_field=MASK_SCOPE_FIELD,
    )
    return KitchenRoster(_MASK_IMAGE_SHA256, metadata)


def sidewalk_metadata(
    image: bytes, *, camera_id: str, region_id: str, device_id: str = DEVICE
) -> RecordValue:
    """Load the fictional blanket no-placement policy for the bounded test zone."""
    return _capture_metadata(
        image,
        camera_id=camera_id,
        region_id=region_id,
        device_id=device_id,
        scope_field=SIDEWALK_SCOPE_FIELD,
    )
