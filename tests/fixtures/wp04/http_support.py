"""An explicitly scoped real loopback peer and deterministic Chat wire fixtures."""

import asyncio
import json
import socket
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Literal, cast

import pytest
from pydantic import SecretStr

from jimu_runtime.domain.base import ContractModel
from jimu_runtime.domain.definition_support import ReviewReportRef
from jimu_runtime.domain.enums import ArtifactKind
from jimu_runtime.domain.references import SchemaRef
from jimu_runtime.domain.runtime.invocations import ProviderKind
from jimu_runtime.domain.validation.review_inputs import ReviewedDeployment
from jimu_runtime.gateway.http import (
    ChatContract,
    ChatEndpoint,
    HttpLimits,
    HttpModelTransport,
)
from jimu_runtime.gateway.invocation import DispatchRequest
from jimu_runtime.gateway.routing import PlannedRoute

_CONNECT = socket.socket.connect
_MODEL = "fixture/inspection"
_DIALECT = "https://json-schema.org/draft/2020-12/schema"


class ChatPayload(ContractModel):
    finding: Literal["clear", "parked"]
    score: int


@dataclass(frozen=True)
class ReceivedRequest:
    line: bytes
    headers: dict[bytes, bytes] = field(repr=False)
    body: bytes = field(repr=False)


type PeerHandler = Callable[
    [asyncio.StreamReader, asyncio.StreamWriter], Awaitable[None]
]


@pytest.fixture
def loopback_only(no_network: None, monkeypatch: pytest.MonkeyPatch) -> set[int]:
    ports: set[int] = set()

    def connect(sock: socket.socket, address: tuple[str, int]) -> None:
        assert (
            sock.family == socket.AF_INET
            and isinstance(address, tuple)
            and address[0] == "127.0.0.1"
            and address[1] in ports
        ), "only this test's loopback listeners are authorized"
        return _CONNECT(sock, address)

    monkeypatch.setattr(socket.socket, "connect", connect)
    return ports


