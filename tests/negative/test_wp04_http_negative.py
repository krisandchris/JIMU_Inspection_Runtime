"""Transport failures preserve only bounded, observed data and never retry."""

import asyncio
import json
import logging
import secrets
import threading
from hashlib import sha256
from types import SimpleNamespace

import pytest
from fixtures.wp04 import http_support
from fixtures.wp04.http_support import (
    FixtureSecrets,
    chat_body,
    chunks,
    endpoint,
    http_peer,
    read_request,
    request,
    response,
    transport_fixture,
)

from jimu_runtime.gateway import http as http_adapter
from jimu_runtime.gateway.invocation import DispatchRequest, TransportCancelled

loopback_only = http_support.loopback_only


async def _dispatch_wire(ports, wire, *, retain_complete_response=False, **limits):
    received = []

    async def handle(reader, writer):
        received.append(await read_request(reader))
        writer.write(wire)
        await writer.drain()

    async with http_peer(ports, handle) as url:
        transport, route = transport_fixture(
            endpoint(url, retain_complete_response=retain_complete_response), **limits
        )
        result = await transport.dispatch(request(transport, route))
    assert len(received) == 1
    return result


@pytest.mark.parametrize(
    ("status", "header", "error_class", "code", "retry_seconds"),
    [
        (429, b"120", "rate_limit", "http_rate_limited", 7.0),
        (429, b"0", "rate_limit", "http_rate_limited", 0.0),
        (429, b"-10", "rate_limit", "http_rate_limited", None),
        (429, b"Thu Jan  1 00:00:00 2070", "rate_limit", "http_rate_limited", 7.0),
        (
            429,
            b"Thu, 01 Jan 2070 00:00:00 +0000",
            "rate_limit",
            "http_rate_limited",
            None,
        ),
        (429, b"Thu 1 Jan 2070 00:00:00", "rate_limit", "http_rate_limited", None),
        (
            503,
            b"Wed, 21 Oct 2015 07:28:00 GMT",
            "unavailable",
            "http_service_unavailable",
            0.0,
        ),
        (
            503,
            b"Thu, 01 Jan 2070 00:00:00 GMT",
            "unavailable",
            "http_service_unavailable",
            7.0,
        ),
        (408, b"10", "timeout", "http_request_timeout", None),
        (401, b"10", "validation", "http_auth_rejected", None),
        (404, b"10", "validation", "http_status_rejected", None),
    ],
)
def test_http_statuses_and_retry_after_are_explicit_single_attempts(
    status, header, error_class, code, retry_seconds, loopback_only
):
    body = b'{"error":"non-authoritative fixture"}'
    raw = response(body, status=status, headers=((b"Retry-After", header),))
    result = asyncio.run(_dispatch_wire(loopback_only, raw))
    assert result.error is not None
    assert (result.error.error_class, result.error.code) == (error_class, code)
    assert result.response_status == status
    assert result.retry_after_seconds == retry_seconds
    assert result.response_digest == sha256(body).hexdigest()
    assert result.parsed_content is None
    assert result.response_bytes is None


def test_redirect_target_cannot_receive_requests(loopback_only):
    async def run():
        redirections = []

        async def target(reader, writer):
            redirections.append(await read_request(reader))

        async with http_peer(loopback_only, target) as target_url:
            body = b""
            raw = response(
                body, status=307, headers=((b"Location", target_url.encode()),)
            )
            result = await _dispatch_wire(loopback_only, raw)
        assert not redirections
        assert result.error is not None and result.error.code == "http_redirect_denied"
        assert result.response_status == 307
        assert result.response_digest == sha256(body).hexdigest()

    asyncio.run(run())


