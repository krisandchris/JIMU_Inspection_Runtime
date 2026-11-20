"""Exercise the isolated adapter's explicitly configured inference routes."""

import asyncio
import hashlib
import json
import socket
from base64 import b64encode
from io import BytesIO
from pathlib import Path
from urllib.parse import urlencode

import httpx
import pytest
from PIL import Image

from examples.live_inspection.providers import live_wire_bridge as bridge
from tests.fixtures.wp04 import http_support

_create_connection = socket.create_connection
_loopback_ports = http_support.loopback_only
_LABEL = "cardboard boxes"
_PROFILE = "sidewalk_goods"
_ROUTES = {_PROFILE: bridge.ProfileRoute(_LABEL)}
_IMAGE_SIZE = (12, 9)


@pytest.fixture
def loopback_only(
    _loopback_ports: set[int], monkeypatch: pytest.MonkeyPatch
) -> set[int]:
    # urllib and synchronous httpx use this helper. Its socket.connect remains
    # guarded by the existing port-scoped fixture.
    monkeypatch.setattr(socket, "create_connection", _create_connection)
    return _loopback_ports


def _image() -> bytes:
    raw = BytesIO()
    Image.new("RGB", _IMAGE_SIZE).save(raw, format="PNG")
    return raw.getvalue()


def _request(capability: str, metadata: dict, image: bytes) -> dict:
    return {
        "model": bridge.MODEL_ID,
        "stream": False,
        "messages": [
            {
                "role": "user",
                "content": [
                    {
                        "type": "text",
                        "text": "Find the configured profile.\n" + json.dumps(metadata),
                    },
                    {
                        "type": "image_url",
                        "image_url": {
                            "url": f"data:image/png;base64,{b64encode(image).decode()}"
                        },
                    },
                ],
            }
        ],
        "response_format": {
            "json_schema": {
                "schema": {
                    "$id": f"urn:test:{capability}-result:1.0.0",
                }
            }
        },
    }


def _wevis(image: bytes, predictions: dict, raw: str) -> bytes:
    return json.dumps(
        {
            "model": "WeVisOne-VL-4B-NF4",
            "task": "detection",
            "image_size": list(_IMAGE_SIZE),
            "image_sha256": hashlib.sha256(image).hexdigest(),
            "model_forward_calls": 3,
            "predictions": predictions,
            "model_response": raw,
        }
    ).encode()


def _audit(directory: Path, response: httpx.Response) -> dict:
    return json.loads(
        (directory / f"{response.headers['x-request-id']}.json").read_bytes()
    )


def _body(response: httpx.Response) -> dict:
    assert response.status_code == 200
    return json.loads(response.json()["choices"][0]["message"]["content"])


def test_discovery_projects_only_profile_and_grounding_runs_independently(
    tmp_path: Path,
    loopback_only: set[int],
) -> None:
    image = _image()
    detected = iter(([1, 1, 2, 2], [2, 2, 7, 7]))
    calls = []

    async def upstream(reader, writer):
        request = await http_support.read_request(reader)
        calls.append(request)
        box = next(detected)
        reply = _wevis(image, {_LABEL: [box]}, f"<ref>{_LABEL}</ref><box>{box}</box>")
        writer.write(http_support.response(reply))
        await writer.drain()

    async def verify() -> None:
        async with http_support.http_peer(loopback_only, upstream) as peer:
            with bridge.start_bridge(
                tmp_path, profile_routes=_ROUTES, wevis_url=peer
            ) as server:
                loopback_only.add(server.server_port)
                async with httpx.AsyncClient(trust_env=False) as client:
                    discovery = await client.post(
                        server.url,
                        json=_request(
                            "profile_discovery",
                            {"eligible_profile_ids": [_PROFILE]},
                            image,
                        ),
                    )
                    grounding = await client.post(
                        server.url,
                        json=_request(
                            "object_grounding",
                            {"profile_id": _PROFILE},
                            image,
                        ),
                    )

        assert _body(discovery) == {"profile_hits": [_PROFILE]}
        assert _body(grounding) == {"boxes": [{"xyxy": [2, 2, 7, 7]}]}
        assert _audit(tmp_path, discovery)["discovery_calls"][0]["detections"] == 1
        assert _audit(tmp_path, grounding)["wevis"]["forward_calls"] == 3
        assert len(calls) == 2
        for request in calls:
            assert request.body == image
            assert request.line.split()[1].endswith(
                urlencode({"label": _LABEL}).encode()
            )

    asyncio.run(verify())


