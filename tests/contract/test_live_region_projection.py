"""Exercise N04 regional projections against a local Chat Completions peer."""

import asyncio
import hashlib
import json
import socket
from base64 import b64encode
from io import BytesIO
from pathlib import Path

import httpx
import pytest
from PIL import Image

from examples.live_inspection.providers import live_wire_bridge as bridge
from jimu_runtime.domain.nodes.observation import N04_REGIONS_MARKER
from jimu_runtime.inspection.perception.region_projection import RegionProjectionStore
from jimu_runtime.inspection.perception.region_request import (
    RegionObject,
    compile_region_request,
)
from tests.fixtures.wp04 import http_support

_create_connection = socket.create_connection
_loopback_ports = http_support.loopback_only
_ROUTES = {"test": bridge.ProfileRoute("test")}
_ID = ("object.first", "object.second")
_QUESTION = {"q": ("Check the subject", {"type": "integer"})}
_REGIONS = compile_region_request(
    tuple(RegionObject(item, "test", (0, 0, 10, 10), (), _QUESTION) for item in _ID),
    width=12,
    height=12,
)


@pytest.fixture
def loopback_only(
    _loopback_ports: set[int], monkeypatch: pytest.MonkeyPatch
) -> set[int]:
    monkeypatch.setattr(socket, "create_connection", _create_connection)
    return _loopback_ports


@pytest.fixture
def image() -> bytes:
    buffer = BytesIO()
    Image.new("RGB", (12, 12)).save(buffer, format="PNG")
    return buffer.getvalue()


