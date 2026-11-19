"""Actual loopback requests validate the host-approved protocol and audit data."""

import asyncio
import base64
import json
import ssl
from hashlib import sha256

import pytest
from fixtures.wp04 import http_support
from fixtures.wp04.http_support import (
    ChatPayload,
    chat_body,
    chunks,
    endpoint,
    http_peer,
    read_request,
    response,
    transport_fixture,
)
from fixtures.wp04.routing_support import routing_fixture

from jimu_runtime.domain.base import ContractModel
from jimu_runtime.domain.fact_schemas.canonicalization import canonical_bytes
from jimu_runtime.gateway import http as http_adapter
from jimu_runtime.gateway.http import ChatContract, HttpModelTransport, VerifiedImage
from jimu_runtime.gateway.invocation import (
    DispatchRequest,
    ModelTransport,
    project_dispatch,
)

loopback_only = http_support.loopback_only

# A generated one-pixel RGB PNG, verified in the synthetic media loader fixture.
_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAIAAACQd1PeAAAADElEQVR4nGNgYGAAAAAEAAH2FzhVAAAAAElFTkSuQmCC"
)


@pytest.mark.parametrize("protocol", ["openai_remote_fixture", "vllm_local_fixture"])
def test_exact_prepared_bytes_image_contract_and_reported_identity(
    protocol, loopback_only, monkeypatch
):
    async def run() -> None:
        received = []
        metadata = json.loads(chat_body())
        if protocol == "openai_remote_fixture":
            metadata["choices"][0]["message"]["annotations"] = []
            metadata["usage"] = {
                "prompt_tokens": 12,
                "completion_tokens": 6,
                "total_tokens": 18,
                "prompt_tokens_details": {"audio_tokens": 0, "cached_tokens": 2},
                "completion_tokens_details": {
                    "reasoning_tokens": 1,
                    "audio_tokens": 0,
                    "accepted_prediction_tokens": 0,
                    "rejected_prediction_tokens": 0,
                },
            }
        else:
            metadata["choices"][0]["stop_reason"] = None
            metadata["choices"][0]["token_ids"] = [1, 2]
            metadata["choices"][0]["message"]["tool_calls"] = []
            metadata["prompt_token_ids"] = None
            metadata["prompt_logprobs"] = None
            metadata["kv_transfer_params"] = None
        visible = json.dumps(metadata).encode()

        async def handle(reader, writer):
            received.append(await read_request(reader))
            writer.write(
                response(
                    visible,
                    headers=(
                        (b"X-Request-ID", b"server-call-7"),
                        (b"X-Jimu-Deployment-Version", b"2.1.0"),
                        (b"X-Jimu-Model-Version", b"3.2.0"),
                    ),
                )
            )
            await writer.drain()

        async with http_peer(loopback_only, handle) as url:
            kind = "remote" if protocol == "openai_remote_fixture" else "local"
            transport, route = transport_fixture(endpoint(url, provider_kind=kind))
            image = VerifiedImage(_PNG, "image/png", sha256(_PNG).hexdigest())
            payload = transport.prepare(route, "Inspect this image once.", image)
            wire: ModelTransport = transport
            result = await wire.dispatch(
                DispatchRequest("loopback-success", route, payload)
            )

        assert len(received) == 1
        request = received[0]
        assert request.line == b"POST /v1/chat/completions HTTP/1.1"
        assert request.headers[b"accept-encoding"] == b"identity"
        assert b"authorization" not in request.headers
        assert request.body == payload
        doc = json.loads(payload)
        assert doc["model"] == route.ownership.model_id
        assert doc["stream"] is False
        assert doc["messages"] == [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "Inspect this image once."},
                    {
                        "type": "image_url",
                        "image_url": {
                            "url": "data:image/png;base64,"
                            + base64.b64encode(_PNG).decode()
                        },
                    },
                ],
            }
        ]
        assert doc["response_format"]["json_schema"]["strict"] is True
        assert (
            doc["response_format"]["json_schema"]["schema"]
            == ChatPayload.model_json_schema()
        )
        assert result.dispatched and result.error is None
        assert result.observation is not None
        assert result.observation.provider_request_id == "server-call-7"
        assert result.observation.model_id == route.ownership.model_id
        assert result.observation.deployment_version == "2.1.0"
        assert result.observation.model_version == "3.2.0"
        assert result.response_status == 200
        assert result.observed_response_bytes == len(visible)
        assert result.response_digest == sha256(visible).hexdigest()
        assert result.response_bytes is None
        assert result.response_bytes_omission_reason == "policy_disabled"
        assert result.parsed_content is not None
        assert (
            ChatPayload.model_validate_json(result.parsed_content).finding == "parked"
        )

    # The proxy is also a loopback URL but never resolves to an authorized port.
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:1")
    monkeypatch.setenv("ALL_PROXY", "http://127.0.0.1:1")
    monkeypatch.setenv("NO_PROXY", "")
    asyncio.run(run())