@pytest.mark.parametrize("framing", ["declared", "close_delimited", "chunked"])
def test_route_and_endpoint_response_cap_prevents_partial_digest(
    framing, loopback_only
):
    body = b"a" * 256
    if framing == "declared":
        raw = response(body)
    elif framing == "chunked":
        raw = response(
            chunks(body[:48], body[48:]) + b"0\r\n\r\n",
            headers=((b"Transfer-Encoding", b"chunked"),),
            omit_length=True,
        )
    else:
        raw = response(body, omit_length=True)

    result = asyncio.run(
        _dispatch_wire(
            loopback_only, raw, route_output_limit=64, retain_complete_response=True
        )
    )
    assert result.dispatched
    assert result.error is not None
    assert (result.error.error_class, result.error.code) == (
        "resource_exhausted",
        "http_response_too_large",
    )
    assert result.response_status == 200
    assert result.response_digest is None
    assert result.response_bytes is None
    assert result.response_bytes_omission_reason == "incomplete"
    assert result.parsed_content is None
    if framing == "declared":
        assert result.observed_response_bytes == 0
    else:
        assert result.observed_response_bytes > 64


@pytest.mark.parametrize("framing", ["declared", "unterminated_chunked"])
def test_truncated_body_is_transport_failure_without_prefix_digest(
    framing, loopback_only
):
    body = b'{"partial":true}'
    headers = ((b"X-Request-ID", b"visible-before-close"),)
    if framing == "declared":
        raw = response(body, length=128, headers=headers)
    else:
        raw = response(
            chunks(body),
            headers=(*headers, (b"Transfer-Encoding", b"chunked")),
            omit_length=True,
        )
    result = asyncio.run(_dispatch_wire(loopback_only, raw))
    assert result.error is not None and result.error.error_class == "transport"
    assert result.response_status == 200
    assert result.observed_response_bytes == len(body)
    assert result.observation is not None
    assert result.observation.provider_request_id == "visible-before-close"
    assert result.response_digest is None
    assert result.response_bytes is None
    assert result.parsed_content is None


def test_compressed_response_is_rejected_before_decode(loopback_only):
    raw = response(b"opaque", headers=((b"Content-Encoding", b"gzip"),))
    result = asyncio.run(_dispatch_wire(loopback_only, raw))
    assert (
        result.error is not None and result.error.code == "http_content_encoding_denied"
    )
    assert result.observed_response_bytes == 0
    assert result.response_digest is None
    assert result.parsed_content is None


@pytest.mark.parametrize(
    ("body", "code"),
    [
        (b'{"model":"a","model":"b","choices":[]}', "http_json_duplicate_key"),
        (chat_body(content='{"finding":"clear","score":NaN}'), "http_json_nonfinite"),
        (chat_body(content='{"finding":"clear","score":1e999}'), "http_json_nonfinite"),
        (
            chat_body(content='{"finding":"clear","score":0,"score":1}'),
            "http_json_duplicate_key",
        ),
        (
            chat_body(content='{"finding":"clear","score":"1"}'),
            "http_invalid_response_content",
        ),
        (
            chat_body(content='{"finding":"clear","score":1,"more":false}'),
            "http_invalid_response_content",
        ),
        (chat_body(finish_reason="length"), "http_invalid_response_envelope"),
        (chat_body(model="some-other/model"), "http_reported_model_mismatch"),
    ],
)
def test_wire_and_content_validation_preserve_only_digest_on_failure(
    body, code, loopback_only
):
    result = asyncio.run(_dispatch_wire(loopback_only, response(body)))
    assert result.error is not None and result.error.code == code
    assert result.response_digest == sha256(body).hexdigest()
    assert result.response_bytes is None
    assert result.parsed_content is None


@pytest.mark.parametrize(
    ("reported", "code"),
    [
        ("4.3.0", "http_reported_version_mismatch"),
        ("forged", "http_invalid_reported_version"),
    ],
)
def test_version_claims_cannot_override_pinned_versions(reported, code, loopback_only):
    raw = response(chat_body(), headers=((b"X-Jimu-Model-Version", reported.encode()),))
    result = asyncio.run(_dispatch_wire(loopback_only, raw))
    assert result.error is not None and result.error.code == code
    assert result.parsed_content is None


