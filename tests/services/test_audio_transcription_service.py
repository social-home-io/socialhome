"""Tests for socialhome.services.audio_transcription_service."""

from __future__ import annotations

import io

import av

from socialhome.services.audio_transcription_service import (
    AudioTranscriptionService,
)

# ── Fixture helpers ─────────────────────────────────────────────────────


def _silent_audio_frame(n_samples: int, sample_rate: int) -> av.AudioFrame:
    frame = av.AudioFrame(format="s16", layout="mono", samples=n_samples)
    frame.sample_rate = sample_rate
    frame.planes[0].update(b"\x00" * (n_samples * 2))
    return frame


def _build_ogg_opus(duration: float = 0.2, sample_rate: int = 48000) -> bytes:
    n_samples = int(sample_rate * duration)
    buf = io.BytesIO()
    out = av.open(buf, mode="w", format="ogg")
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


# ── Fake adapter ────────────────────────────────────────────────────────


class _FakeAdapter:
    """Stand-in for :class:`PlatformAdapter` used by the service."""

    def __init__(
        self,
        *,
        supports: bool = True,
        result: str = "hello world",
        raises: BaseException | None = None,
    ) -> None:
        self.supports_stt = supports
        self._result = result
        self._raises = raises
        self.calls: list[tuple[int, str]] = []

    async def transcribe_audio(self, audio_bytes: bytes, language: str = "en") -> str:
        self.calls.append((len(audio_bytes), language))
        if self._raises is not None:
            raise self._raises
        return self._result


# ── Tests ───────────────────────────────────────────────────────────────


async def test_transcribe_happy_path():
    """Successful adapter call returns the trimmed text."""
    adapter = _FakeAdapter(result="hello world")
    svc = AudioTranscriptionService(adapter)
    out = await svc.transcribe(_build_ogg_opus())
    assert out == "hello world"
    # Adapter saw decoded PCM bytes, not the OGG container.
    assert adapter.calls and adapter.calls[0][0] > 0
    assert adapter.calls[0][1] == "en"


async def test_transcribe_passes_language_through():
    """The ``language`` kwarg is forwarded to the adapter."""
    adapter = _FakeAdapter(result="hallo welt")
    svc = AudioTranscriptionService(adapter)
    out = await svc.transcribe(_build_ogg_opus(), language="de")
    assert out == "hallo welt"
    assert adapter.calls[0][1] == "de"


async def test_transcribe_returns_none_when_no_stt_capability():
    """``supports_stt=False`` → ``None`` without touching the adapter."""
    adapter = _FakeAdapter(supports=False)
    svc = AudioTranscriptionService(adapter)
    assert await svc.transcribe(_build_ogg_opus()) is None
    assert adapter.calls == []


async def test_transcribe_returns_none_on_empty_bytes():
    """Pre-decode short-circuit when the caller hands us nothing."""
    adapter = _FakeAdapter()
    svc = AudioTranscriptionService(adapter)
    assert await svc.transcribe(b"") is None
    assert adapter.calls == []


async def test_transcribe_returns_none_when_decode_fails():
    """A non-decodable blob fails silently — no exception bubbles up."""
    adapter = _FakeAdapter()
    svc = AudioTranscriptionService(adapter)
    out = await svc.transcribe(b"this is definitely not an ogg blob")
    assert out is None
    # Adapter never called because decode failed first.
    assert adapter.calls == []


async def test_transcribe_returns_none_when_adapter_raises():
    """Provider exceptions are swallowed."""
    adapter = _FakeAdapter(raises=RuntimeError("backend down"))
    svc = AudioTranscriptionService(adapter)
    out = await svc.transcribe(_build_ogg_opus())
    assert out is None


async def test_transcribe_returns_none_when_adapter_raises_not_implemented():
    """``NotImplementedError`` from a partial provider → silent ``None``."""
    adapter = _FakeAdapter(raises=NotImplementedError("no stt yet"))
    svc = AudioTranscriptionService(adapter)
    out = await svc.transcribe(_build_ogg_opus())
    assert out is None


async def test_transcribe_returns_none_when_adapter_returns_whitespace():
    """Whitespace-only transcript is treated as 'no speech found'."""
    adapter = _FakeAdapter(result="   \n\t  ")
    svc = AudioTranscriptionService(adapter)
    out = await svc.transcribe(_build_ogg_opus())
    assert out is None


async def test_transcribe_strips_surrounding_whitespace():
    """The returned transcript has no leading/trailing whitespace."""
    adapter = _FakeAdapter(result="  hello world  ")
    svc = AudioTranscriptionService(adapter)
    out = await svc.transcribe(_build_ogg_opus())
    assert out == "hello world"
