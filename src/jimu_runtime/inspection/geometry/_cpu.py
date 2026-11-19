"""Digest-pinned entry point for the synthetic scene's CPU-only pixel tools."""

import argparse
import base64
import binascii
import io
import json
import math
import sys
from collections.abc import Iterator
from typing import BinaryIO, Literal

from PIL import Image

type Operation = Literal[
    "wheel_segmentation", "tactile_paving_segmentation", "wheel_boundary_geometry"
]
type Box = tuple[float, float, float, float]
type Relation = Literal[
    "inside_parking_area",
    "outside_parking_area",
    "over_boundary",
    "blocking_tactile_paving",
]
type Counts = dict[str, int]

# Limits for this particular synthetic implementation, additional to the pinned
# Tool's supervised byte and time limits. It recognizes fixture colors only.
MAX_WIRE_BYTES = 262_144
MAX_DIMENSION = 1024
MAX_PIXELS = 262_144
WHEEL_COLOR = (16, 174, 208)
TACTILE_COLOR = (224, 192, 32)
# The synthetic generator uses this explicit pixel class when a wheel occludes
# tactile paving. Both segmentation commands recognize the double label.
WHEEL_ON_TACTILE_COLOR = (24, 192, 64)
OPERATIONS = (
    "wheel_segmentation",
    "tactile_paving_segmentation",
    "wheel_boundary_geometry",
)


def load_image(
    content: bytes,
    dimensions: tuple[int, int],
    *,
    mask: bool,
) -> Image.Image:
    width, height = dimensions
    if (
        not 0 < len(content) <= MAX_WIRE_BYTES
        or not 0 < width <= MAX_DIMENSION
        or not 0 < height <= MAX_DIMENSION
        or width * height > MAX_PIXELS
    ):
        raise ValueError("synthetic CPU image budget exceeded")
    try:
        with Image.open(io.BytesIO(content), formats=["PNG"]) as image:
            if (
                image.mode != ("L" if mask else "RGB")
                or image.size != dimensions
                or getattr(image, "n_frames", 1) != 1
            ):
                raise ValueError(
                    "synthetic CPU image mode, frame, or dimensions mismatch"
                )
            image.verify()
        with Image.open(io.BytesIO(content), formats=["PNG"]) as image:
            image.load()
            output = image.copy()
    except (OSError, SyntaxError, EOFError, Image.DecompressionBombError) as exc:
        raise ValueError("invalid synthetic CPU image bytes") from exc
    if mask and any(value not in (0, 255) for value in output.tobytes()):
        raise ValueError("mask is not binary")
    return output


def _box(raw: object, dimensions: tuple[int, int]) -> Box:
    if (
        not isinstance(raw, list)
        or len(raw) != 4
        or any(
            type(part) not in (int, float) or not math.isfinite(part) for part in raw
        )
    ):
        raise ValueError("invalid source rectangle")
    x0, y0, x1, y1 = raw
    if not (0 <= x0 < x1 <= dimensions[0] and 0 <= y0 < y1 <= dimensions[1]):
        raise ValueError("rectangle exceeds source image")
    return (x0, y0, x1, y1)


def _indices(data: bytes) -> Iterator[int]:
    return (index for index, value in enumerate(data) if value == 255)


def segment(
    image: Image.Image, color: tuple[int, int, int], box: Box | None
) -> tuple[bytes, int]:
    width, height = image.size
    source = image.tobytes()
    expected = (bytes(color), bytes(WHEEL_ON_TACTILE_COLOR))
    mask = bytearray(width * height)
    count = 0
    for index in range(width * height):
        if source[3 * index : 3 * index + 3] not in expected:
            continue
        y, x = divmod(index, width)
        if box is not None and not (
            box[0] <= x + 0.5 < box[2] and box[1] <= y + 0.5 < box[3]
        ):
            continue
        mask[index] = 255
        count += 1
    output = io.BytesIO()
    Image.frombytes("L", (width, height), bytes(mask)).save(output, format="PNG")
    return output.getvalue(), count


