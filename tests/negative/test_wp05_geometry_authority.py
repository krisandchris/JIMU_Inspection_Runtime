"""Child identity, physical bytes and malformed sidecars fail closed."""

import asyncio
from dataclasses import replace
from hashlib import sha256

import pytest

from jimu_runtime.domain.definition_enums import EvidenceType
from jimu_runtime.domain.enums import FactStatus
from jimu_runtime.domain.runtime.media import BBoxXYXY
from tests.fixtures.wp05.geometry import STEPS, GeometryFixture


def test_swapped_bytes_and_wrong_authority_are_rejected_before_dispatch(tmp_path):
    fixture = GeometryFixture(tmp_path)

    async def run():
        wheel, tactile = await fixture.masks()
        fields, valid = fixture.geometry((wheel, tactile))
        wrong_object = fixture.object.model_copy(update={"object_id": "second-object"})

        swapped = replace(
            valid,
            masks=(
                replace(wheel, content=tactile.content),
                replace(tactile, content=wheel.content),
            ),
        )
        wrong_link = replace(
            valid,
            masks=(
                replace(
                    wheel,
                    proof=replace(tactile.proof, input_path=wheel.proof.input_path),
                ),
                tactile,
            ),
        )
        cases = [
            ({"session": swapped}, "tool_cpu_input_invalid"),
            ({"session": wrong_link}, "tool_ancestor_evidence_invalid"),
            ({"session": valid, "obj": wrong_object}, "tool_ancestor_evidence_invalid"),
            ({"session": valid, "ancestor": False}, "tool_ancestor_evidence_invalid"),
            ({"session": replace(valid, parking_area=None)}, "tool_cpu_input_invalid"),
        ]
        for changes, code in cases:
            result = await fixture.call(STEPS[2], inputs=fields, **changes)
            assert result.execution.status == "not_dispatched"
            assert result.execution.attempt_result.error.code == code
            assert not result.facts and result.response_digest is None
            assert STEPS[2] not in fixture.requests

    asyncio.run(run())


def test_staged_metadata_failure_retains_the_physical_response(tmp_path):
    fixture = GeometryFixture(tmp_path)

    async def run():
        async def changed_asset(dispatch, sidecar):
            asset = await fixture.stage(dispatch, sidecar)
            return asset.model_copy(update={"producer_version": "1.0.1"})

        from jimu_runtime.inspection.geometry import GeometryInput

        session = GeometryInput(
            fixture.media, fixture.original, changed_asset, object_bbox=fixture.bbox
        )
        return await fixture.call(STEPS[0], session=session)

    result = asyncio.run(run())
    assert result.execution.status == "failed"
    assert result.execution.attempt_result.error.code == "tool_sidecar_invalid"
    (raw,) = result.evidence
    assert raw.evidence_type is EvidenceType.TOOL_OUTPUT
    assert (
        raw.sha256
        == result.response_digest
        == sha256(fixture.stdout[STEPS[0]]).hexdigest()
    )
    assert not result.facts


def test_empty_wheel_and_forged_object_bbox_have_distinct_outcomes(tmp_path):
    fixture = GeometryFixture(tmp_path)
    empty_bbox = BBoxXYXY(xyxy=(0.0, 0.0, 2.0, 2.0))

    async def run():
        from jimu_runtime.inspection.geometry import GeometryInput

        fields = {
            "image": fixture.media.media_ref.model_dump(mode="json"),
            "bbox": empty_bbox.model_dump(mode="json"),
        }
        session = GeometryInput(
            fixture.media, fixture.original, fixture.stage, object_bbox=empty_bbox
        )
        rejected = await fixture.call(STEPS[0], inputs=fields, session=session)
        assert rejected.execution.status == "not_dispatched"
        assert rejected.execution.attempt_result.error.code == "tool_cpu_input_invalid"
        assert STEPS[0] not in fixture.requests
        return await fixture.call(
            STEPS[0],
            inputs=fields,
            session=session,
            obj=fixture.object.model_copy(update={"bbox": empty_bbox}),
        )

    partial = asyncio.run(run())
    assert partial.execution.status == "partial"
    (fact,) = partial.facts
    assert fact.status is FactStatus.UNKNOWN
    assert any(asset.evidence_type is EvidenceType.MASK for asset in partial.evidence)


