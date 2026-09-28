"""Tests for socialhome.media.image_processor."""

import io as _io
import random

import pytest
from PIL import Image as _Image

from socialhome.media.image_processor import ImageProcessor, MAGIC_BYTES


def test_image_processor_instantiates():
    """ImageProcessor can be constructed with defaults."""
    proc = ImageProcessor()
    assert proc is not None


def test_magic_bytes_defined():
    """MAGIC_BYTES is available for pre-validation."""
    assert len(MAGIC_BYTES) >= 2


async def test_process_valid_jpeg():
    """process() on a minimal JPEG returns WebP bytes."""
    from PIL import Image
    import io

    img = Image.new("RGB", (100, 100), color="red")
    buf = io.BytesIO()
    img.save(buf, format="JPEG")
    jpeg_bytes = buf.getvalue()

    proc = ImageProcessor()
    result_bytes, filename = await proc.process(jpeg_bytes, "test.jpg")
    assert filename.endswith(".webp")
    assert len(result_bytes) > 0
    result_img = Image.open(io.BytesIO(result_bytes))
    assert result_img.format == "WEBP"


async def test_process_invalid_data():
    """Random bytes are rejected with ValueError."""
    proc = ImageProcessor()
    with pytest.raises(ValueError):
        await proc.process(b"not an image at all", "garbage.jpg")


async def test_generate_thumbnail():
    """generate_thumbnail returns smaller image bytes."""
    from PIL import Image
    import io

    img = Image.new("RGB", (800, 600), color="blue")
    buf = io.BytesIO()
    img.save(buf, format="JPEG")

    proc = ImageProcessor()
    thumb_bytes = await proc.generate_thumbnail(buf.getvalue(), size=200)
    assert len(thumb_bytes) > 0
    thumb = Image.open(io.BytesIO(thumb_bytes))
    assert max(thumb.size) <= 200


async def test_process_png():
    """process() handles PNG input."""
    from PIL import Image
    import io

    img = Image.new("RGBA", (64, 64), color=(0, 255, 0, 128))
    buf = io.BytesIO()
    img.save(buf, format="PNG")

    proc = ImageProcessor()
    result_bytes, filename = await proc.process(buf.getvalue(), "test.png")
    assert filename.endswith(".webp")
    assert len(result_bytes) > 0


async def test_thumbnail_uses_lower_quality_than_main_image():
    """Thumbnails re-encode at THUMBNAIL_WEBP_QUALITY (< IMAGE_WEBP_QUALITY),
    producing smaller bytes than the full-resolution WebP at the same pixels.
    Proxy test: save a fixture twice at the same dimensions, once with each
    quality, compare byte sizes.
    """
    from PIL import Image
    import io

    from socialhome.domain.media_constraints import (
        IMAGE_WEBP_QUALITY,
        THUMBNAIL_WEBP_QUALITY,
    )

    # THUMBNAIL_WEBP_QUALITY must be strictly less than IMAGE_WEBP_QUALITY —
    # otherwise there is nothing to save and the constant is redundant.
    assert THUMBNAIL_WEBP_QUALITY < IMAGE_WEBP_QUALITY

    # Build a non-trivial test image — a uniform colour compresses to a
    # near-empty WebP at any quality; stripes actually exercise the encoder.
    img = Image.new("RGB", (400, 400))
    for x in range(400):
        for y in range(400):
            img.putpixel((x, y), ((x * 5) % 256, (y * 3) % 256, (x ^ y) % 256))

    main_buf = io.BytesIO()
    img.save(main_buf, format="WEBP", quality=IMAGE_WEBP_QUALITY)
    thumb_buf = io.BytesIO()
    img.save(thumb_buf, format="WEBP", quality=THUMBNAIL_WEBP_QUALITY)
    assert len(thumb_buf.getvalue()) < len(main_buf.getvalue())


# ─── HEIC support (#523 — Android Companion App upload regression) ─────