def test_chunk_progress_does_not_extend_total_deadline(loopback_only):
    async def run():
        body = chat_body()

        async def handle(reader, writer):
            await read_request(reader)
            writer.write(
                response(
                    b"", headers=((b"Transfer-Encoding", b"chunked"),), omit_length=True
                )
            )
            for part in (body[:20], body[20:40], body[40:60], body[60:]):
                writer.write(chunks(part))
                await writer.drain()
                await asyncio.sleep(0.08)
            writer.write(b"0\r\n\r\n")
            await writer.drain()

        async with http_peer(loopback_only, handle) as url:
            transport, route = transport_fixture(
                endpoint(url, timeout_ms=900, io_timeout_ms=550),
                route_timeout_ms=260,
            )
            result = await transport.dispatch(request(transport, route))
        assert result.dispatched
        assert result.error is not None and result.error.error_class == "timeout"
        assert result.response_status == 200
        assert 0 < result.observed_response_bytes <= len(body)
        assert result.response_digest is None
        assert result.parsed_content is None

    asyncio.run(run())


def test_io_timeout_before_response_headers_preserves_unknown_receipt(loopback_only):
    async def run():
        received = []

        async def handle(reader, writer):
            received.append(await read_request(reader))
            await reader.read()

        async with http_peer(loopback_only, handle) as url:
            transport, route = transport_fixture(endpoint(url, io_timeout_ms=100))
            result = await transport.dispatch(request(transport, route))
        assert len(received) == 1 and result.dispatched
        assert result.error is not None and result.error.error_class == "timeout"
        assert result.response_status is None
        assert result.observed_response_bytes is None
        assert result.response_digest is None

    asyncio.run(run())


def test_cancelled_socket_is_closed_and_next_call_gets_slot(loopback_only):
    async def run():
        response_started = asyncio.Event()
        client_closed = asyncio.Event()
        requests = []

        async def handle(reader, writer):
            requests.append(await read_request(reader))
            if len(requests) == 1:
                writer.write(response(b"x", length=80))
                await writer.drain()
                response_started.set()
                if await reader.read() == b"":
                    client_closed.set()
            else:
                writer.write(response(chat_body()))
                await writer.drain()

        async with http_peer(loopback_only, handle) as url:
            transport, route = transport_fixture(endpoint(url), host_max_concurrency=1)
            job = asyncio.create_task(transport.dispatch(request(transport, route)))
            await asyncio.wait_for(response_started.wait(), 2)
            # The server has the request and headers were sent, while its body is incomplete.
            job.cancel()
            with pytest.raises(asyncio.CancelledError):
                await job
            await asyncio.wait_for(client_closed.wait(), 2)
            confirmed = await transport.dispatch(
                request(transport, route, dispatch_id="after-cancel")
            )
        assert len(requests) == 2
        assert confirmed.error is None

    asyncio.run(run())


@pytest.mark.parametrize("echo_location", ["header", "escaped_json"])
def test_credential_echo_cannot_reach_results_or_debug_logs(
    echo_location, loopback_only, caplog
):
    async def run():
        token = secrets.token_hex(16)
        resolver = FixtureSecrets(token)
        received_auth = asyncio.Event()
        mask = "".join(f"\\u{ord(ch):04x}" for ch in token)
        body = (
            chat_body(content='{"finding":"' + mask + '","score":1}')
            if echo_location == "escaped_json"
            else chat_body()
        )
        headers = (
            ((b"X-Request-ID", token.encode()), (b"X-Sensitive", token.encode()))
            if echo_location == "header"
            else ()
        )

        async def handle(reader, writer):
            received = await read_request(reader)
            if received.headers.get(b"authorization") == b"Bearer " + token.encode():
                received_auth.set()
            writer.write(response(body, headers=headers))
            await writer.drain()

        async with http_peer(loopback_only, handle) as url:
            cfg = endpoint(url, credential=True, retain_complete_response=True)
            transport, route = transport_fixture(cfg, secrets=resolver)
            outbound = request(transport, route)
            result = await transport.dispatch(outbound)
        assert received_auth.is_set()
        assert resolver.requested == ["fixture-token-ref"]
        assert result.error is not None and result.error.code == "http_credential_echo"
        assert (
            result.observation is None or result.observation.provider_request_id is None
        )
        assert result.response_digest == sha256(body).hexdigest()
        assert result.response_bytes is None
        assert result.response_bytes_omission_reason == "credential_echo"
        assert result.parsed_content is None
        for candidate in (
            repr(resolver),
            repr(cfg),
            repr(outbound),
            repr(result),
            caplog.text,
        ):
            if token in candidate:
                pytest.fail("synthetic bearer leaked through a public representation")
        assert any(record.name == "httpcore.http11" for record in caplog.records)

    caplog.set_level(logging.DEBUG, "httpcore.http11")
    asyncio.run(run())