@asynccontextmanager
async def http_peer(ports: set[int], handler: PeerHandler) -> AsyncIterator[str]:
    tasks: set[asyncio.Task[None]] = set()

    async def run(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        task = asyncio.current_task()
        assert task is not None
        tasks.add(task)
        try:
            await handler(reader, writer)
        except (ConnectionError, asyncio.IncompleteReadError):
            # Intentional oversize/timeout/cancellation can terminate the client.
            pass
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except ConnectionError:
                pass

    async with await asyncio.start_server(run, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        ports.add(port)
        try:
            yield f"http://127.0.0.1:{port}/v1/chat/completions"
        finally:
            ports.remove(port)
            server.close()
            await server.wait_closed()
            errors = [
                task.exception()
                for task in tasks
                if task.done() and not task.cancelled()
            ]
            for task in tasks:
                if not task.done():
                    task.cancel()
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)
            assert not any(errors), errors


async def read_request(reader: asyncio.StreamReader) -> ReceivedRequest:
    header = await reader.readuntil(b"\r\n\r\n")
    line, *header_lines = header[:-4].split(b"\r\n")
    headers = {}
    for header_line in header_lines:
        name, value = header_line.split(b":", 1)
        headers[name.lower()] = value.strip()
    return ReceivedRequest(
        line, headers, await reader.readexactly(int(headers[b"content-length"]))
    )


def chat_body(
    *,
    model: str | None = _MODEL,
    content: str = '{"finding":"parked","score":1}',
    finish_reason: str = "stop",
) -> bytes:
    value: dict[str, object] = {
        "choices": [
            {
                "index": 0,
                "finish_reason": finish_reason,
                "message": {"role": "assistant", "content": content},
            },
        ],
    }
    if model is not None:
        value["model"] = model
    return json.dumps(value, separators=(",", ":")).encode()


def response(
    body: bytes,
    *,
    status: int = 200,
    headers: tuple[tuple[bytes, bytes], ...] = (),
    length: int | None = None,
    omit_length: bool = False,
) -> bytes:
    head = [
        f"HTTP/1.1 {status} fixture".encode(),
        b"Content-Type: application/json",
        b"Connection: close",
    ]
    if not omit_length:
        head.append(
            f"Content-Length: {len(body) if length is None else length}".encode()
        )
    head.extend(key + b": " + value for key, value in headers)
    return b"\r\n".join(head) + b"\r\n\r\n" + body


def chunks(*data: bytes) -> bytes:
    return b"".join(f"{len(chunk):x}\r\n".encode() + chunk + b"\r\n" for chunk in data)


@dataclass
class FixtureSecrets:
    token: str = field(repr=False)
    requested: list[str] = field(default_factory=list)

    def resolve(self, credential_ref: str) -> SecretStr | None:
        self.requested.append(credential_ref)
        return SecretStr(self.token)


def endpoint(
    url: str,
    *,
    provider_kind: ProviderKind = "remote",
    credential: bool = False,
    allow_plaintext_loopback: bool = True,
    retain_complete_response: bool = False,
    timeout_ms: int = 1250,
    io_timeout_ms: int = 1200,
    request_bytes: int = 4096,
    response_bytes: int = 4096,
) -> ChatEndpoint:
    return ChatEndpoint(
        endpoint_id="fixture-http",
        endpoint_version="1.0.0",
        provider_id="loopback-fixture",
        deployment_id="fixture-http",
        provider_kind=provider_kind,
        model_id=_MODEL,
        url=url,
        protocol=(
            "vllm_openai_chat_completions"
            if provider_kind == "local"
            else "openai_chat_completions"
        ),
        credential_ref="fixture-token-ref" if credential else None,
        allow_plaintext_loopback=allow_plaintext_loopback,
        retain_complete_response=retain_complete_response,
        limits=HttpLimits(
            timeout_ms=timeout_ms,
            io_timeout_ms=io_timeout_ms,
            request_bytes=request_bytes,
            response_bytes=response_bytes,
            image_bytes=1024,
            max_retry_after_seconds=7,
        ),
    )


def transport_fixture(
    config: ChatEndpoint,
    *,
    accepts_image: bool = True,
    secrets: FixtureSecrets | None = None,
    route_input_limit: int = 4096,
    route_output_limit: int = 4096,
    route_timeout_ms: int = 1500,
    host_max_concurrency: int = 2,
    response_model: type[ContractModel] = ChatPayload,
    response_schema: bytes | None = None,
    version_evidence: bool = False,
    question_slots: tuple[str, ...] = (),
) -> tuple[HttpModelTransport, PlannedRoute]:
    def schema(identity: str, digest: str) -> SchemaRef:
        return SchemaRef(
            artifact_kind=ArtifactKind.RESPONSE_SCHEMA,
            artifact_id=identity,
            schema_dialect=_DIALECT,
            version="1.0.0",
            digest=digest * 64,
        )

    contract = ChatContract(
        input_schema_ref=schema("transport-in", "a"),
        output_schema_ref=schema("transport-out", "b"),
        response_model=response_model,
        accepts_image=accepts_image,
        response_schema=response_schema,
        question_slots=question_slots,
    )
    # W4.2 transport tests isolate a route already authorized by W4.1. The
    # bridge suite exercises the real policy-to-route and dispatch integration.
    route = SimpleNamespace(
        selected=ReviewedDeployment(
            provider_id=config.provider_id,
            deployment_id=config.deployment_id,
            logical_model_id="fixture-inspection",
            provider_kind=config.provider_kind,
            deployment_version="2.1.0",
            model_version="3.2.0",
        ),
        ownership=SimpleNamespace(
            model_id=config.model_id,
            endpoint_descriptor_ref=config.descriptor_pin,
            version_evidence=(
                ReviewReportRef(
                    report_id="fixture-proof",
                    review_registry_id="fixture-registry",
                    version="1.0.0",
                    sha256="e" * 64,
                )
                if version_evidence
                else None
            ),
        ),
        binding=SimpleNamespace(
            input_schema_ref=contract.input_schema_ref,
            output_schema_ref=contract.output_schema_ref,
        ),
        resource_requirements=SimpleNamespace(
            input_byte_limit=route_input_limit,
            output_byte_limit=route_output_limit,
            timeout_ms=route_timeout_ms,
        ),
        operation=SimpleNamespace(
            response_contract_digest=contract.response_schema_digest,
            contract_snapshot=(
                SimpleNamespace(
                    effective_question_refs=tuple(
                        SimpleNamespace(artifact_id=id_) for id_ in question_slots
                    ),
                )
                if question_slots
                else None
            ),
        ),
        history=(),
    )
    transport = HttpModelTransport(
        (config,),
        (contract,),
        max_concurrency=host_max_concurrency,
        secrets=secrets,
    )
    return transport, cast(PlannedRoute, route)


def request(
    transport: HttpModelTransport,
    route: PlannedRoute,
    *,
    dispatch_id: str = "loopback-fixture",
) -> DispatchRequest:
    return DispatchRequest(
        dispatch_id, route, transport.prepare(route, "Return a bounded finding.")
    )
