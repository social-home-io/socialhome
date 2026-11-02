"""Tests for socialhome.media.video_processor."""

import io
from types import SimpleNamespace

import av
import pytest
from PIL import Image

from socialhome.domain.media_constraints import VIDEO_MAX_DIMENSION
from socialhome.media import video_processor as vp
from socialhome.media.video_processor import VideoProcessor
from tests.video_clips import make_clip


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


async def test_thumbnail_caps_longest_side_at_thumbnail_px():
    """Video poster must be bounded by THUMBNAIL_PX, not the full
    video encode dimension — emitting the full-resolution frame for a
    grid tile is many times the bytes for no UX gain.
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


# ── File API: probe, transcode file to file, poster ────────────────────


def _write(tmp_path, name: str, data: bytes):
    path = tmp_path / name
    path.write_bytes(data)
    return path


async def test_probe_reads_length_stored_size_and_day(tmp_path):
    src = _write(
        tmp_path,
        "a.mp4",
        make_clip(
            4, size=(2560, 1440), metadata={"creation_time": "2026-05-17T16:20:00Z"}
        ),
    )
    probe = await VideoProcessor().probe_file(src)
    assert probe.duration_s == pytest.approx(4.0, abs=0.1)
    # The stored size: longest edge at VIDEO_MAX_DIMENSION.
    assert (probe.width, probe.height) == (VIDEO_MAX_DIMENSION, 1080)
    assert probe.rotation == 0
    assert probe.taken_on == "2026-05-17"


async def test_probe_of_a_portrait_phone_video_swaps_the_sides(tmp_path):
    src = _write(tmp_path, "p.mov", make_clip(2, size=(64, 32), rotation=90))
    probe = await VideoProcessor().probe_file(src)
    assert (probe.width, probe.height, probe.rotation) == (32, 64, 90)


async def test_probe_refuses_a_file_that_is_not_a_video(tmp_path):
    src = _write(tmp_path, "x.mp4", b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 200)
    with pytest.raises(ValueError):
        await VideoProcessor().probe_file(src)
    with pytest.raises(ValueError):
        await VideoProcessor().probe_file(tmp_path / "missing.mp4")


async def test_transcode_file_keeps_the_given_length(tmp_path):
    src = _write(tmp_path, "a.mp4", make_clip(30, fps=10))
    proc = VideoProcessor()
    seen: list[float] = []

    await proc.transcode_file(
        src, tmp_path / "all.webm", max_duration_s=60, on_progress=seen.append
    )
    with av.open(str(tmp_path / "all.webm")) as c:
        # Every frame, the last ones a threaded decoder holds included.
        assert len(list(c.decode(video=0))) == 300
    assert seen == sorted(seen) and seen[-1] >= 28.0

    await proc.transcode_file(src, tmp_path / "cut.webm", max_duration_s=10)
    with av.open(str(tmp_path / "cut.webm")) as c:
        assert c.duration / av.time_base == pytest.approx(10.0, abs=0.2)


async def test_transcode_file_writes_vp9_opus_webm_and_drops_the_files_tags(tmp_path):
    """The stored WebM carries none of the phone's tags — no date, no
    place — only what the encoder writes."""
    src = _write(
        tmp_path,
        "a.mp4",
        make_clip(
            2,
            audio=True,
            metadata={
                "creation_time": "2026-05-17T16:20:00Z",
                "location": "+48.1372+011.5755/",
                "title": "Mias Geburtstag",
            },
        ),
    )
    await VideoProcessor().transcode_file(src, tmp_path / "o.webm", max_duration_s=60)
    with av.open(str(tmp_path / "o.webm")) as c:
        assert c.streams.video[0].codec_context.name == "vp9"
        assert [a.codec_context.name for a in c.streams.audio] == ["opus"]
        tags = {k.lower() for k in c.metadata}
    assert tags <= {"encoder"}


async def test_portrait_video_and_its_poster_are_upright(tmp_path):
    src = _write(
        tmp_path, "p.mov", make_clip(2, size=(64, 32), rotation=90, split_colour=True)
    )
    proc = VideoProcessor()
    await proc.transcode_file(src, tmp_path / "p.webm", max_duration_s=60)
    with av.open(str(tmp_path / "p.webm")) as c:
        frame = next(c.decode(video=0)).to_image()
    assert frame.size == (32, 64)
    assert frame.getpixel((16, 56))[0] > 200  # red (was left) now at the bottom
    with Image.open(io.BytesIO(await proc.thumbnail_file(src))) as poster:
        assert poster.format == "WEBP"
        assert poster.size == (32, 64)


async def test_process_bytes_keeps_the_protocol_60s(tmp_path):
    """The in-memory path (feed / DM sized clips) still cuts at 60 s."""
    out, name = await VideoProcessor().process(make_clip(65), "a.mp4")
    assert name.endswith(".webm")
    with av.open(io.BytesIO(out)) as c:
        assert c.duration / av.time_base == pytest.approx(60.0, abs=1.5)


def test_encoder_threads_leave_a_core_and_stop_at_four(monkeypatch):
    for cores, want in ((1, 1), (2, 1), (4, 3), (18, 4), (None, 1)):
        monkeypatch.setattr(vp.os, "cpu_count", lambda c=cores: c)
        assert vp.encoder_threads() == want


def test_taken_on_prefers_the_phones_local_day():
    container = SimpleNamespace(
        metadata={
            "creation_time": "2026-05-17T22:30:00.000000Z",
            "com.apple.quicktime.creationdate": "2026-05-18T00:30:00+0200",
        }
    )
    stream = SimpleNamespace(metadata={})
    assert vp._taken_on(container, stream) == "2026-05-18"


def test_taken_on_falls_back_to_the_stream_and_ignores_unset_clocks():
    stream = SimpleNamespace(metadata={"creation_time": "2025-12-24T18:00:00Z"})
    assert vp._taken_on(SimpleNamespace(metadata={}), stream) == "2025-12-24"
    for bogus in ("1904-01-01T00:00:00Z", "1970-01-01T00:00:00Z", "2999-01-01", "x"):
        c = SimpleNamespace(metadata={"creation_time": bogus})
        assert vp._taken_on(c, SimpleNamespace(metadata={})) is None
    assert vp._parse_day("2026-02-30T00:00:00Z") is None


def test_rotation_is_normalised_to_quarter_turns():
    assert [vp._normalise_rotation(r) for r in (0, 90, -90, 180, 270, 360, None)] == [
        0,
        90,
        270,
        180,
        270,
        0,
        0,
    ]
    assert vp._normalise_rotation("x") == 0