def test_unauthorized_model_media_secret_and_endpoint_pin_never_send(loopback_only):
    async def run():
        requests = []
        token = secrets.token_hex(16)
        resolver = FixtureSecrets(token)

        async def handle(reader, writer):
            requests.append(await read_request(reader))

        async with http_peer(loopback_only, handle) as url:
            cfg = endpoint(url, credential=True)
            transport, route = transport_fixture(cfg, secrets=resolver)
            approved = request(transport, route)
            altered = json.loads(approved.payload)
            altered["model"] = "not-the-pinned-model"
            wrong_model = await transport.dispatch(
                DispatchRequest("wrong-model", route, json.dumps(altered).encode())
            )

            altered = json.loads(approved.payload)
            altered["messages"][0]["content"].append(
                {"type": "image_url", "image_url": {"url": url}}
            )
            wrong_media = await transport.dispatch(
                DispatchRequest("wrong-media", route, json.dumps(altered).encode())
            )

            altered = json.loads(approved.payload)
            altered["messages"][0]["content"][0]["text"] = token
            exposed = await transport.dispatch(
                DispatchRequest(
                    "credential-in-request", route, json.dumps(altered).encode()
                )
            )

            route.ownership = SimpleNamespace(
                model_id=cfg.model_id,
                endpoint_descriptor_ref=endpoint(url).descriptor_pin,
            )
            wrong_pin = await transport.dispatch(
                DispatchRequest("wrong-pin", route, approved.payload)
            )

        assert not requests
        for result, code in (
            (wrong_model, "http_request_model_mismatch"),
            (wrong_media, "http_invalid_image"),
            (exposed, "http_credential_in_request"),
            (wrong_pin, "http_endpoint_pin_mismatch"),
        ):
            assert not result.dispatched
            assert result.error is not None and result.error.code == code
            assert result.response_status is None
            assert result.response_digest is None

    asyncio.run(run())


def test_credential_required_but_unresolved_and_input_too_large_are_undispatched(
    loopback_only,
):
    async def run():
        received = []

        async def handle(reader, writer):
            received.append(await read_request(reader))

        async with http_peer(loopback_only, handle) as url:
            transport, route = transport_fixture(
                endpoint(url, credential=True), route_input_limit=8192
            )
            missing = await transport.dispatch(request(transport, route))
            oversize = await transport.dispatch(
                DispatchRequest("oversize-input", route, b"x" * 4097)
            )

        assert not received
        assert not missing.dispatched
        assert (
            missing.error is not None
            and missing.error.code == "http_credential_unavailable"
        )
        assert missing.response_digest is None
        assert not oversize.dispatched
        assert oversize.error is not None
        assert (oversize.error.error_class, oversize.error.code) == (
            "resource_exhausted",
            "http_request_too_large",
        )

    asyncio.run(run())


def test_malformed_reply_does_not_log_untrusted_wire_error_details(
    loopback_only, caplog
):
    async def run():
        token = secrets.token_hex(16)
        resolver = FixtureSecrets(token)

        async def handle(reader, writer):
            await read_request(reader)
            # An ordinary incomplete header with a fixture credential included in the line.
            writer.write(b"HTTP/1.1 200 fixture\r\n" + token.encode() + b"\r\n\r\n")
            await writer.drain()

        async with http_peer(loopback_only, handle) as url:
            cfg = endpoint(url, credential=True)
            transport, route = transport_fixture(cfg, secrets=resolver)
            result = await transport.dispatch(request(transport, route))
        assert (
            result.error is not None and result.error.code == "http_transport_failure"
        )
        assert result.response_digest is None
        if token in caplog.text or token in repr(result):
            pytest.fail("synthetic bearer leaked through a transport exception")
        assert any(record.name == "httpcore.http11" for record in caplog.records)

    caplog.set_level(logging.DEBUG, "httpcore.http11")
    asyncio.run(run())