def measure(wheel: Image.Image, tactile: Image.Image, box: Box) -> Counts:
    width, _ = wheel.size
    counts = dict(
        wheel=0, tactile=0, overlap=0, inside=0, outside=0, crossing=0, touching=0
    )
    tactile_bytes = tactile.tobytes()
    counts["tactile"] = tactile_bytes.count(255)
    x0, y0, x1, y1 = box
    for index in _indices(wheel.tobytes()):
        y, x = divmod(index, width)
        counts["wheel"] += 1
        counts["overlap"] += int(tactile_bytes[index] == 255)
        if x + 1 < x0 or x > x1 or y + 1 < y0 or y > y1:
            counts["outside"] += 1
        elif x > x0 and x + 1 < x1 and y > y0 and y + 1 < y1:
            counts["inside"] += 1
        elif (
            x < x1
            and x + 1 > x0
            and y < y1
            and y + 1 > y0
            and (x < x0 or x + 1 > x1 or y < y0 or y + 1 > y1)
        ):
            counts["crossing"] += 1
        else:
            counts["touching"] += 1
    return counts


def classify(counts: Counts) -> tuple[Relation | None, str | None]:
    if not counts["wheel"] or not counts["tactile"]:
        return None, "empty_segmentation"
    inside, outside = counts["inside"], counts["outside"]
    if counts["crossing"] or (inside and outside):
        spatial: Relation = "over_boundary"
    elif counts["touching"]:
        return None, "boundary_contact_unresolved"
    elif outside:
        spatial = "outside_parking_area"
    else:
        spatial = "inside_parking_area"
    if counts["overlap"]:
        if spatial != "inside_parking_area":
            return None, "overlapping_relations_unresolved"
        return "blocking_tactile_paving", None
    return spatial, None


def _read_content(raw: object) -> bytes:
    if not isinstance(raw, str):
        raise ValueError("missing CPU image content")
    try:
        return base64.b64decode(raw, validate=True)
    except (ValueError, binascii.Error) as exc:
        raise ValueError("invalid CPU image encoding") from exc


def run(source: BinaryIO, sink: BinaryIO, operation: Operation) -> None:
    wire = source.read(MAX_WIRE_BYTES + 1)
    if not 0 < len(wire) <= MAX_WIRE_BYTES:
        raise ValueError("CPU request exceeds its bound")
    request = json.loads(wire)
    width, height = request["dimensions"]
    if type(width) is not int or type(height) is not int:
        raise ValueError("invalid CPU frame dimensions")
    dimensions = (width, height)
    if operation == "wheel_boundary_geometry":
        wheel = load_image(_read_content(request["wheel_mask"]), dimensions, mask=True)
        tactile = load_image(
            _read_content(request["tactile_paving_mask"]), dimensions, mask=True
        )
        metrics = measure(wheel, tactile, _box(request["bbox"], dimensions))
        relation, reason = classify(metrics)
        result: dict[str, object] = {
            "metrics": metrics,
            "relation": relation,
            "reason": reason,
        }
    else:
        image = load_image(_read_content(request["image"]), dimensions, mask=False)
        box = (
            _box(request["bbox"], dimensions)
            if operation == "wheel_segmentation"
            else None
        )
        color = WHEEL_COLOR if operation == "wheel_segmentation" else TACTILE_COLOR
        png, count = segment(image, color, box)
        result = {"png": base64.b64encode(png).decode("ascii"), "count": count}
    encoded = json.dumps(result, separators=(",", ":"), sort_keys=True).encode("ascii")
    if len(encoded) > MAX_WIRE_BYTES:
        raise ValueError("CPU response exceeds its bound")
    sink.write(encoded)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("operation", choices=OPERATIONS)
    arguments = parser.parse_args()
    try:
        run(sys.stdin.buffer, sys.stdout.buffer, arguments.operation)
    except (ValueError, TypeError, KeyError):
        sys.exit(2)
