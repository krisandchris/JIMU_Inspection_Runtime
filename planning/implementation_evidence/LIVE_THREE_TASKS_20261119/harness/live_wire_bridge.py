"""Isolated test bridge for Stage1, WeVis local services, and Qwen vLLM."""
from __future__ import annotations

import base64
import hashlib
import json
import math
import time
import uuid
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

MODEL_ID = "jimu/live-adapter"
BRIDGE_VERSION = "1.0.0"
CHAT_PATH = "/v1/chat/completions"
MAX_WIRE = 16 * 1024 * 1024
_STAGE1_TYPES = {
    "nonmotor_vehicle": "non_motor_vehicle",
    "food_service_worker": "person",
}
_DETECTION_LABELS = {
    "nonmotor_vehicle": "non-motor vehicle",
    "food_service_worker": "person",
}
_STAGE1_MODEL = "090ee95e9a1649d35854c9559bac9421b80faba3062b3b1903bd3ae8ea77eb19"
_WEVIS_MODEL = "WeVisOne-VL-4B-NF4"
_QWEN_MODEL = "Qwen3_5-9B"
_WIRE_KEYS = {"model", "messages", "stream", "response_format"}
_QWEN_TOP_EXTRAS = (
    "prompt_logprobs", "prompt_token_ids", "prompt_text", "kv_transfer_params",
    "ec_transfer_params", "metrics",
)
_QWEN_CHOICE_EXTRAS = ("stop_reason", "token_ids", "routed_experts")
_QWEN_MESSAGE_EXTRAS = ("reasoning", "reasoning_content")


class BridgeError(ValueError):
    def __init__(self, code, status=422):
        super().__init__(code)
        self.status = status


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _json(value):
    return json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode()


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


def _image_part(parts, required):
    if len(parts) == 1 and not required:
        return None, None
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


def _open_upstream(url, body, content_type, timeout, audit):
    audit["upstream"] = {
        "url": url, "content_type": content_type, "request": _summary(body),
    }
    request = Request(url, data=body, headers={"Content-Type": content_type}, method="POST")
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
        raise BridgeError("upstream_http_error", 502) from None
    except (OSError, TimeoutError):
        raise BridgeError("upstream_transport_error", 502) from None
    finally:
        audit["upstream"]["round_trip_s"] = round(time.monotonic() - started, 3)
    if len(result) > MAX_WIRE:
        raise BridgeError("upstream_response_too_large", 502)
    try:
        return _loads(result)
    except (ValueError, TypeError):
        raise BridgeError("invalid_upstream_json", 502) from None


def _discover(url, media_type, image, metadata, timeout, audit):
    eligible = metadata["eligible_profile_ids"]
    if not isinstance(eligible, list) or not eligible or any(
        type(profile) is not str for profile in eligible
    ) or len(set(eligible)) != len(eligible):
        raise BridgeError("invalid_eligible_profiles")
    audit["eligible_profile_ids"] = eligible
    response = _open_upstream(url, image, media_type, timeout, audit)
    objects = response["objects"]
    if response["model_id"] != _STAGE1_MODEL:
        raise BridgeError("unexpected_stage1_model", 502)
    if (response["schema_version"], response["policy_version"]) != (
        "routing/1.0", "inspection-routing/1.0"
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
        "model": response["model_id"], "schema_version": response["schema_version"],
        "policy_version": response["policy_version"], "routes": routes,
        "upstream_questions_used": False,
    }
    unsupported = [profile for profile in eligible if profile not in _STAGE1_TYPES]
    if unsupported:
        audit["unsupported_profiles"] = unsupported
        raise BridgeError("stage1_unsupported_profile")
    hits = []
    audit["profile_target_map"] = {}
    for profile in eligible:
        target = _STAGE1_TYPES[profile]
        audit["profile_target_map"][profile] = target
        if target not in routes:
            raise BridgeError("stage1_missing_target_output", 502)
        if routes[target]["route_enabled"]:
            hits.append(profile)
    return {"profile_hits": hits}