@pytest.mark.parametrize("framing", ["close_delimited", "chunked"])
def test_missing_content_length_and_version_keep_exact_observations(
    framing, loopback_only
):
    async def run():
        body = chat_body()

        async def handle(reader, writer):
            await read_request(reader)
            if framing == "chunked":
                writer.write(
                    response(
                        chunks(body[:12], body[12:40], body[40:]) + b"0\r\n\r\n",
                        headers=((b"Transfer-Encoding", b"chunked"),),
                        omit_length=True,
                    )
                )
            else:
                writer.write(response(body, omit_length=True))
            await writer.drain()

        async with http_peer(loopback_only, handle) as url:
            transport, route = transport_fixture(endpoint(url))
            payload = transport.prepare(route, "Return the finding.")
            result = await transport.dispatch(
                DispatchRequest("no-content-length", route, payload)
            )
        assert result.error is None
        assert result.response_digest == sha256(body).hexdigest()
        assert result.observed_response_bytes == len(body)
        assert result.observation is not None
        assert result.observation.model_id == route.ownership.model_id
        assert result.observation.deployment_version is None
        assert result.observation.model_version is None
        assert result.observation.provider_request_id is None

    asyncio.run(run())


def test_real_planned_route_pins_derived_schema_before_send(loopback_only):
    async def run():
        received = []
        selected = routing_fixture().primary_route().selected

        async def handle(reader, writer):
            received.append(await read_request(reader))
            writer.write(
                response(
                    chat_body(),
                    headers=(
                        (b"X-Request-ID", b"n04-loopback-17"),
                        (
                            b"X-Jimu-Deployment-Version",
                            selected.deployment_version.encode(),
                        ),
                        (b"X-Jimu-Model-Version", selected.model_version.encode()),
                    ),
                )
            )
            await writer.drain()

        async with http_peer(loopback_only, handle) as url:
            cfg = endpoint(url, retain_complete_response=True).model_copy(
                update={
                    "provider_id": selected.provider_id,
                    "deployment_id": selected.deployment_id,
                }
            )
            unrelated = routing_fixture(
                endpoint_pin=cfg.descriptor_pin, model_id=cfg.model_id
            ).primary_route()
            derived = canonical_bytes(
                {
                    **ChatPayload.model_json_schema(),
                    "description": "Exact synthetic joint contract resolved for this N04 attempt.",
                }
            )
            contract = ChatContract(
                unrelated.binding.input_schema_ref,
                unrelated.binding.output_schema_ref,
                ChatPayload,
                response_schema=derived,
            )
            route = routing_fixture(
                endpoint_pin=cfg.descriptor_pin,
                model_id=cfg.model_id,
                response_schema_digest=contract.response_schema_digest,
            ).primary_route()
            transport = HttpModelTransport((cfg,), (contract,), max_concurrency=1)
            payload = transport.prepare(route, "Perform one joint observation.")
            rejecting = await transport.dispatch(
                DispatchRequest("wrong-derived-schema", unrelated, payload)
            )
            assert not rejecting.dispatched
            assert (
                rejecting.error is not None
                and rejecting.error.code == "http_schema_pin_unregistered"
            )

            outbound = DispatchRequest("exact-derived-schema", route, payload)
            result = await transport.dispatch(outbound)

        assert len(received) == 1
        assert route.operation.contract_snapshot is not None
        snapshot = route.operation.contract_snapshot
        assert snapshot.response_schema_sha256 == sha256(derived).hexdigest()
        posted = json.loads(received[0].body)
        assert posted["response_format"]["json_schema"]["schema"] == json.loads(derived)
        outcome = project_dispatch(
            outbound,
            result,
            started_at=route.approved_at,
            completed_at=route.approved_at,
            evidence_refs=(snapshot.response_schema_evidence_ref,),
        )
        assert outcome.error is None
        assert outcome.invocation is not None
        assert outcome.invocation.outcome.status == "succeeded"
        assert outcome.output is not None
        assert result.response_bytes == chat_body()
        assert result.response_bytes_omission_reason is None
        assert result.response_digest == sha256(result.response_bytes).hexdigest()
        assert result.response_digest != sha256(outcome.output).hexdigest()
        assert repr(result.response_bytes) not in repr(result)

    asyncio.run(run())


