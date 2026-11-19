"""Render a single red outline on the original image pixel grid."""

from asyncio import to_thread
from hashlib import sha256
from typing import Literal

from jimu_runtime.domain.runtime.media import BBoxXYXY, ImageDimensions
from jimu_runtime.gateway.http_wire import VerifiedImage
from jimu_runtime.inspection.media.decoder import render_rgb_outline_png, verify_image
from jimu_runtime.inspection.media.types import MediaError, MediaLimits

_LIMITS = MediaLimits()
MARKED_FRAME_RENDERER_VERSION: Literal["1.0.0"] = "1.0.0"


def _render(
    original: VerifiedImage, dimensions: ImageDimensions, bbox: BBoxXYXY
) -> VerifiedImage:
    if not 1 <= len(original.content) <= _LIMITS.max_original_bytes:
        raise MediaError("marked_frame_original_size_limit")
    bbox.validate_bounds(dimensions)
    verify_image(
        original.content, original.media_type, dimensions, _LIMITS, encoding="rgb"
    )
    if sha256(original.content).hexdigest() != original.sha256:
        raise MediaError("marked_frame_original_digest_mismatch")
    content = render_rgb_outline_png(original.content, bbox)
    if len(content) > _LIMITS.max_sidecar_bytes:
        raise MediaError("marked_frame_output_size_limit")
    return VerifiedImage(content, "image/png", sha256(content).hexdigest())


async def marked_group_image(
    original: VerifiedImage, dimensions: ImageDimensions, bbox: BBoxXYXY
) -> VerifiedImage:
    """Render version 1.0.0 off-thread and return the digest of the exact PNG."""
    return await to_thread(_render, original, dimensions, bbox)
