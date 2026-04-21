"""HTTP/WebSocket tests for /api/stt/stream."""

from __future__ import annotations

import asyncio
import json
from typing import AsyncIterable


from social_home.app_keys import stt_service_key


class _FakeSttService:
    """Stand-in for SttService that records audio and returns a preset result."""

    def __init__(
        self,
        *,
        supported: bool = True,
        text: str = "hello world",
        raise_exc: Exception | None = None,
    ) -> None:
        self.supported = supported
        self._text = text
        self._raise = raise_exc
        self.received: list[bytes] = []
        self.kwargs: dict = {}
        self.done = asyncio.Event()

    async def transcribe_stream(
        self,
        audio_stream: AsyncIterable[bytes],
        *,
        language: str = "en",
        sample_rate: int = 16000,
        channels: int = 1,
    ) -> str:
        self.kwargs = {
            "language": language,
            "sample_rate": sample_rate,
            "channels": channels,
        }
        async for chunk in audio_stream:
            self.received.append(chunk)
        self.done.set()
        if self._raise is not None:
            raise self._raise
        return self._text


async def test_requires_auth(client):
    """Unauthenticated WS handshake is rejected before upgrade."""
    r = await client.get("/api/stt/stream")
    assert r.status == 401


async def test_unsupported_closes_with_error_frame(client):
    """When the adapter does not support STT, server sends error and closes."""
    client.server.app[stt_service_key] = _FakeSttService(supported=False)
    ws = await client.ws_connect(f"/api/stt/stream?token={client._tok}")
    try:
        msg = await asyncio.wait_for(ws.receive(), timeout=2.0)
        payload = json.loads(msg.data)
        assert payload["type"] == "error"
        assert "not configured" in payload["detail"].lower()
    finally:
        await ws.close()


async def test_happy_path_streams_audio_and_returns_final(client):
    """Full start → binary → end → final exchange."""
    fake = _FakeSttService(text="transcribed sentence")
    client.server.app[stt_service_key] = fake

    ws = await client.ws_connect(f"/api/stt/stream?token={client._tok}")
    try:
        await ws.send_str(
            json.dumps(
                {
                    "type": "start",
                    "language": "de",
                    "sample_rate": 22050,
                    "channels": 1,
                }
            )
        )
        await ws.send_bytes(b"PCMFRAME1")
        await ws.send_bytes(b"PCMFRAME2")
        await ws.send_str(json.dumps({"type": "end"}))

        msg = await asyncio.wait_for(ws.receive(), timeout=2.0)
        payload = json.loads(msg.data)
    finally:
        await ws.close()

    assert payload == {"type": "final", "text": "transcribed sentence"}
    assert fake.received == [b"PCMFRAME1", b"PCMFRAME2"]
    assert fake.kwargs == {"language": "de", "sample_rate": 22050, "channels": 1}


async def test_missing_start_frame_errors(client):
    """An initial binary frame (not start JSON) is rejected."""
    client.server.app[stt_service_key] = _FakeSttService()
    ws = await client.ws_connect(f"/api/stt/stream?token={client._tok}")
    try:
        await ws.send_bytes(b"bytes-before-start")
        msg = await asyncio.wait_for(ws.receive(), timeout=2.0)
        payload = json.loads(msg.data)
        assert payload["type"] == "error"
    finally:
        await ws.close()


async def test_bad_start_json_errors(client):
    """Malformed start frame closes with error."""
    client.server.app[stt_service_key] = _FakeSttService()
    ws = await client.ws_connect(f"/api/stt/stream?token={client._tok}")
    try:
        await ws.send_str("not json at all")
        msg = await asyncio.wait_for(ws.receive(), timeout=2.0)
        payload = json.loads(msg.data)
        assert payload["type"] == "error"
    finally:
        await ws.close()


async def test_adapter_failure_sends_error_frame(client):
    """An exception from the adapter surfaces as an error frame."""
    fake = _FakeSttService(raise_exc=RuntimeError("engine crashed"))
    client.server.app[stt_service_key] = fake

    ws = await client.ws_connect(f"/api/stt/stream?token={client._tok}")
    try:
        await ws.send_str(json.dumps({"type": "start"}))
        await ws.send_bytes(b"x")
        await ws.send_str(json.dumps({"type": "end"}))
        msg = await asyncio.wait_for(ws.receive(), timeout=2.0)
        payload = json.loads(msg.data)
        assert payload["type"] == "error"
    finally:
        await ws.close()
