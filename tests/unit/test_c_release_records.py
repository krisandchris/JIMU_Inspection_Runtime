"""Pure record behavior using deterministic candidate evidence."""

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from jimu_runtime.domain.runtime.bundle_manifest import BundleManifest, ChecksumManifest
from jimu_runtime.domain.runtime.releases import (
    ActivationRecord,
    ReleaseRecord,
    ReleaseStatus,
)

_FIXTURES = Path(__file__).parents[1] / "fixtures/wp01c/releases"


def test_aggregate_artifact_digests_are_independent_of_file_checksum() -> None:
    manifest = BundleManifest.model_validate_json(
        (_FIXTURES / "bundle_manifest.json").read_bytes()
    )
    checksums = ChecksumManifest.model_validate_json(
        (_FIXTURES / "checksums.json").read_bytes()
    )
    first, second, standalone = manifest.artifacts
    checksum = checksums.entries[0]
    assert first.path == second.path == checksum.path
    assert [first.json_pointer, second.json_pointer] == ["/0", "/1"]
    assert (
        len({first.content_ref.digest, second.content_ref.digest, checksum.sha256}) == 3
    )
    assert standalone.json_pointer is None
    assert "json_pointer" not in standalone.model_dump()
    with pytest.raises(ValidationError, match="frozen_instance"):
        first.path = "ontology/other.json"


def test_activation_carries_pin_and_predecessor_without_mutating_release() -> None:
    release = ReleaseRecord.model_validate_json(
        (_FIXTURES / "release.json").read_bytes()
    )
    activation = ActivationRecord.model_validate_json(
        (_FIXTURES / "activation.json").read_bytes()
    )
    assert activation.release_ref.record_id == release.release_id
    assert activation.bundle_ref == release.bundle_ref
    assert activation.previous_activation_ref is not None
    assert activation.previous_activation_ref.record_id == "activation-demo-1"
    assert release.status is ReleaseStatus.APPROVED
    with pytest.raises(ValidationError, match="frozen_instance"):
        release.bundle_ref.bundle_digest = "e" * 64


@pytest.mark.parametrize("status", list(ReleaseStatus))
def test_release_statuses_preserve_their_own_receipt_history(
    status: ReleaseStatus,
) -> None:
    raw = json.loads((_FIXTURES / "release.json").read_bytes())
    raw["status"] = status.value
    if status in {ReleaseStatus.COMPILED, ReleaseStatus.SUBMITTED}:
        del raw["admission_receipt"]
    if status in {
        ReleaseStatus.REJECTED,
        ReleaseStatus.ACCEPTED,
        ReleaseStatus.COMPILED,
        ReleaseStatus.SUBMITTED,
    }:
        del raw["approval_receipt"]
    if status is ReleaseStatus.REJECTED:
        receipt = raw["admission_receipt"]
        receipt["accepted"] = False
        receipt["errors"] = [
            {
                "code": "integrity-failed",
                "path": [],
                "message": "Synthetic rejection",
                "related_refs": [],
            }
        ]
    result = ReleaseRecord.model_validate_json(json.dumps(raw))
    assert result.status is status
    assert result.model_dump(mode="json") == raw
