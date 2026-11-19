"""Bounded, non-streaming Chat Completions wire used by host-approved adapters."""

import base64
import binascii
import json
import re
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from hashlib import sha256
from math import isfinite
from typing import Annotated, Literal, cast

from pydantic import Field, ValidationError

from jimu_runtime.bundle.schema_validation import (
    OfflineResponseSchema,
    OfflineSchemaError,
    compile_offline_response_schema,
)
from jimu_runtime.domain.base import ContractModel
from jimu_runtime.domain.fact_schemas.canonicalization import JSONValue, canonical_bytes
from jimu_runtime.domain.primitives import ArtifactId, Digest
from jimu_runtime.domain.references import SchemaRef

type ImageMediaType = Literal["image/jpeg", "image/png"]

_MODEL_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_./:-]{0,127}")
_IMAGE_MAGIC: dict[ImageMediaType, bytes] = {
    "image/jpeg": b"\xff\xd8\xff",
    "image/png": b"\x89PNG\r\n\x1a\n",
}
_MAX_JSON_DEPTH = 64
_MAX_RESPONSE_SCHEMA_BYTES = 1024 * 1024


class WireError(ValueError):
    """Fixed diagnostic code. Underlying payloads and exceptions stay private."""

    def __init__(self, code: ArtifactId) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True, slots=True)
class VerifiedImage:
    """Trusted media loader provides verified image bytes and the original SHA-256."""

    content: bytes = field(repr=False)
    media_type: ImageMediaType
    sha256: Digest


@dataclass(frozen=True, slots=True)
class ChatContract:
    input_schema_ref: SchemaRef
    output_schema_ref: SchemaRef
    response_model: type[ContractModel]
    accepts_image: bool = False
    response_schema: bytes | None = field(default=None, repr=False)
    question_slots: tuple[ArtifactId, ...] = ()
    _schema_validator: OfflineResponseSchema = field(init=False, repr=False)
    _schema_bytes: bytes = field(init=False, repr=False)
    response_schema_digest: Digest = field(init=False)

    def __post_init__(self) -> None:
        config = self.response_model.model_config
        if config.get("extra") != "forbid" or config.get("strict") is not True:
            raise ValueError("chat_response_contract_must_be_strict")
        schema = self.response_schema
        if schema is None:
            schema = canonical_bytes(self.response_model.model_json_schema())
        if (
            type(schema) is not bytes
            or not 1 <= len(schema) <= _MAX_RESPONSE_SCHEMA_BYTES
        ):
            raise ValueError("chat_response_schema_bytes_invalid")
        document = strict_json(schema)
        if not isinstance(document, dict):
            raise ValueError("chat_response_schema_requires_object")
        if len(set(self.question_slots)) != len(self.question_slots):
            raise ValueError("chat_response_question_slots_duplicate")
        try:
            validator = compile_offline_response_schema(
                cast(dict[str, JSONValue], document), self.question_slots
            )
        except OfflineSchemaError as exc:
            raise WireError(str(exc)) from None
        object.__setattr__(self, "_schema_validator", validator)
        object.__setattr__(self, "_schema_bytes", schema)
        object.__setattr__(self, "response_schema_digest", sha256(schema).hexdigest())

    def response_schema_document(self) -> object:
        return strict_json(self._schema_bytes)

    def validate_response_content(
        self, content: str, value: object
    ) -> tuple[tuple[ArtifactId, ...], tuple[ArtifactId, ...]]:
        if not self.question_slots:
            try:
                self.response_model.model_validate_json(content)
            except ValidationError:
                raise WireError("http_invalid_response_content") from None
        # In independent-slot mode the closed envelope and each Question's
        # exact schema own validation. An invalid slot cannot discard siblings.
        try:
            return self._schema_validator.validate(value)
        except OfflineSchemaError as exc:
            raise WireError(str(exc)) from None


def approved_model_id(value: str) -> bool:
    return _MODEL_ID.fullmatch(value) is not None


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise WireError("http_json_duplicate_key")
        result[key] = value
    return result


def _invalid_number(_: str) -> float:
    raise WireError("http_json_nonfinite")


def _finite_float(value: str) -> float:
    if len(value) > 128:
        raise WireError("http_json_number_precision")
    parsed = float(value)
    if not isfinite(parsed):
        return _invalid_number(value)
    try:
        if Decimal(value) != Decimal(str(parsed)):
            raise WireError("http_json_number_precision")
    except InvalidOperation:
        raise WireError("http_json_number_precision") from None
    return parsed