async def test_heic_image_processes_to_webp():
    """Modern Android cameras (Samsung One UI 6+, Pixel HEIF-on) and
    iPhones default to HEIC. Without ``pillow_heif`` registered,
    Pillow's ``Image.open`` raises ``cannot identify image file`` for
    HEIC bytes and the upload 422s with "Cannot open image" — which
    is what Pascal hit on the HA Android Companion App. This test
    proves the opener registration in
    ``socialhome/media/image_processor.py`` is loaded eagerly at
    module import time (not lazily) so the path works on first
    upload, not just after some warmup."""
    from io import BytesIO

    from PIL import Image

    from socialhome.media.image_processor import ImageProcessor

    # Build a real HEIF byte stream — only possible because the
    # ImageProcessor module already ran ``pillow_heif.register_heif_opener``
    # on import. If a future contributor moves the registration to be
    # lazy, this test fails on the ``img.save(..., format='HEIF')`` line.
    img = Image.new("RGB", (200, 200), color=(255, 0, 128))
    buf = BytesIO()
    img.save(buf, format="HEIF")
    heif_bytes = buf.getvalue()
    assert heif_bytes[:12].startswith(b"\x00\x00\x00")
    assert b"ftypheic" in heif_bytes[:64] or b"ftypmif1" in heif_bytes[:64]

    processor = ImageProcessor()
    webp_bytes, new_name = await processor.process(heif_bytes, "phone-photo.heic")
    assert webp_bytes.startswith(b"RIFF")
    assert b"WEBP" in webp_bytes[:16]
    assert new_name.endswith(".webp")


def _noise_webp(width: int, height: int) -> bytes:
    """Pure noise — WebP's worst case, so the bytes scale with the area."""
    img = _Image.frombytes(
        "RGB",
        (width, height),
        random.Random(7).randbytes(width * height * 3),
    )
    buf = _io.BytesIO()
    img.save(buf, format="WEBP", quality=75)
    return buf.getvalue()


async def test_is_valid_webp_accepts_a_webp_within_the_dimension():
    assert await ImageProcessor().is_valid_webp(_noise_webp(40, 20), max_dimension=40)


async def test_is_valid_webp_refuses_an_oversized_webp():
    assert not await ImageProcessor().is_valid_webp(
        _noise_webp(41, 20),
        max_dimension=40,
    )


async def test_is_valid_webp_refuses_other_formats_and_garbage():
    img = _Image.new("RGB", (8, 8))
    buf = _io.BytesIO()
    img.save(buf, format="PNG")
    processor = ImageProcessor()
    assert not await processor.is_valid_webp(buf.getvalue(), max_dimension=64)
    assert not await processor.is_valid_webp(b"RIFF....WEBPjunk", max_dimension=64)
    # Truncated: the header parses, the pixel data does not.
    good = _noise_webp(32, 32)
    assert not await processor.is_valid_webp(good[: len(good) // 2], max_dimension=64)


async def test_fit_within_returns_an_image_already_under_the_bound():
    data = _noise_webp(64, 64)
    assert await ImageProcessor().fit_within(data, len(data)) is data


async def test_fit_within_shrinks_an_image_over_the_bound():
    """A ~300 KiB cover comes back as a smaller WebP under the bound."""
    data = _noise_webp(800, 600)
    bound = 48 * 1024
    assert len(data) > 4 * bound
    fitted = await ImageProcessor().fit_within(data, bound)
    assert fitted is not None
    assert len(fitted) <= bound
    img = _Image.open(_io.BytesIO(fitted))
    assert img.format == "WEBP"
    # Shrunk, not cropped: the aspect ratio survives.
    assert abs(img.size[0] / img.size[1] - 800 / 600) < 0.02


async def test_fit_within_gives_up_below_the_minimum_dimension():
    data = _noise_webp(400, 400)
    assert await ImageProcessor().fit_within(data, 100, min_dimension=64) is None


async def test_fit_within_rejects_undecodable_bytes_over_the_bound():
    with pytest.raises(ValueError, match="Cannot open image to fit it"):
        await ImageProcessor().fit_within(b"not an image" * 10, 16)


async def test_fit_within_converts_palette_images():
    """A palette-mode source is converted before it is re-encoded."""
    img = _Image.frombytes(
        "RGB", (300, 300), random.Random(3).randbytes(270000)
    ).convert("P")
    buf = _io.BytesIO()
    img.save(buf, format="PNG")
    data = buf.getvalue()
    fitted = await ImageProcessor().fit_within(data, 8 * 1024)
    assert fitted is not None
    assert len(fitted) <= 8 * 1024
