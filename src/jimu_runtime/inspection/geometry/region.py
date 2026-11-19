"""Project an exact pixel-space Region and its verified rectangle asset."""

from hashlib import sha256
from typing import Literal, Self

from pydantic import model_validator

from jimu_runtime.bundle.catalogue import ArtifactCatalogue
from jimu_runtime.bundle.semantic_inputs import require_definition
from jimu_runtime.domain.base import ContractModel
from jimu_runtime.domain.definition_support import GeometryResourceContentRef
from jimu_runtime.domain.definitions.geometry import (
    GeometryResourceDefinition,
    PolygonGeometry,
)
from jimu_runtime.domain.definitions.ontology import RegionDefinition
from jimu_runtime.domain.definitions.resources import StaticAssetDescriptor
from jimu_runtime.domain.primitives import ArtifactId, Digest, Version
from jimu_runtime.domain.runtime.media import BBoxXYXY, CoordinateSystem
from jimu_runtime.domain.tool_mappings import RegionContentRef


class ParkingArea(ContractModel):
    region_id: ArtifactId
    region_version: Version
    region_sha256: Digest
    geometry_resource_id: ArtifactId
    geometry_version: Version
    geometry_sha256: Digest
    source_asset_sha256: Digest
    coordinate_system: CoordinateSystem
    units: Literal["pixel"]
    bbox: BBoxXYXY

    @model_validator(mode="after")
    def original_pixel_rectangle(self) -> Self:
        if self.coordinate_system.frame != "original_image":
            raise ValueError("parking area must use the verified original-image frame")
        self.bbox.validate_bounds(self.coordinate_system.dimensions)
        return self


class _SourceRectangle(ContractModel):
    schema_version: Literal["1.0.0"]
    coordinate_system: Literal["original_image"]
    unit: Literal["pixel"]
    width: int
    height: int
    geometry: PolygonGeometry


def verify_parking_area(
    *,
    catalogue: ArtifactCatalogue,
    region_ref: RegionContentRef,
    region: RegionDefinition,
    geometry_ref: GeometryResourceContentRef,
    geometry: GeometryResourceDefinition,
    source: StaticAssetDescriptor,
    source_bytes: bytes,
    coordinates: CoordinateSystem,
) -> ParkingArea:
    """Verify the source asset and project definitions from an admitted Catalogue."""
    source_entry = catalogue.resolve(geometry.provenance.source_asset_ref)
    if (
        region != require_definition(catalogue, region_ref, RegionDefinition)
        or geometry
        != require_definition(catalogue, geometry_ref, GeometryResourceDefinition)
        or source != source_entry.value
        or source_bytes != source_entry.asset_bytes
        or (region.region_id, region.version)
        != (region_ref.artifact_id, region_ref.version)
        or region.geometry_ref != geometry_ref
        or (geometry.geometry_resource_id, geometry.version)
        != (geometry_ref.artifact_id, geometry_ref.version)
        or geometry.provenance.source_asset_ref not in region.asset_refs
        or (source.asset_id, source.version)
        != (
            geometry.provenance.source_asset_ref.artifact_id,
            geometry.provenance.source_asset_ref.version,
        )
        or geometry.units.member != "pixel"
        or geometry.coordinate_system.member != "original_image"
        or source.mime_type != "application/json"
        or not 0 < len(source_bytes) <= 16_384
        or len(source_bytes) != source.byte_size
        or sha256(source_bytes).hexdigest() != source.sha256
    ):
        raise ValueError("parking area is missing the exact pinned pixel asset")
    raw = _SourceRectangle.model_validate_json(source_bytes)
    if raw.geometry != geometry.geometry or (raw.width, raw.height) != (
        coordinates.dimensions.width,
        coordinates.dimensions.height,
    ):
        raise ValueError("rectangle asset disagrees with the admitted source frame")
    points = [(point.x, point.y) for point in raw.geometry.vertices]
    if len(points) != 4:
        raise ValueError("synthetic geometry requires an axis-aligned source rectangle")
    x0, x1 = min(point[0] for point in points), max(point[0] for point in points)
    y0, y1 = min(point[1] for point in points), max(point[1] for point in points)
    if set(points) != {(x0, y0), (x0, y1), (x1, y0), (x1, y1)} or any(
        x != next_x and y != next_y
        for (x, y), (next_x, next_y) in zip(
            points, (*points[1:], points[0]), strict=True
        )
    ):
        raise ValueError("synthetic geometry requires an axis-aligned source rectangle")
    return ParkingArea(
        region_id=region_ref.artifact_id,
        region_version=region_ref.version,
        region_sha256=region_ref.digest,
        geometry_resource_id=geometry_ref.artifact_id,
        geometry_version=geometry_ref.version,
        geometry_sha256=geometry_ref.digest,
        source_asset_sha256=source.sha256,
        coordinate_system=coordinates,
        units="pixel",
        bbox=BBoxXYXY(xyxy=(float(x0), float(y0), float(x1), float(y1))),
    )
