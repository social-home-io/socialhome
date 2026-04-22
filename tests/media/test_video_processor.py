"""Tests for socialhome.media.video_processor."""

import pytest
from socialhome.media.video_processor import VideoProcessor


def test_video_processor_instantiates():
    """VideoProcessor can be constructed with defaults."""
    proc = VideoProcessor()
    assert proc is not None


def test_accepted_mime_types():
    """ACCEPTED_MIME_TYPES contains expected formats."""
    assert "video/mp4" in VideoProcessor.ACCEPTED_MIME_TYPES
    assert "video/webm" in VideoProcessor.ACCEPTED_MIME_TYPES


async def test_process_rejects_oversized():
    """Data exceeding max_input_bytes raises ValueError."""
    proc = VideoProcessor()
    # Temporarily lower the limit to avoid allocating 200+ MiB.
    proc._max_input_bytes = 100  # type: ignore[misc]
    with pytest.raises(ValueError):
        await proc.process(b"x" * 200, "big.mp4")


async def test_process_rejects_empty():
    """Empty data raises ValueError or RuntimeError (no ffmpeg)."""
    proc = VideoProcessor()
    with pytest.raises((ValueError, RuntimeError)):
        await proc.process(b"", "empty.mp4")


async def test_thumbnail_caps_longest_side_at_400():
    """Video poster must be bounded by THUMBNAIL_PX (400), not the full
    video encode dimension — a 1280 px poster for a 400 px tile is
    ~5× the bytes for no UX gain.
    """
    import io
    import av
    from PIL import Image

    from socialhome.domain.media_constraints import THUMBNAIL_PX

    # Build a tiny WebM clip at 1280x720 so _extract_thumbnail has
    # something larger than THUMBNAIL_PX to downsize.
    buf = io.BytesIO()
    out = av.open(buf, mode="w", format="webm")
    try:
        stream = out.add_stream("vp9", rate=1)
        stream.width = 1280
        stream.height = 720
        stream.pix_fmt = "yuv420p"
        frame_img = Image.new("RGB", (1280, 720), color=(128, 64, 200))
        video_frame = av.VideoFrame.from_image(frame_img)
        for packet in stream.encode(video_frame):
            out.mux(packet)
        for packet in stream.encode(None):
            out.mux(packet)
    finally:
        out.close()
    clip_bytes = buf.getvalue()

    proc = VideoProcessor()
    thumb_bytes = await proc.generate_thumbnail(clip_bytes)
    img = Image.open(io.BytesIO(thumb_bytes))
    assert img.format == "WEBP"
    assert max(img.size) <= THUMBNAIL_PX
