"""Verify isolated mask and goods scene scope without calling live providers."""

import asyncio
import json
from collections.abc import Callable
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest

from examples.live_inspection import host
from examples.live_inspection.host import CaptureScope, bridge_model_endpoints, run_case
from examples.live_inspection.providers.live_wire_bridge import BRIDGE_VERSION, MODEL_ID
from examples.live_inspection.scenes import (
    build_mask_compile_input,
    build_sidewalk_goods_compile_input,
    bundles,
    capability_snapshot,
)
from examples.live_inspection.scenes.trusted_scope import (
    CAMERA,
    MASK_REGION,
    MASK_SCOPE_FIELD,
    SCOPE_EVIDENCE_SHA256,
    SCOPE_IMAGE_SHA256,
    SIDEWALK_REGION,
    SIDEWALK_SCOPE_FIELD,
    kitchen_roster,
    sidewalk_metadata,
)
from examples.nonmotor_vehicle_illegal_parking.compilable.local_trust import (
    LocalEd25519,
)
from examples.nonmotor_vehicle_illegal_parking.compilable.scene import (
    AT,
    JSON_LIMITS,
    LIMITS,
)
from jimu_runtime.bundle.archive import read_bundle
from jimu_runtime.bundle.catalogue import build_catalogue
from jimu_runtime.bundle.compiler import (
    BundleCompileInput,
    CompileEnvironment,
    compile_bundle,
)
from jimu_runtime.domain.definition_enums import ContextProvenanceSource
from jimu_runtime.domain.enums import ArtifactKind
from jimu_runtime.domain.values import BooleanValue, EnumValue, RecordValue
from jimu_runtime.workflow.worker_records import ObservationMessage
from tests.fixtures.wp04 import http_support

loopback_only = http_support.loopback_only

_IMAGES = (
    Path(__file__).resolve().parents[2]
    / "planning/implementation_evidence/LIVE_THREE_TASKS_20261119/images"
)


@pytest.mark.parametrize(
    ("builder", "region_id", "scope_field", "question_ids"),
    (
        (
            build_mask_compile_input,
            MASK_REGION,
            MASK_SCOPE_FIELD,
            ("face_visibility", "mask_status"),
        ),
        (
            build_sidewalk_goods_compile_input,
            SIDEWALK_REGION,
            SIDEWALK_SCOPE_FIELD,
            ("sidewalk_encroachment",),
        ),
    ),
)
def test_scene_compiles_with_separate_known_scope_and_visual_facts(
    tmp_path: Path,
    builder: Callable[[], BundleCompileInput],
    region_id: str,
    scope_field: str,
    question_ids: tuple[str, ...],
) -> None:
    source = builder()
    result = compile_bundle(
        source,
        limits=LIMITS,
        environment=CompileEnvironment(
            capabilities=capability_snapshot(source),
            trust=LocalEd25519(tmp_path).as_trust_source(),
            validation_at=AT,
        ),
    )
    assert result.bundle is not None, result.errors
    files = read_bundle(result.bundle.archive_bytes, LIMITS)
    catalogue = build_catalogue(files.manifest, files.payloads, **JSON_LIMITS)
    (profile,) = catalogue.of_kind(ArtifactKind.PROFILE)
    (plan,) = catalogue.of_kind(ArtifactKind.TOOL_PLAN)
    (rule,) = catalogue.of_kind(ArtifactKind.RULE)
    (region,) = catalogue.of_kind(ArtifactKind.REGION)
    assert region.ref.artifact_id == region_id
    assert profile.value.grounding_grouping.mode == "per_box"
    assert catalogue.resolve(profile.value.tool_plan_ref) == plan
    assert not plan.value.steps
    assert tuple(ref.artifact_id for ref in profile.value.question_refs) == question_ids
    assert (
        tuple(selector.fact_key for selector in rule.value.required_fact_selectors)
        == question_ids
    )
    gate = catalogue.resolve(rule.value.applicability_policy_ref).value.policy
    assert gate.on_unknown == "block"
    assert {selector.field_path for selector in gate.context_allowlist} == {
        (scope_field,),
        (SCOPE_EVIDENCE_SHA256,),
        (SCOPE_IMAGE_SHA256,),
    }
    assert gate.condition_ast.kind == "all"
    assert {argument.kind for argument in gate.condition_ast.arguments} == {
        "context_ref",
        "comparison",
    }
    assert len(gate.condition_ast.arguments) == 3
    provenance = next(
        row.value.policy
        for row in catalogue.of_kind(ArtifactKind.POLICY)
        if row.ref.artifact_id == "context-provenance"
    )
    assert {field.field_path for field in provenance.fields} == {
        (scope_field,),
        (SCOPE_EVIDENCE_SHA256,),
        (SCOPE_IMAGE_SHA256,),
    }
    assert all(
        field.required is False
        and field.allowed_sources == (ContextProvenanceSource.TRUSTED_TASK_METADATA,)
        for field in provenance.fields
    )


