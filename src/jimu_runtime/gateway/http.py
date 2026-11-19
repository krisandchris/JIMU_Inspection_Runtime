"""Async, single-dispatch HTTP adapter for trusted Chat Completions endpoints."""

import asyncio
import json
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from hashlib import sha256
from ipaddress import ip_address
from typing import Annotated, Literal, Protocol, Self

import httpx
from pydantic import (
    ConfigDict,
    Field,
    SecretStr,
    TypeAdapter,
    ValidationError,
    model_validator,
)

from jimu_runtime.bundle.trust import TrustPin
from jimu_runtime.domain.base import ContractModel
from jimu_runtime.domain.primitives import ArtifactId, Digest, RecordId, Version
from jimu_runtime.domain.runtime.invocations import InvocationError, ProviderKind
from jimu_runtime.gateway.http_wire import (
    ChatContract as ChatContract,
)
from jimu_runtime.gateway.http_wire import (
    ChatResponse,
    VllmChatResponse,
    WireError,
    approved_model_id,
    contains_text,
    encode_chat,
    strict_json,
    validate_chat_request,
)
from jimu_runtime.gateway.http_wire import (
    VerifiedImage as VerifiedImage,
)
from jimu_runtime.gateway.invocation import (
    DispatchRequest,
    ProviderObservation,
    ResponseBytesOmissionReason,
    TransportCancelled,
    TransportResult,
)
from jimu_runtime.gateway.routing import PlannedRoute

type ChatProtocol = Literal["openai_chat_completions", "vllm_openai_chat_completions"]
type HTTPErrorClass = Literal[
    "transport",
    "timeout",
    "rate_limit",
    "unavailable",
    "validation",
    "association",
    "resource_exhausted",
    "cancelled",
]

_MIB = 1024 * 1024
_BEARER = re.compile(r"[A-Za-z0-9._~+/-]{8,2048}=*")
_RECORD_ID = TypeAdapter[RecordId](RecordId)
_VERSION = TypeAdapter[Version](Version)
_DEPLOYMENT_VERSION = "x-jimu-deployment-version"
_MODEL_VERSION = "x-jimu-model-version"
_DAY = r"(?:Mon|Tue|Wed|Thu|Fri|Sat|Sun)"
_FULL_DAY = r"(?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday)"
_MONTH = r"(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)"
_CLOCK = r"[0-9]{2}:[0-9]{2}:[0-9]{2}"
_ASCTIME_DATE = re.compile(
    rf"{_DAY} {_MONTH} (?:[0-9]{{2}}| [0-9]) {_CLOCK} [0-9]{{4}}"
)
_IMF_DATE = re.compile(rf"{_DAY}, [0-9]{{2}} {_MONTH} [0-9]{{4}} {_CLOCK} GMT")
_RFC850_DATE = re.compile(rf"{_FULL_DAY}, [0-9]{{2}}-{_MONTH}-[0-9]{{2}} {_CLOCK} GMT")
_IMAGE_LIMIT_CODE = frozenset({"http_image_too_large", "http_request_too_large"})


class HttpLimits(ContractModel):
    timeout_ms: Annotated[int, Field(ge=1, le=600_000)]
    io_timeout_ms: Annotated[int, Field(ge=1, le=600_000)]
    request_bytes: Annotated[int, Field(ge=1, le=16 * _MIB)]
    response_bytes: Annotated[int, Field(ge=1, le=8 * _MIB)]
    image_bytes: Annotated[int, Field(ge=1, le=8 * _MIB)]
    max_retry_after_seconds: Annotated[int, Field(ge=0, le=86_400)]