@pytest.mark.parametrize(
    ("predictions", "raw", "upstream_status", "error"),
    (
        ({}, f"<ref>{_LABEL}</ref><box>none</box>", 200, None),
        ({_LABEL: []}, f"<ref>{_LABEL}</ref><box>none</box>", 200, None),
        ({}, "<unparseable>", 200, "wevis_no_parseable_grounding"),
        ({_LABEL: []}, "<unparseable>", 200, "wevis_no_parseable_grounding"),
        (
            {_LABEL: [[0, 1, 13, 2]]},
            "<box>bogus</box>",
            200,
            "wevis_box_out_of_source_frame",
        ),
        (
            {"other": [[1, 1, 2, 2]]},
            "<box>bogus</box>",
            200,
            "unexpected_wevis_detection_label",
        ),
        (
            {_LABEL: [[1, 1, 2, 2]]},
            f"<ref>{_LABEL}</ref><box>none</box>",
            200,
            "wevis_conflicting_grounding",
        ),
        ({}, "", 500, "upstream_http_error"),
    ),
)
def test_discovery_distinguishes_explicit_absence_from_failure(
    tmp_path: Path,
    loopback_only: set[int],
    predictions: dict,
    raw: str,
    upstream_status: int,
    error: str | None,
) -> None:
    image = _image()

    async def upstream(reader, writer):
        await http_support.read_request(reader)
        writer.write(
            http_support.response(
                _wevis(image, predictions, raw),
                status=upstream_status,
            )
        )
        await writer.drain()

    async def verify() -> None:
        async with http_support.http_peer(loopback_only, upstream) as peer:
            with bridge.start_bridge(
                tmp_path, profile_routes=_ROUTES, wevis_url=peer
            ) as server:
                loopback_only.add(server.server_port)
                async with httpx.AsyncClient(trust_env=False) as client:
                    response = await client.post(
                        server.url,
                        json=_request(
                            "profile_discovery",
                            {"eligible_profile_ids": [_PROFILE]},
                            image,
                        ),
                    )
        if error is None:
            assert _body(response) == {"profile_hits": []}
        else:
            assert response.status_code == 502
            assert response.json()["error"]["code"] == error
        assert (
            _audit(tmp_path, response)["discovery_calls"][0]["upstream"]["status"]
            == upstream_status
        )

    asyncio.run(verify())


def test_unbound_profile_rejected_before_any_upstream_call(
    tmp_path: Path,
    loopback_only: set[int],
) -> None:
    with bridge.start_bridge(tmp_path, profile_routes=_ROUTES) as server:
        loopback_only.add(server.server_port)
        with httpx.Client(trust_env=False) as client:
            response = client.post(
                server.url,
                json=_request(
                    "profile_discovery",
                    {"eligible_profile_ids": [_PROFILE, "unbound"]},
                    _image(),
                ),
            )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "unsupported_discovery_profile"
    assert _audit(tmp_path, response)["unsupported_profiles"] == ["unbound"]
    assert "discovery_calls" not in _audit(tmp_path, response)


@pytest.mark.parametrize(("finish_reason", "status"), (("stop", 200), ("length", 502)))
def test_qwen_preserves_assistant_content(
    tmp_path: Path, loopback_only: set[int], finish_reason: str, status: int
) -> None:
    answer = '  {\n  "sidewalk_encroachment": "insufficient_visibility"\n}\n'
    messages = []

    async def upstream(reader, writer):
        request = await http_support.read_request(reader)
        messages.append(json.loads(request.body))
        reply = {
            "model": "Qwen3_5-9B",
            "object": "chat.completion",
            "id": "qwen-1",
            "usage": {"prompt_tokens": 10, "completion_tokens": 12, "total_tokens": 22},
            "choices": [
                {
                    "index": 0,
                    "finish_reason": finish_reason,
                    "token_ids": [1],
                    "message": {
                        "role": "assistant",
                        "content": answer,
                        "reasoning": "must not become facts",
                    },
                }
            ],
        }
        writer.write(http_support.response(json.dumps(reply).encode()))
        await writer.drain()

    async def verify() -> None:
        request = _request("object_observation", {}, _image())
        async with http_support.http_peer(loopback_only, upstream) as peer:
            with bridge.start_bridge(
                tmp_path, profile_routes=_ROUTES, qwen_url=peer
            ) as server:
                loopback_only.add(server.server_port)
                async with httpx.AsyncClient(trust_env=False) as client:
                    response = await client.post(server.url, json=request)

        assert response.status_code == status
        assert messages == [{**request, "model": "Qwen3_5-9B"}]
        qwen = _audit(tmp_path, response)["qwen"]
        assert qwen["finish_reason"] == finish_reason
        assert qwen["choice_count"] == 1 and qwen["choice_index"] == 0
        assert qwen["token_usage"] == {
            "prompt_tokens": 10,
            "completion_tokens": 12,
            "total_tokens": 22,
        }
        assert qwen["requested_max_tokens"] is None
        if status == 200:
            assert response.json()["choices"][0]["message"] == {
                "role": "assistant",
                "content": answer,
            }
            assert qwen["unaltered_completion_content"] == answer
        else:
            assert response.json()["error"]["code"] == "qwen_completion_incomplete"
            assert "unaltered_completion_content" not in qwen

    asyncio.run(verify())


