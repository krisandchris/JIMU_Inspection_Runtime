"""API byte bounds, strict JSON and redaction at the ASGI request boundary."""

import asyncio
import logging
import secrets
from unittest.mock import AsyncMock, Mock

import httpx
import pytest
from sqlalchemy.exc import MultipleResultsFound
from sqlalchemy.exc import TimeoutError as PoolTimeout
from sqlalchemy.ext.asyncio import AsyncEngine

from jimu_runtime.api.app import create_app
from jimu_runtime.api.auth import Identity
from jimu_runtime.api.boundary import ApiLimits
from jimu_runtime.api.records import RuntimeRecords
from jimu_runtime.domain.runtime.authority import ActorRef
from jimu_runtime.workflow.intake import IntakeService


def test_boundary_rejects_oversize_ambiguity_and_invalid_json_before_intake(
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def run() -> None:
        token = secrets.token_urlsafe(32)
        sentinel = "redact-this-payload"
        actor = ActorRef(
            actor_kind="human", actor_id="tester", identity_registry_id="test"
        )
        scope_gate = AsyncMock(return_value=True)
        authority = Mock(
            authenticate=AsyncMock(
                side_effect=lambda credential: Identity(object(), actor)
                if credential == token
                else None
            ),
            allows=scope_gate,
        )
        intake = Mock(spec=IntakeService)
        app = create_app(
            Mock(spec=AsyncEngine),
            intake,
            authority=authority,
            limits=ApiLimits(
                json_bytes=512,
                query_bytes=128,
                query_fields=2,
                body_timeout_seconds=0.05,
            ),
        )
        async with httpx.AsyncClient(
            base_url="http://127.0.0.1", transport=httpx.ASGITransport(app=app)
        ) as client:
            endpoint = "/api/v1/inference/tasks"
            good_headers = [
                ("Authorization", "Bearer " + token),
                ("X-Jimu-Project", "project"),
                ("X-Jimu-Client", "client"),
                ("Content-Type", "application/json"),
            ]
            cases = [
                ({}, sentinel, 401),
                (good_headers + [good_headers[0]], sentinel, 400),
                (good_headers + [("X-Jimu-Client", "other")], sentinel, 400),
                (good_headers, '{"a": {"x": 0, "x": "' + sentinel + '"}}', 422),
                (good_headers, '{"a": NaN}', 422),
                (good_headers, '{"a": "' + sentinel + '"}', 422),
                (good_headers, "x" * 513, 413),
                (good_headers + [("Content-Encoding", "gzip")], sentinel, 415),
            ]
            for headers, body, expected in cases:
                response = await client.post(endpoint, headers=headers, content=body)
                assert response.status_code == expected, response.text
                assert sentinel not in response.text
                assert token not in response.text
                assert response.headers["cache-control"] == "no-store"
                assert response.json()["request_id"] == response.headers["x-request-id"]

            async def chunked():
                yield b"x" * 300
                yield b"x" * 300

            response = await client.post(
                endpoint, headers=good_headers, content=chunked()
            )
            assert response.status_code == 413

            response = await client.post(
                endpoint + "?limit=1&limit=2", headers=good_headers
            )
            assert response.status_code == 400
            assert (
                await client.post(endpoint + "?a=1&b=2&c=3", headers=good_headers)
            ).json()["code"] == "too_many_query_parameters"
            assert (
                await client.post(endpoint + "?name=" + "x" * 129, headers=good_headers)
            ).status_code == 414

            async def stalled_body():
                yield b"{"
                await asyncio.Event().wait()

            stalled = await client.post(
                endpoint, headers=good_headers, content=stalled_body()
            )
            assert stalled.status_code == 408
            assert stalled.json()["code"] == "request_body_timeout"
            intake.submit.assert_not_awaited()

            resource = endpoint + "/task"
            for error, status in (
                (MultipleResultsFound(sentinel), 500),
                (PoolTimeout(sentinel), 503),
            ):
                fake_primary = AsyncMock(side_effect=error)
                with monkeypatch.context() as fault:
                    fault.setattr(RuntimeRecords, "primary", fake_primary)
                    result = await client.get(resource, headers=good_headers)
                assert result.status_code == status
                assert sentinel not in result.text

            assert sentinel not in caplog.text
            assert token not in caplog.text
            audit = [
                record
                for record in caplog.records
                if record.name == "jimu_runtime.api.boundary"
            ]
            assert audit
            standard = set(logging.LogRecord("", 0, "", 0, "", (), None).__dict__)
            for record in audit:
                extra = {
                    k: v
                    for k, v in record.__dict__.items()
                    if k not in standard | {"message", "asctime"}
                }
                assert extra.keys() == {
                    "request_id",
                    "route_name",
                    "http_status",
                    "outcome_code",
                }
                assert token not in repr(extra) and sentinel not in repr(extra)
                assert record.message == "runtime_api_request"

    with caplog.at_level(logging.INFO, logger="jimu_runtime.api.boundary"):
        asyncio.run(run())
