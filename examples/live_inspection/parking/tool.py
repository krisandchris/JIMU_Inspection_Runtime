"""Mediated original-image segmentation and auditable same-member joins."""

import base64
import sys
from dataclasses import asdict, dataclass
from hashlib import sha256
from pathlib import Path

from PIL import Image, ImageChops

from jimu_runtime.bundle.semantic_inputs import require_definition
from jimu_runtime.bundle.trust import TrustPin
from jimu_runtime.domain.definition_enums import EvidenceType
from jimu_runtime.domain.definitions.tools import ToolDefinition
from jimu_runtime.domain.enums import ArtifactKind
from jimu_runtime.domain.runtime.evidence import (
    EvidenceAsset,
    OriginalImageEvidence,
    TaskEvidenceAccess,
)
from jimu_runtime.domain.runtime.media import CoordinateSystem, FinalizedAsset
from jimu_runtime.domain.runtime.tool_executions import (
    FailedToolResult,
    KnownToolOutputState,
    ToolResult,
)
from jimu_runtime.domain.values import ListValue, RecordValue
from jimu_runtime.inspection.geometry import (
    GEOMETRY_SLOT,
    GeometryInput,
    ParkingArea,
    _check_scope,
    _content,
    _json,
    _ref,
    _typed,
    _wire,
)
from jimu_runtime.inspection.geometry.parking_members import (
    associate_observations,
    match_group,
    match_member,
)
from jimu_runtime.inspection.geometry.parking_members_evidence import (
    GROUP_REPORT,
    ParkingMemberGeometryEvidence,
)
from jimu_runtime.tools.registry import (
    IsolatedCPUWorker,
    ToolDispatch,
    ToolImplementation,
)

from .contact import member_geometry
from .scene import (
    CAPABILITY,
    OBSERVATION,
    OUTPUT,
    TOOL,
    VERSION,
)
from .wevis_masks import (
    APPROVED_IMAGE_SHA256,
    MAX_MASKS_PER_SUBJECT,
    PARKING_INNER,
    PARKING_OUTER,
    TACTILE,
    WEVIS_MODEL,
    WHEEL,
    decode_png,
    pixel_box,
    png,
)

WORKER = Path(__file__).with_name("wevis_masks.py").resolve()


def tool_proof() -> TrustPin:
    return TrustPin(
        identity=CAPABILITY,
        version=VERSION,
        sha256=sha256(WORKER.read_bytes()).hexdigest(),
    )


def _operation(dispatch: ToolDispatch) -> None:
    if (
        dispatch.tool_ref.artifact_id != TOOL
        or dispatch.capability_id != CAPABILITY
        or dispatch.contract_version != VERSION
        or dispatch.implementation_version != VERSION
        or dispatch.invoke_model is not None
        or dispatch.object_ref is None
    ):
        raise ValueError("Unapproved member geometry dispatch")


def _physical_request(dispatch: ToolDispatch, content: bytes) -> dict:
    _operation(dispatch)
    physical, logical = _json(content), _json(dispatch.request)
    media = dispatch.original_media_ref
    coordinates = dispatch.original_coordinates
    object_box = dispatch.object_bbox
    if media is None or coordinates is None or object_box is None:
        raise ValueError("Member geometry needs a pinned original image and Object")
    dimensions = coordinates.dimensions
    image = physical.get("image")
    if (
        physical.keys() != {"dimensions", "image", "member_boxes"}
        or not isinstance(image, str)
        or media.sha256 != APPROVED_IMAGE_SHA256
        or logical.get("image") != media.model_dump(mode="json")
        or sha256(base64.b64decode(image, validate=True)).hexdigest() != media.sha256
        or physical["dimensions"] != [dimensions.width, dimensions.height]
    ):
        raise ValueError("Member geometry physical input changed original image")
    parking = ParkingArea.model_validate(logical["parking_area"])
    if parking.coordinate_system != coordinates or parking.bbox.xyxy != PARKING_OUTER:
        raise ValueError("Unknown scene parking geometry")
    if not dispatch.object_member_boxes or physical["member_boxes"] != [
        list(box.xyxy) for box in dispatch.object_member_boxes
    ]:
        raise ValueError("Member geometry requires the authoritative ordered N03 boxes")
    for raw in physical["member_boxes"]:
        x0, y0, x1, y1 = object_box.xyxy
        left, top, right, bottom = raw
        if not x0 <= left < right <= x1 or not y0 <= top < bottom <= y1:
            raise ValueError("Grounding member lies outside its group")
    return physical