class ChatEndpoint(ContractModel):
    """Deployment-owned endpoint descriptor. Never resolved from a Bundle field."""

    model_config = ConfigDict(hide_input_in_errors=True)
    endpoint_id: ArtifactId
    endpoint_version: Version
    provider_id: ArtifactId
    deployment_id: ArtifactId
    provider_kind: ProviderKind
    model_id: Annotated[str, Field(min_length=1, max_length=128)]
    url: Annotated[str, Field(min_length=1, max_length=2048, repr=False)]
    protocol: ChatProtocol
    credential_ref: ArtifactId | None = Field(default=None, repr=False)
    allow_plaintext_loopback: bool = False
    retain_complete_response: bool = False
    limits: HttpLimits

    @model_validator(mode="after")
    def approved_endpoint(self) -> Self:
        try:
            url = httpx.URL(self.url)
        except (httpx.InvalidURL, ValueError):
            raise ValueError("http_invalid_endpoint") from None
        if (
            not url.is_absolute_url
            or url.userinfo
            or "@" in self.url.partition("://")[2].partition("/")[0]
            or "?" in self.url
            or "#" in self.url
            or "\\" in self.url
            or not url.path.endswith("/chat/completions")
            or url.scheme not in {"http", "https"}
        ):
            raise ValueError("http_invalid_endpoint")
        if url.scheme == "http" and not (
            self.allow_plaintext_loopback and _is_loopback(url)
        ):
            raise ValueError("http_plaintext_denied")
        if not approved_model_id(self.model_id):
            raise ValueError("http_invalid_model_id")
        if (self.protocol == "vllm_openai_chat_completions") != (
            self.provider_kind == "local"
        ):
            raise ValueError("http_protocol_provider_mismatch")
        return self

    @property
    def descriptor_pin(self) -> TrustPin:
        document = self.model_dump(mode="json")
        document["url"] = str(httpx.URL(self.url))
        payload = json.dumps(document, sort_keys=True, separators=(",", ":")).encode()
        return TrustPin(
            identity=self.endpoint_id,
            version=self.endpoint_version,
            sha256=sha256(payload).hexdigest(),
        )


class SecretResolver(Protocol):
    def resolve(self, credential_ref: ArtifactId) -> SecretStr | None:
        """Resolve a bearer token from an already available host secret source."""


def _is_loopback(url: httpx.URL) -> bool:
    try:
        return ip_address(url.host).is_loopback
    except ValueError:
        return False


def _safe_header(headers: httpx.Headers, name: str, secret: str | None) -> str | None:
    values = headers.get_list(name)
    if (
        len(values) != 1
        or not values[0]
        or (secret is not None and secret in values[0])
    ):
        return None
    return values[0]


def _request_id(headers: httpx.Headers, secret: str | None) -> RecordId | None:
    try:
        return _RECORD_ID.validate_python(_safe_header(headers, "x-request-id", secret))
    except ValidationError:
        return None


def _reported_version(
    headers: httpx.Headers, name: str, secret: str | None
) -> Version | None:
    try:
        return _VERSION.validate_python(_safe_header(headers, name, secret))
    except ValidationError:
        return None


def _retry_after(value: str | None, limit: int) -> float | None:
    if value is None or len(value) > 128:
        return None
    if value.isascii() and value.isdecimal():
        return float(min(int(value), limit))
    asctime = _ASCTIME_DATE.fullmatch(value) is not None
    rfc850 = _RFC850_DATE.fullmatch(value) is not None
    if not (asctime or rfc850 or _IMF_DATE.fullmatch(value)):
        return None
    try:
        date = parsedate_to_datetime(value)
        now = datetime.now(UTC)
        if asctime:
            date = date.replace(tzinfo=UTC)
        elif rfc850:
            year = now.year // 100 * 100 + date.year % 100
            if year < now.year - 50:
                year += 100
            if year > now.year + 50:
                year -= 100
            date = date.replace(year=year)
        delay = (date - now).total_seconds()
    except (ValueError, TypeError, OverflowError):
        return None
    return min(max(delay, 0.0), float(limit))


async def _strip_trace_data(event: str, details: dict[str, object]) -> None:
    # httpcore's trace extension sees the terminal event before its DEBUG
    # formatter. The terminal dict may contain raw response headers or errors.
    # Started-event dicts also serve as live request arguments and stay intact.
    if event.endswith((".complete", ".failed")):
        details.clear()