def _exact_response_number(value: str) -> int | Decimal:
    if len(value) > 128:
        raise WireError("http_json_number_precision")
    if not isfinite(float(value)):
        raise WireError("http_json_nonfinite")
    try:
        parsed = Decimal(value)
        if parsed and parsed.adjusted() < -4096:
            raise WireError("http_json_number_precision")
        return int(parsed) if parsed == parsed.to_integral_value() else parsed
    except InvalidOperation:
        raise WireError("http_json_number_precision") from None


def strict_json(
    payload: bytes | bytearray | str, *, exact_numbers: bool = False
) -> object:
    """Reject ambiguous keys, nonfinite numbers, invalid Unicode and excess depth."""
    try:
        value: object = json.loads(
            payload if isinstance(payload, str) else payload.decode("utf-8"),
            object_pairs_hook=_unique_object,
            parse_constant=_invalid_number,
            parse_float=_exact_response_number if exact_numbers else _finite_float,
        )
        _inspect_json(value, depth=0)
    except WireError:
        raise
    except (ValueError, RecursionError):
        raise WireError("http_invalid_json") from None
    return value


def _inspect_json(value: object, *, depth: int) -> None:
    if depth > _MAX_JSON_DEPTH:
        raise WireError("http_json_excess_depth")
    if isinstance(value, str):
        value.encode("utf-8")
    elif isinstance(value, dict):
        for key, item in value.items():
            _inspect_json(key, depth=depth + 1)
            _inspect_json(item, depth=depth + 1)
    elif isinstance(value, list):
        for item in value:
            _inspect_json(item, depth=depth + 1)


def contains_text(value: object, text: str) -> bool:
    """Detect the known credential in parsed keys and values, including escaped JSON."""
    if isinstance(value, str):
        return text in value
    if isinstance(value, dict):
        return any(
            text in key or contains_text(item, text) for key, item in value.items()
        )
    if isinstance(value, list):
        return any(contains_text(item, text) for item in value)
    return False


def _json_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _result_format(contract: ChatContract) -> dict[str, object]:
    return {
        "type": "json_schema",
        "json_schema": {
            "name": "jimu_response",
            "strict": True,
            "schema": contract.response_schema_document(),
        },
    }


def _verify_image(content: bytes, media_type: ImageMediaType, limit: int) -> None:
    if not isinstance(content, bytes) or not content.startswith(
        _IMAGE_MAGIC[media_type]
    ):
        raise WireError("http_invalid_image")
    if len(content) > limit:
        raise WireError("http_image_too_large")