def validate_input(dispatch: ToolDispatch, content: bytes) -> None:
    _physical_request(dispatch, content)


def _observations(logical: dict) -> list[dict]:
    source = logical.get(OBSERVATION)
    if source is None:
        return []
    value = RecordValue.model_validate(source).value["members"]
    if not isinstance(value, ListValue):
        raise ValueError("Parking observation Fact omitted its members list")
    observations = []
    for member in value.value:
        if not isinstance(member, RecordValue):
            raise ValueError("Parking observation member is not a Record")
        observations.append(
            {field: value.value for field, value in member.value.items()}
        )
    return observations


def _output_masks(
    raw: dict,
    image_digest: str,
    dimensions: tuple[int, int],
    *,
    member_index: int | None = None,
    crop: tuple[int, int, int, int] | None = None,
) -> tuple[Image.Image, ...]:
    if raw["status"] != "observed":
        return ()
    if (
        raw["source_sha256"] != image_digest
        or raw["model"] != WEVIS_MODEL
        or raw["member_index"] != member_index
        or raw["label"] != (TACTILE if member_index is None else WHEEL)
        or (None if raw["crop_xyxy"] is None else tuple(raw["crop_xyxy"])) != crop
        or type(raw["model_forward_calls"]) is not int
        or raw["model_forward_calls"] <= 0
        or len(raw["masks"]) != raw["mask_count"]
        or len(raw["masks"]) > MAX_MASKS_PER_SUBJECT
    ):
        raise ValueError("Parking segmentation evidence lost its input identity")
    output = []
    for bitmap in raw["masks"]:
        frame = dimensions if crop is None else (crop[2] - crop[0], crop[3] - crop[1])
        mask = decode_png(base64.b64decode(bitmap, validate=True), frame, "L")
        if crop is not None:
            aligned = Image.new("L", dimensions)
            aligned.paste(mask, crop[:2])
            mask = aligned
        output.append(mask)
    return tuple(output)


def _segmentation_receipt(reply: dict) -> dict:
    fields = (
        "status",
        "request_sha256",
        "response_sha256",
        "model",
        "model_forward_calls",
        "mask_count",
    )
    return {field: reply[field] for field in fields if field in reply}


