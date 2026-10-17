"""Tests for socialhome.media.audio_processor."""

from __future__ import annotations

import io

import av
import pytest

from socialhome.domain.media_constraints import AUDIO_MAX_DURATION_SECONDS
from socialhome.media.audio_processor import AudioProcessor

# ── Fixtures ───────────────────────────────────────────────────────────


def _silent_audio_frame(n_samples: int, sample_rate: int) -> av.AudioFrame:
    """Build a mono ``s16`` ``AudioFrame`` of silence (zero PCM).

    Avoids a numpy dependency by zero-filling the frame's plane
    directly. PyAV ≥ 17 supports the bare-constructor + ``planes[0]``
    write path. The plane buffer is 2 bytes per sample (s16 LE).
    """
    frame = av.AudioFrame(format="s16", layout="mono", samples=n_samples)
    frame.sample_rate = sample_rate
    # ``planes[0]`` is a writeable buffer-like; zeroing it is silence.
    frame.planes[0].update(b"\x00" * (n_samples * 2))
    return frame


def _build_ogg_opus(
    *, duration_seconds: float = 0.2, sample_rate: int = 48000
) -> bytes:
    """Build a tiny OGG/Opus blob using PyAV — silence at 24 kbps."""
    n_samples = int(sample_rate * duration_seconds)
    buf = io.BytesIO()
    out = av.open(buf, mode="w", format="ogg")
    try:
        stream = out.add_stream("libopus", rate=sample_rate)
        stream.bit_rate = 24_000
        frame = _silent_audio_frame(n_samples, sample_rate)
        for packet in stream.encode(frame):
            out.mux(packet)
        for packet in stream.encode(None):
            out.mux(packet)
    finally:
        out.close()
    return buf.getvalue()


def _build_ogg_with_codec(codec_name: str) -> bytes:
    """OGG container with a non-Opus codec (for the rejection test)."""
    sample_rate = 44_100
    n_samples = sample_rate // 4  # 0.25 s
    buf = io.BytesIO()
    out = av.open(buf, mode="w", format="ogg")
    try:
        stream = out.add_stream(codec_name, rate=sample_rate)
        frame = _silent_audio_frame(n_samples, sample_rate)
        # FLAC (and most non-Opus encoders) require a monotonic pts; the
        # Opus encoder happens to tolerate its absence but FLAC errors
        # (EINVAL) without it.
        frame.pts = 0
        for packet in stream.encode(frame):
            out.mux(packet)
        for packet in stream.encode(None):
            out.mux(packet)
    finally:
        out.close()
    return buf.getvalue()


# ── Tests ──────────────────────────────────────────────────────────────


def test_audio_processor_instantiates():
    """Default constructor binds the protocol constants."""
    proc = AudioProcessor()
    assert proc is not None
    assert proc._max_duration == AUDIO_MAX_DURATION_SECONDS


def test_accepted_mime_types():
    """Only ``audio/ogg`` is accepted at the entry point."""
    assert "audio/ogg" in AudioProcessor.ACCEPTED_MIME_TYPES
    assert "audio/mpeg" not in AudioProcessor.ACCEPTED_MIME_TYPES


async def test_process_accepts_ogg_opus_and_renames_to_uuid():
    """A valid OGG/Opus blob is returned unchanged with a ``.ogg`` UUID name."""
    blob = _build_ogg_opus(duration_seconds=0.2)
    proc = AudioProcessor()
    out_bytes, out_name = await proc.process(blob, "voice.ogg")
    assert out_bytes is blob  # unchanged, same object passed back through
    assert out_name.endswith(".ogg")
    # UUID4 hex is 32 chars + ".ogg"
    assert len(out_name) == 32 + len(".ogg")


def _build_webm_opus(duration_seconds: float = 0.2, sample_rate: int = 48000) -> bytes:
    """Build a tiny WebM/Opus blob (Chromium's MediaRecorder shape)."""
    n_samples = int(sample_rate * duration_seconds)
    buf = io.BytesIO()
    out = av.open(buf, mode="w", format="webm")
    try:
        stream = out.add_stream("libopus", rate=sample_rate)
        stream.bit_rate = 24_000
        for packet in stream.encode(_silent_audio_frame(n_samples, sample_rate)):
            out.mux(packet)
        for packet in stream.encode(None):
            out.mux(packet)
    finally:
        out.close()
    return buf.getvalue()


async def test_process_accepts_webm_opus_and_keeps_webm_extension():
    """Chromium-style WebM/Opus blobs are also accepted."""
    blob = _build_webm_opus(duration_seconds=0.2)
    proc = AudioProcessor()
    out_bytes, out_name = await proc.process(blob, "voice.webm")
    assert out_bytes is blob
    assert out_name.endswith(".webm")


