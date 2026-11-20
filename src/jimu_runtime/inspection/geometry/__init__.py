"""Host bindings for the explicitly selected local synthetic CPU inspection tools."""

import base64
import json
import sys
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from functools import partial
from hashlib import sha256
from pathlib import Path
from typing import Literal, cast

from jimu_runtime.bundle.trust import TrustPin
from jimu_runtime.domain.definition_enums import EvidenceType, ToolProviderKind
from jimu_runtime.domain.definitions.tools import ToolDefinition
from jimu_runtime.domain.enums import FactStatus, RuntimeRecordKind
from jimu_runtime.domain.primitives import Digest
from jimu_runtime.domain.runtime.evidence import (
    DerivedImageEvidence,
    EvidenceAsset,
    ObjectEvidenceScope,
    OriginalImageEvidence,
    TaskEvidenceAccess,
)
from jimu_runtime.domain.runtime.media import (
    BBoxXYXY,
    CoordinateSystem,
    FinalizedAsset,
    MediaAsset,
    TransformRecord,
)
from jimu_runtime.domain.runtime.tool_executions import (
    FailedToolResult,
    KnownToolOutputState,
    NonKnownToolOutputState,
    ToolResult,
)
from jimu_runtime.inspection.geometry._cpu import (
    MAX_PIXELS,
    MAX_WIRE_BYTES,
    OPERATIONS,
    classify,
    load_image,
)
from jimu_runtime.inspection.geometry.region import ParkingArea as ParkingArea
from jimu_runtime.inspection.geometry.region import (
    verify_parking_area as verify_parking_area,
)
from jimu_runtime.tools.executor import AncestorToolEvidence
from jimu_runtime.tools.registry import (
    CPUWorkerExchange,
    IsolatedCPUWorker,
    ToolDispatch,
    ToolImplementation,
)
from jimu_runtime.workers.resources import ResourceAdmission

VERSION: Literal["1.0.0"] = "1.0.0"
CPU_ARTIFACT = Path(__file__).with_name("_cpu.py").resolve()
WHEEL_MASK: Literal["wheel_mask"] = "wheel_mask"
TACTILE_MASK: Literal["tactile_paving_mask"] = "tactile_paving_mask"
RELATION = "wheel_boundary_relation"
GEOMETRY_SLOT: Literal["geometry"] = "geometry"
_MASK_FIELDS = {WHEEL_MASK, TACTILE_MASK}
_METRICS = {"wheel", "tactile", "overlap", "inside", "outside", "crossing", "touching"}
_IDENTITY_AFFINE = (1.0, 0.0, 0.0, 0.0, 1.0, 0.0)


@dataclass(frozen=True, slots=True)
class GeometrySidecar:
    slot: str
    evidence_type: EvidenceType
    mime_type: str
    content: bytes = field(repr=False)
    spatial: DerivedImageEvidence
    sha256: Digest


type StageGeometrySidecar = Callable[
    [ToolDispatch, GeometrySidecar], Awaitable[EvidenceAsset]
]


@dataclass(frozen=True, slots=True)
class EvidenceBytes:
    asset: EvidenceAsset
    content: bytes = field(repr=False)

    def verify_bytes(self) -> None:
        if (
            type(self.content) is not bytes
            or not 0 < len(self.content) <= MAX_WIRE_BYTES
            or not isinstance(self.asset.stage, FinalizedAsset)
            or len(self.content) != self.asset.byte_size
            or sha256(self.content).hexdigest() != self.asset.sha256
        ):
            raise ValueError("geometry input bytes do not match the finalized evidence")


@dataclass(frozen=True, slots=True)
class MaskInput:
    proof: AncestorToolEvidence
    content: bytes = field(repr=False)


def _json(raw: bytes) -> dict[str, object]:
    def unique(pairs: list[tuple[str, object]]) -> dict[str, object]:
        values = dict(pairs)
        if len(values) != len(pairs):
            raise ValueError("duplicate CPU wire field")
        return values

    try:
        document = json.loads(raw, object_pairs_hook=unique)
    except (RecursionError, UnicodeError) as exc:
        raise ValueError("invalid CPU wire response") from exc
    if not isinstance(document, dict):
        raise ValueError("CPU wire requires a closed object")
    return document


def _wire(body: object) -> bytes:
    return json.dumps(
        body, allow_nan=False, sort_keys=True, separators=(",", ":")
    ).encode()


