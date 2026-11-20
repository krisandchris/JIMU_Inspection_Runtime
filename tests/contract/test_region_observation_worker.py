"""Exercise normal intake and Worker execution against one regional VLM response."""

import asyncio
import hashlib
import json
import socket
from base64 import b64decode
from contextlib import contextmanager
from io import BytesIO
from pathlib import Path
from urllib.parse import urlencode

import pytest
from PIL import Image

from examples.live_inspection import host
from examples.live_inspection.providers import live_wire_bridge as bridge
from examples.live_inspection.scenes import (
    build_sidewalk_goods_compile_input,
    capability_snapshot,
    trusted_scope,
)
from tests.fixtures.wp04 import http_support

_create_connection = socket.create_connection
_loopback_ports = http_support.loopback_only
_BOXES = ((498, 512, 900, 656), (940, 594, 1176, 815))
_LABEL = "cardboard boxes"
_QUESTION = "sidewalk_encroachment"


@pytest.fixture
def loopback_only(
    _loopback_ports: set[int], monkeypatch: pytest.MonkeyPatch
) -> set[int]:
    # urllib opens the bridge's upstream sockets through this guarded helper.
    monkeypatch.setattr(socket, "create_connection", _create_connection)
    return _loopback_ports


@pytest.mark.parametrize(
    ("second_region", "second_outcome", "second_status"),
    (
        pytest.param({}, "partial", "error", id="missing-answer"),
        pytest.param(
            {_QUESTION: "insufficient_visibility"},
            "complete",
            "unknown",
            id="unknown-answer",
        ),
    ),
)
def test_worker_projects_one_original_image_call_and_preserves_each_decision(
    tmp_path: Path,
    loopback_only: set[int],
    monkeypatch: pytest.MonkeyPatch,
    second_region: dict[str, str],
    second_outcome: str,
    second_status: str,
) -> None:
    with BytesIO() as buffer:
        Image.new("RGB", trusted_scope.CAPTURE_IMAGE_SIZE, (36, 49, 71)).save(
            buffer, format="PNG"
        )
        original = buffer.getvalue()
    image_path = tmp_path / "input.png"
    image_path.write_bytes(original)
    digest = hashlib.sha256(original).hexdigest()
    # Register the deterministic test capture before building its signed Bundle.
    monkeypatch.setattr(trusted_scope, "_SIDEWALK_IMAGE_SHA256", digest)
    calls: list[http_support.ReceivedRequest] = []

    async def upstream(
        reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        request = await http_support.read_request(reader)
        calls.append(request)
        if b"label=" in request.line:
            payload = {
                "model": "WeVisOne-VL-4B-NF4",
                "task": "detection",
                "image_size": trusted_scope.CAPTURE_IMAGE_SIZE,
                "image_sha256": digest,
                "model_forward_calls": 1,
                "predictions": {_LABEL: _BOXES},
                "model_response": f"<ref>{_LABEL}</ref><box>{_BOXES}</box>",
            }
        else:
            body = json.loads(request.body)
            schema = body["response_format"]["json_schema"]
            if schema["name"] == "jimu_region_response":
                answer = {
                    "regions": {
                        "r2": second_region,
                        "r1": {_QUESTION: "occupies_sidewalk"},
                    }
                }
            else:
                assert schema["schema"]["$id"].split(":")[-2] == (
                    "candidate_prediction-result"
                )
                answer = {"categories": []}
            payload = json.loads(
                http_support.chat_body(model="Qwen3_5-9B", content=json.dumps(answer))
            )
            payload.update(object="chat.completion", id=f"upstream-{len(calls)}")
        writer.write(http_support.response(json.dumps(payload).encode()))
        await writer.drain()

    output = tmp_path / "runtime"

    async def run() -> dict:
        async with http_support.http_peer(loopback_only, upstream) as url:

            @contextmanager
            def start_local_bridge(log_directory, **options):
                with bridge.start_bridge(
                    log_directory,
                    stage1_url=url,
                    wevis_url=url,
                    qwen_url=url,
                    **options,
                ) as server:
                    loopback_only.add(server.server_port)
                    try:
                        yield server
                    finally:
                        loopback_only.remove(server.server_port)

            monkeypatch.setattr(host, "start_bridge", start_local_bridge)
            return await host.run_case(
                output,
                image_path,
                source=build_sidewalk_goods_compile_input(region_projection=True),
                capability_builder=capability_snapshot,
                capture_scope=host.CaptureScope.SIDEWALK_POLICY,
                region_id=trusted_scope.SIDEWALK_REGION,
                max_worker_steps=32,
            )

    result = asyncio.run(run())
    detection_calls = [call for call in calls if b"label=" in call.line]
    assert len(detection_calls) == 2  # N02 discovery and independent N03 grounding.
    for call in detection_calls:
        assert call.body == original
        assert call.line.split()[1].endswith(urlencode({"label": _LABEL}).encode())
    chat_calls = [call for call in calls if call not in detection_calls]
    regional_calls = [
        call
        for call in chat_calls
        if json.loads(call.body)["response_format"]["json_schema"]["name"]
        == "jimu_region_response"
    ]
    (region_call,) = regional_calls
    assert len(chat_calls) == 3  # One physical N04 request and two N06 requests.
    prompt, image = json.loads(region_call.body)["messages"][0]["content"]
    media_type, encoded = image["image_url"]["url"].split(",", 1)
    assert media_type == "data:image/png;base64"
    assert b64decode(encoded, validate=True) == original
    prompt_metadata = json.loads(prompt["text"].splitlines()[-1])
    assert prompt_metadata["image_size_pixels"] == list(
        trusted_scope.CAPTURE_IMAGE_SIZE
    )
    assert prompt_metadata["boxes"] == {
        "r1": [324, 500, 586, 641],
        "r2": [612, 580, 766, 796],
    }

    (record_path,) = (output / "provider" / "region-batches").glob("*.json")
    batch_record = json.loads(record_path.read_bytes())
    assert batch_record["state"] == "completed"
    assert batch_record["physical_http_dispatches"] == 1
    request_evidence = batch_record["physical_request"]
    assert (
        request_evidence["wire"]["sha256"]
        == hashlib.sha256(region_call.body).hexdigest()
    )
    assert request_evidence["batch"]["execution_id"] == result["execution_id"]
    assert (
        request_evidence["batch"]["image_sha256"] == result["media"]["sha256"] == digest
    )

    observations = {
        tuple(message["inputs"]["object_record"]["bbox"]["xyxy"]): message
        for message in result["node_outputs"]
        if message["kind"] == "observation"
    }
    assert set(observations) == set(_BOXES)
    decisions = {
        item["object_ref"]["record_id"]: item for item in result["final"]["decisions"]
    }
    decision_traces = {
        message["draft"]["inputs"]["object_record"]["object_id"]: message["draft"]
        for message in result["node_outputs"]
        if message["kind"] == "decision"
    }
    assert decisions == {
        object_id: draft["decision"] for object_id, draft in decision_traces.items()
    }
    object_ids = [
        observations[box]["inputs"]["object_record"]["object_id"] for box in _BOXES
    ]
    assert set(decisions) == set(object_ids)
    assert request_evidence["batch"]["object_ids"] == object_ids
    physical_id = batch_record["physical_request_id"]
    for box, object_id, outcome, status, decision in zip(
        _BOXES,
        object_ids,
        ("complete", second_outcome),
        ("known", second_status),
        ("violation", "uncertain"),
        strict=True,
    ):
        observation = observations[box]
        assert observation["inputs"]["image_mode"] == "original_regions_v1"
        assert observation["batch"]["object_ref"]["record_id"] == object_id
        assert observation["batch"]["outcome"] == outcome
        assert observation["invocation"]["object_ref"]["record_id"] == object_id
        assert observation["invocation"]["provider_request_id"] == physical_id
        (question,) = observation["batch"]["question_results"]
        assert question["question_ref"]["artifact_id"] == _QUESTION
        fact = question["primary_fact"]
        assert fact["object_id"] == object_id
        assert fact["status"] == status
        assert decision_traces[object_id]["inputs"]["fact_refs"] == [
            {"record_kind": "fact", "record_id": fact["fact_id"]}
        ]
        final = decisions[object_id]
        assert final["result"] == decision
        (rule,) = final["rule_evaluations"]
        assert rule["rule"]["rule_ref"]["artifact_id"] == "sidewalk_goods_encroachment"
        evaluation = rule["result"]
        if status == "known":
            assert evaluation["outcome"] == "evaluated" and evaluation["matched"]
        else:
            assert evaluation["outcome"] == "blocked"
            assert evaluation["predicate"]["truth"] == "unknown"
            assert (
                f"predicate.fact.{status}"
                in evaluation["predicate"]["diagnostic_codes"]
            )
