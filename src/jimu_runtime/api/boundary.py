"""HTTP authentication, safe error handling and bounded bodies."""

import json
import logging
from asyncio import timeout
from collections.abc import Callable, Coroutine
from dataclasses import dataclass
from math import isfinite
from re import fullmatch
from typing import cast
from uuid import uuid4

from fastapi import Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute
from pydantic import ValidationError
from sqlalchemy.exc import DBAPIError
from sqlalchemy.exc import TimeoutError as PoolTimeout
from starlette.requests import ClientDisconnect

from jimu_runtime.api.auth import Access, Action, ApiAuthority, Resource
from jimu_runtime.api.contracts import ErrorResponse
from jimu_runtime.bundle.store import StoreBusy, StoreConflict
from jimu_runtime.domain.base import ContractModel
from jimu_runtime.domain.runtime.authority import ActorRef
from jimu_runtime.domain.runtime.intake import IdempotencyScope
from jimu_runtime.inspection.media.types import MediaError
from jimu_runtime.workflow.intake import IntakeAuthorizationError, IntakeError

_LOGGER = logging.getLogger(__name__)


class ApiError(Exception):
    def __init__(self, status: int, code: str) -> None:
        self.status = status
        self.code = code
        super().__init__(code)


@dataclass(frozen=True, slots=True)
class ApiLimits:
    json_bytes: int = 256 * 1024
    trace_bytes: int = 256 * 1024
    evidence_bytes: int = 4 * 1024 * 1024
    query_bytes: int = 4096
    query_fields: int = 20
    body_timeout_seconds: float = 15.0
    admission_concurrency: int = 2

    def __post_init__(self) -> None:
        bounded = (
            (self.json_bytes, 512, 128 * 1024 * 1024),
            (self.trace_bytes, 512, 128 * 1024 * 1024),
            (self.evidence_bytes, 1, 128 * 1024 * 1024),
            (self.query_bytes, 128, 16 * 1024),
            (self.query_fields, 1, 100),
            (self.admission_concurrency, 1, 64),
        )
        if any(
            type(value) is not int or not low <= value <= high
            for value, low, high in bounded
        ):
            raise ValueError("invalid API limit")
        seconds = self.body_timeout_seconds
        if (
            type(seconds) not in (float, int)
            or not isfinite(seconds)
            or not 0 < seconds <= 120
        ):
            raise ValueError("invalid request body deadline")


def _retryable_database(error: DBAPIError) -> bool:
    sqlstate = getattr(error.orig, "sqlstate", None)
    return error.connection_invalidated or (
        isinstance(sqlstate, str)
        and (
            sqlstate.startswith("08")
            or sqlstate
            in {
                "40001",
                "40P01",
                "55P03",
                "57P01",
                "57P02",
                "57P03",
            }
        )
    )


def _check_query(request: Request, limits: ApiLimits) -> None:
    if len(request.scope.get("query_string", b"")) > limits.query_bytes:
        raise ApiError(414, "query_too_large")
    fields: set[str] = set()
    for field, _ in request.query_params.multi_items():
        if field in fields:
            raise ApiError(400, "ambiguous_query_parameter")
        if len(fields) >= limits.query_fields:
            raise ApiError(400, "too_many_query_parameters")
        fields.add(field)


def _error(request_id: str, status: int, code: str) -> JSONResponse:
    headers = {"WWW-Authenticate": "Bearer"} if status == 401 else None
    return JSONResponse(
        ErrorResponse(code=code, request_id=request_id).model_dump(),
        status_code=status,
        headers=headers,
    )


def single_header(request: Request, name: str) -> str | None:
    values = request.headers.getlist(name)
    if len(values) > 1:
        raise ApiError(400, "ambiguous_request_header")
    return values[0] if values else None