def _content(value: bytes) -> str:
    return base64.b64encode(value).decode("ascii")


def _typed(kind: str, value: object) -> dict[str, object]:
    return {"kind": kind, "value": value}


def _ref(asset: EvidenceAsset) -> dict[str, str]:
    return {
        "record_kind": RuntimeRecordKind.EVIDENCE.value,
        "record_id": asset.evidence_id,
    }


def _mask_payload(
    asset: EvidenceAsset, coordinates: CoordinateSystem
) -> dict[str, object]:
    dimensions = coordinates.dimensions
    return _typed(
        "record",
        {
            "evidence_id": _typed("string", asset.evidence_id),
            "sha256": _typed("string", asset.sha256),
            "source_media_sha256": _typed(
                "string",
                cast(DerivedImageEvidence, asset.spatial).original_media_ref.sha256,
            ),
            "coordinate_system": _typed(
                "record",
                {
                    "coordinate_system_id": _typed(
                        "string", coordinates.coordinate_system_id
                    ),
                    "frame": _typed("enum", coordinates.frame),
                    "dimensions": _typed(
                        "record",
                        {
                            "width": _typed("integer", dimensions.width),
                            "height": _typed("integer", dimensions.height),
                        },
                    ),
                },
            ),
        },
    )


def _operation(dispatch: ToolDispatch) -> str:
    name = dispatch.tool_ref.artifact_id
    if (
        name not in OPERATIONS
        or dispatch.capability_id != f"synthetic.cpu.{name}"
        or dispatch.contract_version != VERSION
        or dispatch.implementation_version != VERSION
        or dispatch.invoke_model is not None
        or dispatch.object_ref is None
    ):
        raise ValueError("synthetic CPU exchange requires an explicit scene Tool")
    return name


def _check_scope(
    dispatch: ToolDispatch, asset: EvidenceAsset, *, original: bool = False
) -> None:
    if (
        asset.scope.execution_id != dispatch.execution_id
        or (
            isinstance(asset.scope, ObjectEvidenceScope)
            and (asset.scope.profile_ref, asset.scope.object_ref)
            != (dispatch.profile_ref, dispatch.object_ref)
        )
        or (not original and not isinstance(asset.scope, ObjectEvidenceScope))
    ):
        raise ValueError("geometry evidence is unavailable in the exact object scope")


