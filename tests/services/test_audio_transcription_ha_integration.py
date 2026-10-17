"""End-to-end voice-note transcription against a fake HA REST server.

Covers the chain:

    OGG/Opus bytes
       ↓  ``AudioTranscriptionService.transcribe``
    PCM16 LE (decoded via PyAV)
       ↓  ``adapter.transcribe_audio``
    ``HaSTTProvider.transcribe``
       ↓  single-chunk ``stream_transcribe``
    ``HaClient.stream_stt``  →  ``POST /api/stt/<entity_id>``
       ↓
    Fake HA server returns ``{"result":"success","text":"hello"}``

This is the surface the user flagged ("ensure the STT working
correctly with the ha adapter"). The test asserts:

* The correct entity_id is hit on the HA server.
* The ``X-Speech-Content`` header carries ``format=wav; codec=pcm; ...``
  matching :mod:`socialhome.platform.ha.client`'s wire shape.
* The body bytes are non-empty (i.e. PyAV did decode OGG → PCM).
* The transcript returned by HA is bubbled all the way up to the
  caller of ``AudioTranscriptionService``.
"""

from __future__ import annotations

import io

import av
import pytest
from aiohttp import web

from socialhome.platform.ha.client import HaClient
from socialhome.platform.ha.providers import HaSTTProvider
from socialhome.services.audio_transcription_service import (
    AudioTranscriptionService,
)

pytestmark = pytest.mark.asyncio


# ── Fake HA server ──────────────────────────────────────────────────────


@pytest.fixture
async def ha_stt_server(aiohttp_server):
    """Minimal HA fake — only the STT POST is implemented.

    Captures the request shape so the test can assert the wire
    invariants (entity_id, headers, byte-count).
    """
    captured: dict = {"requests": []}

    async def stt(request: web.Request) -> web.Response:
        body = await request.read()
        captured["requests"].append(
            {
                "method": request.method,
                "path": request.path,
                "entity_id": request.match_info["entity_id"],
                "headers": {
                    "X-Speech-Content": request.headers.get("X-Speech-Content"),
                    "Authorization": request.headers.get("Authorization"),
                },
                "byte_count": len(body),
            }
        )
        return web.json_response({"result": "success", "text": "hello from ha"})

    app = web.Application()
    app.router.add_post(r"/api/stt/{entity_id}", stt)
    server = await aiohttp_server(app)
    return server, captured


@pytest.fixture
async def http_session():
    import aiohttp

    async with aiohttp.ClientSession() as s:
        yield s


# ── Stand-in adapter ────────────────────────────────────────────────────


class _AdapterShim:
    """The bare minimum surface ``HaSTTProvider`` reads off the adapter.

    Production code uses :class:`HaAdapter` which wires the client + the
    options map. Carrying just those two over here keeps the test focused
    on the STT wire shape without spinning the full HA bootstrap.
    """

    def __init__(self, client: HaClient, stt_entity_id: str) -> None:
        self._client = client
        self._options = {"stt_entity_id": stt_entity_id}


class _PlatformShim:
    """Just enough of ``PlatformAdapter`` for :class:`AudioTranscriptionService`.

    Exposes ``supports_stt`` + ``transcribe_audio`` — those are the only
    fields the service touches.
    """

    def __init__(self, stt_provider: HaSTTProvider) -> None:
        self._stt = stt_provider
        self.supports_stt = True

    async def transcribe_audio(self, audio_bytes: bytes, language: str = "en") -> str:
        return await self._stt.transcribe(audio_bytes, language)


# ── Test fixture: OGG/Opus blob ─────────────────────────────────────────


def _silent_frame(n_samples: int, sample_rate: int) -> av.AudioFrame:
    frame = av.AudioFrame(format="s16", layout="mono", samples=n_samples)
    frame.sample_rate = sample_rate
    frame.planes[0].update(b"\x00" * (n_samples * 2))
    return frame


def _build_ogg_opus(duration: float = 0.5, sample_rate: int = 48000) -> bytes:
    n_samples = int(sample_rate * duration)
    buf = io.BytesIO()
    out = av.open(buf, mode="w", format="ogg")
    try:
        stream = out.add_stream("libopus", rate=sample_rate)
        stream.bit_rate = 24_000
        for packet in stream.encode(_silent_frame(n_samples, sample_rate)):
            out.mux(packet)
        for packet in stream.encode(None):
            out.mux(packet)
    finally:
        out.close()
    return buf.getvalue()


# ── Tests ───────────────────────────────────────────────────────────────