def test_mixed_catalogue_keeps_stage1_and_wevis_bindings(
    tmp_path: Path, loopback_only: set[int]
) -> None:
    image = _image()
    person = "food_service_worker"
    calls = []

    async def upstream(reader, writer):
        request = await http_support.read_request(reader)
        calls.append(request)
        if b"label=" in request.line:
            reply = _wevis(image, {_LABEL: [[1, 1, 2, 2]]}, "<box>fixture</box>")
        else:
            reply = json.dumps(
                {
                    "model_id": bridge._STAGE1_MODEL,
                    "schema_version": "routing/1.0",
                    "policy_version": "inspection-routing/1.0",
                    "objects": [
                        {
                            "target_type": "person",
                            "route_enabled": True,
                            "presence_probability": 0.8,
                            "selection_reason": "fixture",
                            "questions": ["outside-runtime-catalogue"],
                        }
                    ],
                }
            ).encode()
        writer.write(http_support.response(reply))
        await writer.drain()

    async def verify() -> None:
        routes = {
            **_ROUTES,
            person: bridge.ProfileRoute("person", stage1_target="person"),
        }
        async with http_support.http_peer(loopback_only, upstream) as peer:
            with bridge.start_bridge(
                tmp_path, profile_routes=routes, stage1_url=peer, wevis_url=peer
            ) as server:
                loopback_only.add(server.server_port)
                async with httpx.AsyncClient(trust_env=False) as client:
                    response = await client.post(
                        server.url,
                        json=_request(
                            "profile_discovery",
                            {"eligible_profile_ids": [_PROFILE, person]},
                            image,
                        ),
                    )
        assert _body(response) == {"profile_hits": [_PROFILE, person]}
        discovery_calls = _audit(tmp_path, response)["discovery_calls"]
        assert [call["provider"] for call in discovery_calls] == [
            "stage1",
            "wevis_detection",
        ]
        assert discovery_calls[0]["stage1"]["upstream_questions_used"] is False
        assert [call.body for call in calls] == [image, image]

    asyncio.run(verify())


@pytest.mark.parametrize("association_ok", (True, False))
def test_grounding_association_is_all_or_nothing(
    tmp_path: Path, loopback_only: set[int], association_ok: bool
) -> None:
    image = _image()
    boxes = [[1, 2, 3, 4], [5, 6, 7, 8]]
    received = []

    async def upstream(reader, writer):
        await http_support.read_request(reader)
        reply = _wevis(image, {_LABEL: boxes}, "<box>fixture</box>")
        writer.write(http_support.response(reply))
        await writer.drain()

    def attest(content, coordinates):
        received.append((content, coordinates))
        if not association_ok:
            raise ValueError("Unknown roster entry")
        return ("entry-1", "entry-2")

    async def verify() -> None:
        async with http_support.http_peer(loopback_only, upstream) as peer:
            with bridge.start_bridge(
                tmp_path,
                profile_routes=_ROUTES,
                grounding_attestations={_PROFILE: attest},
                wevis_url=peer,
            ) as server:
                loopback_only.add(server.server_port)
                async with httpx.AsyncClient(trust_env=False) as client:
                    response = await client.post(
                        server.url,
                        json=_request(
                            "object_grounding", {"profile_id": _PROFILE}, image
                        ),
                    )
        assert received == [(image, ((1, 2, 3, 4), (5, 6, 7, 8)))]
        projected = [{"xyxy": box} for box in boxes]
        record = _audit(tmp_path, response)["grounding_scope_association"]
        if association_ok:
            assert _body(response) == {"boxes": projected}
            assert record == {
                "status": "matched",
                "identities": ["entry-1", "entry-2"],
                "boxes": projected,
            }
        else:
            assert response.status_code == 422
            assert (
                response.json()["error"]["code"] == "grounding_scope_association_failed"
            )
            assert record == {"status": "rejected", "boxes": projected}

    asyncio.run(verify())