@dataclass(frozen=True, slots=True)
class GeometryInput:
    """Trusted per-call media, selected children, and verified Region projection."""

    media: MediaAsset
    original: EvidenceBytes
    stage: StageGeometrySidecar
    object_bbox: BBoxXYXY | None = None
    parking_area: ParkingArea | None = None
    masks: tuple[MaskInput, ...] = ()
    member_boxes: tuple[BBoxXYXY, ...] = ()

    @property
    def ancestor_evidence(self) -> tuple[AncestorToolEvidence, ...]:
        return tuple(item.proof for item in self.masks)

    def exchange(self) -> CPUWorkerExchange:
        return CPUWorkerExchange(self._prepare, self._materialize)

    def _source(self, dispatch: ToolDispatch) -> CoordinateSystem:
        self.original.verify_bytes()
        original = self.original.asset
        _check_scope(dispatch, original, original=True)
        if not isinstance(original.spatial, OriginalImageEvidence):
            raise ValueError("geometry requires the original image evidence")
        original.validate_original_media(self.media)
        if (original.storage, original.stage) != (
            self.media.storage,
            self.media.stage,
        ) or (
            original.spatial.coordinate_system != dispatch.original_coordinates
            or self.media.media_ref != dispatch.original_media_ref
            or isinstance(original.access_scope, TaskEvidenceAccess)
            and original.access_scope.task_id != dispatch.task_id
        ):
            raise ValueError(
                "original image evidence requires the exact dispatch source and frame"
            )
        return original.spatial.coordinate_system

    def _prepare(self, dispatch: ToolDispatch) -> bytes:
        operation = _operation(dispatch)
        coordinates = self._source(dispatch)
        dimensions = coordinates.dimensions
        request = _json(dispatch.request)
        physical: dict[str, object] = {
            "dimensions": (dimensions.width, dimensions.height)
        }
        if operation == "wheel_boundary_geometry":
            region = self.parking_area
            if (
                region is None
                or region.coordinate_system != coordinates
                or region.model_dump(mode="json") != request.get("parking_area")
                or len(self.masks) != len(_MASK_FIELDS)
                or {item.proof.input_path for item in self.masks}
                != {(name,) for name in _MASK_FIELDS}
            ):
                raise ValueError(
                    "geometry requires exact selected masks and a verified Region"
                )
            physical["bbox"] = region.bbox.xyxy
            for item in self.masks:
                asset = item.proof.asset
                EvidenceBytes(asset, item.content).verify_bytes()
                asset.validate_original_media(self.media)
                _check_scope(dispatch, asset)
                path = item.proof.input_path[0]
                spatial = asset.spatial
                if (
                    asset.evidence_type is not EvidenceType.MASK
                    or asset.mime_type != "image/png"
                    or asset.access_scope != self.original.asset.access_scope
                    or not isinstance(spatial, DerivedImageEvidence)
                    or len(spatial.transforms) != 1
                    or spatial.transforms[0].source != coordinates
                    or spatial.coordinate_system.dimensions != dimensions
                    or spatial.transforms[0].affine != _IDENTITY_AFFINE
                    or (
                        spatial.transforms[0].implementation_id,
                        spatial.transforms[0].implementation_version,
                    )
                    != (f"synthetic.cpu.{asset.producer_id}", asset.producer_version)
                    or _mask_payload(asset, coordinates) != request.get(path)
                    or not any(
                        link.artifact_ref.record_kind is RuntimeRecordKind.EVIDENCE
                        and link.artifact_ref.record_id == asset.evidence_id
                        and link.digest == asset.sha256
                        for link in dispatch.input_refs
                    )
                ):
                    raise ValueError(
                        "geometry mask is not the exact source-aligned input"
                    )
                physical[path] = _content(item.content)
        else:
            if (
                request.get("image") != self.media.media_ref.model_dump(mode="json")
                or self.masks
            ):
                raise ValueError("segmentation needs the exact pinned media input")
            physical["image"] = _content(self.original.content)
            if operation == "wheel_segmentation":
                box = self.object_bbox
                if (
                    box is None
                    or box != dispatch.object_bbox
                    or box.model_dump(mode="json") != request.get("bbox")
                ):
                    raise ValueError("wheel mask requires the exact Object bbox")
                box.validate_bounds(dimensions)
                physical["bbox"] = box.xyxy
        return _wire(physical)

    async def _stage(
        self,
        dispatch: ToolDispatch,
        coordinates: CoordinateSystem,
        slot: str,
        evidence_type: EvidenceType,
        mime: str,
        content: bytes,
    ) -> EvidenceAsset:
        target = CoordinateSystem(
            frame="derived_image",
            coordinate_system_id=f"{dispatch.tool_execution_id}.{slot}",
            dimensions=coordinates.dimensions,
        )
        spatial = DerivedImageEvidence(
            kind="derived_image",
            original_media_ref=self.media.media_ref,
            transforms=(
                TransformRecord(
                    source=coordinates,
                    target=target,
                    implementation_id=dispatch.capability_id,
                    implementation_version=dispatch.implementation_version,
                    affine=_IDENTITY_AFFINE,
                ),
            ),
        )
        asset = await self.stage(
            dispatch,
            GeometrySidecar(
                slot, evidence_type, mime, content, spatial, sha256(content).hexdigest()
            ),
        )
        EvidenceBytes(asset, content).verify_bytes()
        _check_scope(dispatch, asset)
        if (
            (
                asset.evidence_type,
                asset.mime_type,
                asset.spatial,
                asset.producer_id,
                asset.producer_version,
            )
            != (
                evidence_type,
                mime,
                spatial,
                dispatch.tool_ref.artifact_id,
                dispatch.implementation_version,
            )
            or asset.access_scope != self.original.asset.access_scope
            or asset.evidence_id
            in {
                self.original.asset.evidence_id,
                *(item.proof.asset.evidence_id for item in self.masks),
            }
        ):
            raise ValueError("staged geometry evidence changed bytes or provenance")
        return asset

    async def _materialize(
        self,
        dispatch: ToolDispatch,
        content: bytes,
    ) -> tuple[bytes, tuple[EvidenceAsset, ...]]:
        operation = _operation(dispatch)
        coordinates = self._source(dispatch)
        result = _json(content)
        dimensions = coordinates.dimensions.width, coordinates.dimensions.height
        reason = None
        if operation == "wheel_boundary_geometry":
            geometry, relation, reason = _geometry_report(dispatch, content)
            sources = tuple(item.proof.asset for item in self.masks)
            asset = await self._stage(
                dispatch,
                coordinates,
                GEOMETRY_SLOT,
                EvidenceType.GEOMETRY,
                "application/json",
                geometry,
            )
            names = (
                *((item.proof.input_path[0], item.proof.asset) for item in self.masks),
                (GEOMETRY_SLOT, asset),
            )
            output, value = RELATION, _typed("enum", relation)
        else:
            if result.keys() != {"png", "count"} or not isinstance(
                png := result["png"], str
            ):
                raise ValueError("invalid CPU segmentation result shape")
            mask = base64.b64decode(png, validate=True)
            image = load_image(mask, dimensions, mask=True)
            mask_pixels = image.tobytes()
            count = mask_pixels.count(255)
            if type(result["count"]) is not int or result["count"] != count:
                raise ValueError("CPU segmentation mask count mismatch")
            output = WHEEL_MASK if operation == "wheel_segmentation" else TACTILE_MASK
            if output == WHEEL_MASK:
                if self.object_bbox is None:
                    raise ValueError("wheel mask lacks its Object bbox")
                x0, y0, x1, y1 = self.object_bbox.xyxy
                for index, pixel in enumerate(mask_pixels):
                    if pixel == 255:
                        y, x = divmod(index, dimensions[0])
                        if not (x0 <= x + 0.5 < x1 and y0 <= y + 0.5 < y1):
                            raise ValueError(
                                "wheel mask extends beyond its Object bbox"
                            )
            asset = await self._stage(
                dispatch, coordinates, output, EvidenceType.MASK, "image/png", mask
            )
            sources = ()
            names = ((output, asset),)
            value = _mask_payload(asset, coordinates)
            if not count:
                reason = "empty_segmentation"

        evidence = self.original.asset, *sources, asset
        refs = [_ref(source) for source in evidence]
        state: dict[str, object] = {
            "output_name": output,
            "fact_status": "known" if reason is None else "unknown",
            "evidence_refs": refs,
        }
        body: dict[str, object] = {
            "status": "succeeded" if reason is None else "partial",
            "diagnostics": [],
            "evidence_refs": refs,
            "evidence": [
                {"name": name, "evidence_refs": [_ref(source)]}
                for name, source in names
            ],
            "output_states": [state],
        }
        if reason is None:
            body["payload"] = _typed("record", {output: value})
        else:
            state["reason"] = {
                "reason_code": reason,
                "message": "Synthetic CPU result is inconclusive.",
                "diagnostic_refs": [_ref(asset)],
            }
        return _wire(body), evidence


