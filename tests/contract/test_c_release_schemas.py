"""Offline generated JSON Schema and strict wire roundtrips for C4 release roots."""

import json
from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest
from jsonschema import Draft202012Validator, FormatChecker
from pydantic import TypeAdapter, ValidationError

from jimu_runtime.domain.base import ContractModel
from jimu_runtime.domain.runtime.bundle_manifest import BundleManifest, ChecksumManifest
from jimu_runtime.domain.runtime.releases import (
    ActivationRecord,
    ActivationScope,
    AdmissionReceipt,
    ApprovalReceipt,
    ReleaseRecord,
)

_FIXTURES = Path(__file__).parents[1] / "fixtures/wp01c/releases"


@pytest.mark.parametrize(
    ("model", "name", "nested"),
    [
        (BundleManifest, "bundle_manifest", None),
        (ChecksumManifest, "checksums", None),
        (AdmissionReceipt, "release", "admission_receipt"),
        (ApprovalReceipt, "release", "approval_receipt"),
        (ReleaseRecord, "release", None),
        (ActivationRecord, "activation", None),
    ],
)
def test_release_root_wire_and_schema(
    model: type[ContractModel], name: str, nested: str | None
) -> None:
    raw = json.loads((_FIXTURES / f"{name}.json").read_text())
    if nested is not None:
        raw = raw[nested]
    schema = model.model_json_schema()
    Draft202012Validator.check_schema(schema)
    validator = Draft202012Validator(schema, format_checker=FormatChecker())
    validator.validate(raw)
    parsed = model.model_validate_json(json.dumps(raw))
    assert parsed.model_dump(mode="json") == raw
    assert model.model_validate(parsed) == parsed
    assert model.model_validate_json(parsed.model_dump_json()) == parsed

    missing_version = {
        key: value for key, value in raw.items() if key != "schema_version"
    }
    for invalid in (missing_version, {**raw, "arbitrary_config": {}}):
        assert not validator.is_valid(invalid)
        with pytest.raises(ValidationError):
            model.model_validate_json(json.dumps(invalid))


@pytest.mark.parametrize(
    "scope",
    [
        {"scope": "global"},
        {
            "scope": "scene",
            "scene_type": {
                "vocabulary_ref": {
                    "artifact_kind": "vocabulary",
                    "artifact_id": "scene-types",
                    "version": "1.0.0",
                    "digest": "b" * 64,
                },
                "member": "street",
            },
        },
        {"scope": "region", "region_id": "street-demo"},
        {"scope": "device", "device_id": "robot-demo-01"},
    ],
)
def test_activation_scope_branches(scope: dict[str, object]) -> None:
    adapter = TypeAdapter(ActivationScope)
    schema = adapter.json_schema()
    Draft202012Validator.check_schema(schema)
    Draft202012Validator(schema).validate(scope)
    instance = adapter.validate_python(scope)
    assert adapter.dump_python(instance, mode="json") == scope
    assert adapter.validate_json(adapter.dump_json(instance)) == instance


@pytest.mark.parametrize(
    ("model", "fixture", "path", "row_identity", "maximum"),
    [
        (
            BundleManifest,
            "bundle_manifest",
            ("artifacts",),
            ("content_ref", "artifact_id"),
            16_384,
        ),
        (
            BundleManifest,
            "bundle_manifest",
            ("source", "artifacts"),
            ("artifact_id",),
            16_384,
        ),
        (
            BundleManifest,
            "bundle_manifest",
            ("integrity", "signatures"),
            ("signature_id",),
            64,
        ),
        (
            BundleManifest,
            "bundle_manifest",
            ("required_capabilities",),
            ("capability_id",),
            1024,
        ),
        (ChecksumManifest, "checksums", ("entries",), ("path",), 16_384),
        (
            AdmissionReceipt,
            "release",
            ("capability_report", "required"),
            ("requirement", "capability_id"),
            1024,
        ),
        (
            AdmissionReceipt,
            "release",
            ("capability_report", "optional"),
            ("requirement", "capability_id"),
            1024,
        ),
        (AdmissionReceipt, "release", ("errors",), ("code",), 128),
        (AdmissionReceipt, "release", ("warnings",), ("code",), 128),
    ],
    ids=[
        "inventory",
        "source-artifacts",
        "signatures",
        "requirements",
        "checksums",
        "required-assessments",
        "optional-assessments",
        "errors",
        "warnings",
    ],
)
def test_array_budget_in_pydantic_and_json_schema(
    model: type[ContractModel],
    fixture: str,
    path: tuple[str, ...],
    row_identity: tuple[str, ...],
    maximum: int,
) -> None:
    raw = json.loads((_FIXTURES / f"{fixture}.json").read_text())
    if model is AdmissionReceipt:
        raw = raw["admission_receipt"]
    container = raw
    for key in path[:-1]:
        container = container[key]
    field = path[-1]
    # The accepted receipt fixture keeps errors empty. A rejection uses an
    # ordinary structured issue, with the same shape as a warning.
    template = raw["warnings"][0] if field == "errors" else container[field][0]
    validator = Draft202012Validator(model.model_json_schema())

    container[field] = []
    validator.validate(raw)
    model.model_validate_json(json.dumps(raw))

    def row(index: int) -> dict[str, Any]:
        entry = deepcopy(template)
        identity = entry
        for key in row_identity[:-1]:
            identity = identity[key]
        identity[row_identity[-1]] = f"item-{index:05d}"
        if path == ("artifacts",):
            entry["json_pointer"] = f"/{index}"
        return entry

    if field == "errors":
        raw["accepted"] = False
    container[field] = [row(index) for index in range(maximum)]
    validator.validate(raw)
    model.model_validate_json(json.dumps(raw))

    container[field].append(row(maximum))
    error = next(validator.iter_errors(raw))
    assert error.validator == "maxItems"
    assert tuple(error.absolute_path) == path
    with pytest.raises(ValidationError) as exc:
        model.model_validate_json(json.dumps(raw))
    violation = exc.value.errors()[0]
    assert violation["type"] == "too_long"
    assert violation["loc"] == path
