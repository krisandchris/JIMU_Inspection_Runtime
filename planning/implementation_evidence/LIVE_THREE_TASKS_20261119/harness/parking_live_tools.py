"""Build the trial Bundle and mediate live segmentation through N05."""
import base64
import json
import sys
from dataclasses import replace
from hashlib import sha256
from io import BytesIO
from pathlib import Path

from PIL import Image
from examples.nonmotor_vehicle_illegal_parking.compilable import scene
from jimu_runtime.bundle.semantic_inputs import require_definition
from jimu_runtime.domain.definition_enums import EvidenceType
from jimu_runtime.domain.definitions.tools import ToolDefinition
from jimu_runtime.domain.enums import ArtifactKind, RuntimeRecordKind
from jimu_runtime.domain.runtime.evidence import DerivedImageEvidence
from jimu_runtime.domain.runtime.tool_executions import KnownToolOutputState
from jimu_runtime.inspection.geometry import (
    GeometryInput, ParkingArea, _check_scope, _content, _geometry_report,
    _IDENTITY_AFFINE, _json, _mask_claim, _mask_payload, _ref, _typed, _wire,
)
from jimu_runtime.tools.registry import IsolatedCPUWorker, ToolImplementation

TOOL_ARTIFACT = Path(__file__).with_name("parking_tool_worker.py")
VERSION = "1.0.0"
MODES = {
    "wheel_segmentation": "trial.live.wevis.wheel_segmentation",
    "tactile_paving_segmentation": "trial.live.wevis.tactile_paving_segmentation",
    "wheel_boundary_geometry": "trial.deterministic.wheel_boundary_geometry",
}
MASKS = {"wheel_segmentation": "wheel_mask", "tactile_paving_segmentation": "tactile_paving_mask"}
PINNED_AREA = (344, 498, 606, 702)
IMAGE_SIZE = (1536,1024)


def build_source():
    original = scene.load_source()
    x0,y0,x1,y1 = PINNED_AREA
    polygon = {"kind":"polygon","vertices":[{"x":x,"y":y} for x,y in ((x0,y0),(x1,y0),(x1,y1),(x0,y1))]}
    area = json.dumps({
        "schema_version":VERSION,"coordinate_system":"original_image","unit":"pixel",
        "width":IMAGE_SIZE[0],"height":IMAGE_SIZE[1],"geometry":polygon,
    }).encode()
    out = []
    for item in original.artifacts:
        body = json.loads(item.content)
        if item.kind is ArtifactKind.GEOMETRY_RESOURCE:
            body["geometry"] = polygon
        elif item.kind is ArtifactKind.STATIC_ASSET:
            body.update(sha256=sha256(area).hexdigest(),byte_size=len(area))
        elif item.kind is ArtifactKind.TOOL:
            body["implementation_requirement"]["capability_id"] = MODES[item.artifact_id]
        else:
            out.append(item)
            continue
        out.append(replace(item,content=json.dumps(body).encode()))
    requirements = tuple(
        requirement.model_copy(update={"capability_id": MODES[requirement.capability_id.split(".")[-1]]})
        if requirement.capability_id.startswith("synthetic.cpu.") else requirement
        for requirement in original.metadata.required_capabilities
    )
    (asset,) = original.assets
    return scene.with_source_provenance(replace(
        original, artifacts=tuple(out), assets=(replace(asset,content=area),),
        metadata=replace(original.metadata, required_capabilities=requirements),
    ))


def operation(dispatch):
    name = dispatch.tool_ref.artifact_id
    if (
        name not in MODES or dispatch.capability_id != MODES[name]
        or dispatch.contract_version != VERSION or dispatch.implementation_version != VERSION
        or dispatch.invoke_model is not None or dispatch.object_ref is None
    ):
        raise ValueError("Unapproved live parking Tool")
    return name


