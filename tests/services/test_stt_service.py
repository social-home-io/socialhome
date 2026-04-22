"""Unit tests for SttService with a fake adapter (§platform/stt)."""

from __future__ import annotations

from typing import AsyncIterable

import pytest

from socialhome.services.stt_service import SttService, SttUnsupportedError


class _FakeAdapter:
    def __init__(self, *, supports: bool = True, result: str = "ok") -> None:
        self.supports_stt = supports
        self._result = result
        self.calls: list[dict] = []

    async def stream_transcribe_audio(
        self,
        audio_stream: AsyncIterable[bytes],
        *,
        language: str = "en",
        sample_rate: int = 16000,
        channels: int = 1,
    ) -> str:
        chunks = [c async for c in audio_stream]
        self.calls.append(
            {
                "chunks": chunks,
                "language": language,
                "sample_rate": sample_rate,
                "channels": channels,
            }
        )
        return self._result


async def _iter_chunks(*frames: bytes) -> AsyncIterable[bytes]:
    for f in frames:
        yield f


async def test_transcribe_stream_forwards_all_args():
    adapter = _FakeAdapter(result="hello world")
    svc = SttService(adapter)

    text = await svc.transcribe_stream(
        _iter_chunks(b"a", b"b"),
        language="de",
        sample_rate=22050,
        channels=2,
    )

    assert text == "hello world"
    assert adapter.calls == [
        {
            "chunks": [b"a", b"b"],
            "language": "de",
            "sample_rate": 22050,
            "channels": 2,
        }
    ]


async def test_supported_mirrors_adapter():
    assert SttService(_FakeAdapter(supports=True)).supported is True
    assert SttService(_FakeAdapter(supports=False)).supported is False


async def test_raises_when_adapter_does_not_support_stt():
    svc = SttService(_FakeAdapter(supports=False))

    with pytest.raises(SttUnsupportedError):
        await svc.transcribe_stream(_iter_chunks(b"x"))


async def test_supported_defaults_false_when_attribute_missing():
    """Defensive default: older adapter instances without supports_stt."""

    class _Legacy:
        async def stream_transcribe_audio(self, *args, **kwargs):
            return ""

    svc = SttService(_Legacy())  # type: ignore[arg-type]
    assert svc.supported is False
    with pytest.raises(SttUnsupportedError):
        await svc.transcribe_stream(_iter_chunks(b"x"))