def test_region_requires_verified_pinned_bytes_and_frame(tmp_path):
    fixture = GeometryFixture(tmp_path)
    with pytest.raises(ValueError, match="exact pinned pixel asset"):
        fixture.verify_region(
            fixture.region_bytes.replace(b'"width": 64', b'"width": 65')
        )
    with pytest.raises(ValueError, match="admitted source frame"):
        fixture.coordinates = fixture.coordinates.model_copy(
            update={
                "dimensions": fixture.coordinates.dimensions.model_copy(
                    update={"width": 65}
                )
            }
        )
        fixture.verify_region(fixture.region_bytes)


def test_registered_worker_rejects_swapped_physical_fields_and_unreadable_source(
    tmp_path,
):
    import json

    from jimu_runtime.tools.registry import CPUWorkerExchange

    fixture = GeometryFixture(tmp_path)

    async def run():
        masks = await fixture.masks()
        fields, session = fixture.geometry(masks)
        exchange = session.exchange()

        def swap(dispatch):
            wire = json.loads(exchange.prepare(dispatch))
            wheel, tactile = wire["wheel_mask"], wire["tactile_paving_mask"]
            wire["wheel_mask"], wire["tactile_paving_mask"] = tactile, wheel
            return json.dumps(wire).encode()

        def unreadable(dispatch):
            raise OSError("synthetic mask read failure")

        for prepare, code in [
            (swap, "tool_cpu_input_invalid"),
            (unreadable, "tool_cpu_input_unreadable"),
        ]:
            result = await fixture.call(
                STEPS[2],
                inputs=fields,
                session=session,
                cpu_exchange=CPUWorkerExchange(prepare, exchange.materialize),
            )
            assert result.execution.status == "not_dispatched"
            assert result.execution.attempt_result.error.code == code
            assert STEPS[2] not in fixture.requests

    asyncio.run(run())


def test_registered_worker_checks_normalized_mask_against_actual_stdout(tmp_path):
    import base64
    import io
    import json

    from PIL import Image

    from jimu_runtime.inspection.geometry import GeometryInput
    from jimu_runtime.tools.registry import CPUWorkerExchange

    fixture = GeometryFixture(tmp_path)
    session = GeometryInput(
        fixture.media, fixture.original, fixture.stage, object_bbox=fixture.bbox
    )
    exchange = session.exchange()
    mask = Image.new("L", (64, 48))
    mask.putpixel((44, 25), 255)
    png = io.BytesIO()
    mask.save(png, format="PNG")
    replacement = json.dumps(
        {"png": base64.b64encode(png.getvalue()).decode(), "count": 1}
    ).encode()

    async def wrong_response(dispatch, _physical_response):
        return await exchange.materialize(dispatch, replacement)

    result = asyncio.run(
        fixture.call(
            STEPS[0],
            session=session,
            cpu_exchange=CPUWorkerExchange(exchange.prepare, wrong_response),
        )
    )
    assert result.execution.status == "failed"
    assert result.execution.attempt_result.error.code == "tool_result_invalid"
    assert result.response_digest == sha256(fixture.stdout[STEPS[0]]).hexdigest()
    assert not result.facts


@pytest.mark.parametrize("source", ["unapproved_png", "approved_jpeg"])
def test_synthetic_execution_requires_approved_lossless_source(tmp_path, source):
    import io

    from PIL import Image

    if source == "approved_jpeg":
        image = Image.new("RGB", (64, 48))
        raw = io.BytesIO()
        image.save(raw, format="JPEG")
        fixture = GeometryFixture(tmp_path, media_bytes=raw.getvalue())
    else:
        fixture = GeometryFixture(tmp_path, allowlisted=False)

    result = asyncio.run(fixture.call(STEPS[0]))
    assert result.execution.status == "not_dispatched"
    assert result.execution.attempt_result.error.code == "tool_cpu_input_invalid"
    assert not fixture.requests
