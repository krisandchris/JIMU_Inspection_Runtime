"""Source-derived JSON Schema checks against deterministic intake wire data."""

import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator, FormatChecker
from jsonschema import ValidationError as SchemaValidationError
from pydantic import ValidationError

from jimu_runtime.domain.base import ContractModel
from jimu_runtime.domain.runtime.evidence import EvidenceAsset
from jimu_runtime.domain.runtime.intake import (
    AcceptedTask,
    ContextSnapshot,
    TaskSubmission,
)
from jimu_runtime.domain.runtime.media import MediaAsset

FIXTURES = Path(__file__).parents[1] / "fixtures/wp01c/intake"


@pytest.mark.parametrize(
    ("model", "fixture"),
    [
        (TaskSubmission, "task-submission"),
        (AcceptedTask, "accepted-task"),
        (MediaAsset, "media-asset"),
        (ContextSnapshot, "context-snapshot"),
        (EvidenceAsset, "evidence-asset"),
    ],
)
def test_strict_persisted_root(model: type[ContractModel], fixture: str) -> None:
    schema = model.model_json_schema()
    Draft202012Validator.check_schema(schema)
    assert schema["additionalProperties"] is False
    assert "schema_version" in schema["required"]
    validator = Draft202012Validator(schema, format_checker=FormatChecker())
    valid = (FIXTURES / f"{fixture}-valid.json").read_bytes()
    invalid = (FIXTURES / f"{fixture}-invalid.json").read_bytes()

    validator.validate(json.loads(valid))
    parsed = model.model_validate_json(valid)
    validator.validate(parsed.model_dump(mode="json"))
    assert model.model_validate_json(parsed.model_dump_json()) == parsed

    with pytest.raises(SchemaValidationError):
        validator.validate(json.loads(invalid))
    with pytest.raises(ValidationError):
        model.model_validate_json(invalid)


def test_snapshot_uses_five_discriminated_provenance_sources() -> None:
    provenance = ContextSnapshot.model_json_schema()["$defs"]["ContextValueProvenance"]
    assert provenance["discriminator"]["propertyName"] == "source"
    assert set(provenance["discriminator"]["mapping"]) == {
        "trusted_task_metadata",
        "device_registry",
        "camera_registry",
        "region_definition",
        "declared_derivation",
    }
