"""Pinned temporary CPU host for real WeVis masks and deterministic geometry."""
import base64
import hashlib
import io
import json
import math
import sys
import uuid
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import ProxyHandler, Request, build_opener

from PIL import Image, ImageChops
from jimu_runtime.inspection.geometry._cpu import classify, measure

MAX_REQUEST = 12 * 1024 * 1024
MAX_REPLY = 8 * 1024 * 1024
WEVIS = "http://127.0.0.1:18103/v1/segment"
LABELS = {"wheel_segmentation": "bicycle wheel",
          "tactile_paving_segmentation": "yellow tactile paving for visually impaired pedestrians"}

def decode_bytes(text):
    return base64.b64decode(text, validate=True)

def decode_png(raw, size, mode):
    with Image.open(io.BytesIO(raw), formats=["PNG"]) as image:
        if image.mode != mode or image.size != tuple(size):
            raise ValueError("PNG mode or dimensions differ from source")
        return image.copy()

def png(image):
    stream = io.BytesIO()
    image.save(stream, format="PNG")
    return stream.getvalue()

def bbox(coords, size):
    x0, y0, x1, y1 = coords
    width, height = size
    physical = math.ceil(x0 - .5), math.ceil(y0 - .5), math.ceil(x1 - .5), math.ceil(y1 - .5)
    if not 0 <= physical[0] < physical[2] <= width or not 0 <= physical[1] < physical[3] <= height:
        raise ValueError("Bounding box has no in-frame pixel centers")
    return physical

def segment(operation, payload, audit_dir):
    label = LABELS[operation]
    dims = tuple(payload["dimensions"])
    source = decode_bytes(payload["image"])
    rgb = decode_png(source, dims, "RGB")
    crop = None
    if operation == "wheel_segmentation":
        crop = bbox(payload["bbox"], dims)
        rgb = rgb.crop(crop)
        crop_bytes = png(rgb)
    else:
        crop_bytes = source
    wire_url = WEVIS + "?" + urlencode({"label": label})
    request = Request(wire_url, data=crop_bytes, headers={"Content-Type":"image/png"}, method="POST")
    with build_opener(ProxyHandler({})).open(request, timeout=70) as reply:
        wire = reply.read(MAX_REPLY + 1)
        status = reply.status
    if len(wire) > MAX_REPLY:
        raise ValueError("WeVis wire exceeds byte limit")
    body = json.loads(wire)
    request_digest = hashlib.sha256(crop_bytes).hexdigest()
    if (
        status != 200 or body["model"] != "WeVisOne-VL-4B-NF4"
        or body["task"] != "segmentation" or body["model_forward_calls"] < 1
        or body["image_sha256"] != request_digest or tuple(body["image_size"]) != rgb.size
    ):
        raise ValueError("WeVis response identity mismatch")
    mask = Image.new("L", rgb.size)
    for member in body["masks"]:
        member_mask = decode_png(decode_bytes(member["png_base64"]), rgb.size, "L")
        if any(member_mask.histogram()[1:255]):
            raise ValueError("nonbinary WeVis mask")
        mask = ImageChops.lighter(mask, member_mask)
    if crop is not None:
        aligned = Image.new("L", dims)
        aligned.paste(mask, crop[:2])
        mask = aligned
    content = png(mask)
    count = mask.histogram()[255]
    identity = uuid.uuid4().hex
    (audit_dir / f"{operation}-{identity}.wevis.json").write_bytes(wire)
    (audit_dir / f"{operation}-{identity}.audit.json").write_text(json.dumps({
        "model":body["model"], "label":label, "crop_xyxy":crop, "jet_source_size":dims,
        "source_sha256": hashlib.sha256(source).hexdigest(), "upstream_image_sha256":request_digest,
        "upstream_response_sha256":hashlib.sha256(wire).hexdigest(),
        "aligned_mask_sha256":hashlib.sha256(content).hexdigest(), "aligned_foreground":count,
        "model_forward_calls":body["model_forward_calls"], "raw_predictions":body["predictions"],
    }, ensure_ascii=False, indent=2))
    return {"png": base64.b64encode(content).decode(), "count": count}

def main(operation, audit_dir):
    wire = sys.stdin.buffer.read(MAX_REQUEST + 1)
    if len(wire) > MAX_REQUEST:
        raise ValueError("Tool request too large")
    data = json.loads(wire)
    if operation in LABELS:
        result = segment(operation, data, audit_dir)
    elif operation == "wheel_boundary_geometry":
        dims = data["dimensions"]
        wheel = decode_png(decode_bytes(data["wheel_mask"]), dims, "L")
        tactile = decode_png(decode_bytes(data["tactile_paving_mask"]), dims, "L")
        metrics = measure(wheel, tactile, tuple(data["bbox"]))
        relation, reason = classify(metrics)
        result = {"metrics": metrics, "relation": relation, "reason": reason}
    else:
        raise ValueError("Unregistered Tool operation")
    sys.stdout.write(json.dumps(result, separators=(",", ":")))

if __name__ == "__main__":
    main(sys.argv[1], Path(sys.argv[2]))