def _body(
    image: bytes,
    object_id: str,
    *,
    execution_id: str = "execution.one",
    attempt_number: int = 1,
    prompt: str = _REGIONS.prompt,
) -> dict:
    metadata = {
        "jimu_region_request": {
            "execution_id": execution_id,
            "attempt_number": attempt_number,
            "release_id": "release.one",
            "bundle_digest": "a" * 64,
            "image_sha256": hashlib.sha256(image).hexdigest(),
            "prompt": prompt,
            "response_schema": _REGIONS.schema,
            "object_ids": _REGIONS.object_ids,
        },
        "jimu_projection_object": object_id,
    }
    payload = {
        "model": bridge.MODEL_ID,
        "stream": False,
        "messages": [
            {
                "role": "user",
                "content": [
                    {
                        "type": "text",
                        "text": f"{N04_REGIONS_MARKER}\n"
                        + json.dumps(metadata, sort_keys=True, separators=(",", ":")),
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
            "type": "json_schema",
            "json_schema": {
                "name": "jimu_response",
                "strict": True,
                "schema": {
                    "$id": "urn:test:object_observation-result:1.0.0",
                    "type": "object",
                    "properties": {"q": _QUESTION["q"][1]},
                    "required": ["q"],
                    "additionalProperties": False,
                },
            },
        },
    }
    return payload


def _reply(content: str, **extra: object) -> bytes:
    value = json.loads(http_support.chat_body(model="Qwen3_5-9B", content=content))
    value.update(object="chat.completion", id="upstream-1")
    value.update(extra)
    return http_support.response(json.dumps(value).encode())


def _content(response: httpx.Response) -> str:
    assert response.status_code == 200, response.text
    value = response.json()
    assert value["id"] == response.headers["x-request-id"]
    return value["choices"][0]["message"]["content"]


def test_region_bridge_concurrent_projection_recovery_and_execution_isolation(
    tmp_path: Path, loopback_only: set[int], image: bytes
) -> None:
    calls = []

    async def verify() -> None:
        arrived = asyncio.Event()
        finish = asyncio.Event()

        async def upstream(reader, writer):
            calls.append(await http_support.read_request(reader))
            arrived.set()
            await finish.wait()
            writer.write(_reply('{"regions":{"r2":{"q":2},"r1":{"q":1}}}'))
            await writer.drain()

        async with http_support.http_peer(loopback_only, upstream) as peer:
            config = {
                "profile_routes": _ROUTES,
                "qwen_url": peer,
                "enable_region_projection": True,
                "qwen_enable_thinking": False,
            }
            async with httpx.AsyncClient(trust_env=False) as client:
                with bridge.start_bridge(tmp_path, **config) as server:
                    loopback_only.add(server.server_port)
                    first_request = asyncio.create_task(
                        client.post(server.url, json=_body(image, _ID[0]))
                    )
                    await asyncio.wait_for(arrived.wait(), timeout=2)
                    second_request = asyncio.create_task(
                        client.post(server.url, json=_body(image, _ID[1]))
                    )
                    await asyncio.sleep(0.1)
                    assert not second_request.done()
                    finish.set()
                    first, second = await asyncio.gather(first_request, second_request)

                assert _content(first) == '{"q":1}'
                assert _content(second) == '{"q":2}'
                shared_id = first.headers["x-request-id"]
                assert second.headers["x-request-id"] == shared_id
                assert len(calls) == 1
                outbound = json.loads(calls[0].body)
                assert outbound["chat_template_kwargs"] == {"enable_thinking": False}
                assert outbound["messages"][0]["content"] == [
                    {"type": "text", "text": _REGIONS.prompt},
                    _body(image, _ID[0])["messages"][0]["content"][1],
                ]
                assert (
                    outbound["response_format"]["json_schema"]["schema"]
                    == _REGIONS.schema
                )

                record = json.loads(
                    (tmp_path / "region-batches" / f"{shared_id}.json").read_bytes()
                )
                assert record["state"] == "completed"
                assert record["physical_http_dispatches"] == 1
                assert (
                    record["physical_request"]["wire"]["sha256"]
                    == hashlib.sha256(calls[0].body).hexdigest()
                )
                evidence = record["physical_response"]
                assert evidence["qwen"]["upstream_completion_id"] == "upstream-1"
                assert evidence["upstream"]["response"]["sha256"]
                assert evidence["qwen"]["unaltered_completion_content"] == (
                    '{"regions":{"r2":{"q":2},"r1":{"q":1}}}'
                )
                audits = [
                    json.loads(path.read_bytes())
                    for path in tmp_path.glob("live-*.json")
                ]
                assert {item["physical_request_id"] for item in audits} == {shared_id}
                assert sorted(item["physical_http_dispatches"] for item in audits) == [
                    0,
                    1,
                ]

                with bridge.start_bridge(tmp_path, **config) as restarted:
                    loopback_only.add(restarted.server_port)
                    cached = await client.post(restarted.url, json=_body(image, _ID[1]))
                    conflict = await client.post(
                        restarted.url,
                        json=_body(image, _ID[1], prompt="changed closure"),
                    )
                    distinct = await client.post(
                        restarted.url,
                        json=_body(image, _ID[1], execution_id="execution.two"),
                    )
                    retry = await client.post(
                        restarted.url,
                        json=_body(image, _ID[1], attempt_number=2),
                    )

                assert _content(cached) == '{"q":2}'
                assert cached.headers["x-request-id"] == shared_id
                assert conflict.status_code == 409
                assert conflict.json()["error"]["code"] == "region_batch_conflict"
                assert _content(distinct) == _content(retry) == '{"q":2}'
                assert (
                    len(
                        {
                            shared_id,
                            distinct.headers["x-request-id"],
                            retry.headers["x-request-id"],
                        }
                    )
                    == 3
                )
                assert len(calls) == 3

                with bridge.start_bridge(
                    tmp_path, **(config | {"qwen_enable_thinking": True})
                ) as changed:
                    loopback_only.add(changed.server_port)
                    settings_conflict = await client.post(
                        changed.url, json=_body(image, _ID[1])
                    )
                assert settings_conflict.status_code == 409
                assert len(calls) == 3

                record["projections"][_ID[1]] = '{"q":999}'
                (tmp_path / "region-batches" / f"{shared_id}.json").write_text(
                    json.dumps(record)
                )
                with bridge.start_bridge(tmp_path, **config) as tampered:
                    loopback_only.add(tampered.server_port)
                    rejected = await client.post(
                        tampered.url, json=_body(image, _ID[1])
                    )
                assert rejected.status_code == 503
                assert (
                    rejected.json()["error"]["code"]
                    == "region_projection_record_invalid"
                )
                assert len(calls) == 3

    asyncio.run(verify())


@pytest.mark.parametrize(
    ("model_content", "error"),
    [
        ('{"regions":{"r1":{"q":4,"q":5}}}', "region_invalid_completion_association"),
        ('{"regions":{"r3":{"q":4}}}', "region_invalid_completion_association"),
        ('{"regions":{"r1":{"q":0.10000000000000000000001}}}', None),
    ],
)
def test_region_bridge_replays_failure_or_projects_missing_and_invalid_slots(
    tmp_path: Path,
    loopback_only: set[int],
    image: bytes,
    model_content: str,
    error: str | None,
) -> None:
    calls = []

    async def upstream(reader, writer):
        calls.append(await http_support.read_request(reader))
        writer.write(_reply(model_content))
        await writer.drain()

    async def verify() -> None:
        async with http_support.http_peer(loopback_only, upstream) as peer:
            with bridge.start_bridge(
                tmp_path,
                profile_routes=_ROUTES,
                qwen_url=peer,
                enable_region_projection=True,
            ) as server:
                loopback_only.add(server.server_port)
                async with httpx.AsyncClient(trust_env=False) as client:
                    first = await client.post(server.url, json=_body(image, _ID[0]))
                    second = await client.post(server.url, json=_body(image, _ID[1]))
            assert first.headers["x-request-id"] == second.headers["x-request-id"]
            assert len(calls) == 1
            assert "chat_template_kwargs" not in json.loads(calls[0].body)
            if error is None:
                assert _content(first) == '{"q":0.10000000000000000000001}'
                assert _content(second) == "{}"
            else:
                for response in (first, second):
                    assert response.status_code == 502
                    assert response.json()["error"]["code"] == error

    asyncio.run(verify())


def test_region_bridge_interrupted_completion_is_never_redispatched(
    tmp_path: Path,
    loopback_only: set[int],
    image: bytes,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = []
    original = RegionProjectionStore._write

    def fail_completion(path, record):
        if record["state"] == "completed":
            raise OSError("test crash before committing the response")
        original(path, record)

    monkeypatch.setattr(RegionProjectionStore, "_write", staticmethod(fail_completion))

    async def upstream(reader, writer):
        calls.append(await http_support.read_request(reader))
        writer.write(_reply('{"regions":{"r2":{"q":2}}}'))
        await writer.drain()

    async def verify() -> None:
        async with http_support.http_peer(loopback_only, upstream) as peer:
            async with httpx.AsyncClient(trust_env=False) as client:
                options = {
                    "profile_routes": _ROUTES,
                    "qwen_url": peer,
                    "enable_region_projection": True,
                }
                with bridge.start_bridge(tmp_path, **options) as server:
                    loopback_only.add(server.server_port)
                    first = await client.post(server.url, json=_body(image, _ID[0]))
                with bridge.start_bridge(tmp_path, **options) as restarted:
                    loopback_only.add(restarted.server_port)
                    second = await client.post(restarted.url, json=_body(image, _ID[1]))
            assert first.status_code == 503
            assert second.status_code == 409
            assert (
                second.json()["error"]["code"] == "region_physical_dispatch_unresolved"
            )
            shared_id = first.headers["x-request-id"]
            assert second.headers["x-request-id"] == shared_id
            assert len(calls) == 1
            record = json.loads(
                (tmp_path / "region-batches" / f"{shared_id}.json").read_bytes()
            )
            assert record["state"] == "dispatch_intent"
            assert record["physical_http_dispatches"] is None

    asyncio.run(verify())


def test_region_bridge_checks_opt_in_source_and_slot_contract_before_dispatch(
    tmp_path: Path, loopback_only: set[int], image: bytes
) -> None:
    calls = []

    async def upstream(reader, writer):
        calls.append(await http_support.read_request(reader))
        writer.write(_reply('{"regions":{}}'))
        await writer.drain()

    async def verify() -> None:
        async with http_support.http_peer(loopback_only, upstream) as peer:
            options = {"profile_routes": _ROUTES, "qwen_url": peer}
            async with httpx.AsyncClient(trust_env=False) as client:
                with bridge.start_bridge(tmp_path, **options) as disabled:
                    loopback_only.add(disabled.server_port)
                    response = await client.post(
                        disabled.url, json=_body(image, _ID[0])
                    )
                    assert response.status_code == 422
                    assert (
                        response.json()["error"]["code"] == "region_projection_disabled"
                    )
                with bridge.start_bridge(
                    tmp_path, **options, enable_region_projection=True
                ) as enabled:
                    loopback_only.add(enabled.server_port)
                    for field, replacement, error in (
                        ("image_sha256", "0" * 64, "region_source_image_mismatch"),
                        (
                            "response_schema",
                            _REGIONS.schema | {"$defs": {}},
                            "region_schema_definitions_mismatch",
                        ),
                        (
                            "object_ids",
                            [_ID[0], _ID[0]],
                            "region_invalid_batch_metadata",
                        ),
                    ):
                        body = _body(image, _ID[0])
                        message = body["messages"][0]["content"][0]
                        metadata = json.loads(message["text"].split("\n")[1])
                        metadata["jimu_region_request"][field] = replacement
                        message["text"] = f"{N04_REGIONS_MARKER}\n" + json.dumps(
                            metadata
                        )
                        response = await client.post(enabled.url, json=body)
                        assert response.status_code == 422
                        assert response.json()["error"]["code"] == error
            assert not calls
            assert not list((tmp_path / "region-batches").iterdir())

    asyncio.run(verify())


@pytest.mark.parametrize("malformed_region", [None, {"q": 3, "injected": True}])
def test_bad_known_region_preserves_other_object(
    tmp_path: Path,
    loopback_only: set[int],
    image: bytes,
    malformed_region: object,
) -> None:
    calls = []

    async def upstream(reader, writer):
        calls.append(await http_support.read_request(reader))
        writer.write(
            _reply(json.dumps({"regions": {"r1": {"q": 2}, "r2": malformed_region}}))
        )
        await writer.drain()

    async def verify() -> None:
        async with http_support.http_peer(loopback_only, upstream) as peer:
            with bridge.start_bridge(
                tmp_path,
                profile_routes=_ROUTES,
                qwen_url=peer,
                enable_region_projection=True,
            ) as server:
                loopback_only.add(server.server_port)
                async with httpx.AsyncClient(trust_env=False) as client:
                    valid = await client.post(server.url, json=_body(image, _ID[0]))
                    invalid = await client.post(server.url, json=_body(image, _ID[1]))
        assert json.loads(_content(valid)) == {"q": 2}
        # Return the malformed payload for validation by r2's logical schema.
        assert json.loads(_content(invalid)) == malformed_region
        assert valid.headers["x-request-id"] == invalid.headers["x-request-id"]
        assert len(calls) == 1

    asyncio.run(verify())


@pytest.mark.parametrize(
    "invalid_envelope",
    [
        {"id": None},
        {"id": 18},
        {"id": "x" * 129},
        {"id": "bad id"},
        {"usage": {"prompt_tokens": "7", "completion_tokens": 1, "total_tokens": 8}},
        {"created": -1},
        {"unregistered_extension": True},
    ],
)
def test_invalid_physical_envelope_never_becomes_a_logical_success(
    tmp_path: Path,
    loopback_only: set[int],
    image: bytes,
    invalid_envelope: dict,
) -> None:
    calls = []

    async def upstream(reader, writer):
        calls.append(await http_support.read_request(reader))
        writer.write(_reply('{"regions":{"r1":{"q":2}}}', **invalid_envelope))
        await writer.drain()

    async def verify() -> None:
        async with http_support.http_peer(loopback_only, upstream) as peer:
            with bridge.start_bridge(
                tmp_path,
                profile_routes=_ROUTES,
                qwen_url=peer,
                enable_region_projection=True,
            ) as server:
                loopback_only.add(server.server_port)
                async with httpx.AsyncClient(trust_env=False) as client:
                    first = await client.post(server.url, json=_body(image, _ID[0]))
                    second = await client.post(server.url, json=_body(image, _ID[1]))
        for response in (first, second):
            assert response.status_code == 502
            assert response.json()["error"]["code"] == "qwen_invalid_response_envelope"
        assert first.headers["x-request-id"] == second.headers["x-request-id"]
        assert len(calls) == 1
        (record_path,) = (tmp_path / "region-batches").glob("*.json")
        record = json.loads(record_path.read_bytes())
        assert record["state"] == "failed"
        assert record["physical_http_dispatches"] == 1
        assert "validated_envelope" not in record["physical_response"]["qwen"]

    asyncio.run(verify())