def test_mask_roster_fails_closed_if_association_is_not_exactly_one_to_one() -> None:
    original = (_IMAGES / "mask.png").read_bytes()
    roster = kitchen_roster(original, camera_id=CAMERA, region_id=MASK_REGION)
    valid = (579.072, 0.0, 1276.416, 1021.952)
    unrelated = (20.0, 400.0, 310.0, 890.0)
    # A matching provider box stays a provider box, and carries no mask label.
    assert roster.match(original, (valid,)) == ("employee-01",)
    assert roster.metadata.value[MASK_SCOPE_FIELD] == BooleanValue(
        kind="bool", value=True
    )
    for boxes in ((), (unrelated,), (valid, unrelated), (valid, valid)):
        with pytest.raises(ValueError):
            roster.match(original, boxes)
    with pytest.raises(ValueError, match="does not apply"):
        roster.match(original + b"different capture", (valid,))


@pytest.mark.parametrize(
    ("image_name", "region", "load_scope"),
    (
        ("mask", MASK_REGION, kitchen_roster),
        ("goods", SIDEWALK_REGION, sidewalk_metadata),
    ),
)
def test_synthetic_evidence_requires_exact_capture_and_camera(
    image_name, region, load_scope
) -> None:
    original = (_IMAGES / f"{image_name}.png").read_bytes()
    with pytest.raises(ValueError, match="does not apply"):
        load_scope(original, camera_id="different.camera", region_id=region)
    with pytest.raises(ValueError, match="does not apply"):
        load_scope(original, camera_id=CAMERA, region_id="different.region")
    with pytest.raises(ValueError, match="does not apply"):
        load_scope(
            original, camera_id=CAMERA, region_id=region, device_id="different.device"
        )
    with pytest.raises(ValueError, match="does not apply"):
        load_scope(original + b"different capture", camera_id=CAMERA, region_id=region)


def test_sidewalk_metadata_records_policy_without_predetermined_observation() -> None:
    original = (_IMAGES / "goods.png").read_bytes()
    metadata = sidewalk_metadata(original, camera_id=CAMERA, region_id=SIDEWALK_REGION)
    assert metadata.value[SIDEWALK_SCOPE_FIELD] == BooleanValue(kind="bool", value=True)
    assert isinstance(metadata.value[SCOPE_EVIDENCE_SHA256], EnumValue)
    assert set(metadata.value) == {
        SIDEWALK_SCOPE_FIELD,
        SCOPE_EVIDENCE_SHA256,
        SCOPE_IMAGE_SHA256,
    }


@pytest.mark.parametrize(
    ("builder", "filename"),
    (
        (build_mask_compile_input, "food-service-mask.json"),
        (build_sidewalk_goods_compile_input, "sidewalk-goods.json"),
    ),
)
@pytest.mark.parametrize(
    "field", ({}, {"trusted_scope_field": None}, {"trusted_scope_field": ""})
)
def test_scene_builder_rejects_missing_scope_configuration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, builder, filename: str, field: dict
) -> None:
    original = Path(bundles.__file__).with_name(filename)
    document = json.loads(original.read_bytes())
    del document["trusted_scope_field"]
    (tmp_path / filename).write_text(json.dumps({**document, **field}))
    monkeypatch.setattr(bundles, "_DIRECTORY", tmp_path)
    with pytest.raises(ValueError, match="trusted_scope_field"):
        builder()


