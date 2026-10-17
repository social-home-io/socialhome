"""Voice-note transcription — runs ``adapter.stt`` over an OGG/Opus blob.

Voice notes ship from the SPA as OGG/Opus. The platform STT providers
all want raw PCM16 at the rate they were configured for (HA Whisper:
16 kHz mono). This service is the boundary that decodes the container,
resamples, and hands the result to ``adapter.transcribe_audio``.

Both legs of the voice-note flow use it:

* **Sender-side.** Right after the audio bubble is persisted (empty
  ``content``), :class:`DmService` calls
  :meth:`AudioTranscriptionService.transcribe` on the just-uploaded
  blob and, on a non-``None`` result, patches the row via
  ``update_message_content`` + emits ``DmMessageUpdated``.
* **Receiver-side.** When a remote audio message lands without a
  transcript, :class:`AudioTranscriptScheduler` polls for it and
  calls the same method to fill the gap locally.

Fail-silent: any failure (no STT capability, decode error, adapter
raise, empty transcription) returns ``None`` so the audio bubble
still plays for the user.
"""

from __future__ import annotations

import asyncio
import io
import logging
from typing import TYPE_CHECKING, cast

import av

if TYPE_CHECKING:
    from ..platform.adapter import PlatformAdapter

log = logging.getLogger(__name__)

#: HA Whisper's default. The STTProvider protocol documents these
#: as the defaults too; keeping the decode target aligned with the
#: streaming-STT path means a single STT entity transcribes both
#: live dictation and voice notes without reconfiguration.
_STT_TARGET_SAMPLE_RATE: int = 16000
_STT_TARGET_CHANNELS: int = 1


class AudioTranscriptionService:
    """Decode OGG/Opus → PCM16 → ``adapter.transcribe_audio``."""

    __slots__ = ("_adapter",)

    def __init__(self, adapter: PlatformAdapter) -> None:
        self._adapter = adapter

    async def transcribe(
        self,
        audio_bytes: bytes,
        *,
        language: str = "en",
    ) -> str | None:
        """Return the transcript for *audio_bytes*, or ``None``.

        Returns ``None`` rather than raising on any of:

        - The adapter has no :class:`STTProvider` configured.
        - The container can't be decoded as OGG/Opus.
        - The STT provider raises.
        - The STT provider returns an empty / whitespace-only string.

        The empty-string case means "the audio was processed but no
        intelligible speech was found" — for example, the user held
        the mic but didn't say anything. We treat that as "no
        transcript", same as a hard failure, so the SPA renders the
        audio without a transcript line.
        """
        if not self._adapter.supports_stt:
            return None
        if not audio_bytes:
            return None

        try:
            pcm = await asyncio.get_running_loop().run_in_executor(
                None, _decode_to_pcm16, audio_bytes
            )
        except Exception as exc:  # decode-side failures
            log.warning("audio transcribe: decode failed: %s", exc, exc_info=True)
            return None

        if not pcm:
            return None

        try:
            text = await self._adapter.transcribe_audio(pcm, language)
        except NotImplementedError:
            # Capability flag said yes but the provider refused. Treat
            # as a misconfiguration the user can't usefully act on.
            return None
        except Exception as exc:
            log.warning("audio transcribe: provider failed: %s", exc, exc_info=True)
            return None

        text = (text or "").strip()
        return text or None


def _decode_to_pcm16(audio_bytes: bytes) -> bytes:
    """Synchronous OGG/Opus → 16 kHz mono PCM16 LE.

    Runs inside an executor — never call directly from async code.
    Uses PyAV's resampler so the output rate / layout are exact
    regardless of what the container declared.
    """
    # ``av.open`` returns ``InputContainer | OutputContainer`` for the
    # union of read/write modes. We always read, so a runtime
    # ``cast`` keeps mypy happy without paying any runtime cost.
    container = cast(
        "av.container.InputContainer",
        av.open(io.BytesIO(audio_bytes), format=None),
    )
    try:
        stream = next((s for s in container.streams if s.type == "audio"), None)
        if stream is None:
            return b""

        resampler = av.AudioResampler(
            format="s16",
            layout="mono" if _STT_TARGET_CHANNELS == 1 else "stereo",
            rate=_STT_TARGET_SAMPLE_RATE,
        )

        out = io.BytesIO()
        for frame in container.decode(stream):
            # Only audio frames reach this path — the stream we
            # selected is type=audio — so the cast is a no-op at
            # runtime that satisfies the ``resample(AudioFrame|None)``
            # signature mypy enforces.
            audio_frame = cast("av.AudioFrame", frame)
            for resampled in resampler.resample(audio_frame):
                # s16/mono is single-planar — ``planes[0]`` is the PCM
                # buffer, LE on every platform we ship to. ``bytes(...)``
                # copies the live memoryview into a stable buffer for
                # the byte queue we ship to the adapter.
                _append_pcm(out, resampled)
        # Flush the resampler's internal tail.
        for resampled in resampler.resample(None):
            _append_pcm(out, resampled)
        return out.getvalue()
    finally:
        container.close()


def _append_pcm(out: io.BytesIO, frame: av.AudioFrame) -> None:
    """Append the PCM samples from *frame*'s plane[0] to *out*.

    Numpy-free path — uses PyAV's buffer protocol. The plane is sized
    to the format-specific stride; we trim to ``samples * 2`` bytes
    (s16 mono → 2 bytes/sample, single channel) so we don't append
    the trailing padding the AV allocator leaves on small frames.
    """
    plane = frame.planes[0]
    needed = frame.samples * 2
    raw = bytes(plane)[:needed]
    out.write(raw)
