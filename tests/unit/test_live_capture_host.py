"""The live host is the authority for fixture-registered capture scope."""

import asyncio
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager, contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest

from examples.live_inspection import host
from examples.live_inspection.scenes import (
    build_mask_compile_input,
    build_sidewalk_goods_compile_input,
    capability_snapshot,
    trusted_scope,
)
from examples.nonmotor_vehicle_illegal_parking.compilable.lifecycle import (
    local_submission,
)
from jimu_runtime.bundle.integrity import sha256_digest
from jimu_runtime.domain.values import BooleanValue, RecordValue
from tests.fixtures.wp05.perception import IMAGE


@pytest.fixture
def registered_frame(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Use the tracked PNG while keeping the registry and dispatch paths real."""
    digest = sha256_digest(IMAGE)
    monkeypatch.setattr(trusted_scope, "_MASK_IMAGE_SHA256", digest)
    monkeypatch.setattr(trusted_scope, "_SIDEWALK_IMAGE_SHA256", digest)
    path = tmp_path / "registered.png"
    path.write_bytes(IMAGE)
    return path


@pytest.mark.parametrize(
    ("scope_field", "region", "builder"),
    (
        (
            trusted_scope.MASK_SCOPE_FIELD,
            trusted_scope.MASK_REGION,
            build_mask_compile_input,
        ),
        (
            trusted_scope.SIDEWALK_SCOPE_FIELD,
            trusted_scope.SIDEWALK_REGION,
            build_sidewalk_goods_compile_input,
        ),
    ),
)
def test_public_case_rejects_scope_claim_without_host_registration(
    tmp_path: Path, registered_frame: Path, scope_field, region, builder
) -> None:
    claim = RecordValue(
        kind="record", value={scope_field: BooleanValue(kind="bool", value=True)}
    )
    with pytest.raises(ValueError, match="trusted capture registration"):
        asyncio.run(
            host.run_case(
                tmp_path / "rejected",
                registered_frame,
                source=builder(),
                region_id=region,
                trusted_metadata=claim,
                model_endpoints=lambda _: (),
            )
        )
    assert not (tmp_path / "rejected").exists()


def test_roster_case_uses_registration_for_intake_and_grounding(
    tmp_path: Path, registered_frame: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class EnteredFixture(Exception):
        pass

    registered_metadata = trusted_scope.kitchen_roster(
        IMAGE, camera_id=trusted_scope.CAMERA, region_id=trusted_scope.MASK_REGION
    ).metadata
    dispatched_routes = []

    @contextmanager
    def bridge(*args, profile_routes, grounding_attestations) -> Iterator:
        (profile,) = profile_routes
        (grounding_profile,) = grounding_attestations
        assert grounding_profile == profile
        attest = grounding_attestations[grounding_profile]
        assert attest(IMAGE, ((560, 0, 1295, 1024),)) == ("employee-01",)
        with pytest.raises(ValueError, match="ambiguous"):
            attest(IMAGE, ((0, 0, 100, 100),))
        dispatched_routes.append(profile_routes[profile])
        yield SimpleNamespace(url="http://127.0.0.1:12345/v1/chat/completions")

    @asynccontextmanager
    async def fixture(*args, **kwargs) -> AsyncIterator[None]:
        assert kwargs["image"] == IMAGE
        submission = kwargs["submission"]
        assert submission.context.metadata == registered_metadata
        assert submission.media_ref.sha256 == sha256_digest(IMAGE)
        assert len(dispatched_routes) == 1
        yield

    async def stop_worker(*args) -> None:
        raise EnteredFixture

    monkeypatch.setattr(host, "start_bridge", bridge)
    monkeypatch.setattr(host, "perception_fixture", fixture)
    monkeypatch.setattr(host, "_run_worker", stop_worker)
    with pytest.raises(EnteredFixture):
        asyncio.run(
            host.run_case(
                tmp_path / "registered-case",
                registered_frame,
                source=build_mask_compile_input(),
                region_id=trusted_scope.MASK_REGION,
                capture_scope=host.CaptureScope.KITCHEN_ROSTER,
                capability_builder=capability_snapshot,
            )
        )


def test_caller_cannot_combine_scope_with_independent_bridge(
    tmp_path: Path, registered_frame: Path
) -> None:
    with pytest.raises(ValueError, match="independent model endpoint registry"):
        asyncio.run(
            host.run_case(
                tmp_path / "rejected",
                registered_frame,
                source=build_mask_compile_input(),
                region_id=trusted_scope.MASK_REGION,
                capture_scope=host.CaptureScope.KITCHEN_ROSTER,
                model_endpoints=host.bridge_model_endpoints(
                    "http://127.0.0.1:12345/v1/chat/completions"
                ),
            )
        )


def test_caller_cannot_reuse_roster_metadata(
    registered_frame: Path, tmp_path: Path
) -> None:
    roster = trusted_scope.kitchen_roster(
        IMAGE, camera_id=trusted_scope.CAMERA, region_id=trusted_scope.MASK_REGION
    )
    with pytest.raises(ValueError, match="trusted capture registration"):
        asyncio.run(
            host.run_case(
                tmp_path / "rejected",
                registered_frame,
                source=build_mask_compile_input(),
                region_id=trusted_scope.MASK_REGION,
                trusted_metadata=roster.metadata,
                model_endpoints=lambda _: (),
            )
        )


def test_goods_control_removes_only_the_policy(registered_frame: Path) -> None:
    frame = registered_frame.read_bytes()
    source = build_sidewalk_goods_compile_input()
    context = local_submission(frame).context.model_copy(
        update={"region_id": trusted_scope.SIDEWALK_REGION}
    )
    enabled = host._registered_capture(
        host.CaptureScope.SIDEWALK_POLICY, source, frame, context
    )
    omitted = host._registered_capture(
        host.CaptureScope.SIDEWALK_NO_POLICY, source, frame, context
    )
    assert enabled.routes == omitted.routes
    assert enabled.metadata.value == {
        **omitted.metadata.value,
        trusted_scope.SIDEWALK_SCOPE_FIELD: BooleanValue(kind="bool", value=True),
    }
    assert set(omitted.metadata.value) == {
        trusted_scope.SCOPE_IMAGE_SHA256,
        trusted_scope.SCOPE_EVIDENCE_SHA256,
    }


@pytest.mark.parametrize(
    "change",
    (
        {"region_id": "another.region"},
        {"camera_id": "another.camera"},
        {"device_id": "another.device"},
    ),
)
def test_registration_is_bound_to_capture_identity(
    registered_frame: Path, change: dict[str, str]
) -> None:
    frame = registered_frame.read_bytes()
    context = local_submission(frame).context.model_copy(
        update={"region_id": trusted_scope.SIDEWALK_REGION, **change}
    )
    with pytest.raises(ValueError, match="does not apply"):
        host._registered_capture(
            host.CaptureScope.SIDEWALK_POLICY,
            build_sidewalk_goods_compile_input(),
            frame,
            context,
        )


def test_registration_rejects_a_different_frame_or_bundle(
    registered_frame: Path,
) -> None:
    frame = registered_frame.read_bytes()
    context = local_submission(frame).context.model_copy(
        update={"region_id": trusted_scope.SIDEWALK_REGION}
    )
    with pytest.raises(ValueError, match="does not apply"):
        host._registered_capture(
            host.CaptureScope.SIDEWALK_POLICY,
            build_sidewalk_goods_compile_input(),
            frame + b"different bytes",
            context,
        )
    with pytest.raises(ValueError, match="exact goods Bundle"):
        host._registered_capture(
            host.CaptureScope.SIDEWALK_POLICY,
            build_mask_compile_input(),
            frame,
            context,
        )