def _build_mp4_aac(duration_seconds: float = 0.2, sample_rate: int = 48000) -> bytes:
    """Build a tiny MP4/AAC blob (Safari MediaRecorder shape).

    Some PyAV builds ship without the FDK / native AAC encoder, in
    which case the test that depends on this fixture is skipped.
    """
    n_samples = int(sample_rate * duration_seconds)
    buf = io.BytesIO()
    out = av.open(buf, mode="w", format="mp4")
    try:
        stream = out.add_stream("aac", rate=sample_rate)
        stream.bit_rate = 32_000
        for packet in stream.encode(_silent_audio_frame(n_samples, sample_rate)):
            out.mux(packet)
        for packet in stream.encode(None):
            out.mux(packet)
    finally:
        out.close()
    return buf.getvalue()


async def test_process_accepts_mp4_aac_and_keeps_m4a_extension():
    """Safari-style MP4/AAC voice notes are accepted."""
    try:
        blob = _build_mp4_aac(duration_seconds=0.2)
    except av.error.FFmpegError:
        pytest.skip("PyAV build lacks an AAC encoder")
    proc = AudioProcessor()
    out_bytes, out_name = await proc.process(blob, "voice.m4a")
    assert out_bytes is blob
    assert out_name.endswith(".m4a")


async def test_process_rejects_non_audio_mp4():
    """An MP4 file with no audio stream is rejected (e.g. a silent video)."""
    n_samples = 1024
    sample_rate = 44_100
    try:
        # Build a real MP4 with VIDEO + no audio.
        buf = io.BytesIO()
        out = av.open(buf, mode="w", format="mp4")
        try:
            stream = out.add_stream("h264", rate=15)
            stream.width = 16
            stream.height = 16
            stream.pix_fmt = "yuv420p"
            from PIL import Image as _Img

            for _ in range(2):
                frame = av.VideoFrame.from_image(_Img.new("RGB", (16, 16)))
                for p in stream.encode(frame):
                    out.mux(p)
            for p in stream.encode(None):
                out.mux(p)
        finally:
            out.close()
        video_mp4 = buf.getvalue()
    except av.error.FFmpegError:
        pytest.skip("PyAV build lacks h264")

    # Sanity: the bytes pass the ftyp magic check…
    assert video_mp4[4:8] == b"ftyp"
    proc = AudioProcessor()
    with pytest.raises(ValueError, match="No audio stream"):
        await proc.process(video_mp4, "fake.m4a")
    # Suppress unused-var warning when sample_rate / n_samples are
    # not strictly necessary for the codec failure mode.
    _ = (n_samples, sample_rate)


async def test_process_rejects_oversized():
    """Data exceeding ``max_input_bytes`` raises before probe."""
    proc = AudioProcessor()
    proc._max_input_bytes = 100  # type: ignore[misc]
    with pytest.raises(ValueError):
        await proc.process(b"OggS" + b"x" * 200, "big.ogg")


async def test_process_rejects_non_ogg_magic():
    """Anything that doesn't start with ``OggS`` is rejected fast."""
    proc = AudioProcessor()
    with pytest.raises(ValueError, match="OGG, WebM, or MP4"):
        await proc.process(b"ID3\x04\x00\x00", "fake.mp3")


async def test_process_rejects_non_opus_codec():
    """OGG with a non-Opus/AAC stream (here FLAC) is rejected.

    Uses FLAC rather than Vorbis for the sample: FLAC is a native
    ffmpeg codec (always built into libavcodec), whereas ``libvorbis``
    is an external encoder PyAV's manylinux wheel dropped in 17.1.0 —
    a Vorbis fixture would fail to *build* (UnknownCodecError) on that
    wheel even though production never needs the Vorbis encoder. FLAC
    keeps the rejection check independent of the wheel's codec set.
    """
    blob = _build_ogg_with_codec("flac")
    proc = AudioProcessor()
    with pytest.raises(ValueError, match="Opus or AAC"):
        await proc.process(blob, "flac.ogg")


async def test_process_rejects_overlong_duration():
    """A clip longer than the protocol cap raises before any save."""
    blob = _build_ogg_opus(duration_seconds=0.6)
    proc = AudioProcessor()
    proc._max_duration = 0.3  # type: ignore[misc]
    with pytest.raises(ValueError, match="exceeds"):
        await proc.process(blob, "long.ogg")


async def test_process_rejects_invalid_ogg_body():
    """Magic bytes pass but the container body is junk."""
    proc = AudioProcessor()
    blob = b"OggS" + b"\x00" * 30  # OGG magic but garbage body
    with pytest.raises(ValueError):
        await proc.process(blob, "junk.ogg")
