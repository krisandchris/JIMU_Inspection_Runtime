"""Loopback-only integration adapter for the Stage1, WeVis, and Qwen services."""

from __future__ import annotations

import base64
import hashlib
import json
import math
import time
import uuid
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from http.client import HTTPException
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from io import BytesIO
from pathlib import Path
from threading import BoundedSemaphore, Thread
from urllib.error import HTTPError
from urllib.parse import urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

from PIL import Image
from pydantic import TypeAdapter, ValidationError

from jimu_runtime.domain.nodes.observation import (
    is_region_observation_template,
)
from jimu_runtime.domain.primitives import RecordId
from jimu_runtime.gateway.http_wire import ChatResponse, strict_json
from jimu_runtime.inspection.perception.region_projection import (
    RegionProjectionError,
    RegionProjectionRequest,
    RegionProjectionStore,
)

MODEL_ID = "jimu/live-adapter"
BRIDGE_VERSION = "1.2.1"
CHAT_PATH = "/v1/chat/completions"
MAX_WIRE = 16 * 1024 * 1024
_STAGE1_MODEL = "090ee95e9a1649d35854c9559bac9421b80faba3062b3b1903bd3ae8ea77eb19"
_WEVIS_MODEL = "WeVisOne-VL-4B-NF4"
_QWEN_MODEL = "Qwen3_5-9B"
_WIRE_KEYS = {"model", "messages", "stream", "response_format"}
_QWEN_TOP_EXTRAS = (
    "prompt_logprobs",
    "prompt_token_ids",
    "prompt_text",
    "kv_transfer_params",
    "ec_transfer_params",
    "metrics",
)
_QWEN_CHOICE_EXTRAS = ("stop_reason", "token_ids", "routed_experts")
_QWEN_MESSAGE_EXTRAS = ("reasoning", "reasoning_content")
_RECORD_ID = TypeAdapter(RecordId)

type Box = tuple[float, float, float, float]
type GroundingAttestation = Callable[[bytes, tuple[Box, ...]], tuple[str, ...]]


@dataclass(frozen=True)
class ProfileRoute:
    """A host-approved label for N03 and either a Stage1 target or WeVis for N02."""

    detection_label: str
    stage1_target: str | None = None

    def __post_init__(self) -> None:
        for value in (self.detection_label, self.stage1_target):
            if value is not None and (
                not isinstance(value, str)
                or not value.strip()
                or len(value) > 128
                or any(ord(character) < 32 for character in value)
            ):
                raise ValueError("invalid_profile_route_label")
        if self.detection_label is None:
            raise ValueError("missing_detection_label")


class BridgeError(ValueError):
    def __init__(self, code: str, status: int = 422) -> None:
        super().__init__(code)
        self.status = status


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class LiveBridgeServer(ThreadingHTTPServer):
    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.server_port}{CHAT_PATH}"


def _json(value):
    return json.dumps(
        value, ensure_ascii=False, allow_nan=False, separators=(",", ":")
    ).encode()


def _sha(value):
    return hashlib.sha256(value).hexdigest()


def _summary(value):
    return {"bytes": len(value), "sha256": _sha(value)}


