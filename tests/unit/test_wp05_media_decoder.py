"""Physical inputs for the bounded CPU decoder."""

from io import BytesIO

import pytest
from PIL import Image

from jimu_runtime.domain.runtime.media import ImageDimensions
from jimu_runtime.inspection.media import MediaError, MediaLimits
from jimu_runtime.inspection.media.decoder import verify_image


def pixels(mode="RGB", fmt="PNG", size=(4, 3), color=0, **kwargs):
    output = BytesIO()
    with Image.new(mode, size, color) as image:
        image.save(output, format=fmt, **kwargs)
    return output.getvalue()


@pytest.mark.parametrize(
    ("mime", "fmt"), [("image/jpeg", "JPEG"), ("image/png", "PNG")]
)
def test_original_decodes_exact_pixel_grid(mime, fmt):
    verify_image(
        pixels(fmt=fmt),
        mime,
        ImageDimensions(width=4, height=3),
        MediaLimits(),
        encoding="rgb",
    )


@pytest.mark.parametrize(
    ("blob", "mime", "dimensions", "diagnostic"),
    [
        (pixels()[:-20], "image/png", (4, 3), "media_decode_failed"),
        (pixels(fmt="JPEG")[:-40], "image/jpeg", (4, 3), "media_decode_failed"),
        (pixels(fmt="JPEG"), "image/png", (4, 3), "media_decode_failed"),
        (pixels(mode="L"), "image/png", (4, 3), "media_rgb_required"),
        (pixels(mode="RGBA"), "image/png", (4, 3), "media_rgb_required"),
        (pixels(), "image/png", (3, 4), "media_dimensions_mismatch"),
        (pixels(size=(5, 5)), "image/png", (5, 5), "media_dimensions_limit"),
        (
            pixels(transparency=(0, 0, 0)),
            "image/png",
            (4, 3),
            "media_frame_unsupported",
        ),
    ],
)
def test_original_requires_bounded_unambiguous_rgb(blob, mime, dimensions, diagnostic):
    with pytest.raises(MediaError, match=diagnostic):
        verify_image(
            blob,
            mime,
            ImageDimensions(width=dimensions[0], height=dimensions[1]),
            MediaLimits(max_pixels=20),
            encoding="rgb",
        )


def test_original_does_not_apply_exif_orientation_or_accept_animation():
    exif = Image.Exif()
    exif[274] = 6
    rotated = pixels(fmt="JPEG", exif=exif)
    with Image.new("RGB", (4, 3), "white") as second:
        animated = pixels(save_all=True, append_images=[second])
    for blob, mime in ((rotated, "image/jpeg"), (animated, "image/png")):
        with pytest.raises(MediaError, match="media_frame_unsupported"):
            verify_image(
                blob,
                mime,
                ImageDimensions(width=4, height=3),
                MediaLimits(),
                encoding="rgb",
            )


@pytest.mark.parametrize(
    ("mode", "color"), [("1", 0), ("1", 255), ("L", 0), ("L", 255)]
)
def test_binary_png_accepts_one_bit_or_extreme_grayscale_values(mode, color):
    verify_image(
        pixels(mode=mode, color=color),
        "image/png",
        ImageDimensions(width=4, height=3),
        MediaLimits(),
        encoding="binary_mask",
    )


@pytest.mark.parametrize(
    ("mode", "color"), [("L", 1), ("L", 127), ("L", 254), ("P", 0), ("RGB", 0)]
)
def test_binary_png_rejects_ambiguous_modes_and_intermediate_values(mode, color):
    with pytest.raises(MediaError, match="media_binary_mask_required"):
        verify_image(
            pixels(mode=mode, color=color),
            "image/png",
            ImageDimensions(width=4, height=3),
            MediaLimits(),
            encoding="binary_mask",
        )


@pytest.mark.parametrize(
    ("mime", "fmt"), [("image/jpeg", "JPEG"), ("image/png", "PNG")]
)
def test_concatenated_images_are_not_a_single_image(mime, fmt):
    image = pixels(fmt=fmt)
    with pytest.raises(MediaError, match="media_decode_failed|media_frame_unsupported"):
        verify_image(
            image + image,
            mime,
            ImageDimensions(width=4, height=3),
            MediaLimits(),
            encoding="rgb",
        )


def test_progressive_jpeg_and_exif_payload_are_allowed_without_rotation():
    exif = Image.Exif()
    exif[270] = "camera metadata"
    progressive = pixels(fmt="JPEG", progressive=True, exif=exif)
    verify_image(
        progressive,
        "image/jpeg",
        ImageDimensions(width=4, height=3),
        MediaLimits(),
        encoding="rgb",
    )