def _metrics(raw: dict[str, object], pixels: int) -> dict[str, int]:
    data = raw.get("metrics")
    if (
        raw.keys() != {"metrics", "relation", "reason"}
        or not isinstance(data, dict)
        or set(data) != _METRICS
        or any(
            type(value) is not int or not 0 <= value <= min(MAX_PIXELS, pixels)
            for value in data.values()
        )
    ):
        raise ValueError("invalid CPU geometry metrics")
    metrics = cast(dict[str, int], data)
    if metrics["overlap"] > min(metrics["wheel"], metrics["tactile"]) or metrics[
        "wheel"
    ] != sum(metrics[key] for key in ("inside", "outside", "crossing", "touching")):
        raise ValueError("inconsistent CPU geometry metrics")
    return metrics


def _base64_bytes(raw: object) -> bytes:
    if not isinstance(raw, str):
        raise ValueError("CPU image wire must hold base64 bytes")
    return base64.b64decode(raw, validate=True)


def _mask_claim(raw: object) -> tuple[str, str]:
    if isinstance(raw, dict) and (fields := raw.get("value")) is not None:
        if isinstance(fields, dict):
            identity, digest = fields.get("evidence_id"), fields.get("sha256")
            if isinstance(identity, dict) and isinstance(digest, dict):
                record_id, sha = identity.get("value"), digest.get("value")
                if isinstance(record_id, str) and isinstance(sha, str):
                    return record_id, sha
    raise ValueError("physical mask requires its exact typed logical identity")