class SafeRoute(APIRoute):
    """Keep credentials, payloads, resource IDs and exception details out of audit logs."""

    def get_route_handler(
        self,
    ) -> Callable[[Request], Coroutine[object, object, Response]]:
        handler = super().get_route_handler()

        async def safe(request: Request) -> Response:
            request_id = uuid4().hex
            code = "ok"
            try:
                _check_query(request, request.app.state.api_limits)
                response = await handler(request)
            except ApiError as error:
                code = error.code
                response = _error(request_id, error.status, code)
            except RequestValidationError:
                code = "invalid_request"
                response = _error(request_id, 422, code)
            except IntakeAuthorizationError:
                code = "access_denied"
                response = _error(request_id, 403, code)
            except IntakeError as error:
                if error.code == "intake_submission_too_large":
                    code = "request_too_large"
                    response = _error(request_id, 413, code)
                else:
                    code = "intake_pending" if error.retryable else "intake_rejected"
                    response = _error(request_id, 503 if error.retryable else 409, code)
            except MediaError as error:
                if str(error) in {
                    "original_media_unavailable",
                    "original_media_identity_conflict",
                }:
                    code = "original_media_unavailable"
                    response = _error(request_id, 409, code)
                else:
                    code = "runtime_media_failed"
                    response = _error(request_id, 500, code)
            except StoreConflict:
                code = "operation_conflict"
                response = _error(request_id, 409, code)
            except (StoreBusy, PoolTimeout):
                code = "runtime_unavailable"
                response = _error(request_id, 503, code)
            except DBAPIError as error:
                retryable = _retryable_database(error)
                code = "runtime_unavailable" if retryable else "runtime_request_failed"
                response = _error(request_id, 503 if retryable else 500, code)
            except ClientDisconnect:
                code = "request_interrupted"
                response = _error(request_id, 400, code)
            except Exception:
                code = "runtime_request_failed"
                response = _error(request_id, 500, code)
            response.headers["Cache-Control"] = "no-store"
            response.headers["X-Content-Type-Options"] = "nosniff"
            response.headers["X-Request-ID"] = request_id
            _LOGGER.info(
                "runtime_api_request",
                extra={
                    "request_id": request_id,
                    "route_name": self.name,
                    "http_status": response.status_code,
                    "outcome_code": code,
                },
            )
            return response

        return safe


class HttpBoundary:
    def __init__(self, authority: ApiAuthority, limits: ApiLimits) -> None:
        self.authority = authority
        self.limits = limits

    async def access(self, request: Request) -> Access:
        header = single_header(request, "authorization")
        if header is None or len(header) > 8192:
            raise ApiError(401, "authentication_required")
        matched = fullmatch(r"(?i:Bearer) ([A-Za-z0-9._~+/-]+={0,2})", header)
        if matched is None:
            raise ApiError(401, "authentication_required")
        identity = await self.authority.authenticate(matched[1])
        if identity is None:
            raise ApiError(401, "authentication_required")
        ActorRef.model_validate(identity.actor)
        project = single_header(request, "x-jimu-project")
        client = single_header(request, "x-jimu-client")
        try:
            scope = IdempotencyScope.model_validate(
                {"project_id": project, "client_id": client}
            )
        except ValidationError:
            raise ApiError(422, "invalid_scope") from None
        access = Access(identity, scope)
        await self.require(access, Resource(action=Action.ACCESS_SCOPE, scope=scope))
        return access

    async def require(
        self, access: Access, resource: Resource, *, conceal: bool = False
    ) -> None:
        if (
            resource.scope != access.scope
            or await self.authority.allows(access.identity, resource) is not True
        ):
            raise (
                ApiError(404, "resource_not_found")
                if conceal
                else ApiError(403, "access_denied")
            )


def _unique_pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
    body: dict[str, object] = {}
    for key, value in pairs:
        if key in body:
            raise ValueError("duplicate JSON key")
        body[key] = value
    return body


def _nonfinite(_value: str) -> object:
    raise ValueError("nonfinite JSON value")


async def read_body(
    request: Request, *, media_type: str, limit: int, deadline_seconds: float
) -> bytes:
    content_type = single_header(request, "content-type")
    if (
        content_type is None
        or content_type.split(";", 1)[0].strip().lower() != media_type
    ):
        raise ApiError(415, "unsupported_content_type")
    encoding = single_header(request, "content-encoding")
    if encoding not in (None, "identity"):
        raise ApiError(415, "unsupported_content_encoding")
    length = single_header(request, "content-length")
    if length is not None:
        if fullmatch(r"[0-9]{1,12}", length) is None:
            raise ApiError(400, "invalid_content_length")
        if int(length) > limit:
            raise ApiError(413, "request_too_large")
    data = bytearray()
    try:
        async with timeout(deadline_seconds):
            async for chunk in request.stream():
                if len(chunk) > limit - len(data):
                    raise ApiError(413, "request_too_large")
                data.extend(chunk)
    except TimeoutError:
        raise ApiError(408, "request_body_timeout") from None
    if length is not None and len(data) != int(length):
        raise ApiError(400, "invalid_content_length")
    return bytes(data)


async def read_json[T: ContractModel](
    request: Request, model: type[T], limits: ApiLimits
) -> T:
    raw = await read_body(
        request,
        media_type="application/json",
        limit=limits.json_bytes,
        deadline_seconds=limits.body_timeout_seconds,
    )
    try:
        decoded = raw.decode("utf-8")
        checked = cast(
            object,
            json.loads(
                decoded, object_pairs_hook=_unique_pairs, parse_constant=_nonfinite
            ),
        )
        if not isinstance(checked, dict):
            raise ValueError("expected a JSON object")
    except (ValueError, RecursionError):
        raise ApiError(422, "invalid_json") from None
    try:
        return model.model_validate_json(raw)
    except ValidationError:
        raise ApiError(422, "invalid_request") from None