def _reject_duplicates(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise BridgeError("duplicate_json_key")
        result[key] = value
    return result


def _reject_constant(_):
    raise BridgeError("invalid_json_number")


def _loads(value):
    return json.loads(
        value, object_pairs_hook=_reject_duplicates, parse_constant=_reject_constant
    )


def _image_part(parts):
    if len(parts) != 2 or parts[1]["type"] != "image_url":
        raise BridgeError("expected_one_image")
    url = parts[1]["image_url"]["url"]
    header, sep, encoded = url.partition(",")
    if not sep or header not in ("data:image/png;base64", "data:image/jpeg;base64"):
        raise BridgeError("invalid_image_data_url")
    image = base64.b64decode(encoded, validate=True)
    if not image or len(image) > 4 * 1024 * 1024:
        raise BridgeError("invalid_image_size")
    return header[5:-7], image


def _open_upstream(url, body, content_type, timeout, audit, *, parse_response=_loads):
    audit["upstream"] = {
        "url": url,
        "content_type": content_type,
        "request": _summary(body),
    }
    request = Request(
        url, data=body, headers={"Content-Type": content_type}, method="POST"
    )
    started = time.monotonic()
    try:
        opener = build_opener(ProxyHandler({}), NoRedirect())
        with opener.open(request, timeout=timeout) as response:
            result = response.read(MAX_WIRE + 1)
            audit["upstream"].update(status=response.status, response=_summary(result))
            request_id = response.headers.get("x-request-id")
            if request_id:
                audit["upstream"]["request_id"] = request_id
    except HTTPError as error:
        with error:
            response_bytes = error.read(MAX_WIRE + 1)
            audit["upstream"].update(
                status=error.code, response=_summary(response_bytes)
            )
            request_id = error.headers.get("x-request-id")
            if request_id:
                audit["upstream"]["request_id"] = request_id
        raise BridgeError("upstream_http_error", 502) from None
    except (OSError, HTTPException):
        raise BridgeError("upstream_transport_error", 502) from None
    finally:
        audit["upstream"]["round_trip_s"] = round(time.monotonic() - started, 3)
    if len(result) > MAX_WIRE:
        raise BridgeError("upstream_response_too_large", 502)
    try:
        return parse_response(result)
    except (ValueError, TypeError):
        raise BridgeError("invalid_upstream_json", 502) from None


def _discover(
    stage1_url, wevis_url, media_type, image, metadata, profile_routes, timeout, audit
):
    eligible = metadata["eligible_profile_ids"]
    if (
        not isinstance(eligible, list)
        or not eligible
        or any(type(profile) is not str for profile in eligible)
        or len(set(eligible)) != len(eligible)
    ):
        raise BridgeError("invalid_eligible_profiles")
    audit["eligible_profile_ids"] = eligible
    if any(profile not in profile_routes for profile in eligible):
        audit["unsupported_profiles"] = [
            profile for profile in eligible if profile not in profile_routes
        ]
        raise BridgeError("unsupported_discovery_profile")

    stage1_targets = {
        profile: profile_routes[profile].stage1_target
        for profile in eligible
        if profile_routes[profile].stage1_target is not None
    }
    discovery_calls = []
    audit["discovery_calls"] = discovery_calls
    stage1_hits = set()
    if stage1_targets:
        call = {"provider": "stage1", "profile_ids": list(stage1_targets)}
        discovery_calls.append(call)
        stage1_hits = _stage1_hits(
            stage1_url, media_type, image, stage1_targets, timeout, call
        )
    hits = []
    for profile in eligible:
        if profile in stage1_targets:
            if profile in stage1_hits:
                hits.append(profile)
            continue
        call = {"provider": "wevis_detection", "profile_id": profile}
        discovery_calls.append(call)
        boxes = _detect(
            wevis_url,
            media_type,
            image,
            profile_routes[profile].detection_label,
            timeout,
            call,
        )
        call["detections"] = len(boxes)
        if boxes:
            hits.append(profile)
    if len(discovery_calls) == 1:
        for key in ("upstream", "stage1", "wevis", "profile_target_map"):
            if key in discovery_calls[0]:
                audit[key] = discovery_calls[0][key]
    return {"profile_hits": hits}


def _stage1_hits(url, media_type, image, targets, timeout, audit):
    response = _open_upstream(url, image, media_type, timeout, audit)
    objects = response["objects"]
    if response["model_id"] != _STAGE1_MODEL:
        raise BridgeError("unexpected_stage1_model", 502)
    if (response["schema_version"], response["policy_version"]) != (
        "routing/1.0",
        "inspection-routing/1.0",
    ):
        raise BridgeError("unexpected_stage1_contract", 502)
    routes = {}
    for item in objects:
        target = item["target_type"]
        if target in routes or type(item["route_enabled"]) is not bool:
            raise BridgeError("invalid_stage1_routes", 502)
        routes[target] = {
            "route_enabled": item["route_enabled"],
            "presence_probability": item["presence_probability"],
            "selection_reason": item["selection_reason"],
        }
    audit["stage1"] = {
        "model": response["model_id"],
        "schema_version": response["schema_version"],
        "policy_version": response["policy_version"],
        "routes": routes,
        "upstream_questions_used": False,
    }
    audit["profile_target_map"] = targets
    if any(target not in routes for target in targets.values()):
        raise BridgeError("stage1_missing_target_output", 502)
    return {
        profile
        for profile, target in targets.items()
        if routes[target]["route_enabled"]
    }


def _detect(url, media_type, image, label, timeout, audit):
    audit["wevis_query"] = label
    try:
        with Image.open(BytesIO(image), formats=("PNG", "JPEG")) as original:
            width, height = original.size
    except (OSError, Image.DecompressionBombError):
        raise BridgeError("invalid_detection_image") from None
    response = _open_upstream(
        f"{url}?{urlencode({'label': label})}", image, media_type, timeout, audit
    )
    if response["model"] != _WEVIS_MODEL or response["task"] != "detection":
        raise BridgeError("unexpected_wevis_identity_or_task", 502)
    if (
        response["image_sha256"] != _sha(image)
        or type(response["model_forward_calls"]) is not int
        or response["model_forward_calls"] < 1
    ):
        raise BridgeError("invalid_wevis_inference_evidence", 502)
    size = response["image_size"]
    if (
        not isinstance(size, list)
        or any(type(edge) is not int for edge in size)
        or size != [width, height]
    ):
        raise BridgeError("wevis_image_size_mismatch", 502)
    predictions = response["predictions"]
    model_response = response["model_response"]
    if not isinstance(predictions, dict) or not isinstance(model_response, str):
        raise BridgeError("invalid_wevis_predictions", 502)
    audit["wevis"] = {
        "model": response["model"],
        "model_response": model_response,
        "image_size": [width, height],
        "forward_calls": response["model_forward_calls"],
        "predicted_labels": list(predictions),
    }
    absence = f"<ref>{label}</ref><box>none</box>"
    if model_response.strip() == absence:
        if predictions not in ({}, {label: []}):
            raise BridgeError("wevis_conflicting_grounding", 502)
        return []
    if not predictions:
        raise BridgeError("wevis_no_parseable_grounding", 502)
    if set(predictions) != {label}:
        raise BridgeError("unexpected_wevis_detection_label", 502)
    coordinates = predictions[label]
    if not isinstance(coordinates, list):
        raise BridgeError("invalid_wevis_coordinates", 502)
    if not coordinates or not model_response.strip():
        raise BridgeError("wevis_no_parseable_grounding", 502)
    boxes = []
    for box in coordinates:
        if (
            type(box) is not list
            or len(box) != 4
            or any(
                type(edge) not in (int, float)
                or (type(edge) is float and not math.isfinite(edge))
                for edge in box
            )
        ):
            raise BridgeError("invalid_wevis_box", 502)
        x0, y0, x1, y1 = box
        if not (0 <= x0 < x1 <= width and 0 <= y0 < y1 <= height):
            raise BridgeError("wevis_box_out_of_source_frame", 502)
        boxes.append({"xyxy": box})
    return boxes


def _project_qwen(url, body, timeout, audit, *, require_completion_id=False):
    request = {**body, "model": _QWEN_MODEL}
    raw = _open_upstream(
        url,
        _json(request),
        "application/json",
        timeout,
        audit,
        parse_response=strict_json,
    )
    choices = raw["choices"]
    if raw["model"] != _QWEN_MODEL or raw["object"] != "chat.completion":
        raise BridgeError("unexpected_qwen_identity", 502)
    choice = (
        choices[0]
        if isinstance(choices, list)
        and len(choices) == 1
        and isinstance(choices[0], dict)
        else {}
    )
    index, finish = choice.get("index"), choice.get("finish_reason")
    usage = raw.get("usage")
    completion_id, fingerprint = raw.get("id"), raw.get("system_fingerprint")
    audit["qwen"] = {
        "actual_model": raw["model"],
        "upstream_completion_id": completion_id
        if isinstance(completion_id, str) and len(completion_id) <= 128
        else None,
        "system_fingerprint": fingerprint
        if isinstance(fingerprint, str) and len(fingerprint) <= 128
        else None,
        "requested_max_tokens": request.get("max_tokens"),
        "choice_count": len(choices) if isinstance(choices, list) else None,
        "choice_index": index if type(index) is int else None,
        "finish_reason": finish
        if isinstance(finish, str) and len(finish) <= 64
        else None,
        "token_usage": {
            key: usage[key]
            for key in ("prompt_tokens", "completion_tokens", "total_tokens")
            if isinstance(usage, dict) and type(usage.get(key)) is int
        },
    }
    if finish != "stop" or type(index) is not int or index != 0:
        raise BridgeError("qwen_completion_incomplete", 502)
    message = choice["message"]
    if message["role"] != "assistant" or any(
        message.get(key) not in (None, [])
        for key in ("refusal", "tool_calls", "function_call", "audio")
    ):
        raise BridgeError("qwen_not_plain_answer", 502)
    content = message["content"]
    if not isinstance(content, str) or not content.strip():
        raise BridgeError("qwen_missing_answer", 502)
    # Strip only identified vLLM envelope extensions. Never modify content.
    discarded = {}
    for prefix, fields, keys in (
        ("", raw, _QWEN_TOP_EXTRAS),
        ("choices.0.", choice, _QWEN_CHOICE_EXTRAS),
        ("choices.0.message.", message, _QWEN_MESSAGE_EXTRAS),
    ):
        for key in keys:
            if key in fields:
                value = fields.pop(key)
                discarded[f"{prefix}{key}"] = (
                    None if value is None else _summary(_json(value))
                )
    try:
        envelope = ChatResponse.model_validate(raw)
        if require_completion_id:
            _RECORD_ID.validate_python(envelope.id)
    except ValidationError:
        raise BridgeError("qwen_invalid_response_envelope", 502) from None
    audit["qwen"].update(
        removed_envelope_fields=discarded,
        validated_envelope=_summary(_json(raw)),
        unaltered_completion_content=content,
        completion_content=_summary(content.encode()),
    )
    raw["model"] = MODEL_ID
    return raw


def _region_qwen(
    store: RegionProjectionStore,
    url: str,
    timeout: float,
    thinking: bool | None,
    parts: list,
    schema: dict,
    image: bytes,
    audit: dict,
) -> dict:
    lines = parts[0]["text"].split("\n")
    if len(lines) != 2:
        raise BridgeError("region_invalid_projection_prompt")
    request = RegionProjectionRequest.from_wire(
        strict_json(lines[1]), logical_schema=schema, image=image
    )
    body = {
        "model": _QWEN_MODEL,
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": request.metadata.prompt},
                    {
                        "type": "image_url",
                        "image_url": {"url": parts[1]["image_url"]["url"]},
                    },
                ],
            }
        ],
        "stream": False,
        "response_format": {
            "type": "json_schema",
            "json_schema": {
                "name": "jimu_region_response",
                "strict": True,
                "schema": request.metadata.response_schema,
            },
        },
    }
    if thinking is not None:
        body["chat_template_kwargs"] = {"enable_thinking": thinking}
    wire = _json(body)
    if len(wire) > MAX_WIRE:
        raise BridgeError("region_physical_request_too_large", 413)

    def invoke(upstream_audit: dict) -> str:
        try:
            completion = _project_qwen(
                url, body, timeout, upstream_audit, require_completion_id=True
            )
            if upstream_audit["upstream"]["status"] != 200:
                raise BridgeError("region_unexpected_upstream_status", 502)
            return completion["choices"][0]["message"]["content"]
        except BridgeError as error:
            raise RegionProjectionError(str(error), error.status) from None
        except (
            ValueError,
            KeyError,
            TypeError,
            IndexError,
            AttributeError,
            RecursionError,
        ):
            raise RegionProjectionError("region_invalid_upstream_wire", 502) from None

    content = store.resolve(
        request,
        upstream_request=wire,
        upstream_configuration={
            "url": url,
            "model": _QWEN_MODEL,
            "bridge_version": BRIDGE_VERSION,
            "enable_thinking": thinking,
            "timeout_seconds": timeout,
        },
        invoke=invoke,
        audit=audit,
    )
    return _assistant_envelope(content, request.request_id)