def _physical_input(
    approved: frozenset[Digest],
    dispatch: ToolDispatch,
    content: bytes,
) -> None:
    operation = _operation(dispatch)
    original = dispatch.original_media_ref
    source = dispatch.original_coordinates
    if original is None or source is None or original.sha256 not in approved:
        raise ValueError("the original image is not an approved synthetic PNG")
    logical, physical = _json(dispatch.request), _json(content)
    dimensions = physical.get("dimensions")
    if (
        not isinstance(dimensions, list)
        or any(type(item) is not int for item in dimensions)
        or dimensions != [source.dimensions.width, source.dimensions.height]
    ):
        raise ValueError("physical CPU dimensions differ from the authorized source")
    if operation == "wheel_boundary_geometry":
        if physical.keys() != {"dimensions", "bbox", *_MASK_FIELDS}:
            raise ValueError(
                "physical geometry wire lacks its exact mask and rectangle slots"
            )
        region = ParkingArea.model_validate(logical.get("parking_area"))
        if region.coordinate_system != source or physical["bbox"] != list(
            region.bbox.xyxy
        ):
            raise ValueError(
                "physical geometry rectangle differs from its pinned input"
            )
        for slot in _MASK_FIELDS:
            evidence_id, digest = _mask_claim(logical.get(slot))
            bitmap = _base64_bytes(physical[slot])
            if (
                sha256(bitmap).hexdigest() != digest
                or not bitmap.startswith(b"\x89PNG\r\n\x1a\n")
                or not any(
                    item.artifact_ref.record_kind is RuntimeRecordKind.EVIDENCE
                    and item.artifact_ref.record_id == evidence_id
                    and item.digest == digest
                    for item in dispatch.input_refs
                )
            ):
                raise ValueError(
                    "CPU mask wire differs from the selected ancestor slot"
                )
        return

    fields = {"dimensions", "image"}
    if operation == "wheel_segmentation":
        fields.add("bbox")
        box = dispatch.object_bbox
        if (
            box is None
            or physical.get("bbox") != list(box.xyxy)
            or logical.get("bbox") != box.model_dump(mode="json")
        ):
            raise ValueError("physical wheel bbox differs from the bound Object")
    if physical.keys() != fields or logical.get("image") != original.model_dump(
        mode="json"
    ):
        raise ValueError("physical CPU image wire does not bind the original")
    image = _base64_bytes(physical.get("image"))
    if sha256(image).hexdigest() != original.sha256 or not image.startswith(
        b"\x89PNG\r\n\x1a\n"
    ):
        raise ValueError("physical CPU image differs from the approved synthetic PNG")


def _geometry_report(
    dispatch: ToolDispatch,
    content: bytes,
) -> tuple[bytes, str | None, str | None]:
    logical = _json(dispatch.request)
    region = ParkingArea.model_validate(logical.get("parking_area"))
    source = dispatch.original_coordinates
    original = dispatch.original_media_ref
    physical = dispatch.physical_request
    if (
        source is None
        or original is None
        or physical is None
        or dispatch.object_ref is None
    ):
        raise ValueError("geometry processing requires its exact dispatch inputs")
    wire = _json(content)
    metrics = _metrics(wire, source.dimensions.width * source.dimensions.height)
    relation, reason = classify(metrics)
    if (relation, reason) != (wire["relation"], wire["reason"]):
        raise ValueError("CPU geometry relation does not match its measurements")
    return (
        _wire(
            {
                "schema_version": VERSION,
                "tool_execution_id": dispatch.tool_execution_id,
                "execution_id": dispatch.execution_id,
                "attempt_id": dispatch.attempt_id,
                "object_ref": dispatch.object_ref.model_dump(mode="json"),
                "profile_ref": dispatch.profile_ref.model_dump(mode="json"),
                "request_sha256": sha256(physical).hexdigest(),
                "response_sha256": sha256(content).hexdigest(),
                "implementation": dispatch.implementation_proof_ref.model_dump(
                    mode="json"
                ),
                "parking_area": region.model_dump(mode="json"),
                "original_sha256": original.sha256,
                "inputs": {
                    name: {"evidence_id": reference, "sha256": digest}
                    for name in _MASK_FIELDS
                    for reference, digest in (_mask_claim(logical.get(name)),)
                },
                "metrics": metrics,
                "relation": relation,
                "reason": reason,
            }
        ),
        relation,
        reason,
    )