def _detect(url, media_type, image, metadata, labels, timeout, audit):
    from PIL import Image
    from io import BytesIO

    profile = metadata["profile_id"]
    if profile not in labels:
        raise BridgeError("wevis_unsupported_profile")
    label = labels[profile]
    audit.update(profile_id=profile, wevis_query=label)
    response = _open_upstream(
        f"{url}?{urlencode({'label': label})}", image, media_type, timeout, audit
    )
    if response["model"] != _WEVIS_MODEL or response["task"] != "detection":
        raise BridgeError("unexpected_wevis_identity_or_task", 502)
    if response["image_sha256"] != _sha(image) or type(response["model_forward_calls"]) is not int or response["model_forward_calls"] < 1:
        raise BridgeError("invalid_wevis_inference_evidence", 502)
    with Image.open(BytesIO(image)) as original:
        width, height = original.size
    if response["image_size"] != [width, height]:
        raise BridgeError("wevis_image_size_mismatch", 502)
    predictions = response["predictions"]
    audit["wevis"] = {
        "model": response["model"], "model_response": response["model_response"],
        "image_size": [width, height], "forward_calls": response["model_forward_calls"],
        "predicted_labels": list(predictions),
    }
    if not isinstance(predictions, dict):
        raise BridgeError("invalid_wevis_predictions", 502)
    if not predictions:
        # An empty parser result can represent either a model absence or unparseable
        # output. Preserve this distinction unless an explicit absence is recognized.
        raise BridgeError("wevis_no_parseable_grounding", 502)
    if set(predictions) != {label}:
        raise BridgeError("unexpected_wevis_detection_label", 502)
    coordinates = predictions[label]
    if not isinstance(coordinates, list):
        raise BridgeError("invalid_wevis_coordinates", 502)
    if not coordinates and response["model_response"].strip() != f"<ref>{label}</ref><box>none</box>":
        raise BridgeError("wevis_no_parseable_grounding", 502)
    boxes = []
    for box in coordinates:
        if type(box) is not list or len(box) != 4 or any(
            type(edge) not in (int, float) or not math.isfinite(edge) for edge in box
        ):
            raise BridgeError("invalid_wevis_box", 502)
        x0, y0, x1, y1 = box
        if not (0 <= x0 < x1 <= width and 0 <= y0 < y1 <= height):
            raise BridgeError("wevis_box_out_of_source_frame", 502)
        boxes.append({"xyxy": box})
    return {"boxes": boxes}