@pytest.mark.parametrize("reported", ["missing", "null"])
@pytest.mark.parametrize("proof", [False, True])
def test_missing_model_requires_independent_version_evidence(
    reported,
    proof,
    loopback_only,
):
    document = json.loads(chat_body(model=None))
    if reported == "null":
        document["model"] = None
    body = json.dumps(document).encode()
    result = asyncio.run(
        _dispatch_wire(loopback_only, response(body), version_evidence=proof)
    )
    assert result.response_digest == sha256(body).hexdigest()
    assert result.observation is None or result.observation.model_id is None
    if proof:
        assert result.error is None
        assert result.validated_response_contract_digest is not None
    else:
        assert (
            result.error is not None
            and result.error.code == "http_reported_model_unavailable"
        )
        assert result.parsed_content is None
        assert result.validated_response_contract_digest is None


def test_question_envelope_rejects_unregistered_slots(loopback_only):
    schema = b'{"type":"object","properties":{"q-one":{"type":"string"}},"required":["q-one"],"additionalProperties":false}'
    body = chat_body(content='{"q-one":"clear","injected":"forged"}')
    result = asyncio.run(
        _dispatch_wire(
            loopback_only,
            response(body),
            response_schema=schema,
            question_slots=("q-one",),
        )
    )
    assert (
        result.error is not None
        and result.error.code == "offline_schema_question_envelope"
    )
    assert result.parsed_content is None
    assert result.invalid_question_slots == ()
    assert result.missing_question_slots == ()


@pytest.mark.parametrize("phase", ["request", "response"])
def test_validation_deadline_preserves_slot_until_worker_really_exits(
    phase,
    loopback_only,
    monkeypatch,
):
    started = threading.Event()
    unblock = threading.Event()
    target = "_check_request" if phase == "request" else "_parse_success"
    original = getattr(http_adapter, target)
    validation_calls = []

    def slow(*args, **kwargs):
        validation_calls.append(threading.get_ident())
        if len(validation_calls) == 1:
            started.set()
            assert unblock.wait(5)
        return original(*args, **kwargs)

    monkeypatch.setattr(http_adapter, target, slow)

    async def run():
        requests = []

        async def handle(reader, writer):
            requests.append(await read_request(reader))
            writer.write(response(chat_body()))
            await writer.drain()

        async with http_peer(loopback_only, handle) as url:
            transport, route = transport_fixture(
                endpoint(url, timeout_ms=150),
                host_max_concurrency=1,
            )
            loop_thread = threading.get_ident()
            first = asyncio.create_task(transport.dispatch(request(transport, route)))
            try:
                assert await asyncio.to_thread(started.wait, 2)
                timed_out = await asyncio.wait_for(first, 1)
                assert (
                    timed_out.error is not None
                    and timed_out.error.error_class == "timeout"
                )
                assert timed_out.dispatched is (phase == "response")
                if phase == "response":
                    assert timed_out.response_digest is not None
                    assert timed_out.response_bytes_omission_reason == "unvalidated"
                second = await transport.dispatch(
                    request(transport, route, dispatch_id="validation-wait")
                )
                assert not second.dispatched
                assert (
                    second.error is not None and second.error.error_class == "timeout"
                )
                assert len(validation_calls) == 1
                assert validation_calls[0] != loop_thread
                assert len(requests) == int(phase == "response")
            finally:
                unblock.set()
            passed = await transport.dispatch(
                request(transport, route, dispatch_id="validation-complete")
            )
            assert passed.error is None
            assert len(requests) == 1 + int(phase == "response")

    asyncio.run(run())


def test_pending_dispatch_rechecks_authority_immediately_before_socket(loopback_only):
    async def run():
        requests = []
        confirmed = []

        async def handle(reader, writer):
            requests.append(await read_request(reader))

        async def reject():
            confirmed.append(True)
            return False

        async with http_peer(loopback_only, handle) as url:
            transport, route = transport_fixture(endpoint(url))
            payload = await transport.prepare_async(
                route, "Return later authorization."
            )
            outcome = await transport.dispatch(
                DispatchRequest(
                    "revoked-after-queue", route, payload, pre_send_allowed=reject
                )
            )
        assert confirmed == [True] and not requests
        assert not outcome.dispatched
        assert outcome.error is not None
        assert outcome.error.code == "http_presend_authorization_lost"

    asyncio.run(run())


