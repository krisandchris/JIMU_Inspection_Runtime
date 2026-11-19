"""Reject contradictory release evidence and ambiguous Bundle metadata."""

import json
from pathlib import Path
from typing import Any

import pytest
from jsonschema import Draft202012Validator
from pydantic import ValidationError

from jimu_runtime.domain.base import ContractModel
from jimu_runtime.domain.runtime.bundle_manifest import (
    BundleManifest,
    ChecksumEntry,
    ChecksumManifest,
)
from jimu_runtime.domain.runtime.releases import (
    ActivationRecord,
    AdmissionReceipt,
    ReleaseRecord,
)

_FIXTURES = Path(__file__).parents[1] / "fixtures/wp01c/releases"


def _raw(name: str) -> dict[str, Any]:
    return json.loads((_FIXTURES / f"{name}.json").read_text())


@pytest.mark.parametrize(
    "path",
    [
        "manifest.json",
        "checksums.json",
        "",
        "/x",
        ".",
        "..",
        "a/../x",
        "a/./x",
        "a//x",
        "a/",
        "a\\x",
        "C:/x",
        "file\n",
    ],
)
def test_payload_paths_are_normalized_and_exclude_metadata(path: str) -> None:
    raw = {"path": path, "size": 0, "sha256": "0" * 64}
    assert not Draft202012Validator(ChecksumEntry.model_json_schema()).is_valid(raw)
    with pytest.raises(ValidationError):
        ChecksumEntry.model_validate(raw)


@pytest.mark.parametrize(
    ("path", "value", "message"),
    [
        (
            ("artifacts", 1, "content_ref", "artifact_id"),
            "nonmotor-vehicle",
            "unique kind/id/version",
        ),
        (("artifacts", 1, "json_pointer"), "/0", "locators overlap"),
        (("artifacts", 1, "json_pointer"), "", "locators overlap"),
        (("artifacts", 1, "json_pointer"), "#fragment", "pattern"),
        (("artifacts", 1, "json_pointer"), "/bad~2token", "pattern"),
        (("artifacts", 1, "json_pointer"), None, "absent rather than null"),
        (
            ("artifacts", 1, "path"),
            "resources/assets/blob.bin",
            "requires a JSON payload",
        ),
        (
            ("compatibility", "minimum_runtime_version_inclusive"),
            "10.0.0",
            "nonempty and ordered",
        ),
        (("integrity", "canonicalization_id"), "other", "jcs-sha256-v1"),
        (("integrity", "root_digest"), "A" * 64, "pattern"),
        (("integrity", "signatures", 0, "public_key"), "inline", "extra_forbidden"),
    ],
)
def test_manifest_rejects_ambiguous_or_undeclared_data(
    path: tuple[str | int, ...], value: object, message: str
) -> None:
    raw = _raw("bundle_manifest")
    target: Any = raw
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = value
    with pytest.raises(ValidationError, match=message):
        BundleManifest.model_validate_json(json.dumps(raw))


def test_checksum_entries_are_strict_unique_and_sorted() -> None:
    raw = _raw("checksums")
    entries = raw["entries"]
    for invalid in (
        [entries[1], entries[0]],
        [entries[0], entries[0]],
        [{**entries[0], "size": True}],
    ):
        with pytest.raises(ValidationError):
            ChecksumManifest.model_validate({**raw, "entries": invalid})


@pytest.mark.parametrize("status", ["unsupported", "not_evaluated"])
def test_required_capability_state_is_never_silently_promoted(status: str) -> None:
    admission = _raw("release")["admission_receipt"]
    required = admission["capability_report"]["required"][0]
    required.pop("implementation_version")
    required["status"] = status
    with pytest.raises(ValidationError, match="required capabilities to be supported"):
        AdmissionReceipt.model_validate_json(json.dumps(admission))
    admission["accepted"] = False
    with pytest.raises(ValidationError, match="rejected receipts require errors"):
        AdmissionReceipt.model_validate_json(json.dumps(admission))
    admission["errors"] = admission["warnings"]
    assert not AdmissionReceipt.model_validate_json(json.dumps(admission)).accepted


@pytest.mark.parametrize(
    ("model", "fixture", "path", "value", "message"),
    [
        (ReleaseRecord, "release", ("status",), "active", "valid ReleaseStatus"),
        (ReleaseRecord, "release", ("status",), "submitted", "forbid receipts"),
        (ReleaseRecord, "release", ("status",), "rejected", "matching its outcome"),
        (
            ReleaseRecord,
            "release",
            ("status",),
            "accepted",
            "does not permit business approval",
        ),
        (
            ReleaseRecord,
            "release",
            ("admission_receipt", "bundle_digest"),
            "0" * 64,
            "must pin the Release Bundle",
        ),
        (
            ReleaseRecord,
            "release",
            ("approval_receipt", "release_ref", "record_id"),
            "other-release",
            "pin the same Release",
        ),
        (
            ReleaseRecord,
            "release",
            ("approval_receipt", "admission_receipt_id"),
            "other-admission",
            "pin the same Release",
        ),
        (
            ReleaseRecord,
            "release",
            ("approval_receipt", "approved_at"),
            "2026-11-16T02:30:00Z",
            "must follow admission",
        ),
        (
            ReleaseRecord,
            "release",
            (
                "admission_receipt",
                "capability_report",
                "required",
                0,
                "implementation_version",
            ),
            "2.0.0",
            "requires an allowed implementation version",
        ),
        (
            ActivationRecord,
            "activation",
            ("scope",),
            {"scope": "global", "device_id": "robot-1"},
            "extra_forbidden",
        ),
        (ActivationRecord, "activation", ("scope",), {"scope": "region"}, "missing"),
        (
            ActivationRecord,
            "activation",
            ("previous_activation_ref", "record_id"),
            "activation-demo-2",
            "distinct record",
        ),
        (
            ActivationRecord,
            "activation",
            ("release_ref", "record_kind"),
            "task",
            "literal_error",
        ),
    ],
)
def test_release_evidence_rejects_wrong_shapes_or_pins(
    model: type[ContractModel],
    fixture: str,
    path: tuple[str | int, ...],
    value: object,
    message: str,
) -> None:
    raw = _raw(fixture)
    target: Any = raw
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = value
    with pytest.raises(ValidationError, match=message):
        model.model_validate_json(json.dumps(raw))