def encode_chat(
    model: str,
    prompt: str,
    image: VerifiedImage | None,
    contract: ChatContract,
    *,
    request_limit: int,
    image_limit: int,
) -> bytes:
    if not isinstance(prompt, str):
        raise WireError("http_invalid_prompt")
    if len(prompt) > request_limit:
        raise WireError("http_request_too_large")
    if not prompt.strip():
        raise WireError("http_invalid_prompt")
    try:
        if len(prompt.encode("utf-8")) > request_limit:
            raise WireError("http_request_too_large")
    except UnicodeError:
        raise WireError("http_invalid_prompt") from None

    content: list[dict[str, object]] = [{"type": "text", "text": prompt}]
    if image is not None:
        if not contract.accepts_image or image.media_type not in _IMAGE_MAGIC:
            raise WireError("http_image_not_allowed")
        _verify_image(image.content, image.media_type, min(image_limit, request_limit))
        if sha256(image.content).hexdigest() != image.sha256:
            raise WireError("http_image_digest_mismatch")
        if 4 * ((len(image.content) + 2) // 3) > request_limit:
            raise WireError("http_request_too_large")
        encoded = base64.b64encode(image.content).decode("ascii")
        content.append(
            {
                "type": "image_url",
                "image_url": {"url": f"data:{image.media_type};base64,{encoded}"},
            }
        )
    body = _json_bytes(
        {
            "model": model,
            "messages": [{"role": "user", "content": content}],
            "stream": False,
            "response_format": _result_format(contract),
        }
    )
    if len(body) > request_limit:
        raise WireError("http_request_too_large")
    return body


def _require_request_image(value: object, image_limit: int) -> None:
    if not isinstance(value, dict) or set(value) != {"type", "image_url"}:
        raise WireError("http_invalid_image")
    target = value["image_url"]
    if (
        value["type"] != "image_url"
        or not isinstance(target, dict)
        or set(target) != {"url"}
    ):
        raise WireError("http_invalid_image")
    url = target["url"]
    if not isinstance(url, str):
        raise WireError("http_invalid_image")
    prefix, separator, data = url.partition(",")
    for media_type in _IMAGE_MAGIC:
        if separator and prefix == f"data:{media_type};base64":
            if len(data) > 4 * ((image_limit + 2) // 3):
                raise WireError("http_image_too_large")
            try:
                image = base64.b64decode(data, validate=True)
            except (ValueError, binascii.Error):
                raise WireError("http_invalid_image") from None
            _verify_image(image, media_type, image_limit)
            return
    raise WireError("http_invalid_image")


def validate_chat_request(
    payload: bytes, model: str, contract: ChatContract, *, image_limit: int
) -> object:
    """Validate the exact bytes authorized by the caller's dispatch journal."""
    root = strict_json(payload)
    if not isinstance(root, dict) or set(root) != {
        "model",
        "messages",
        "stream",
        "response_format",
    }:
        raise WireError("http_invalid_request")
    if root["model"] != model:
        raise WireError("http_request_model_mismatch")
    if root["stream"] is not False or _json_bytes(
        root["response_format"]
    ) != _json_bytes(_result_format(contract)):
        raise WireError("http_request_contract_mismatch")
    messages = root["messages"]
    if not isinstance(messages, list) or len(messages) != 1:
        raise WireError("http_invalid_request")
    message = messages[0]
    if (
        not isinstance(message, dict)
        or set(message) != {"role", "content"}
        or message["role"] != "user"
    ):
        raise WireError("http_invalid_request")
    parts = message["content"]
    allowed_parts = 2 if contract.accepts_image else 1
    if not isinstance(parts, list) or not 1 <= len(parts) <= allowed_parts:
        raise WireError("http_invalid_request")
    prompt = parts[0]
    if (
        not isinstance(prompt, dict)
        or set(prompt) != {"type", "text"}
        or prompt["type"] != "text"
        or not isinstance(prompt["text"], str)
        or not prompt["text"].strip()
    ):
        raise WireError("http_invalid_prompt")
    if len(parts) == 2:
        _require_request_image(parts[1], image_limit)
    return root


class _Message(ContractModel):
    role: Literal["assistant"]
    content: Annotated[str, Field(repr=False, min_length=1)]
    refusal: None = None
    annotations: Annotated[list[None], Field(max_length=0)] | None = None
    tool_calls: Annotated[list[None], Field(max_length=0)] | None = None
    function_call: None = None
    audio: None = None


class _Choice(ContractModel):
    index: Annotated[int, Field(ge=0, le=0)]
    finish_reason: Literal["stop"]
    message: _Message
    logprobs: None = None


type TokenCount = Annotated[int, Field(ge=0, le=2**63 - 1)]


class _PromptTokensDetails(ContractModel):
    audio_tokens: TokenCount | None = None
    cached_tokens: TokenCount | None = None


class _CompletionTokensDetails(ContractModel):
    accepted_prediction_tokens: TokenCount | None = None
    audio_tokens: TokenCount | None = None
    reasoning_tokens: TokenCount | None = None
    rejected_prediction_tokens: TokenCount | None = None


class _Usage(ContractModel):
    prompt_tokens: TokenCount
    completion_tokens: TokenCount
    total_tokens: TokenCount
    prompt_tokens_details: _PromptTokensDetails | None = None
    completion_tokens_details: _CompletionTokensDetails | None = None


class _Response(ContractModel):
    model: Annotated[str, Field(min_length=1, max_length=128)] | None = None
    id: Annotated[str, Field(max_length=128)] | None = None
    object: Literal["chat.completion"] | None = None
    created: TokenCount | None = None
    usage: _Usage | None = None
    service_tier: Annotated[str, Field(max_length=32)] | None = None
    system_fingerprint: Annotated[str, Field(max_length=128)] | None = None


class ChatResponse(_Response):
    """Accepted OpenAI-compatible completion envelope."""

    choices: Annotated[list[_Choice], Field(min_length=1, max_length=1, repr=False)]


class _VllmMessage(_Message):
    reasoning: None = None
    reasoning_content: None = None


class _VllmChoice(ContractModel):
    index: Annotated[int, Field(ge=0, le=0)]
    finish_reason: Literal["stop"]
    message: _VllmMessage
    logprobs: None = None
    stop_reason: TokenCount | Annotated[str, Field(max_length=256)] | None = None
    token_ids: Annotated[list[TokenCount], Field(max_length=65536)] | None = Field(
        default=None, repr=False
    )


class VllmChatResponse(_Response):
    """Accepted vLLM compatibility completion envelope."""

    choices: Annotated[list[_VllmChoice], Field(min_length=1, max_length=1, repr=False)]
    prompt_logprobs: None = None
    prompt_token_ids: Annotated[list[TokenCount], Field(max_length=65536)] | None = (
        Field(default=None, repr=False)
    )
    kv_transfer_params: None = None