class LiveGeometryInput(GeometryInput):
    def _source(self, dispatch):
        content = self.original.content
        asset = self.original.asset
        coordinates = dispatch.original_coordinates
        _check_scope(dispatch, asset, original=True)
        asset.validate_original_media(self.media)
        if (
            coordinates is None or asset.spatial.coordinate_system != coordinates
            or self.media.media_ref != dispatch.original_media_ref
            or (self.media.storage, self.media.stage) != (asset.storage, asset.stage)
            or asset.access_scope.task_id != dispatch.task_id
            or len(content) != asset.byte_size
            or sha256(content).hexdigest() != asset.sha256
        ):
            raise ValueError("Tool needs the exact finalized original image")
        return coordinates

    def _prepare(self, dispatch):
        name = operation(dispatch)
        coordinates = self._source(dispatch)
        physical = {"dimensions": [coordinates.dimensions.width, coordinates.dimensions.height]}
        if name in MASKS:
            physical["image"] = _content(self.original.content)
            if name == "wheel_segmentation":
                if self.object_bbox is None or self.object_bbox != dispatch.object_bbox:
                    raise ValueError("Wheel crop does not match the Object")
                physical["bbox"] = self.object_bbox.xyxy
        else:
            if (
                self.parking_area is None or self.parking_area.coordinate_system != coordinates
                or {x.proof.input_path for x in self.masks} != {(name,) for name in MASKS.values()}
            ):
                raise ValueError("Tool lacks an approved Region or exact two masks")
            physical["bbox"] = self.parking_area.bbox.xyxy
            for item in self.masks:
                asset = item.proof.asset
                _check_scope(dispatch, asset)
                asset.validate_original_media(self.media)
                spatial = asset.spatial
                if (
                    asset.evidence_type is not EvidenceType.MASK or asset.mime_type != "image/png"
                    or asset.access_scope != self.original.asset.access_scope
                    or not isinstance(spatial, DerivedImageEvidence)
                    or len(spatial.transforms) != 1
                    or spatial.coordinate_system.dimensions != coordinates.dimensions
                    or spatial.transforms[0].source != coordinates
                    or spatial.transforms[0].implementation_id != MODES[asset.producer_id]
                    or spatial.transforms[0].implementation_version != asset.producer_version
                    or spatial.transforms[0].affine != _IDENTITY_AFFINE
                    or len(item.content) != asset.byte_size
                    or sha256(item.content).hexdigest() != asset.sha256
                ):
                    raise ValueError("Tool mask was not selected in original coordinates")
                physical[item.proof.input_path[0]] = _content(item.content)
        return _wire(physical)

    async def _materialize(self, dispatch, content):
        name = operation(dispatch)
        coordinates = self._source(dispatch)
        if name in MASKS:
            output = slot = MASKS[name]
            mask, count = verify_mask(dispatch, content)
            reason = None if count else "empty_segmentation"
            asset = await self._stage(dispatch, coordinates, slot, EvidenceType.MASK, "image/png", mask)
            value = _mask_payload(asset,coordinates)
            sources = ()
            names = ((slot,asset),)
        else:
            output = "wheel_boundary_relation"
            geometry, relation, reason = _geometry_report(dispatch, content)
            asset = await self._stage(dispatch, coordinates, "geometry", EvidenceType.GEOMETRY, "application/json", geometry)
            sources = tuple(item.proof.asset for item in self.masks)
            names = tuple((item.proof.input_path[0], item.proof.asset) for item in self.masks) + (("geometry", asset),)
            value = _typed("enum", relation)
        evidence = self.original.asset,*sources,asset
        refs = [_ref(item) for item in evidence]
        state = {"output_name":output,"fact_status":"known" if reason is None else "unknown","evidence_refs":refs}
        body = {
            "status":"succeeded" if reason is None else "partial",
            "diagnostics":[],"evidence_refs":refs, "output_states":[state],
            "evidence":[{"name":slot,"evidence_refs":[_ref(item)]} for slot,item in names],
        }
        if reason is None:
            body["payload"] = _typed("record",{output:value})
        else:
            state["reason"] = {"reason_code":reason,"message":"Live mask or deterministic geometry is inconclusive.","diagnostic_refs":[_ref(asset)]}
        return _wire(body),evidence


