"""Reject invalid runtime context, media and evidence claims."""

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from jimu_runtime.domain.runtime.evidence import EvidenceAsset
from jimu_runtime.domain.runtime.intake import (
    AcceptedTask,
    ContextSnapshot,
    TaskSubmission,
)
from jimu_runtime.domain.runtime.media import (
    BBoxXYXY,
    ImageDimensions,
    MediaAsset,
    StorageReference,
)

FIXTURES = Path(__file__).parents[1] / "fixtures/wp01c/intake"


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("captured_at", "2026-11-17T10:30:00+08:00"),
        ("captured_at", "2026-11-17T02:30:00"),
        ("original_timestamp", "2026-11-17T10:30:00"),
        ("original_timestamp", "2026-11-17T10:30:00+0800"),
        ("original_timestamp", "2026-02-30T10:30:00+08:00"),
        ("original_timestamp", "2026-11-17T10:31:00+08:00"),
        ("uncertainty_ms", True),
        ("source", "provider_guess"),
    ],
)
def test_clock_provenance_rejects_ambiguous_or_inconsistent_time(
    field: str, value: object
) -> None:
    payload = json.loads((FIXTURES / "task-submission-valid.json").read_bytes())
    payload["context"]["clock"][field] = value
    with pytest.raises(ValidationError):
        TaskSubmission.model_validate_json(json.dumps(payload))


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("media_ref", [{"media_id": "frame-1", "sha256": "a" * 64}]),
        ("release_id", "release-already-resolved"),
        ("bundle_digest", "b" * 64),
    ],
)
def test_submission_accepts_only_one_media_ref_and_an_optional_release_request(
    field: str, value: object
) -> None:
    payload = json.loads((FIXTURES / "task-submission-valid.json").read_bytes())
    payload[field] = value
    with pytest.raises(ValidationError):
        TaskSubmission.model_validate_json(json.dumps(payload))


@pytest.mark.parametrize("entry_index", [0, 1, 2, 3])
def test_snapshot_rejects_sources_from_another_identity(entry_index: int) -> None:
    payload = json.loads((FIXTURES / "context-snapshot-valid.json").read_bytes())
    provenance = payload["values"][entry_index]["provenance"]
    if entry_index == 0:
        provenance["task_id"] = "other-task"
    else:
        ref_field = ("", "device_ref", "camera_ref", "region_ref")[entry_index]
        provenance[ref_field]["digest"] = "0" * 64
    with pytest.raises(ValidationError, match="different"):
        ContextSnapshot.model_validate_json(json.dumps(payload))


@pytest.mark.parametrize("path", [["scene"], ["scene", "district"]])
def test_snapshot_rejects_duplicate_and_overlapping_paths(path: list[str]) -> None:
    payload = json.loads((FIXTURES / "context-snapshot-valid.json").read_bytes())
    payload["values"][-1]["field_path"] = path
    with pytest.raises(ValidationError, match="overlap"):
        ContextSnapshot.model_validate_json(json.dumps(payload))


@pytest.mark.parametrize(
    "object_key",
    [
        "../frame.jpg",
        "media/../frame.jpg",
        "media/.",
        "/frame.jpg",
        "https://edge.test/a",
        "media/x.jpg\n",
    ],
)
def test_storage_reference_rejects_url_or_traversal(object_key: str) -> None:
    with pytest.raises(ValidationError):
        StorageReference(store_id="demo", object_key=object_key)