def geometry_report(
    dispatch: ToolDispatch, content: bytes
) -> tuple[bytes, bool | None]:
    if dispatch.physical_request is None:
        raise ValueError("Missing mediated member inputs")
    physical = _physical_request(dispatch, dispatch.physical_request)
    logical, raw = _json(dispatch.request), _json(content)
    dimensions = tuple(physical["dimensions"])
    member_boxes = tuple(tuple(box) for box in physical["member_boxes"])
    if len(raw["members"]) != len(member_boxes):
        raise ValueError("Worker response must preserve every grounding member")
    source_digest = dispatch.original_media_ref.sha256
    image = decode_png(
        base64.b64decode(physical["image"], validate=True), dimensions, "RGB"
    )
    tactile_masks = _output_masks(raw["tactile"], source_digest, dimensions)
    if tactile_masks and raw["tactile"]["request_sha256"] != source_digest:
        raise ValueError("Tactile segmentation must use the unchanged original image")
    tactile = None
    if tactile_masks:
        tactile = Image.new("L", dimensions)
        for mask in tactile_masks:
            tactile = ImageChops.lighter(tactile, mask)
    observations = associate_observations(_observations(logical), len(member_boxes))
    crop_boxes = []
    for box in member_boxes:
        try:
            crop_boxes.append(pixel_box(box, dimensions))
        except ValueError:
            crop_boxes.append(None)
    members = []
    for index, (result, bbox, crop, observation) in enumerate(
        zip(raw["members"], member_boxes, crop_boxes, observations, strict=True),
        start=1,
    ):
        state, visual_evidence = observation
        if crop is None and result["status"] == "observed":
            raise ValueError("Uncropable member cannot have segmentation")
        wheels = _output_masks(
            result, source_digest, dimensions, member_index=index, crop=crop
        )
        if wheels:
            # Verify request identity independently from the raw response metadata.
            if result["request_sha256"] != sha256(png(image.crop(crop))).hexdigest():
                raise ValueError("Wheel segmentation must use the exact member crop")
        others = tuple(
            pixel
            for other_id, pixel in enumerate(crop_boxes, start=1)
            if other_id != index and pixel is not None
        )
        geometry = member_geometry(
            wheels, tactile, PARKING_OUTER, PARKING_INNER, others
        )
        members.append(
            {
                "member_index": index,
                "bbox": bbox,
                "crop_xyxy": crop,
                "parking_state": state,
                "visual_evidence": visual_evidence,
                "geometry": asdict(geometry),
                "matches": match_member(state, geometry),
                "segmentation": _segmentation_receipt(result),
            }
        )
    matched = match_group([row["matches"] for row in members])
    report = {
        "schema_version": VERSION,
        "kind": GROUP_REPORT,
        "tool_execution_id": dispatch.tool_execution_id,
        "execution_id": dispatch.execution_id,
        "attempt_id": dispatch.attempt_id,
        "object_ref": dispatch.object_ref.model_dump(mode="json"),
        "profile_ref": dispatch.profile_ref.model_dump(mode="json"),
        "implementation": dispatch.implementation_proof_ref.model_dump(mode="json"),
        "input_refs": [ref.model_dump(mode="json") for ref in dispatch.input_refs],
        "original_sha256": source_digest,
        "request_sha256": sha256(dispatch.physical_request).hexdigest(),
        "response_sha256": sha256(content).hexdigest(),
        "parking_area": logical["parking_area"],
        "parking_inner": PARKING_INNER,
        "tactile_segmentation": _segmentation_receipt(raw["tactile"]),
        "members": members,
        "matches": matched,
    }
    encoded = _wire(report)
    ParkingMemberGeometryEvidence.model_validate_json(encoded)
    return encoded, matched


