import asyncio
from unittest.mock import AsyncMock, Mock

import pytest

from jimu_runtime.domain.runtime.intake import IdempotencyScope
from jimu_runtime.observability.contracts import Readiness
from jimu_runtime.observability.postgres import ObservabilityUnavailable
from jimu_runtime.observability.serve import ScrapeApplication

_SCOPE = IdempotencyScope(project_id="fixture.project", client_id="fixture.client")


async def _request(app, path, *, headers=(), method="GET"):
    output = []

    async def send(event):
        output.append(event)

    await app(
        {"type": "http", "path": path, "method": method, "headers": list(headers)},
        AsyncMock(),
        send,
    )
    return output[0]["status"], output[1]["body"]


def test_readiness_and_scrape_require_same_verified_scope():
    async def run():
        reader = Mock()
        reader.metrics = AsyncMock(return_value=b"metrics\n")
        reader.readiness = AsyncMock(
            return_value=Readiness(
                database="ready",
                workflow_schema="ready",
                worker_claim="unknown",
            )
        )
        authorize = AsyncMock(return_value=None)
        app = ScrapeApplication(reader, _SCOPE, authorize)
        assert await _request(app, "/livez") == (200, b"ok\n")
        authorize.assert_not_awaited()
        assert (
            await _request(app, "/readyz", headers=((b"authorization", b"x" * 8192),))
        )[0] == 431
        authorize.assert_not_awaited()
        assert (await _request(app, "/readyz"))[0] == 401
        authorize.return_value = IdempotencyScope(
            project_id=_SCOPE.project_id, client_id="other.client"
        )
        assert (await _request(app, "/metrics"))[0] == 401
        reader.metrics.assert_not_awaited()
        reader.readiness.assert_not_awaited()
        authorize.return_value = _SCOPE
        assert await _request(app, "/metrics") == (200, b"metrics\n")
        assert (await _request(app, "/readyz"))[0] == 200
        reader.metrics.assert_awaited_once_with(_SCOPE)
        assert (await _request(app, "/metrics", method="POST"))[0] == 405
        assert (await _request(app, "/api/traces"))[0] == 404

    asyncio.run(run())


@pytest.mark.parametrize("path", ("/metrics", "/readyz"))
def test_failures_expose_operational_status_only(path):
    async def run():
        reader = Mock()
        reader.metrics = AsyncMock(
            side_effect=ObservabilityUnavailable("sensitive database diagnostic")
        )
        reader.readiness = AsyncMock(
            return_value=Readiness(
                database="unavailable",
                workflow_schema="unknown",
                worker_claim="unknown",
            )
        )
        app = ScrapeApplication(reader, _SCOPE, AsyncMock(return_value=_SCOPE))
        status, body = await _request(app, path)
        assert status == 503 and b"sensitive" not in body

    asyncio.run(run())


def test_bearer_scheme_is_case_insensitive_but_token_bytes_are_exact():
    from jimu_runtime.observability.serve import _valid_bearer

    token = b"FixtureToken"
    assert _valid_bearer([(b"Authorization", b"bEaReR  FixtureToken")], token)
    assert not _valid_bearer([(b"authorization", b"bearer fixturetoken")], token)
    assert not _valid_bearer([(b"authorization", b"Basic FixtureToken")], token)
    assert not _valid_bearer([(b"authorization", b"Bearer FixtureToken ")], token)
    assert not _valid_bearer(
        [
            (b"authorization", b"Bearer FixtureToken"),
            (b"AUTHORIZATION", b"Bearer FixtureToken"),
        ],
        token,
    )