def validate_inputs(dispatch, content):
    name = operation(dispatch)
    physical, logical = _json(content),_json(dispatch.request)
    coordinates = dispatch.original_coordinates
    dims = [coordinates.dimensions.width,coordinates.dimensions.height]
    if physical["dimensions"] != dims:
        raise ValueError("Source dimensions mismatch")
    if name in MASKS:
        if (
            logical["image"] != dispatch.original_media_ref.model_dump(mode="json")
            or sha256(base64.b64decode(physical["image"],validate=True)).hexdigest() != dispatch.original_media_ref.sha256
        ):
            raise ValueError("Source hash mismatch")
        if name == "wheel_segmentation" and (
            dispatch.object_bbox is None
            or physical["bbox"] != list(dispatch.object_bbox.xyxy)
            or logical["bbox"] != dispatch.object_bbox.model_dump(mode="json")
        ):
            raise ValueError("Source Object bbox mismatch")
    else:
        parking = ParkingArea.model_validate(logical["parking_area"])
        if parking.coordinate_system != coordinates or list(parking.bbox.xyxy) != physical["bbox"]:
            raise ValueError("Pinned ROI mismatch")
        for slot in MASKS.values():
            evidence_id, digest = _mask_claim(logical[slot])
            if (
                digest != sha256(base64.b64decode(physical[slot],validate=True)).hexdigest()
                or not any(
                    ref.artifact_ref.record_kind is RuntimeRecordKind.EVIDENCE
                    and ref.artifact_ref.record_id == evidence_id and ref.digest == digest
                    for ref in dispatch.input_refs
                )
            ):
                raise ValueError("Selected mask hash mismatch")


def verify_mask(dispatch, content):
    raw = _json(content)
    bitmap = base64.b64decode(raw["png"],validate=True)
    dims = dispatch.original_coordinates.dimensions
    with Image.open(BytesIO(bitmap), formats=["PNG"]) as image:
        if image.mode != "L" or image.size != (dims.width,dims.height):
            raise ValueError("Mask isn't aligned to original frame")
        histogram = image.histogram()
        if any(histogram[1:255]) or histogram[255] != raw["count"]:
            raise ValueError("Mask count mismatch")
        if dispatch.tool_ref.artifact_id == "wheel_segmentation" and image.getbbox() is not None:
            left, top, right, bottom = image.getbbox()
            x0,y0,x1,y1 = dispatch.object_bbox.xyxy
            if not (x0 <= left+.5 < right-.5 < x1 and y0 <= top+.5 < bottom-.5 < y1):
                if not (x0 <= left+.5 <= right-.5 < x1 and y0 <= top+.5 <= bottom-.5 < y1):
                    raise ValueError("Wheel mask falls outside Object")
    return bitmap,raw["count"]


def validate_result(dispatch, content, result, evidence):
    name = operation(dispatch)
    if name in MASKS:
        slot = output = MASKS[name]
        expected, count = verify_mask(dispatch,content)
        known = count > 0
    else:
        slot, output = "geometry","wheel_boundary_relation"
        expected, relation, reason = _geometry_report(dispatch,content)
        known = reason is None
    named = next(item for item in result.evidence if item.name == slot)
    asset = next(item for item in evidence if item.evidence_id == named.evidence_refs[0].record_id)
    if (
        asset.sha256 != sha256(expected).hexdigest() or asset.byte_size != len(expected)
        or asset.producer_id != name or asset.producer_version != VERSION
        or result.output_states[0].output_name != output
        or isinstance(result.output_states[0],KnownToolOutputState) != known
    ):
        raise ValueError("Tool publication differs from observed physical output")
    if known:
        expected_value = _mask_payload(asset,dispatch.original_coordinates) if name in MASKS else _typed("enum",relation)
        if result.payload.model_dump(mode="json") != _typed("record",{output:expected_value}):
            raise ValueError("Tool Fact payload disagrees with exact result")


def entries_for(audit_dir):
    audit_dir.mkdir(exist_ok=False)
    def entries(fixture):
        catalogue = fixture.scene.execution.catalogue
        registrations = []
        for member in catalogue.of_kind(ArtifactKind.TOOL):
            definition = require_definition(catalogue,member.ref,ToolDefinition)
            name = definition.tool_id
            capability = MODES[name]
            proof = fixture.tool_proofs[capability]
            runner = IsolatedCPUWorker(
                fixture.resources,
                (sys.executable,"-I",str(TOOL_ARTIFACT),name,str(audit_dir)),
                "cpu","live-parking-tools",VERSION,proof,TOOL_ARTIFACT,mediated=True,
            )
            registrations.append(ToolImplementation(
                capability,VERSION,VERSION,proof,definition.provider_kind,
                definition.input_schema_ref,definition.output_schema_ref,"cpu",runner,
                physical_input_check=validate_inputs,physical_result_check=validate_result,
            ))
        return tuple(registrations)
    return entries
