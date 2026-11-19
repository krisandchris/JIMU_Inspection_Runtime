"""Strict versioned roots and discriminated replay branches round-trip offline."""

import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator
from jsonschema import ValidationError as SchemaValidationError
from pydantic import TypeAdapter, ValidationError

from jimu_runtime.domain.definitions.geometry import GeometryResourceDefinition
from jimu_runtime.domain.runtime.replay import (
    ArtifactReplayRequest,
    InferenceReplayRequest,
    ReplayRequest,
    ReplayRun,
)

FIXTURES = Path(__file__).parents[1] / "fixtures/wp01c/geometry_replay"


@pytest.mark.parametrize(
    ("contract", "name"),
    [
        (GeometryResourceDefinition, "geometry"),
        (ArtifactReplayRequest, "artifact-replay-request"),
        (InferenceReplayRequest, "inference-replay-request"),
        (ReplayRequest, "artifact-replay-request"),
        (ReplayRequest, "inference-replay-request"),
        (ReplayRun, "artifact-replay-run"),
        (ReplayRun, "inference-replay-run"),
    ],
)
def test_strict_contract_pairs(contract: object, name: str) -> None:
    adapter = TypeAdapter(contract)
    schema = adapter.json_schema()
    Draft202012Validator.check_schema(schema)
    validator = Draft202012Validator(schema)
    valid = (FIXTURES / f"{name}-valid.json").read_bytes()
    invalid = (FIXTURES / f"{name}-invalid.json").read_bytes()
    payload = json.loads(valid)
    validator.validate(payload)
    parsed = adapter.validate_json(valid)
    assert adapter.validate_json(adapter.dump_json(parsed)) == parsed
    validator.validate(adapter.dump_python(parsed, mode="json"))

    missing_version = {
        key: value for key, value in payload.items() if key != "schema_version"
    }
    for rejected in (
        invalid,
        json.dumps(missing_version),
        json.dumps(payload | {"extra": 0}),
    ):
        with pytest.raises(ValidationError):
            adapter.validate_json(rejected)
        with pytest.raises(SchemaValidationError):
            validator.validate(json.loads(rejected))


@pytest.mark.parametrize("contract", [ReplayRequest, ReplayRun])
def test_replay_unions_discriminate_by_kind(contract: object) -> None:
    schema = TypeAdapter(contract).json_schema()
    assert schema["discriminator"]["propertyName"] == "kind"
    assert set(schema["discriminator"]["mapping"]) == {"artifact", "inference"}
    for branch in schema["$defs"].values():
        if branch.get("properties", {}).get("kind", {}).get("const") in {
            "artifact",
            "inference",
        }:
            assert branch["additionalProperties"] is False
