"""Bounded JPEG/PNG CPU operations for the trusted local media adapter."""

from io import BytesIO
from typing import Literal

from PIL import Image, UnidentifiedImageError

from jimu_runtime.domain.runtime.media import ImageDimensions
from jimu_runtime.inspection.media.types import ImageMime, MediaError, MediaLimits

_FORMATS: dict[ImageMime, str] = {"image/jpeg": "JPEG", "image/png": "PNG"}
_PNG_END = b"\x00\x00\x00\x00IEND\xaeB\x60\x82"


def _single_jpeg(content: bytes) -> bool:
    """Locate the first top-level EOI while skipping segments and stuffed data."""
    if not content.startswith(b"\xff\xd8"):
        return False
    end = len(content)
    position = 2
    scanning = False
    while position < end:
        if scanning:
            position = content.find(b"\xff", position)
            if position < 0:
                return False
        elif content[position] != 0xFF:
            return False
        position += 1
        while position < end and content[position] == 0xFF:
            position += 1
        if position == end:
            return False
        marker = content[position]
        position += 1
        if marker == 0xD9:
            return position == end
        if scanning and (marker == 0 or 0xD0 <= marker <= 0xD7):
            continue
        if marker <= 0x01 or 0xD0 <= marker <= 0xD8 or position + 2 > end:
            return False
        length = int.from_bytes(content[position : position + 2], "big")
        if length < 2:
            return False
        position += length
        scanning = marker == 0xDA
    return False


def verify_image(
    content: bytes,
    mime: ImageMime,
    dimensions: ImageDimensions,
    limits: MediaLimits,
    *,
    encoding: Literal["rgb", "binary_mask"],
) -> None:
    expected_format = _FORMATS[mime]
    if mime == "image/png" and not content.endswith(_PNG_END):
        raise MediaError("media_decode_failed")
    if mime == "image/jpeg" and not _single_jpeg(content):
        raise MediaError("media_decode_failed")
    try:
        with (
            BytesIO(content) as source,
            Image.open(source, formats=(expected_format,)) as image,
        ):
            if image.format != expected_format:
                raise MediaError("media_format_mismatch")
            width, height = image.size
            if (
                not 1 <= width <= limits.max_dimension
                or not 1 <= height <= limits.max_dimension
                or width * height > limits.max_pixels
            ):
                raise MediaError("media_dimensions_limit")
            if (width, height) != (dimensions.width, dimensions.height):
                raise MediaError("media_dimensions_mismatch")
            if (
                getattr(image, "n_frames", 1) != 1
                or "loop" in image.info
                or "transparency" in image.info
            ):
                raise MediaError("media_frame_unsupported")
            if encoding == "rgb" and image.mode != "RGB":
                raise MediaError("media_rgb_required")
            if encoding == "binary_mask" and image.mode not in {"1", "L"}:
                raise MediaError("media_binary_mask_required")
            image.verify()
            # PNG verification consumes IEND up to its four-byte CRC.
            if mime == "image/png" and source.tell() != len(content) - 4:
                raise MediaError("media_frame_unsupported")
        # verify() checks container integrity. load() forces actual pixel decoding.
        with Image.open(BytesIO(content), formats=(expected_format,)) as image:
            image.load()
            if image.getexif().get(274, 1) != 1 or "transparency" in image.info:
                raise MediaError("media_frame_unsupported")
            if encoding == "binary_mask" and image.mode == "L":
                histogram = image.histogram()
                if any(histogram[1:255]):
                    raise MediaError("media_binary_mask_required")
    except MediaError:
        raise
    except (
        Image.DecompressionBombError,
        UnidentifiedImageError,
        OSError,
        ValueError,
        SyntaxError,
        OverflowError,
        TypeError,
        IndexError,
    ):
        raise MediaError("media_decode_failed") from None
