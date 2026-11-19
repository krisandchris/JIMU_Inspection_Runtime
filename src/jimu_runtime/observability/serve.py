"""Small ASGI adapter for one authorized Prometheus target."""

from collections.abc import Awaitable, Callable
from hmac import compare_digest
from os import environ
from pathlib import Path
from typing import TypedDict

from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncEngine

from jimu_runtime.domain.runtime.intake import IdempotencyScope
from jimu_runtime.observability.postgres import (
    ObservabilityUnavailable,
    PostgresObservability,
)
from jimu_runtime.workflow.postgres import create_postgres_engine

type _Headers = list[tuple[bytes, bytes]]
type AuthorizeScrape = Callable[[_Headers], Awaitable[IdempotencyScope | None]]
type _Send = Callable[[dict[str, object]], Awaitable[None]]
type _Receive = Callable[[], Awaitable[dict[str, object]]]


class _Scope(TypedDict, total=False):
    type: str
    path: str
    method: str
    headers: _Headers


class ScrapeApplication:
    """No URL parameter selects an owner or a resource for this target."""

    def __init__(
        self,
        reader: PostgresObservability,
        scope: IdempotencyScope,
        authorize: AuthorizeScrape,
        *,
        engine: AsyncEngine | None = None,
    ) -> None:
        self._reader = reader
        self._scope = IdempotencyScope.model_validate(scope)
        self._authorize = authorize
        self._engine = engine

    async def __call__(self, request: _Scope, receive: _Receive, send: _Send) -> None:
        if request["type"] == "lifespan":
            while True:
                event = await receive()
                if event["type"] == "lifespan.startup":
                    await send({"type": "lifespan.startup.complete"})
                if event["type"] == "lifespan.shutdown":
                    if self._engine is not None:
                        await self._engine.dispose()
                    await send({"type": "lifespan.shutdown.complete"})
                    return
        if request["type"] != "http":
            raise ValueError("unsupported observability transport")
        path = request["path"]
        if path not in ("/livez", "/readyz", "/metrics"):
            await _reply(send, 404, b"not found\n")
            return
        if request["method"] != "GET":
            await _reply(send, 405, b"method not allowed\n", extra=[(b"allow", b"GET")])
            return
        if path == "/livez":
            await _reply(send, 200, b"ok\n")
            return
        headers = request.get("headers", [])
        if (
            len(headers) > 64
            or sum(len(name) + len(value) for name, value in headers) > 8192
        ):
            await _reply(send, 431, b"request headers too large\n")
            return
        try:
            owner = await self._authorize(headers)
        except Exception:
            await _reply(send, 503, b"unavailable\n")
            return
        if owner != self._scope:
            await _reply(
                send, 401, b"unauthorized\n", extra=[(b"www-authenticate", b"Bearer")]
            )
            return
        if path == "/readyz":
            status = await self._reader.readiness(self._scope)
            await _reply(
                send,
                200 if status.ready else 503,
                status.model_dump_json().encode(),
                b"application/json",
            )
            return
        try:
            output = await self._reader.metrics(self._scope)
        except (ObservabilityUnavailable, ValueError):
            await _reply(send, 503, b"unavailable\n")
            return
        await _reply(send, 200, output, b"text/plain; version=0.0.4; charset=utf-8")


async def _reply(
    send: _Send,
    status: int,
    body: bytes,
    mime: bytes = b"text/plain; charset=utf-8",
    *,
    extra: _Headers | None = None,
) -> None:
    headers = [(b"content-type", mime), (b"cache-control", b"no-store")]
    if extra:
        headers.extend(extra)
    await send({"type": "http.response.start", "status": status, "headers": headers})
    await send({"type": "http.response.body", "body": body})


def _secret(name: str) -> bytes:
    with Path(environ[name]).open("rb") as secret:
        value = secret.read(4097)
    if not 1 <= len(value) <= 4096:
        raise ValueError("observability credential file has invalid size")
    return value.rstrip(b"\n")


def create_app() -> ScrapeApplication:
    """Factory for trusted single-scope deployment, with secret-file credentials."""
    try:
        scope = IdempotencyScope(
            project_id=environ["JIMU_OBSERVABILITY_PROJECT_ID"],
            client_id=environ["JIMU_OBSERVABILITY_CLIENT_ID"],
        )
        token = _secret("JIMU_OBSERVABILITY_TOKEN_FILE")
        if len(token) < 32 or any(not 33 <= byte <= 126 for byte in token):
            raise ValueError("invalid scrape credential")
        url = make_url(_secret("JIMU_OBSERVABILITY_DB_URL_FILE").decode("utf-8"))
        engine = create_postgres_engine(url)
    except Exception:
        raise RuntimeError(
            "observability deployment configuration is invalid"
        ) from None

    async def authorize(headers: _Headers) -> IdempotencyScope | None:
        return scope if _valid_bearer(headers, token) else None

    return ScrapeApplication(
        PostgresObservability(engine), scope, authorize, engine=engine
    )


def _valid_bearer(headers: _Headers, token: bytes) -> bool:
    supplied = [value for name, value in headers if name.lower() == b"authorization"]
    if len(supplied) != 1:
        return False
    scheme, delim, credential = supplied[0].partition(b" ")
    return (
        bool(delim)
        and scheme.lower() == b"bearer"
        and compare_digest(credential.lstrip(b" "), token)
    )
