"""Speech-to-text orchestrator (§platform/stt).

Thin wrapper around the platform adapter's streaming STT primitive. The
service exists mostly so the route handler stays thin and to surface a
stable domain exception (:class:`SttUnsupportedError`) when the
configured adapter cannot transcribe — callers translate that to an
``{"type":"error"}`` WebSocket frame / HTTP 501.

No buffering, no retries, no persistence. The adapter is expected to
stream bytes directly to the upstream STT engine (HA's
``POST /api/stt/{entity_id}`` for the HA adapter) so microphone bytes
reach Whisper with only network-level queuing between them.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, AsyncIterable

if TYPE_CHECKING:
    from ..platform.adapter import AbstractPlatformAdapter

log = logging.getLogger(__name__)


class SttUnsupportedError(RuntimeError):
    """Raised when the active platform adapter does not support STT.

    The route layer converts this to a 501 HTTP response or a
    ``{"type":"error","detail":"..."}`` WebSocket frame depending on
    the transport used by the caller.
    """


class SttService:
    """Forwards streamed audio to the adapter and returns the transcript."""

    __slots__ = ("_adapter",)

    def __init__(self, adapter: "AbstractPlatformAdapter") -> None:
        self._adapter = adapter

    @property
    def supported(self) -> bool:
        """Mirror of ``adapter.supports_stt`` for the UI capability probe."""
        return bool(getattr(self._adapter, "supports_stt", False))

    async def transcribe_stream(
        self,
        audio_stream: AsyncIterable[bytes],
        *,
        language: str = "en",
        sample_rate: int = 16000,
        channels: int = 1,
    ) -> str:
        """Transcribe a live PCM16 stream.

        Raises :class:`SttUnsupportedError` when the adapter has no STT
        backing. Otherwise returns whatever the adapter returns — empty
        string on engine-side errors, the transcript on success.
        """
        if not self.supported:
            raise SttUnsupportedError(
                "Speech-to-text is not configured for the active platform "
                "adapter. For HA mode, set [homeassistant].stt_entity_id in "
                "socialhome.toml."
            )
        return await self._adapter.stream_transcribe_audio(
            audio_stream,
            language=language,
            sample_rate=sample_rate,
            channels=channels,
        )
