"""Runtime data relationships for accepted media and context provenance."""

import json
from pathlib import Path

import pytest

from jimu_runtime.domain.definition_enums import ContextProvenanceSource
from jimu_runtime.domain.definition_support import AssetDimensions
from jimu_runtime.domain.enums import TaskStatus
from jimu_runtime.domain.runtime.evidence import DerivedImageEvidence, EvidenceAsset
from jimu_runtime.domain.runtime.intake import (
    AcceptedTask,
    ContextSnapshot,
    TaskSubmission,
)
from jimu_runtime.domain.runtime.media import BBoxXYXY, ImageDimensions, MediaAsset

FIXTURES = Path(__file__).parents[1] / "fixtures/wp01c/intake"


def test_acceptance_resolves_release_and_pins_the_single_rgb_asset() -> None:
    submission = TaskSubmission.model_validate_json(
        (FIXTURES / "task-submission-valid.json").read_bytes()
    )
    accepted = AcceptedTask.model_validate_json(
        (FIXTURES / "accepted-task-valid.json").read_bytes()
    )
    asset = MediaAsset.model_validate_json(
        (FIXTURES / "media-asset-valid.json").read_bytes()
    )

    assert submission.requested_release_id is None
    assert accepted.release_id == "release-1"
    assert accepted.status is TaskStatus.ACCEPTED
    assert accepted.context == submission.context
    assert accepted.context.clock.captured_at.isoformat() == "2026-11-17T02:30:00+00:00"
    assert accepted.context.clock.original_timestamp == "2026-11-17T10:30:00+08:00"
    assert accepted.context.clock.original_timezone == "Asia/Shanghai"
    assert accepted.idempotency_scope == submission.idempotency_scope
    accepted.validate_media(asset)


def test_snapshot_records_each_provenance_branch_and_freezes_values() -> None:
    snapshot = ContextSnapshot.model_validate_json(
        (FIXTURES / "context-snapshot-valid.json").read_bytes()
    )
    assert {entry.provenance.source for entry in snapshot.values} == set(
        ContextProvenanceSource
    )
    assert isinstance(snapshot.values, tuple)
    with pytest.raises(TypeError):
        snapshot.values[0] = snapshot.values[1]  # type: ignore[index]


def test_crop_preserves_mapping_to_the_original_image() -> None:
    asset = MediaAsset.model_validate_json(
        (FIXTURES / "media-asset-valid.json").read_bytes()
    )
    evidence = EvidenceAsset.model_validate_json(
        (FIXTURES / "evidence-asset-valid.json").read_bytes()
    )

    assert ImageDimensions is AssetDimensions
    evidence.validate_original_media(asset)
    assert isinstance(evidence.spatial, DerivedImageEvidence)
    assert evidence.spatial.original_media_ref == asset.media_ref
    assert evidence.spatial.transforms[0].source.dimensions == asset.dimensions
    assert evidence.spatial.coordinate_system.dimensions == ImageDimensions(
        width=400, height=200
    )
    BBoxXYXY.model_validate({"xyxy": [0, 0, 1280, 720]}).validate_bounds(
        asset.dimensions
    )


def test_original_image_evidence_preserves_verified_media() -> None:
    asset = MediaAsset.model_validate_json(
        (FIXTURES / "media-asset-valid.json").read_bytes()
    )
    payload = json.loads((FIXTURES / "evidence-asset-valid.json").read_bytes())
    payload.update(
        evidence_type="original_image",
        sha256=asset.media_ref.sha256,
        mime_type=asset.mime_type,
        byte_size=asset.byte_size,
        stage=asset.stage.model_dump(mode="json"),
        created_at=asset.stage.model_dump(mode="json")["staged_at"],
        scope={"scope": "execution", "execution_id": "execution-1"},
        access_scope={"scope": "project", "project_id": "demo"},
        spatial={
            "kind": "original_image",
            "original_media_ref": asset.media_ref.model_dump(mode="json"),
            "coordinate_system": payload["spatial"]["transforms"][0]["source"],
        },
    )
    evidence = EvidenceAsset.model_validate_json(json.dumps(payload))

    evidence.validate_original_media(asset)