def _project_qwen(url, body, timeout, audit):
    request = {**body, "model": _QWEN_MODEL}
    raw = _open_upstream(url, _json(request), "application/json", timeout, audit)
    choices = raw["choices"]
    if raw["model"] != _QWEN_MODEL or raw["object"] != "chat.completion":
        raise BridgeError("unexpected_qwen_identity", 502)
    if len(choices) != 1 or choices[0]["finish_reason"] != "stop" or choices[0]["index"] != 0:
        raise BridgeError("qwen_completion_incomplete", 502)
    choice = choices[0]
    message = choice["message"]
    if message["role"] != "assistant" or any(
        message.get(key) not in (None, []) for key in ("refusal", "tool_calls", "function_call", "audio")
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
                discarded[f"{prefix}{key}"] = None if value is None else _summary(_json(value))
    audit["qwen"] = {
        "actual_model": raw["model"], "upstream_completion_id": raw.get("id"),
        "system_fingerprint": raw.get("system_fingerprint"),
        "removed_envelope_fields": discarded,
        "unaltered_completion_content": content,
        "completion_content": _summary(content.encode()),
    }
    raw["model"] = MODEL_ID
    return raw


@contextmanager
def start_bridge(
    log_directory: Path,
    *,
    stage1_url="http://127.0.0.1:18101/v1/route",
    wevis_url="http://127.0.0.1:18103/v1/detect",
    qwen_url="http://192.168.32.6:8029/v1/chat/completions",
    request_timeout=90,
    port=0,
    detection_labels=None,
):
    """Start a background HTTP server on loopback. Its URL includes the Chat path."""
    log_directory = Path(log_directory)
    log_directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    labels = {**_DETECTION_LABELS, **(detection_labels or {})}

    class Handler(BaseHTTPRequestHandler):
        def setup(self):
            super().setup()
            self.connection.settimeout(request_timeout)

        def log_message(self, *_):
            return

        def do_POST(self):
            trace_id = "live-" + uuid.uuid4().hex
            audit = {
                "bridge_request_id": trace_id, "timestamp_epoch": time.time(),
                "bridge_identity": {
                    "model_id": MODEL_ID, "deployment_version": BRIDGE_VERSION,
                    "wire_model_version": BRIDGE_VERSION,
                    "scope": "temporary_aggregate_adapter",
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
                audit["runtime_request"] = _summary(raw)
                body = _loads(raw)
                if set(body) != _WIRE_KEYS or body["model"] != MODEL_ID or body["stream"] is not False:
                    raise BridgeError("incompatible_chat_request")
                (user,) = body["messages"]
                parts = user["content"]
                if user["role"] != "user" or parts[0]["type"] != "text":
                    raise BridgeError("invalid_chat_messages")
                prompt = parts[0]["text"]
                schema = body["response_format"]["json_schema"]["schema"]
                capability = schema["$id"].split(":")[-2]
                audit.update(
                    capability=capability, schema_id=schema["$id"],
                    response_schema=_summary(_json(schema)), prompt=_summary(prompt.encode()),
                )
                media_type, image = _image_part(parts, required=True)
                audit.update(image=_summary(image), image_media_type=media_type)
                if capability == "profile_discovery-result":
                    result = _discover(
                        stage1_url, media_type, image, _loads(prompt.splitlines()[-1]),
                        request_timeout, audit
                    )
                elif capability == "object_grounding-result":
                    result = _detect(
                        wevis_url, media_type, image, _loads(prompt.splitlines()[-1]),
                        labels, request_timeout, audit
                    )
                elif capability in ("object_observation-result", "candidate_prediction-result"):
                    envelope = _project_qwen(qwen_url, body, request_timeout, audit)
                    result = None
                else:
                    raise BridgeError("unsupported_capability")
                if result is not None:
                    audit["projected_result"] = result
                    envelope = {
                        "model": MODEL_ID, "object": "chat.completion", "id": trace_id,
                        "choices": [{"index": 0, "finish_reason": "stop", "message": {
                            "role": "assistant", "content": _json(result).decode(),
                        }}],
                    }
                response = _json(envelope)
            except BridgeError as error:
                status = error.status
                audit["failure"] = str(error)
                response = _json({"error": {"code": str(error), "type": "temporary_live_bridge"}})
            except (ValueError, KeyError, TypeError, IndexError, AttributeError, OSError) as error:
                status = 502 if "upstream" in audit else 422
                audit["failure"] = type(error).__name__
                response = _json({"error": {"code": "invalid_bridge_wire", "type": "temporary_live_bridge"}})
            audit.update(runtime_http=status, runtime_response=_summary(response))
            (log_directory / f"{trace_id}.json").write_bytes(_json(audit) + b"\n")
            try:
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(response)))
                self.send_header("X-Request-ID", trace_id)
                if status == 200:
                    self.send_header("X-Jimu-Deployment-Version", BRIDGE_VERSION)
                    self.send_header("X-Jimu-Model-Version", BRIDGE_VERSION)
                self.end_headers()
                self.wfile.write(response)
            except (BrokenPipeError, ConnectionResetError):
                pass

    with ThreadingHTTPServer(("127.0.0.1", port), Handler) as bridge:
        bridge.url = f"http://127.0.0.1:{bridge.server_port}{CHAT_PATH}"
        worker = Thread(target=bridge.serve_forever, daemon=True)
        worker.start()
        try:
            yield bridge
        finally:
            bridge.shutdown()
            worker.join()
