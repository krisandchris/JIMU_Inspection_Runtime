"""Bounded, digest-pinned worker for real parking wheel and tactile masks."""

from __future__ import annotations

import base64
import io
import json
import math
import sys
from hashlib import sha256
from pathlib import Path
from time import monotonic
from urllib.parse import urlencode
from urllib.request import ProxyHandler, Request, build_opener
from uuid import uuid4

from PIL import Image

MAX_REQUEST = 12 * 1024 * 1024
MAX_RESPONSE = 8 * 1024 * 1024
MAX_WORKER_REPLY = 2 * 1024 * 1024
MAX_PIXELS = 4_000_000
MAX_MASKS_PER_SUBJECT = 16
DEADLINE_SECONDS = 66
# The outer rectangle encloses the visible paint in the test frame. The
# inherited inner rectangle lies wholly within it. Both use original pixels.
PARKING_OUTER = (0, 470, 660, 730)
PARKING_INNER = (344, 498, 606, 702)
APPROVED_IMAGE_SHA256 = (
    "a17325853ec562bbe1946769abd0074def1ab853adf603e58605dfed5f11f9be"
)
WEVIS_MODEL = "WeVisOne-VL-4B-NF4"
WHEEL = "bicycle wheel"
TACTILE = "yellow tactile paving for visually impaired pedestrians"


def png(image: Image.Image) -> bytes:
    stream = io.BytesIO()
    image.save(stream, format="PNG")
    return stream.getvalue()


def decode_png(content: bytes, size: tuple[int, int], mode: str) -> Image.Image:
    with Image.open(io.BytesIO(content), formats=["PNG"]) as image:
        if (
            image.size != size
            or image.mode != mode
            or getattr(image, "n_frames", 1) != 1
        ):
            raise ValueError("PNG dimensions, mode or frame count do not match")
        if mode == "L" and any(image.histogram()[1:255]):
            raise ValueError("WeVis returned a non-binary mask")
        return image.copy()


def pixel_box(
    xyxy: tuple[float, float, float, float], size: tuple[int, int]
) -> tuple[int, int, int, int]:
    x0, y0, x1, y1 = xyxy
    left, top, right, bottom = (
        math.ceil(x0 - 0.5),
        math.ceil(y0 - 0.5),
        math.ceil(x1 - 0.5),
        math.ceil(y1 - 0.5),
    )
    if not 0 <= left < right <= size[0] or not 0 <= top < bottom <= size[1]:
        raise ValueError("No valid original-image pixel centers in member box")
    return (left, top, right, bottom)


def _segment(
    endpoint: str,
    directory: Path,
    label: str,
    image: bytes,
    dimensions: tuple[int, int],
    source_digest: str,
    member_index: int | None,
    crop: tuple[int, int, int, int] | None,
    deadline: float,
) -> dict:
    remaining = deadline - monotonic()
    if remaining <= 0:
        return {"status": "segmentation_budget_exhausted"}
    material = sha256(image).hexdigest()
    identity = uuid4().hex
    stem = directory / identity
    audit = {
        "label": label,
        "source_sha256": source_digest,
        "member_index": member_index,
        "crop_xyxy": crop,
        "request_sha256": material,
        "image_size": dimensions,
    }
    try:
        request = Request(
            f"{endpoint}?{urlencode({'label': label})}",
            data=image,
            headers={"Content-Type": "image/png"},
            method="POST",
        )
        with build_opener(ProxyHandler({})).open(
            request, timeout=min(remaining, 22)
        ) as reply:
            wire = reply.read(MAX_RESPONSE + 1)
            status = reply.status
        if len(wire) > MAX_RESPONSE:
            raise ValueError("WeVis response exceeded byte limit")
        stem.with_suffix(".wevis.json").write_bytes(wire)
        audit["response_sha256"] = sha256(wire).hexdigest()
        body = json.loads(wire)
        if (
            status != 200
            or body["model"] != WEVIS_MODEL
            or body["task"] != "segmentation"
            or type(body["model_forward_calls"]) is not int
            or body["model_forward_calls"] < 1
            or body["image_sha256"] != material
            or tuple(body["image_size"]) != dimensions
            or not isinstance(body["masks"], list)
            or len(body["masks"]) > MAX_MASKS_PER_SUBJECT
        ):
            raise ValueError(
                "WeVis upstream identity, dimensions or mask count mismatch"
            )
        masks = [item["png_base64"] for item in body["masks"]]
        for mask in masks:
            decode_png(base64.b64decode(mask, validate=True), dimensions, "L")
        audit.update(
            model=body["model"],
            model_forward_calls=body["model_forward_calls"],
            predicted_boxes=body["predictions"],
            mask_count=len(masks),
        )
        return {"status": "observed", "masks": masks, **audit}
    except (OSError, ValueError, KeyError, TypeError) as exc:
        audit["failure_type"] = type(exc).__name__
        return {"status": "segmentation_unavailable", **audit}
    finally:
        stem.with_suffix(".audit.json").write_text(json.dumps(audit, indent=2) + "\n")


def run(endpoint: str, directory: Path) -> None:
    wire = sys.stdin.buffer.read(MAX_REQUEST + 1)
    if not 0 < len(wire) <= MAX_REQUEST:
        raise ValueError("Worker request exceeded its input budget")
    request = json.loads(wire)
    dimensions = tuple(request["dimensions"])
    width, height = dimensions
    if not 0 < width * height <= MAX_PIXELS:
        raise ValueError("Parking image exceeds pixel budget")
    source = base64.b64decode(request["image"], validate=True)
    digest = sha256(source).hexdigest()
    rgb = decode_png(source, dimensions, "RGB")
    deadline = monotonic() + DEADLINE_SECONDS
    tactile = _segment(
        endpoint, directory, TACTILE, source, dimensions, digest, None, None, deadline
    )
    members = []
    for index, member_box in enumerate(request["member_boxes"], start=1):
        if monotonic() >= deadline:
            members.append({"status": "segmentation_budget_exhausted"})
            continue
        try:
            crop = pixel_box(tuple(member_box), dimensions)
        except ValueError:
            members.append({"status": "member_crop_unavailable"})
            continue
        member = rgb.crop(crop)
        members.append(
            _segment(
                endpoint,
                directory,
                WHEEL,
                png(member),
                member.size,
                digest,
                index,
                crop,
                deadline,
            )
        )
    # The retained report covers every input even when segmentation or the reply
    # budget fails. Preserve complete observed replies before the limit.
    while True:
        content = json.dumps(
            {"tactile": tactile, "members": members}, separators=(",", ":")
        ).encode()
        if len(content) <= MAX_WORKER_REPLY:
            break
        for index in reversed(range(len(members))):
            if members[index]["status"] == "observed":
                members[index] = {"status": "segmentation_reply_budget_exhausted"}
                break
        else:
            if tactile["status"] != "observed":
                raise ValueError("Parking reply cannot fit its transport budget")
            tactile = {"status": "segmentation_reply_budget_exhausted"}
    sys.stdout.buffer.write(content)


if __name__ == "__main__":
    run(sys.argv[1], Path(sys.argv[2]))