@pytest.mark.parametrize(
    "details", [{"cached_tokens": "2"}, {"unknown": 1}, {"cached_tokens": 2**70}]
)
def test_usage_metadata_rejects_unknown_wrong_or_excessive_values(
    details, loopback_only
):
    doc = json.loads(chat_body())
    doc["usage"] = {
        "prompt_tokens": 9,
        "completion_tokens": 1,
        "total_tokens": 10,
        "prompt_tokens_details": details,
    }
    body = json.dumps(doc).encode()
    result = asyncio.run(_dispatch_wire(loopback_only, response(body)))
    assert (
        result.error is not None
        and result.error.code == "http_invalid_response_envelope"
    )
    assert result.parsed_content is None
    assert result.response_digest == sha256(body).hexdigest()


@pytest.mark.parametrize("phase", ["before_send", "sent_unknown", "acknowledged"])
def test_cancellation_reports_only_the_observed_transport_boundary(
    phase, loopback_only, monkeypatch
):
    async def run():
        reached = asyncio.Event()
        closed = asyncio.Event()
        hold = asyncio.Event()
        requests = []
        original_framing = http_adapter._check_response_framing

        def watch_headers(headers, limit):
            original_framing(headers, limit)
            reached.set()

        async def handle(reader, writer):
            requests.append(await read_request(reader))
            if phase == "acknowledged":
                writer.write(
                    response(
                        b"x", length=80, headers=((b"X-Request-ID", b"cancel-visible"),)
                    )
                )
                await writer.drain()
            else:
                reached.set()
            if await reader.read() == b"":
                closed.set()

        async def authorize():
            if phase == "before_send":
                reached.set()
                await hold.wait()
            return True

        if phase == "acknowledged":
            monkeypatch.setattr(http_adapter, "_check_response_framing", watch_headers)
        async with http_peer(loopback_only, handle) as url:
            transport, route = transport_fixture(
                endpoint(url, retain_complete_response=True)
            )
            payload = await transport.prepare_async(
                route, "Validate cancellation receipt."
            )
            task = asyncio.create_task(
                transport.dispatch(
                    DispatchRequest(
                        "cancel-transport", route, payload, pre_send_allowed=authorize
                    )
                )
            )
            await asyncio.wait_for(reached.wait(), 1)
            task.cancel()
            with pytest.raises(asyncio.CancelledError) as catching:
                await task
            assert type(catching.value) is asyncio.CancelledError
            receipt = TransportCancelled.observe(catching.value)
            assert receipt is not None and receipt.error is not None
            assert receipt.error.error_class == "cancelled"
            assert receipt.response_digest is None
            assert receipt.response_bytes is None and receipt.parsed_content is None
            assert receipt.dispatched is (phase != "before_send")
            if phase == "acknowledged":
                assert receipt.response_status == 200
                assert receipt.observation is not None
                assert receipt.observation.provider_request_id == "cancel-visible"
            else:
                assert receipt.response_status is None and receipt.observation is None
            if phase != "before_send":
                await asyncio.wait_for(closed.wait(), 1)
        assert len(requests) == int(phase != "before_send")

    asyncio.run(run())


def test_callers_asyncio_timeout_stays_a_timeout(loopback_only):
    async def run():
        async def handle(_reader, _writer):
            pytest.fail("cancelled authorization gate must not send")

        async def authorize():
            await asyncio.Event().wait()
            return True

        async with http_peer(loopback_only, handle) as url:
            transport, route = transport_fixture(endpoint(url))
            payload = await transport.prepare_async(
                route, "Wait for granted authority."
            )
            with pytest.raises(TimeoutError) as timed_out:
                async with asyncio.timeout(0.05):
                    await transport.dispatch(
                        DispatchRequest(
                            "caller-timeout", route, payload, pre_send_allowed=authorize
                        )
                    )
        cancellation = timed_out.value.__cause__
        assert type(cancellation) is asyncio.CancelledError
        receipt = TransportCancelled.observe(cancellation)
        assert receipt is not None and not receipt.dispatched

    asyncio.run(run())
