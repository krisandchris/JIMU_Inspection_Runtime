"""Pure-data geometry and replay validation on deterministic fixtures."""

import json
from pathlib import Path
from typing import Any

import pytest
from pydantic import TypeAdapter, ValidationError

from jimu_runtime.domain.definitions.geometry import (
    MAX_GEOMETRY_COORDINATE,
    MIN_GEOMETRY_COORDINATE,
    GeometryResourceDefinition,
    PolygonGeometry,
)
from jimu_runtime.domain.fact_schemas.canonicalization import canonical_bytes
from jimu_runtime.domain.runtime.executions import ExecutionRecord
from jimu_runtime.domain.runtime.replay import (
    ArtifactReplayRun,
    InferenceReplayRun,
    ReplayKind,
    ReplayRequest,
    ReplayRun,
)
from jimu_runtime.domain.values import MAX_LIST_ITEMS

FIXTURES = Path(__file__).parents[1] / "fixtures/wp01c"


def read_fixture(name: str) -> dict[str, Any]:
    return json.loads((FIXTURES / f"geometry_replay/{name}-valid.json").read_bytes())


def test_geometry_preserves_coordinate_values_identifiers_and_source() -> None:
    wire = read_fixture("geometry")
    resource = GeometryResourceDefinition.model_validate(wire)
    vertex = resource.geometry.vertices[0]
    assert type(vertex.x) is int
    assert type(vertex.y) is float
    assert resource.model_dump(mode="json") == wire
    assert resource.units.member == "synthetic-unit"
    assert resource.coordinate_system.member == "synthetic-survey-plane"
    assert (
        resource.provenance.source_asset_ref.artifact_id == "synthetic-boundary-source"
    )
    with pytest.raises(ValidationError, match="frozen_instance"):
        vertex.x = 10


def test_geometry_max_vertices_and_binary64_noncollinearity() -> None:
    vertices = [{"x": index, "y": index**2} for index in range(MAX_LIST_ITEMS)]
    full = PolygonGeometry.model_validate({"kind": "polygon", "vertices": vertices})
    assert len(full.vertices) == MAX_LIST_ITEMS
    tiny = PolygonGeometry.model_validate(
        {
            "kind": "polygon",
            "vertices": [
                {"x": 0.0, "y": 0.0},
                {"x": 1e-300, "y": 0.0},
                {"x": 0.0, "y": 1e-300},
            ],
        }
    )
    assert len(tiny.vertices) == 3


def test_artifact_run_retains_source_and_scoring_provenance() -> None:
    wire = read_fixture("artifact-replay-run")
    result = TypeAdapter(ReplayRun).validate_json(json.dumps(wire))
    assert isinstance(result, ArtifactReplayRun)
    assert result.kind is ReplayKind.ARTIFACT
    assert (
        result.request.source
        == TypeAdapter(ReplayRequest)
        .validate_python(read_fixture("artifact-replay-request"))
        .source
    )
    assert result.request.scoring_protocol.version == "1.0.0"
    assert result.request.source_artifacts[0].ref.record_id == "source-evidence-001"
    assert result.scoring_evidence.ref.record_id == "scoring-result-001"


def test_inference_run_checks_target_task_id_and_full_pins() -> None:
    run = TypeAdapter(ReplayRun).validate_json(
        json.dumps(read_fixture("inference-replay-run"))
    )
    assert isinstance(run, InferenceReplayRun)
    assert run.kind is ReplayKind.INFERENCE
    assert (
        run.target_execution_ref.record_id != run.request.source.execution_ref.record_id
    )
    execution = json.loads((FIXTURES / "executions/contracts.json").read_bytes())[
        "execution"
    ]
    target = {
        **execution,
        "execution_id": run.target_execution_ref.record_id,
        "task_id": run.request.source.task_id,
        "pins": run.request.target_pins.model_dump(mode="json"),
    }
    run.validate_target_execution(
        ExecutionRecord.model_validate_json(json.dumps(target))
    )
    for bad in (
        {**target, "task_id": "another-task"},
        {**target, "execution_id": "another-execution"},
        {**target, "pins": {**target["pins"], "bundle_digest": "0" * 64}},
        {**target, "pins": {**target["pins"], "model_pins": []}},
    ):
        with pytest.raises(ValueError, match="identity, Task and pins"):
            run.validate_target_execution(
                ExecutionRecord.model_validate_json(json.dumps(bad))
            )


def test_replay_source_matches_historical_execution_and_full_pins() -> None:
    request = TypeAdapter(ReplayRequest).validate_python(
        read_fixture("inference-replay-request")
    )
    execution = json.loads((FIXTURES / "executions/contracts.json").read_bytes())[
        "execution"
    ]
    request.source.validate_execution(
        ExecutionRecord.model_validate_json(json.dumps(execution))
    )
    for bad in (
        {**execution, "task_id": "another-task"},
        {**execution, "execution_id": "another-execution"},
        {**execution, "pins": {**execution["pins"], "input_digest": "5" * 64}},
    ):
        with pytest.raises(
            ValueError, match="source Execution identity, Task and pins"
        ):
            request.source.validate_execution(
                ExecutionRecord.model_validate_json(json.dumps(bad))
            )


def test_geometry_roundtrips_through_jcs_at_coordinate_limits() -> None:
    wire = read_fixture("geometry")
    wire["geometry"]["vertices"] = [
        {"x": MIN_GEOMETRY_COORDINATE, "y": -0.0},
        {"x": MAX_GEOMETRY_COORDINATE, "y": 0.0},
        {"x": 0.0, "y": 1e-300},
    ]
    geometry = GeometryResourceDefinition.model_validate(wire)
    canonical = canonical_bytes(geometry.model_dump(mode="json"))
    assert GeometryResourceDefinition.model_validate_json(canonical) == geometry
