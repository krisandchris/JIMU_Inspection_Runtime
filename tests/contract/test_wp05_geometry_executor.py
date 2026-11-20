"""The actual signed scene and ToolExecutor produce and reuse physical sidecars."""

import asyncio
import json
from hashlib import sha256

from jimu_runtime.domain.definition_enums import EvidenceType
from jimu_runtime.domain.enums import FactStatus
from jimu_runtime.domain.runtime.facts import KnownFact
from jimu_runtime.domain.runtime.tool_executions import ReturnedToolAttempt
from jimu_runtime.inspection.geometry._cpu import load_image
from tests.fixtures.wp05.geometry import STEPS, GeometryFixture


def test_signed_geometry_runs_all_pinned_children_and_preserves_evidence(tmp_path):
    fixture = GeometryFixture(tmp_path)

    async def run():
        masks = await fixture.masks()
        fields, session = fixture.geometry(masks)
        return masks, await fixture.call(STEPS[2], inputs=fields, session=session)

    masks, geometry = asyncio.run(run())
    wheel, tactile = masks
    assert load_image(wheel.content, (64, 48), mask=True).tobytes().count(255) == 42
    assert load_image(tactile.content, (64, 48), mask=True).tobytes().count(255) == 192

    assert geometry.execution.actual_implementation_version == "1.0.0"
    assert geometry.execution.status == "succeeded"
    (fact,) = geometry.facts
    assert isinstance(fact, KnownFact)
    assert fact.status is FactStatus.KNOWN
    assert fact.value.value == "outside_parking_area"
    assert fact.source.output_name == "wheel_boundary_relation"
    assert fact.attempt_id == geometry.execution.attempt_ref.record_id

    assets = {asset.evidence_id: asset for asset in geometry.evidence}
    result = geometry.execution.attempt_result
    assert isinstance(result, ReturnedToolAttempt)
    names = {
        slot.name: assets[slot.evidence_refs[0].record_id]
        for slot in result.result.evidence
    }
    output = names["geometry"]
    assert output.evidence_type is EvidenceType.GEOMETRY
    assert names["wheel_mask"] == wheel.proof.asset
    assert names["tactile_paving_mask"] == tactile.proof.asset
    assert output.retention != wheel.proof.asset.retention
    assert {ref.record_id for ref in fact.evidence_refs} == {
        fixture.original.asset.evidence_id,
        wheel.proof.asset.evidence_id,
        tactile.proof.asset.evidence_id,
        output.evidence_id,
    }
    assert {item.artifact_ref.record_id for item in geometry.execution.input_refs} == {
        fixture.object.object_id,
        wheel.proof.asset.evidence_id,
        tactile.proof.asset.evidence_id,
    }
    assert {item.artifact_ref.record_id for item in geometry.execution.output_refs} == {
        asset.evidence_id
        for asset in geometry.evidence
        if asset.evidence_type
        in {
            EvidenceType.GEOMETRY,
            EvidenceType.TOOL_OUTPUT,
        }
    }

    stitched = fixture.sidecars[output.evidence_id]
    worker = fixture.stdout[STEPS[2]]
    report = json.loads(stitched)
    assert report["metrics"] == json.loads(worker)["metrics"]
    assert report["relation"] == "outside_parking_area"
    assert report["inputs"]["wheel_mask"]["sha256"] == wheel.proof.asset.sha256
    assert (
        json.loads(stitched)["parking_area"]["source_asset_sha256"]
        == fixture.region_source.sha256
    )
    assert sha256(stitched).hexdigest() == output.sha256
    assert sha256(worker).hexdigest() == geometry.response_digest
    assert len(worker) == geometry.response_byte_count
    assert report["execution_id"] == geometry.execution.execution_id
    assert report["tool_execution_id"] == geometry.execution.tool_execution_id
    assert report["attempt_id"] == geometry.execution.attempt_ref.record_id
    assert report["object_ref"] == geometry.execution.object_ref.model_dump(mode="json")
    assert report["profile_ref"] == geometry.execution.profile_ref.model_dump(
        mode="json"
    )
    assert report["request_sha256"] == geometry.request_digest
    assert report["response_sha256"] == geometry.response_digest
    assert fixture.requests.keys() == set(STEPS)
    assert geometry.request_digest == sha256(fixture.requests[STEPS[2]][1]).hexdigest()
    for logical, physical, lease in fixture.requests.values():
        assert logical != physical
        assert 0 < len(physical) <= lease.input_byte_limit


def test_two_supervised_segmenters_witness_tactile_overlap_from_same_rgb(tmp_path):
    import io

    from PIL import Image

    from jimu_runtime.domain.runtime.media import BBoxXYXY
    from jimu_runtime.inspection.geometry._cpu import (
        TACTILE_COLOR,
        WHEEL_COLOR,
        WHEEL_ON_TACTILE_COLOR,
    )

    image = Image.new("RGB", (64, 48))
    image.putpixel((14, 16), WHEEL_ON_TACTILE_COLOR)
    image.putpixel((15, 16), WHEEL_COLOR)
    image.putpixel((16, 16), TACTILE_COLOR)
    raw = io.BytesIO()
    image.save(raw, format="PNG")
    fixture = GeometryFixture(tmp_path, media_bytes=raw.getvalue())
    fixture.bbox = BBoxXYXY(xyxy=(12.0, 14.0, 18.0, 18.0))
    fixture.object = fixture.object.model_copy(update={"bbox": fixture.bbox})

    async def run():
        wheel, tactile = await fixture.masks()
        fields, session = fixture.geometry((wheel, tactile))
        overlap = await fixture.call(STEPS[2], inputs=fields, session=session)
        return wheel, tactile, overlap

    wheel, tactile, overlap = asyncio.run(run())
    wheel_grid = load_image(wheel.content, (64, 48), mask=True)
    tactile_grid = load_image(tactile.content, (64, 48), mask=True)
    assert wheel_grid.getpixel((14, 16)) == tactile_grid.getpixel((14, 16)) == 255
    assert wheel_grid.getpixel((15, 16)) == 255
    assert tactile_grid.getpixel((15, 16)) == 0
    assert wheel_grid.getpixel((16, 16)) == 0
    assert tactile_grid.getpixel((16, 16)) == 255
    assert overlap.execution.status == "succeeded"
    assert overlap.facts[0].value.value == "blocking_tactile_paving"
    assert json.loads(fixture.stdout[STEPS[2]])["metrics"]["overlap"] == 1