def test_acceptance_rejects_staged_or_different_media() -> None:
    accepted = AcceptedTask.model_validate_json(
        (FIXTURES / "accepted-task-valid.json").read_bytes()
    )
    payload = json.loads((FIXTURES / "media-asset-valid.json").read_bytes())
    payload["stage"] = {"stage": "staged", "staged_at": "2026-11-17T02:30:00Z"}
    staged = MediaAsset.model_validate_json(json.dumps(payload))
    with pytest.raises(ValueError, match="exact finalized"):
        accepted.validate_media(staged)

    payload = json.loads((FIXTURES / "media-asset-valid.json").read_bytes())
    payload["media_ref"]["media_id"] = "another-frame"
    different = MediaAsset.model_validate_json(json.dumps(payload))
    with pytest.raises(ValueError, match="exact finalized"):
        accepted.validate_media(different)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("verified_at", "2026-11-17T02:29:59Z"),
        ("verified_sha256", "b" * 64),
    ],
)
def test_finalized_media_requires_consistent_verification(
    field: str, value: object
) -> None:
    payload = json.loads((FIXTURES / "media-asset-valid.json").read_bytes())
    payload["stage"][field] = value
    with pytest.raises(ValidationError):
        MediaAsset.model_validate_json(json.dumps(payload))


@pytest.mark.parametrize(
    "xyxy",
    [
        [1, 0, 1, 10],
        [0, 2, 10, 1],
        [-1, 0, 10, 10],
        [True, 0, 10, 10],
        [0, 0, float("inf"), 10],
        [0, 0, 2**53, 10],
        [0, 0, "10", 10],
    ],
)
def test_box_rejects_invalid_pixel_coordinates(xyxy: list[object]) -> None:
    with pytest.raises(ValidationError):
        BBoxXYXY.model_validate({"xyxy": xyxy})


def test_box_must_fit_owning_image_dimensions() -> None:
    box = BBoxXYXY.model_validate({"xyxy": [0, 0, 11, 10]})
    with pytest.raises(ValueError, match="exceeds image"):
        box.validate_bounds(ImageDimensions(width=10, height=10))


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("spatial", {"kind": "non_spatial"}),
        ("scope", {"scope": "object", "execution_id": "execution-1"}),
        (
            "access_scope",
            {"scope": "project", "project_id": "demo", "task_id": "task-1"},
        ),
        ("created_at", "2026-11-17T02:32:00Z"),
        ("mime_type", "text/plain"),
    ],
)
def test_evidence_rejects_incomplete_or_conflicting_metadata(
    field: str, value: object
) -> None:
    payload = json.loads((FIXTURES / "evidence-asset-valid.json").read_bytes())
    payload[field] = value
    with pytest.raises(ValidationError):
        EvidenceAsset.model_validate_json(json.dumps(payload))


def test_spatial_evidence_rejects_disconnected_transform_chain() -> None:
    payload = json.loads((FIXTURES / "evidence-asset-valid.json").read_bytes())
    transform = payload["spatial"]["transforms"][0]
    payload["spatial"]["transforms"].append(
        {
            **transform,
            "source": {**transform["target"], "coordinate_system_id": "not-crop-1"},
            "target": {**transform["target"], "coordinate_system_id": "crop-2"},
        }
    )
    with pytest.raises(ValidationError, match="matching coordinate systems"):
        EvidenceAsset.model_validate_json(json.dumps(payload))


def test_spatial_evidence_rejects_singular_transform() -> None:
    payload = json.loads((FIXTURES / "evidence-asset-valid.json").read_bytes())
    payload["spatial"]["transforms"][0]["affine"] = [1, 1, 0, 1, 1, 0]
    with pytest.raises(ValidationError, match="invertible"):
        EvidenceAsset.model_validate_json(json.dumps(payload))


def test_spatial_evidence_source_must_match_original_media_dimensions() -> None:
    asset = MediaAsset.model_validate_json(
        (FIXTURES / "media-asset-valid.json").read_bytes()
    )
    payload = json.loads((FIXTURES / "evidence-asset-valid.json").read_bytes())
    payload["spatial"]["transforms"][0]["source"]["dimensions"]["width"] += 1
    evidence = EvidenceAsset.model_validate_json(json.dumps(payload))
    with pytest.raises(ValueError, match="match original media dimensions"):
        evidence.validate_original_media(asset)