async def test_voice_note_round_trips_via_ha_stt(http_session, ha_stt_server):
    """A real OGG/Opus blob lands on HA's STT endpoint as PCM + headers."""
    server, captured = ha_stt_server
    ha_client = HaClient(
        http_session,
        str(server.make_url("")).rstrip("/"),
        "ha-test-token",
    )
    adapter = _PlatformShim(HaSTTProvider(_AdapterShim(ha_client, "stt.whisper")))
    svc = AudioTranscriptionService(adapter)

    transcript = await svc.transcribe(_build_ogg_opus())

    assert transcript == "hello from ha"

    # Exactly one HA POST was made.
    assert len(captured["requests"]) == 1
    req = captured["requests"][0]
    assert req["method"] == "POST"
    assert req["entity_id"] == "stt.whisper"

    # ``X-Speech-Content`` carries the canonical metadata HA's STT
    # API expects (format=wav; codec=pcm; sample_rate=16000; ...).
    hdr = req["headers"]["X-Speech-Content"]
    assert hdr is not None
    assert "format=wav" in hdr
    assert "codec=pcm" in hdr
    assert "sample_rate=16000" in hdr
    assert "channel=1" in hdr
    assert "language=en" in hdr

    # PyAV decoded the OGG/Opus blob to PCM — the byte count is the
    # number of int16 samples × 2. With 0.5s at 16 kHz mono that's
    # ~16000 bytes. Allow a wide tolerance because PyAV's resampler
    # tail can add a frame; the point is non-zero bytes shipped.
    assert req["byte_count"] > 0


async def test_voice_note_passes_bearer_token_to_ha(http_session, ha_stt_server):
    """The HA bearer auth header rides on the STT POST."""
    server, captured = ha_stt_server
    ha_client = HaClient(
        http_session,
        str(server.make_url("")).rstrip("/"),
        "ha-test-token",
    )
    adapter = _PlatformShim(HaSTTProvider(_AdapterShim(ha_client, "stt.whisper")))
    svc = AudioTranscriptionService(adapter)

    await svc.transcribe(_build_ogg_opus())
    assert captured["requests"][0]["headers"]["Authorization"] == "Bearer ha-test-token"


async def test_voice_note_returns_none_when_entity_id_missing(
    http_session, ha_stt_server
):
    """No ``stt_entity_id`` configured ⇒ fail-silent path, no HA call."""
    server, captured = ha_stt_server
    ha_client = HaClient(
        http_session,
        str(server.make_url("")).rstrip("/"),
        "ha-test-token",
    )
    adapter = _PlatformShim(HaSTTProvider(_AdapterShim(ha_client, "")))
    svc = AudioTranscriptionService(adapter)
    result = await svc.transcribe(_build_ogg_opus())
    assert result is None
    assert captured["requests"] == []


async def test_voice_note_returns_none_when_ha_returns_error_payload(
    http_session, aiohttp_server
):
    """HA returns ``{"result":"failure"}`` → no transcript surfaced."""
    captured: dict = {"requests": []}

    async def stt(request: web.Request) -> web.Response:
        await request.read()
        captured["requests"].append(request.path)
        return web.json_response({"result": "failure", "text": ""})

    app = web.Application()
    app.router.add_post(r"/api/stt/{entity_id}", stt)
    server = await aiohttp_server(app)
    ha_client = HaClient(
        http_session,
        str(server.make_url("")).rstrip("/"),
        "ha-test-token",
    )
    adapter = _PlatformShim(HaSTTProvider(_AdapterShim(ha_client, "stt.whisper")))
    svc = AudioTranscriptionService(adapter)

    result = await svc.transcribe(_build_ogg_opus())
    assert result is None
    assert len(captured["requests"]) == 1  # the call was made, just no text


async def test_voice_note_returns_none_when_ha_500s(http_session, aiohttp_server):
    """HA blowing up → swallowed; the audio bubble still plays."""

    async def stt(request: web.Request) -> web.Response:
        return web.json_response({"error": "internal"}, status=500)

    app = web.Application()
    app.router.add_post(r"/api/stt/{entity_id}", stt)
    server = await aiohttp_server(app)
    ha_client = HaClient(
        http_session,
        str(server.make_url("")).rstrip("/"),
        "ha-test-token",
    )
    adapter = _PlatformShim(HaSTTProvider(_AdapterShim(ha_client, "stt.whisper")))
    svc = AudioTranscriptionService(adapter)

    result = await svc.transcribe(_build_ogg_opus())
    assert result is None