@dataclass(slots=True)
class _Receipt:
    dispatch_id: RecordId
    dispatched: bool = False
    status: int | None = None
    observed_bytes: int | None = None
    digest: Digest | None = None
    response_bytes: bytes | None = field(default=None, repr=False)
    request_id: RecordId | None = None
    model_id: str | None = None
    deployment_version: Version | None = None
    model_version: Version | None = None
    retry_after_seconds: float | None = None
    validated_digest: Digest | None = None
    normalized_digest: Digest | None = None
    normalized_bytes: int | None = None
    invalid_question_slots: tuple[ArtifactId, ...] = ()
    missing_question_slots: tuple[ArtifactId, ...] = ()

    def result(
        self,
        *,
        error: InvocationError | None = None,
        parsed_content: bytes | None = None,
    ) -> TransportResult:
        observation = None
        if (
            self.request_id is not None
            or self.model_id is not None
            or self.deployment_version is not None
            or self.model_version is not None
        ):
            observation = ProviderObservation(
                provider_request_id=self.request_id,
                model_id=self.model_id,
                deployment_version=self.deployment_version,
                model_version=self.model_version,
            )
        raw = self.response_bytes if error is None else None
        omission: ResponseBytesOmissionReason | None = None
        if self.dispatched and raw is None:
            if self.digest is None:
                omission = "incomplete"
            elif error is None:
                omission = "policy_disabled"
            elif error.code == "http_credential_echo":
                omission = "credential_echo"
            else:
                omission = "unvalidated"
        return TransportResult(
            dispatched=self.dispatched,
            response_dispatch_id=self.dispatch_id,
            error=error,
            observation=observation,
            response_digest=self.digest,
            response_bytes=raw,
            response_bytes_omission_reason=omission,
            parsed_content=parsed_content,
            normalized_response_digest=self.normalized_digest
            if parsed_content is not None
            else None,
            normalized_response_bytes=self.normalized_bytes
            if parsed_content is not None
            else None,
            retry_after_seconds=self.retry_after_seconds,
            response_status=self.status,
            observed_response_bytes=self.observed_bytes,
            validated_response_contract_digest=self.validated_digest
            if error is None
            else None,
            invalid_question_slots=self.invalid_question_slots if error is None else (),
            missing_question_slots=self.missing_question_slots if error is None else (),
        )

    def failed(self, error_class: HTTPErrorClass, code: ArtifactId) -> TransportResult:
        return self.result(
            error=InvocationError(
                error_class=error_class, code=code, diagnostic_refs=()
            )
        )


def _status_failure(status: int) -> tuple[HTTPErrorClass, ArtifactId] | None:
    if 200 <= status < 300:
        return None
    if status == 429:
        return "rate_limit", "http_rate_limited"
    if status == 408:
        return "timeout", "http_request_timeout"
    if status >= 500:
        return "unavailable", "http_service_unavailable"
    if status in {401, 403}:
        return "validation", "http_auth_rejected"
    if 300 <= status < 400:
        return "validation", "http_redirect_denied"
    return "validation", "http_status_rejected"


def _echoes_credential(response: httpx.Response, body: bytearray, secret: str) -> bool:
    token = secret.encode("ascii")
    phrase = response.extensions.get("reason_phrase")
    return (
        token in body
        or (isinstance(phrase, bytes) and token in phrase)
        or any(token in name or token in value for name, value in response.headers.raw)
    )


def _check_response_framing(headers: httpx.Headers, limit: int) -> None:
    content_encoding = headers.get("content-encoding")
    if content_encoding is not None and content_encoding.lower() != "identity":
        raise WireError("http_content_encoding_denied")
    declared = headers.get("content-length")
    if declared is not None:
        if (
            "transfer-encoding" in headers
            or not declared.isascii()
            or not declared.isdecimal()
        ):
            raise WireError("http_invalid_content_length")
        # Integer conversion is bounded by the trusted limit's decimal width.
        significant = declared.lstrip("0") or "0"
        if len(significant) > len(str(limit)) or int(significant) > limit:
            raise WireError("http_response_too_large")


@dataclass(frozen=True, slots=True)
class _Parsed:
    model_id: str | None
    content: bytes | None = field(default=None, repr=False)
    error: str | None = None
    invalid_slots: tuple[ArtifactId, ...] = ()
    missing_slots: tuple[ArtifactId, ...] = ()


def _parse_success(
    body: bytearray,
    contract: ChatContract,
    route: PlannedRoute,
    protocol: ChatProtocol,
    *,
    secret: str | None,
) -> _Parsed:
    model_id = None
    try:
        wire = strict_json(body)
        if secret is not None and contains_text(wire, secret):
            raise WireError("http_credential_echo")
        model = wire.get("model") if isinstance(wire, dict) else None
        if model is not None:
            if not isinstance(model, str) or not approved_model_id(model):
                raise WireError("http_invalid_reported_model")
            model_id = model
            if model_id != route.ownership.model_id:
                raise WireError("http_reported_model_mismatch")

        try:
            envelope_model = (
                VllmChatResponse
                if protocol == "vllm_openai_chat_completions"
                else ChatResponse
            )
            envelope = envelope_model.model_validate(wire)
        except ValidationError:
            raise WireError("http_invalid_response_envelope") from None
        if model_id is None and route.ownership.version_evidence is None:
            raise WireError("http_reported_model_unavailable")

        content = envelope.choices[0].message.content
        decoded = strict_json(content, exact_numbers=True)
        if secret is not None and contains_text(decoded, secret):
            raise WireError("http_credential_echo")
        invalid, missing = contract.validate_response_content(content, decoded)
    except WireError as exc:
        return _Parsed(model_id=model_id, error=exc.code)
    return _Parsed(
        model_id, content.encode("utf-8"), invalid_slots=invalid, missing_slots=missing
    )


