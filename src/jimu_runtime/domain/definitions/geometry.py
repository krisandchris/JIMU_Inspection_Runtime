"""Bounded static Region geometry data; coordinate systems are vocabulary pins."""

from fractions import Fraction
from typing import Annotated, Literal, Self

from pydantic import AfterValidator, BeforeValidator, Field, StrictInt, model_validator

from jimu_runtime.domain.base import ContractModel
from jimu_runtime.domain.definition_support import (
    DefinitionContract,
    StaticAssetContentRef,
    VocabularyMemberRef,
    immutable_sequence,
)
from jimu_runtime.domain.primitives import ArtifactId, Version
from jimu_runtime.domain.values import MAX_LIST_ITEMS, FiniteNumber

# Keep even integer-valued JCS output in the exact binary64 integer range.
MAX_GEOMETRY_COORDINATE = 2**53 - 1
MIN_GEOMETRY_COORDINATE = -MAX_GEOMETRY_COORDINATE


def _coordinate_bound(value: int | float) -> int | float:
    if not MIN_GEOMETRY_COORDINATE <= value <= MAX_GEOMETRY_COORDINATE:
        raise ValueError("geometry coordinate exceeds the exact binary64 integer range")
    return value


type GeometryCoordinate = Annotated[
    StrictInt | FiniteNumber,
    AfterValidator(_coordinate_bound),
    Field(
        json_schema_extra={
            "minimum": MIN_GEOMETRY_COORDINATE,
            "maximum": MAX_GEOMETRY_COORDINATE,
        }
    ),
]


class GeometryPoint(ContractModel):
    x: GeometryCoordinate
    y: GeometryCoordinate


class PolygonGeometry(ContractModel):
    """One 2D exterior ring with implicit closure and no holes."""

    kind: Literal["polygon"]
    vertices: Annotated[
        tuple[GeometryPoint, ...],
        Field(
            min_length=3,
            max_length=MAX_LIST_ITEMS,
            json_schema_extra={"uniqueItems": True},
        ),
        BeforeValidator(immutable_sequence),
    ]

    @model_validator(mode="after")
    def reject_degenerate_vertices(self) -> Self:
        points = {(point.x, point.y) for point in self.vertices}
        if len(points) != len(self.vertices):
            raise ValueError("polygon vertices must be unique; closure is implicit")

        # Exact arithmetic over the supplied binary64 values avoids a scale-dependent
        # tolerance and preserves the behavior across units and coordinate systems.
        origin, second, *rest = self.vertices
        origin_x, origin_y = Fraction(origin.x), Fraction(origin.y)
        dx = Fraction(second.x) - origin_x
        dy = Fraction(second.y) - origin_y
        if all(
            dx * (Fraction(point.y) - origin_y) == dy * (Fraction(point.x) - origin_x)
            for point in rest
        ):
            raise ValueError("polygon vertices must not all be collinear")
        return self


class GeometryProvenance(ContractModel):
    source_asset_ref: StaticAssetContentRef


class GeometryResourceDefinition(DefinitionContract):
    geometry_resource_id: ArtifactId
    version: Version
    coordinate_system: VocabularyMemberRef
    units: VocabularyMemberRef
    geometry: PolygonGeometry
    provenance: GeometryProvenance