class _WeakReply(ContractModel):
    finding: str
    score: int


def test_pinned_derived_schema_rejects_value_allowed_by_weaker_model(loopback_only):
    async def run():
        body = chat_body(content='{"finding":"vendor-unknown","score":1}')

        async def handle(reader, writer):
            await read_request(reader)
            writer.write(response(body))
            await writer.drain()

        shape = _WeakReply.model_json_schema()
        shape["properties"]["finding"] = {"type": "string", "enum": ["clear", "parked"]}
        assert _WeakReply(finding="vendor-unknown", score=1)
        async with http_peer(loopback_only, handle) as url:
            transport, route = transport_fixture(
                endpoint(url, retain_complete_response=True),
                response_model=_WeakReply,
                response_schema=canonical_bytes(shape),
            )
            payload = await transport.prepare_async(
                route, "Inspect the exact contract."
            )
            result = await transport.dispatch(
                DispatchRequest("derived-enum-failure", route, payload)
            )
        assert (
            result.error is not None
            and result.error.code == "offline_schema_value_mismatch"
        )
        assert result.parsed_content is None
        assert result.validated_response_contract_digest is None
        assert result.response_digest == sha256(body).hexdigest()
        assert result.response_bytes is None

    asyncio.run(run())


@pytest.mark.parametrize(
    ("content", "expected_invalid", "expected_missing"),
    [
        ('{"q-one":"parked","q-two":1.0000000000000000001}', ("q-two",), ("q-three",)),
        ('{"q-one":"parked","q-two":2,"q-three":false}', (), ()),
    ],
)
def test_question_slots_validate_independently_under_closed_n04_envelope(
    content,
    expected_invalid,
    expected_missing,
    loopback_only,
):
    async def run():
        body = chat_body(content=content)
        slots = ("q-one", "q-two", "q-three")
        schema = canonical_bytes(
            {
                "type": "object",
                "properties": {
                    "q-one": {"enum": ["parked", "clear"]},
                    "q-two": {"type": "number", "minimum": 2},
                    "q-three": {"type": "boolean"},
                },
                "required": list(slots),
                "additionalProperties": False,
            }
        )

        async def handle(reader, writer):
            await read_request(reader)
            writer.write(response(body))
            await writer.drain()

        async with http_peer(loopback_only, handle) as url:
            transport, route = transport_fixture(
                endpoint(url),
                response_schema=schema,
                question_slots=slots,
            )
            payload = await transport.prepare_async(
                route, "Inspect these questions together."
            )
            result = await transport.dispatch(
                DispatchRequest("question-slot-fixture", route, payload)
            )
            # The same declared schema cannot be mounted on an operation without
            # the exact selected Question refs.
            route.operation.contract_snapshot = None
            wrong = await transport.dispatch(
                DispatchRequest("wrong-question-snapshot", route, payload)
            )
        assert result.error is None
        assert result.validated_response_contract_digest == sha256(schema).hexdigest()
        assert result.invalid_question_slots == expected_invalid
        assert result.missing_question_slots == expected_missing
        assert result.parsed_content == content.encode()
        assert (
            not wrong.dispatched
            and wrong.error.code == "http_question_slots_pin_mismatch"
        )

    asyncio.run(run())


def test_dispatch_reuses_verifying_tls_context(loopback_only, monkeypatch):
    original = http_adapter.httpx.AsyncHTTPTransport
    observed = []

    def factory(*args, **kwargs):
        observed.append(kwargs["verify"])
        return original(*args, **kwargs)

    monkeypatch.setattr(http_adapter.httpx, "AsyncHTTPTransport", factory)

    async def run():
        async def handle(reader, writer):
            await read_request(reader)
            writer.write(response(chat_body()))
            await writer.drain()

        async with http_peer(loopback_only, handle) as url:
            transport, route = transport_fixture(endpoint(url))
            body = await transport.prepare_async(route, "Inspect once per dispatch.")
            for identity in ("loop-tls-one", "loop-tls-two"):
                result = await transport.dispatch(
                    DispatchRequest(identity, route, body)
                )
                assert result.error is None

    asyncio.run(run())
    assert len(observed) == 2
    assert observed[0] is observed[1]
    assert observed[0].check_hostname
    assert observed[0].verify_mode == ssl.CERT_REQUIRED