def test_raw_scope_boolean_cannot_authorize_capture(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="require a trusted capture registration"):
        asyncio.run(
            run_case(
                tmp_path,
                _IMAGES / "mask.png",
                source=build_mask_compile_input(),
                region_id=MASK_REGION,
                model_endpoints=bridge_model_endpoints("http://127.0.0.1:1"),
                trusted_metadata=RecordValue(
                    kind="record",
                    value={MASK_SCOPE_FIELD: BooleanValue(kind="bool", value=True)},
                ),
            )
        )


def test_historical_configured_observations_preserve_evidence_shape() -> None:
    historical_run = _IMAGES.parent / "parking-per-box" / "result.json"
    records = json.loads(historical_run.read_bytes())["node_outputs"]
    observations = [row for row in records if row["kind"] == "observation"]
    assert len(observations) == 2
    for row in observations:
        restored = ObservationMessage.model_validate_json(json.dumps(row))
        assert restored.original_image_evidence is None
        assert all(not fact.evidence_refs for fact in restored.facts)
        assert restored.model_dump(mode="json") == row


def test_empty_plan_preserves_original_fact_evidence_and_mandatory_rule(
    tmp_path: Path, loopback_only: set[int], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The visual-only configured workflow needs no Tool to prove original bytes."""
    image = _IMAGES / "mask.png"
    replies = {
        "profile_discovery-result": {"profile_hits": ["food_service_worker"]},
        "object_grounding-result": {
            "boxes": [{"xyxy": [579.072, 0.0, 1276.416, 1021.952]}]
        },
        "object_observation-result": {
            "face_visibility": "face_assessable",
            "mask_status": "not_wearing_mask",
        },
        "candidate_prediction-result": {"categories": []},
    }

    async def peer(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        request = await http_support.read_request(reader)
        body = json.loads(request.body)
        schema = body["response_format"]["json_schema"]["schema"]
        capability = schema["$id"].split(":")[-2]
        writer.write(
            http_support.response(
                http_support.chat_body(
                    model=MODEL_ID, content=json.dumps(replies[capability])
                ),
                headers=(
                    (b"X-Request-ID", f"fixture-{capability}".encode()),
                    (b"X-Jimu-Deployment-Version", BRIDGE_VERSION.encode()),
                    (b"X-Jimu-Model-Version", BRIDGE_VERSION.encode()),
                ),
            )
        )
        await writer.drain()

    async def run() -> None:
        async with http_support.http_peer(loopback_only, peer) as url:

            @contextmanager
            def local_bridge(_directory, *, profile_routes, grounding_attestations):
                assert (
                    set(profile_routes)
                    == set(grounding_attestations)
                    == {"food_service_worker"}
                )
                validate = grounding_attestations["food_service_worker"]
                assert validate(
                    image.read_bytes(), ((579.072, 0.0, 1276.416, 1021.952),)
                ) == ("employee-01",)
                yield SimpleNamespace(url=url)

            monkeypatch.setattr(host, "start_bridge", local_bridge)
            result = await run_case(
                tmp_path / "case",
                image,
                source=build_mask_compile_input(),
                capability_builder=capability_snapshot,
                region_id=MASK_REGION,
                capture_scope=CaptureScope.KITCHEN_ROSTER,
            )
        messages = {row["kind"]: row for row in result["node_outputs"]}
        observation = messages["observation"]
        original = observation["original_image_evidence"]
        assert original["evidence_type"] == "original_image"
        assert original["sha256"] == result["media"]["sha256"]
        assert original["spatial"]["original_media_ref"] == result["media"]
        assert original["scope"]["execution_id"] == result["execution_id"]
        evidence_ids = {
            original["evidence_id"],
            *(row["evidence_id"] for row in observation["durable_request_evidence"]),
        }
        for row in observation["batch"]["question_results"]:
            fact = row["primary_fact"]
            assert fact["status"] == "known"
            assert {ref["record_id"] for ref in fact["evidence_refs"]} == evidence_ids
        assert messages["tool"]["output"]["summary"]["steps"] == []
        assert messages["tool"]["executions"] == []
        assert messages["decision"]["draft"]["candidate"]["categories"] == []
        coverage = result["final"]["coverage"]
        assert coverage["summary_result"] == "violation"
        assert coverage["coverage_status"] == "complete"
        (match,) = coverage["matches"]
        assert match["rule"]["rule_ref"]["artifact_id"] == "food_service_worker_no_mask"

    asyncio.run(run())