@dataclass(frozen=True, slots=True)
class ParkingGeometryInput(GeometryInput):
    def _source(self, dispatch: ToolDispatch) -> CoordinateSystem:
        # The production photo carries its own larger byte budget. Keep the
        # base exchange's finalized scope, source hash and coordinate checks.
        original = self.original.asset
        coordinates = dispatch.original_coordinates
        _check_scope(dispatch, original, original=True)
        original.validate_original_media(self.media)
        if (
            coordinates is None
            or not isinstance(original.spatial, OriginalImageEvidence)
            or coordinates != original.spatial.coordinate_system
            or isinstance(original.access_scope, TaskEvidenceAccess)
            and original.access_scope.task_id != dispatch.task_id
            or dispatch.original_media_ref != self.media.media_ref
            or not isinstance(original.stage, FinalizedAsset)
            or not 0 < len(self.original.content) <= 8 * 1024 * 1024
            or (original.storage, original.stage)
            != (self.media.storage, self.media.stage)
            or original.sha256 != sha256(self.original.content).hexdigest()
            or original.byte_size != len(self.original.content)
        ):
            raise ValueError("Parking geometry requires the finalized original image")
        return coordinates

    def _prepare(self, dispatch: ToolDispatch) -> bytes:
        _operation(dispatch)
        coords = self._source(dispatch)
        if (
            self.object_bbox != dispatch.object_bbox
            or self.parking_area is None
            or self.parking_area.model_dump(mode="json")
            != _json(dispatch.request)["parking_area"]
            or not self.member_boxes
            or self.masks
        ):
            raise ValueError(
                "Parking exchange needs the exact group, member boxes and region"
            )
        physical = _wire(
            {
                "dimensions": (coords.dimensions.width, coords.dimensions.height),
                "image": _content(self.original.content),
                "member_boxes": tuple(box.xyxy for box in self.member_boxes),
            }
        )
        validate_input(dispatch, physical)
        return physical

    async def _materialize(
        self,
        dispatch: ToolDispatch,
        content: bytes,
    ) -> tuple[bytes, tuple[EvidenceAsset, ...]]:
        coords = self._source(dispatch)
        report, matched = geometry_report(dispatch, content)
        geometry = await self._stage(
            dispatch,
            coords,
            GEOMETRY_SLOT,
            EvidenceType.GEOMETRY,
            "application/json",
            report,
        )
        evidence = self.original.asset, geometry
        refs = [_ref(asset) for asset in evidence]
        state = {
            "output_name": OUTPUT,
            "fact_status": "unknown" if matched is None else "known",
            "evidence_refs": refs,
        }
        response = {
            "status": "partial" if matched is None else "succeeded",
            "diagnostics": [],
            "evidence_refs": refs,
            "evidence": [{"name": GEOMETRY_SLOT, "evidence_refs": [_ref(geometry)]}],
            "output_states": [state],
        }
        if matched is None:
            state["reason"] = {
                "reason_code": "parking_members_inconclusive",
                "message": "Some grounded members remain unresolved and none has a confirmed match.",
                "diagnostic_refs": [_ref(geometry)],
            }
        else:
            response["payload"] = _typed("record", {OUTPUT: _typed("bool", matched)})
        return _wire(response), evidence


def validate_result(
    dispatch: ToolDispatch,
    content: bytes,
    result: ToolResult,
    evidence: tuple[EvidenceAsset, ...],
) -> None:
    expected, matched = geometry_report(dispatch, content)
    if isinstance(result, FailedToolResult):
        raise ValueError("Grouped parking geometry must return a coverage-aware result")
    (connection,) = result.evidence
    (state,) = result.output_states
    (geometry,) = tuple(
        asset
        for asset in evidence
        if asset.evidence_id == connection.evidence_refs[0].record_id
    )
    if (
        connection.name != GEOMETRY_SLOT
        or geometry.sha256 != sha256(expected).hexdigest()
        or geometry.byte_size != len(expected)
        or state.output_name != OUTPUT
        or isinstance(state, KnownToolOutputState) != (matched is not None)
    ):
        raise ValueError(
            "Grouped parking Fact is not backed by the expected member geometry"
        )
    if matched is not None and (
        result.payload is None
        or result.payload.model_dump(mode="json")
        != _typed("record", {OUTPUT: _typed("bool", matched)})
    ):
        raise ValueError("Grouped parking result changed deterministic truth")


def entries_for(directory: Path, endpoint: str):
    directory.mkdir(mode=0o700, parents=True, exist_ok=False)

    def entries(fixture):
        catalogue = fixture.scene.execution.catalogue
        (row,) = catalogue.of_kind(ArtifactKind.TOOL)
        definition = require_definition(catalogue, row.ref, ToolDefinition)
        proof = fixture.tool_proofs[CAPABILITY]
        runner = IsolatedCPUWorker(
            fixture.resources,
            (sys.executable, "-I", str(WORKER), endpoint, str(directory)),
            "cpu",
            "live-parking-members",
            VERSION,
            proof,
            WORKER,
            mediated=True,
        )
        return (
            ToolImplementation(
                CAPABILITY,
                VERSION,
                VERSION,
                proof,
                definition.provider_kind,
                definition.input_schema_ref,
                definition.output_schema_ref,
                "cpu",
                runner,
                physical_input_check=validate_input,
                physical_result_check=validate_result,
            ),
        )

    return entries