def _physical_result(
    dispatch: ToolDispatch,
    content: bytes,
    normalized: ToolResult,
    evidence: tuple[EvidenceAsset, ...],
) -> None:
    operation = _operation(dispatch)
    source = dispatch.original_coordinates
    if source is None or isinstance(normalized, FailedToolResult):
        raise ValueError("physical CPU result requires a normalized evidence envelope")
    relation: str | None = None
    slot: str
    output: str
    if operation == "wheel_boundary_geometry":
        material, relation, reason = _geometry_report(dispatch, content)
        slot, output, evidence_type = GEOMETRY_SLOT, RELATION, EvidenceType.GEOMETRY
        known = reason is None
    else:
        slot = WHEEL_MASK if operation == "wheel_segmentation" else TACTILE_MASK
        output, evidence_type = slot, EvidenceType.MASK
        raw = _json(content)
        if raw.keys() != {"png", "count"}:
            raise ValueError("physical mask response has an invalid shape")
        material = _base64_bytes(raw.get("png"))
        image = load_image(
            material, (source.dimensions.width, source.dimensions.height), mask=True
        )
        count = raw["count"]
        if type(count) is not int or count != image.tobytes().count(255):
            raise ValueError("physical mask count does not match its PNG")
        known = count > 0
        reason = None if known else "empty_segmentation"
    matching = next(
        (
            asset
            for asset in evidence
            if any(
                name.name == slot
                and name.evidence_refs[0].record_id == asset.evidence_id
                and len(name.evidence_refs) == 1
                for name in normalized.evidence
            )
        ),
        None,
    )
    if (
        matching is None
        or matching.evidence_type is not evidence_type
        or matching.sha256 != sha256(material).hexdigest()
        or matching.byte_size != len(material)
        or matching.producer_id != dispatch.tool_ref.artifact_id
        or matching.producer_version != dispatch.implementation_version
        or len(normalized.output_states) != 1
        or normalized.output_states[0].output_name != output
        or isinstance(normalized.output_states[0], KnownToolOutputState) != known
        or normalized.status != ("succeeded" if known else "partial")
    ):
        raise ValueError(
            "normalized CPU evidence must match this exact physical output"
        )
    value = (
        _typed("enum", relation)
        if operation == "wheel_boundary_geometry"
        else _mask_payload(matching, source)
    )
    if known:
        expected = _typed("record", {output: value})
        if (
            normalized.payload is None
            or normalized.payload.model_dump(mode="json") != expected
        ):
            raise ValueError(
                "normalized CPU Fact must match this exact physical output"
            )
    else:
        state = normalized.output_states[0]
        if (
            normalized.payload is not None
            or not isinstance(state, NonKnownToolOutputState)
            or state.fact_status is not FactStatus.UNKNOWN
            or state.reason.reason_code != reason
            or not any(
                ref.record_id == matching.evidence_id
                for ref in state.reason.diagnostic_refs
            )
        ):
            raise ValueError(
                "nonknown physical output must preserve its exact diagnostic"
            )


def synthetic_cpu_implementations(
    admission: ResourceAdmission,
    scope_id: str,
    owner_id: str,
    tools: tuple[ToolDefinition, ...],
    proofs: Mapping[str, TrustPin],
    *,
    synthetic_image_digests: frozenset[Digest] = frozenset(),
) -> tuple[ToolImplementation, ...]:
    """Register only the approved local synthetic capabilities and exact command."""
    entries = []
    for definition in tools:
        requirement = definition.implementation_requirement
        operation = definition.tool_id
        expected = f"synthetic.cpu.{operation}"
        proof = proofs.get(expected)
        if (
            operation not in OPERATIONS
            or requirement.capability_id != expected
            or requirement.contract_version != VERSION
            or VERSION not in requirement.allowed_implementation_versions
            or definition.provider_kind is not ToolProviderKind.BUILTIN
            or definition.model_binding_ref is not None
            or proof is None
            or (proof.identity, proof.version) != (expected, VERSION)
        ):
            raise ValueError("synthetic CPU tool was not explicitly approved")
        worker = IsolatedCPUWorker(
            admission,
            (sys.executable, "-I", str(CPU_ARTIFACT), operation),
            scope_id,
            owner_id,
            VERSION,
            proof,
            CPU_ARTIFACT,
            mediated=True,
        )
        entries.append(
            ToolImplementation(
                expected,
                VERSION,
                VERSION,
                proof,
                definition.provider_kind,
                definition.input_schema_ref,
                definition.output_schema_ref,
                "cpu",
                worker,
                physical_input_check=partial(_physical_input, synthetic_image_digests),
                physical_result_check=_physical_result,
            )
        )
    return tuple(entries)