def _assistant_envelope(content: str, request_id: str) -> dict:
    return {
        "model": MODEL_ID,
        "object": "chat.completion",
        "id": request_id,
        "choices": [
            {
                "index": 0,
                "finish_reason": "stop",
                "message": {"role": "assistant", "content": content},
            }
        ],
    }


def _attest_grounding(
    attest: GroundingAttestation, image: bytes, boxes: list, audit: dict
) -> None:
    coordinates = tuple(
        (x0, y0, x1, y1) for x0, y0, x1, y1 in (box["xyxy"] for box in boxes)
    )
    try:
        identities = attest(image, coordinates)
        if (
            not isinstance(identities, tuple)
            or len(identities) != len(coordinates)
            or any(
                not isinstance(identity, str) or not identity for identity in identities
            )
            or len(set(identities)) != len(identities)
        ):
            raise ValueError("incomplete_authority_association")
    except ValueError:
        audit["grounding_scope_association"] = {"status": "rejected", "boxes": boxes}
        raise BridgeError("grounding_scope_association_failed") from None
    audit["grounding_scope_association"] = {
        "status": "matched",
        "boxes": boxes,
        "identities": identities,
    }


@contextmanager
def start_bridge(
    log_directory: Path,
    *,
    profile_routes: Mapping[str, ProfileRoute],
    grounding_attestations: Mapping[str, GroundingAttestation] | None = None,
    stage1_url: str = "http://127.0.0.1:18101/v1/route",
    wevis_url: str = "http://127.0.0.1:18103/v1/detect",
    qwen_url: str = "http://192.168.32.6:8029/v1/chat/completions",
    request_timeout: float = 90,
    port: int = 0,
    enable_region_projection: bool = False,
    qwen_enable_thinking: bool | None = None,
) -> Iterator[LiveBridgeServer]:
    """Serve explicit host routes and optional N04 region projection over loopback."""
    log_directory = Path(log_directory)
    log_directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    routes = dict(profile_routes)
    attestations = dict(grounding_attestations or {})
    if not routes or any(
        not isinstance(profile, str)
        or not profile.strip()
        or not isinstance(route, ProfileRoute)
        for profile, route in routes.items()
    ):
        raise ValueError("invalid_profile_routes")
    if any(
        profile not in routes or not callable(attest)
        for profile, attest in attestations.items()
    ):
        raise ValueError("invalid_grounding_attestations")
    for url in (stage1_url, wevis_url, qwen_url):
        endpoint = urlsplit(url)
        if (
            endpoint.scheme not in {"http", "https"}
            or not endpoint.hostname
            or endpoint.username is not None
            or endpoint.query
            or endpoint.fragment
        ):
            raise ValueError("invalid_upstream_url")
    if not math.isfinite(request_timeout) or request_timeout <= 0:
        raise ValueError("invalid_request_timeout")
    if type(enable_region_projection) is not bool or (
        qwen_enable_thinking is not None and type(qwen_enable_thinking) is not bool
    ):
        raise ValueError("invalid_region_projection_configuration")
    region_store = (
        RegionProjectionStore(
            log_directory / "region-batches", wait_seconds=request_timeout
        )
        if enable_region_projection
        else None
    )
    requests = BoundedSemaphore(4)

    class Handler(BaseHTTPRequestHandler):
        def setup(self):
            super().setup()
            self.connection.settimeout(request_timeout)

        def log_message(self, *_):
            return

        def do_POST(self):
            if not requests.acquire(blocking=False):
                self.send_error(503)
                return
            try:
                self._dispatch()
            finally:
                requests.release()

        def _dispatch(self):
            trace_id = "live-" + uuid.uuid4().hex
            audit = {
                "bridge_request_id": trace_id,
                "timestamp_epoch": time.time(),
                "bridge_identity": {
                    "model_id": MODEL_ID,
                    "deployment_version": BRIDGE_VERSION,
                    "wire_model_version": BRIDGE_VERSION,
                    "scope": "live_inspection_example_adapter",
                },
            }
            status = 200
            try:
                if self.path != CHAT_PATH:
                    raise BridgeError("unknown_bridge_path", 404)
                length = int(self.headers.get("Content-Length", "0"))
                if not 0 < length <= MAX_WIRE:
                    raise BridgeError("invalid_request_size", 413)
                raw = self.rfile.read(length)
                if len(raw) != length:
                    raise BridgeError("incomplete_request_body")
                audit["runtime_request"] = _summary(raw)
                body = _loads(raw)
                if (
                    set(body) != _WIRE_KEYS
                    or body["model"] != MODEL_ID
                    or body["stream"] is not False
                ):
                    raise BridgeError("incompatible_chat_request")
                (user,) = body["messages"]
                parts = user["content"]
                if user["role"] != "user" or parts[0]["type"] != "text":
                    raise BridgeError("invalid_chat_messages")
                prompt = parts[0]["text"]
                schema = body["response_format"]["json_schema"]["schema"]
                capability = schema["$id"].split(":")[-2]
                audit.update(
                    capability=capability,
                    schema_id=schema["$id"],
                    response_schema=_summary(_json(schema)),
                    prompt=_summary(prompt.encode()),
                )
                media_type, image = _image_part(parts)
                audit.update(image=_summary(image), image_media_type=media_type)
                if capability == "profile_discovery-result":
                    result = _discover(
                        stage1_url,
                        wevis_url,
                        media_type,
                        image,
                        _loads(prompt.splitlines()[-1]),
                        routes,
                        request_timeout,
                        audit,
                    )
                elif capability == "object_grounding-result":
                    profile = _loads(prompt.splitlines()[-1])["profile_id"]
                    if profile not in routes:
                        raise BridgeError("unsupported_grounding_profile")
                    audit["profile_id"] = profile
                    boxes = _detect(
                        wevis_url,
                        media_type,
                        image,
                        routes[profile].detection_label,
                        request_timeout,
                        audit,
                    )
                    if profile in attestations:
                        _attest_grounding(attestations[profile], image, boxes, audit)
                    result = {"boxes": boxes}
                elif capability in (
                    "object_observation-result",
                    "candidate_prediction-result",
                ):
                    if (
                        capability == "object_observation-result"
                        and is_region_observation_template(prompt)
                    ):
                        if region_store is None:
                            raise BridgeError("region_projection_disabled")
                        envelope = _region_qwen(
                            region_store,
                            qwen_url,
                            request_timeout,
                            qwen_enable_thinking,
                            parts,
                            schema,
                            image,
                            audit,
                        )
                    else:
                        envelope = _project_qwen(qwen_url, body, request_timeout, audit)
                    result = None
                else:
                    raise BridgeError("unsupported_capability")
                if result is not None:
                    audit["projected_result"] = result
                    envelope = _assistant_envelope(_json(result).decode(), trace_id)
                response = _json(envelope)
            except (BridgeError, RegionProjectionError) as error:
                status = error.status
                audit["failure"] = str(error)
                response = _json(
                    {"error": {"code": str(error), "type": "temporary_live_bridge"}}
                )
            except (
                ValueError,
                KeyError,
                TypeError,
                IndexError,
                AttributeError,
                OSError,
                RecursionError,
            ) as error:
                status = (
                    502 if "upstream" in audit or audit.get("discovery_calls") else 422
                )
                audit["failure"] = type(error).__name__
                response = _json(
                    {
                        "error": {
                            "code": "invalid_bridge_wire",
                            "type": "temporary_live_bridge",
                        }
                    }
                )
            audit.update(runtime_http=status, runtime_response=_summary(response))
            (log_directory / f"{trace_id}.json").write_bytes(_json(audit) + b"\n")
            try:
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(response)))
                self.send_header(
                    "X-Request-ID", audit.get("physical_request_id", trace_id)
                )
                if status == 200:
                    self.send_header("X-Jimu-Deployment-Version", BRIDGE_VERSION)
                    self.send_header("X-Jimu-Model-Version", BRIDGE_VERSION)
                self.end_headers()
                self.wfile.write(response)
            except (BrokenPipeError, ConnectionResetError):
                pass

    with LiveBridgeServer(("127.0.0.1", port), Handler) as bridge:
        worker = Thread(target=bridge.serve_forever, daemon=True)
        worker.start()
        try:
            yield bridge
        finally:
            bridge.shutdown()
            worker.join()
