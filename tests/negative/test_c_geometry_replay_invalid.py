"""Geometry budgets, provenance and distinct replay branch invariants."""

import json
from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest
from jsonschema import Draft202012Validator
from pydantic import TypeAdapter, ValidationError

from jimu_runtime.domain.definitions.geometry import (
    MAX_GEOMETRY_COORDINATE,
    MIN_GEOMETRY_COORDINATE,
    GeometryPoint,
    GeometryResourceDefinition,
    PolygonGeometry,
)
from jimu_runtime.domain.runtime.replay import ReplayRequest, ReplayRun
from jimu_runtime.domain.values import MAX_LIST_ITEMS

FIXTURES = Path(__file__).parents[1] / "fixtures/wp01c/geometry_replay"


def read_fixture(name: str) -> dict[str, Any]:
    return json.loads((FIXTURES / f"{name}-valid.json").read_bytes())


@pytest.mark.parametrize(
    "value",
    [
        True,
        "-1",
        MIN_GEOMETRY_COORDINATE - 1,
        MAX_GEOMETRY_COORDINATE + 1,
        float("nan"),
        float("inf"),
        float("-inf"),
    ],
)
def test_geometry_coordinates_are_bounded_and_strict(value: object) -> None:
    with pytest.raises(ValidationError):
        GeometryPoint.model_validate({"x": value, "y": 0})


def test_coordinate_range_matches_schema_and_preserves_integer_edges() -> None:
    wire = {"x": MIN_GEOMETRY_COORDINATE, "y": MAX_GEOMETRY_COORDINATE}
    validator = Draft202012Validator(GeometryPoint.model_json_schema())
    validator.validate(wire)
    assert GeometryPoint.model_validate(wire).model_dump(mode="json") == wire
    for invalid in ({"x": wire["x"] - 1, "y": 0}, {"x": 0, "y": wire["y"] + 1}):
        assert not validator.is_valid(invalid)
        with pytest.raises(ValidationError):
            GeometryPoint.model_validate(invalid)


def test_geometry_rejects_collinear_and_explicitly_closed_vertices() -> None:
    geometry = read_fixture("geometry")["geometry"]
    collinear = [{"x": index, "y": index * 2} for index in range(3)]
    with pytest.raises(ValidationError, match="must not all be collinear"):
        PolygonGeometry.model_validate(geometry | {"vertices": collinear})
    geometry["vertices"].append(deepcopy(geometry["vertices"][0]))
    with pytest.raises(ValidationError, match="closure is implicit"):
        PolygonGeometry.model_validate(geometry)


def test_geometry_rejects_over_budget_vertices_and_unsupported_shapes() -> None:
    vertices = [{"x": index, "y": index**2} for index in range(MAX_LIST_ITEMS + 1)]
    with pytest.raises(ValidationError, match="at most 256"):
        PolygonGeometry.model_validate({"kind": "polygon", "vertices": vertices})
    resource = read_fixture("geometry")
    for change in ({"kind": "multipolygon"}, {"holes": []}):
        invalid = resource | {"geometry": resource["geometry"] | change}
        with pytest.raises(ValidationError):
            GeometryResourceDefinition.model_validate(invalid)


@pytest.mark.parametrize(
    ("field", "replacement"),
    [
        ("coordinate_system", "raw-coordinates"),
        ("units", "pixel"),
        ("provenance", {"source_asset_ref": {"artifact_kind": "region"}}),
        ("provenance", {"source_url": "https://example.invalid"}),
    ],
)
def test_geometry_requires_closed_pinned_metadata(
    field: str, replacement: object
) -> None:
    resource = read_fixture("geometry")
    with pytest.raises(ValidationError):
        GeometryResourceDefinition.model_validate(resource | {field: replacement})


@pytest.mark.parametrize("field", ["source_artifacts", "scoring_protocol"])
def test_artifact_request_rejects_missing_immutable_inputs(field: str) -> None:
    request = read_fixture("artifact-replay-request")
    del request[field]
    with pytest.raises(ValidationError):
        TypeAdapter(ReplayRequest).validate_python(request)


def test_artifact_request_rejects_conflicting_digests_for_one_source_record() -> None:
    request = read_fixture("artifact-replay-request")
    request["source_artifacts"].append(
        request["source_artifacts"][0] | {"digest": "3" * 64}
    )
    with pytest.raises(ValidationError, match="unique by kind and ID"):
        TypeAdapter(ReplayRequest).validate_python(request)


def test_artifact_run_does_not_reuse_source_evidence_identity() -> None:
    run = read_fixture("artifact-replay-run")
    run["scoring_evidence"]["ref"] = run["request"]["source_artifacts"][0]["ref"]
    with pytest.raises(ValidationError, match="evidence must have a new identity"):
        TypeAdapter(ReplayRun).validate_json(json.dumps(run))


def test_inference_run_requires_distinct_original_and_target_execution() -> None:
    run = read_fixture("inference-replay-run")
    run["target_execution_ref"] = run["request"]["source"]["execution_ref"]
    with pytest.raises(ValidationError, match="distinct target Execution"):
        TypeAdapter(ReplayRun).validate_json(json.dumps(run))


@pytest.mark.parametrize("name", ["artifact-replay-run", "inference-replay-run"])
def test_run_rejects_wrong_request_branch_and_status_field(name: str) -> None:
    run = read_fixture(name)
    request = run["request"]
    wrong = "artifact" if request["kind"] == "inference" else "inference"
    adapter = TypeAdapter(ReplayRun)
    with pytest.raises(ValidationError):
        adapter.validate_json(json.dumps(run | {"request": request | {"kind": wrong}}))
    with pytest.raises(ValidationError):
        adapter.validate_json(json.dumps(run | {"status": "executed"}))


def test_inference_request_rejects_different_original_task_input() -> None:
    fixture = FIXTURES / "inference-replay-request-input-mismatch.json"
    with pytest.raises(
        ValidationError, match="preserve the original Task input digest"
    ):
        TypeAdapter(ReplayRequest).validate_json(fixture.read_bytes())
