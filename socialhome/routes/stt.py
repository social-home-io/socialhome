"""Speech-to-text WebSocket route (§platform/stt).

Endpoint: ``GET /api/stt/stream`` (with ``Upgrade: websocket``). Auth
mirrors :mod:`routes.ws` — bearer token via ``Authorization`` header or
``?token=`` query parameter for browser handshakes that cannot set
custom headers.

Frame protocol (one per connection):

* Client → server, text: ``{"type":"start","language":"en",
  "sample_rate":16000,"channels":1}`` — must arrive first.
* Client → server, binary: raw PCM16 little-endian chunks. Chunks are
  streamed to the platform adapter as they arrive — no buffering.
* Client → server, text: ``{"type":"end"}`` — closes the upstream HTTP
  body so the STT engine produces its final result.
* Server → client, text: ``{"type":"final","text":"..."}`` then the
  server closes the WebSocket.
* Server → client, text: ``{"type":"error","detail":"..."}`` on any
  failure (bad start frame, no STT support, adapter failure) followed
  by WS close.

The route wires inbound frames into an ``asyncio.Queue`` that the
adapter drains as an async iterable, so the HA POST body flows
byte-for-byte from the browser microphone through to HA's Whisper
pipeline.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import AsyncIterator

from aiohttp import WSMsgType, web

from .. import app_keys as K
from ..services.stt_service import SttUnsupportedError
from .base import BaseView

log = logging.getLogger(__name__)


# Sentinel queued after the client's ``end`` frame to stop the adapter's
# async iterator. ``None`` is reserved for "queue cleared before end".
_STREAM_EOF: object = object()


class SttStreamView(BaseView):
    """``GET /api/stt/stream`` — push-to-talk audio streaming to the adapter."""

    async def get(self) -> web.StreamResponse:
        ctx = self.user
        if ctx is None or ctx.user_id is None:
            return web.json_response({"error": "unauthenticated"}, status=401)

        ws = web.WebSocketResponse(heartbeat=30.0, max_msg_size=2 * 1024 * 1024)
        await ws.prepare(self.request)

        stt_service = self.request.app.get(K.stt_service_key)
        if stt_service is None or not stt_service.supported:
            await _send_error(ws, "STT is not configured on this server.")
            await ws.close()
            return ws

        try:
            start = await _read_start_frame(ws)
        except _BadStartError as exc:
            await _send_error(ws, str(exc))
            await ws.close()
            return ws

        language = str(start.get("language") or "en")
        sample_rate = int(start.get("sample_rate") or 16000)
        channels = int(start.get("channels") or 1)

        audio_q: asyncio.Queue = asyncio.Queue(maxsize=64)

        async def _audio_iter() -> AsyncIterator[bytes]:
            while True:
                chunk = await audio_q.get()
                if chunk is _STREAM_EOF:
                    return
                yield chunk

        transcribe_task = asyncio.create_task(
            stt_service.transcribe_stream(
                _audio_iter(),
                language=language,
                sample_rate=sample_rate,
                channels=channels,
            ),
        )

        try:
            await _drain_client_frames(ws, audio_q)
        except (ConnectionError, OSError, ValueError) as exc:
            # Transport-level failures (peer disconnect, malformed frame).
            # Bugs surface as unexpected exception types and should propagate.
            log.warning("stt: client loop error for %s: %s", ctx.user_id, exc)
            await audio_q.put(_STREAM_EOF)
            transcribe_task.cancel()
            await _send_error(ws, "Client stream aborted.")
            await ws.close()
            return ws

        try:
            text = await transcribe_task
        except SttUnsupportedError as exc:
            await _send_error(ws, str(exc))
            await ws.close()
            return ws
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.warning("stt: adapter error for %s: %s", ctx.user_id, exc)
            await _send_error(ws, "Transcription failed.")
            await ws.close()
            return ws

        await ws.send_str(json.dumps({"type": "final", "text": text}))
        await ws.close()
        return ws


class _BadStartError(ValueError):
    """Raised when the first frame is missing / malformed."""


async def _read_start_frame(ws: web.WebSocketResponse) -> dict:
    """Wait for and return the initial ``start`` JSON frame.

    Raises :class:`_BadStartError` if the first frame is not a valid
    ``start`` text frame.
    """
    msg = await ws.receive()
    if msg.type != WSMsgType.TEXT:
        raise _BadStartError("Expected initial 'start' text frame.")
    try:
        payload = json.loads(msg.data)
    except json.JSONDecodeError as exc:
        raise _BadStartError("Initial frame was not valid JSON.") from exc
    if not isinstance(payload, dict) or payload.get("type") != "start":
        raise _BadStartError("Initial frame must have type='start'.")
    return payload


async def _drain_client_frames(
    ws: web.WebSocketResponse,
    audio_q: asyncio.Queue,
) -> None:
    """Forward binary frames to ``audio_q`` until ``{type:"end"}`` or close."""
    async for msg in ws:
        if msg.type == WSMsgType.BINARY:
            await audio_q.put(msg.data)
        elif msg.type == WSMsgType.TEXT:
            try:
                payload = json.loads(msg.data)
            except json.JSONDecodeError:
                continue
            if isinstance(payload, dict) and payload.get("type") == "end":
                break
        elif msg.type == WSMsgType.ERROR:
            break
    await audio_q.put(_STREAM_EOF)


async def _send_error(ws: web.WebSocketResponse, detail: str) -> None:
    """Send a standard error frame. Best-effort; swallow send failures."""
    try:
        await ws.send_str(json.dumps({"type": "error", "detail": detail}))
    except Exception:
        pass