def _check_request(
    payload: bytes,
    model_id: str,
    contract: ChatContract,
    image_limit: int,
    secret: str | None,
) -> None:
    decoded = validate_chat_request(
        payload, model_id, contract, image_limit=image_limit
    )
    if secret is not None and contains_text(decoded, secret):
        raise WireError("http_credential_in_request")


class HttpModelTransport:
    """Each call makes at most one physical POST to one exact approved endpoint."""

    def __init__(
        self,
        endpoints: tuple[ChatEndpoint, ...],
        contracts: tuple[ChatContract, ...],
        *,
        max_concurrency: int,
        secrets: SecretResolver | None = None,
        allow_network: bool = False,
    ) -> None:
        if type(max_concurrency) is not int or not 1 <= max_concurrency <= 1024:
            raise ValueError("http_invalid_concurrency")
        if type(allow_network) is not bool:
            raise ValueError("http_invalid_network_policy")
        endpoints = tuple(
            ChatEndpoint.model_validate(endpoint) for endpoint in endpoints
        )
        self._endpoints = {(e.provider_id, e.deployment_id): e for e in endpoints}
        self._contracts = {
            (c.input_schema_ref, c.output_schema_ref, c.response_schema_digest): c
            for c in contracts
        }
        if len(self._endpoints) != len(endpoints) or len(self._contracts) != len(
            contracts
        ):
            raise ValueError("http_ambiguous_host_registration")
        self._secrets = secrets
        self._allow_network = allow_network
        self._slots = asyncio.BoundedSemaphore(max_concurrency)
        self._validation_slots = asyncio.BoundedSemaphore(max_concurrency)
        self._tls = httpx.create_ssl_context(verify=True, trust_env=False)

    async def _validate_in_worker[T](self, work: Callable[[], T]) -> T:
        await self._validation_slots.acquire()
        task = asyncio.create_task(asyncio.to_thread(work))

        def completed(result: asyncio.Task[T]) -> None:
            self._validation_slots.release()
            if not result.cancelled():
                result.exception()

        task.add_done_callback(completed)
        # The CPU task keeps its slot until completion even after its awaiting
        # dispatch's deadline or cancellation. It owns no receipt or I/O state.
        return await asyncio.shield(task)

    def _resolve(self, route: PlannedRoute) -> tuple[ChatEndpoint, ChatContract]:
        endpoint = self._endpoints.get(
            (route.selected.provider_id, route.selected.deployment_id)
        )
        if endpoint is None:
            raise WireError("http_endpoint_unregistered")
        if (
            endpoint.provider_kind != route.selected.provider_kind
            or endpoint.model_id != route.ownership.model_id
            or endpoint.descriptor_pin != route.ownership.endpoint_descriptor_ref
        ):
            raise WireError("http_endpoint_pin_mismatch")
        if not self._allow_network and not _is_loopback(httpx.URL(endpoint.url)):
            raise WireError("http_network_disabled")
        key = (
            route.binding.input_schema_ref,
            route.binding.output_schema_ref,
            route.operation.response_contract_digest,
        )
        contract = self._contracts.get(key)
        if contract is None:
            raise WireError("http_schema_pin_unregistered")
        if contract.question_slots:
            snapshot = route.operation.contract_snapshot
            if snapshot is None or contract.question_slots != tuple(
                ref.artifact_id for ref in snapshot.effective_question_refs
            ):
                raise WireError("http_question_slots_pin_mismatch")
        return endpoint, contract

    def raw_response_retention(self, route: PlannedRoute) -> bool:
        """Resolve the exact pinned endpoint's response byte retention permission."""
        endpoint, _ = self._resolve(route)
        return endpoint.retain_complete_response

    def prepare(
        self, route: PlannedRoute, prompt: str, media: VerifiedImage | None = None
    ) -> bytes:
        """Render exact outbound bytes before the gateway records send intent."""
        endpoint, contract = self._resolve(route)
        return encode_chat(
            endpoint.model_id,
            prompt,
            media,
            contract,
            request_limit=min(
                endpoint.limits.request_bytes,
                route.resource_requirements.input_byte_limit,
            ),
            image_limit=endpoint.limits.image_bytes,
        )

    async def prepare_async(
        self, route: PlannedRoute, prompt: str, media: VerifiedImage | None = None
    ) -> bytes:
        return await self._validate_in_worker(
            lambda: self.prepare(route, prompt, media)
        )

    def _bearer(self, endpoint: ChatEndpoint) -> str | None:
        if endpoint.credential_ref is None:
            return None
        try:
            secret = (
                self._secrets.resolve(endpoint.credential_ref)
                if self._secrets
                else None
            )
        except Exception:
            raise WireError("http_credential_unavailable") from None
        if not isinstance(secret, SecretStr):
            raise WireError("http_credential_unavailable")
        value = secret.get_secret_value()
        if len(value) > 2048 or not _BEARER.fullmatch(value) or value in endpoint.url:
            raise WireError("http_credential_invalid")
        return value

    async def dispatch(self, request: DispatchRequest) -> TransportResult:
        """Return safe receipt data; propagate cooperative task cancellation."""
        receipt = _Receipt(request.dispatch_id)
        try:
            endpoint, contract = self._resolve(request.route)
        except WireError as exc:
            return receipt.failed("association", exc.code)

        requirements = request.route.resource_requirements
        loop = asyncio.get_running_loop()
        deadline = (
            loop.time()
            + min(endpoint.limits.timeout_ms, requirements.timeout_ms) / 1000
        )
        parsed: bytes | None
        try:
            async with asyncio.timeout_at(deadline), self._slots:
                input_limit = min(
                    endpoint.limits.request_bytes, requirements.input_byte_limit
                )
                if len(request.payload) > input_limit:
                    raise WireError("http_request_too_large")
                secret = self._bearer(endpoint)
                await self._validate_in_worker(
                    lambda: _check_request(
                        request.payload,
                        endpoint.model_id,
                        contract,
                        min(endpoint.limits.image_bytes, input_limit),
                        secret,
                    )
                )
                if loop.time() >= deadline:
                    raise TimeoutError
                parsed = await self._exchange(
                    endpoint,
                    contract,
                    request,
                    receipt,
                    secret=secret,
                    deadline=deadline,
                    timeout_seconds=min(
                        endpoint.limits.io_timeout_ms / 1000, deadline - loop.time()
                    ),
                    response_limit=min(
                        endpoint.limits.response_bytes, requirements.output_byte_limit
                    ),
                )
                if loop.time() >= deadline:
                    raise TimeoutError
        except asyncio.CancelledError as error:
            code = (
                "http_dispatch_cancelled"
                if receipt.dispatched
                else "http_cancelled_before_send"
            )
            TransportCancelled(receipt.failed("cancelled", code)).attach(error)
            raise
        except (TimeoutError, httpx.TimeoutException):
            return receipt.failed("timeout", "http_deadline_exceeded")
        except httpx.TransportError:
            return receipt.failed("transport", "http_transport_failure")
        except WireError as exc:
            return receipt.failed(_wire_error_class(exc.code), exc.code)

        assert receipt.status is not None
        failure = _status_failure(receipt.status)
        if failure is not None:
            return receipt.failed(*failure)
        return receipt.result(parsed_content=parsed)

    async def _exchange(
        self,
        endpoint: ChatEndpoint,
        contract: ChatContract,
        request: DispatchRequest,
        receipt: _Receipt,
        *,
        secret: str | None,
        deadline: float,
        timeout_seconds: float,
        response_limit: int,
    ) -> bytes | None:
        headers = {
            "accept": "application/json",
            "content-type": "application/json",
            "accept-encoding": "identity",
        }
        if secret is not None:
            headers["authorization"] = f"Bearer {secret}"
        outbound = httpx.Request(
            "POST",
            endpoint.url,
            headers=headers,
            content=request.payload,
            extensions={
                "trace": _strip_trace_data,
                "timeout": dict.fromkeys(
                    ("connect", "read", "write", "pool"), timeout_seconds
                ),
            },
        )
        # The low-level stock transport performs no cookie, redirect, environment
        # proxy or request/response logging processing.
        async with httpx.AsyncHTTPTransport(
            verify=self._tls,
            trust_env=False,
            retries=0,
            http1=True,
            http2=False,
            limits=httpx.Limits(max_connections=1, max_keepalive_connections=0),
        ) as transport:
            if request.pre_send_allowed is not None:
                try:
                    allowed = await request.pre_send_allowed()
                except TimeoutError:
                    raise
                except Exception:
                    raise WireError("http_presend_authorization_lost") from None
                if allowed is not True:
                    raise WireError("http_presend_authorization_lost")
            if asyncio.get_running_loop().time() >= deadline:
                raise TimeoutError
            receipt.dispatched = True
            response = await transport.handle_async_request(outbound)
            try:
                return await self._consume(
                    response,
                    contract,
                    request.route,
                    endpoint.protocol,
                    receipt,
                    secret=secret,
                    response_limit=response_limit,
                    retry_after_limit=endpoint.limits.max_retry_after_seconds,
                    retain_complete_response=endpoint.retain_complete_response,
                )
            finally:
                await response.aclose()

    async def _consume(
        self,
        response: httpx.Response,
        contract: ChatContract,
        route: PlannedRoute,
        protocol: ChatProtocol,
        receipt: _Receipt,
        *,
        secret: str | None,
        response_limit: int,
        retry_after_limit: int,
        retain_complete_response: bool,
    ) -> bytes | None:
        if not 100 <= response.status_code <= 599:
            raise WireError("http_invalid_status")
        receipt.status = response.status_code
        receipt.observed_bytes = 0
        receipt.request_id = _request_id(response.headers, secret)
        receipt.deployment_version = _reported_version(
            response.headers, _DEPLOYMENT_VERSION, secret
        )
        receipt.model_version = _reported_version(
            response.headers, _MODEL_VERSION, secret
        )
        if receipt.status in {429, 503}:
            receipt.retry_after_seconds = _retry_after(
                _safe_header(response.headers, "retry-after", secret), retry_after_limit
            )
        _check_response_framing(response.headers, response_limit)
        body = bytearray()
        async for chunk in response.aiter_raw():
            receipt.observed_bytes += len(chunk)
            if receipt.observed_bytes > response_limit:
                raise WireError("http_response_too_large")
            body.extend(chunk)
        receipt.digest = sha256(body).hexdigest()
        if secret is not None and _echoes_credential(response, body, secret):
            raise WireError("http_credential_echo")

        if _status_failure(receipt.status) is not None:
            return None
        content_type = (
            response.headers.get("content-type", "").partition(";")[0].strip().lower()
        )
        if content_type != "application/json":
            raise WireError("http_response_media_type")
        if (
            _DEPLOYMENT_VERSION in response.headers
            and receipt.deployment_version is None
        ) or (_MODEL_VERSION in response.headers and receipt.model_version is None):
            raise WireError("http_invalid_reported_version")
        if receipt.deployment_version not in {
            None,
            route.selected.deployment_version,
        } or receipt.model_version not in {None, route.selected.model_version}:
            raise WireError("http_reported_version_mismatch")
        parsed = await self._validate_in_worker(
            lambda: _parse_success(body, contract, route, protocol, secret=secret)
        )
        receipt.model_id = parsed.model_id
        if parsed.error is not None:
            raise WireError(parsed.error)
        if parsed.content is not None:
            receipt.normalized_digest = sha256(parsed.content).hexdigest()
            receipt.normalized_bytes = len(parsed.content)
        receipt.validated_digest = contract.response_schema_digest
        receipt.invalid_question_slots = parsed.invalid_slots
        receipt.missing_question_slots = parsed.missing_slots
        if retain_complete_response:
            receipt.response_bytes = bytes(body)
        return parsed.content


def _wire_error_class(code: ArtifactId) -> HTTPErrorClass:
    if code in _IMAGE_LIMIT_CODE or code == "http_response_too_large":
        return "resource_exhausted"
    if code in {
        "http_request_model_mismatch",
        "http_request_contract_mismatch",
        "http_reported_model_mismatch",
        "http_reported_model_unavailable",
        "http_presend_authorization_lost",
        "http_reported_version_mismatch",
    }:
        return "association"
    if code in {"http_invalid_content_length", "http_invalid_status"}:
        return "transport"
    return "validation"
