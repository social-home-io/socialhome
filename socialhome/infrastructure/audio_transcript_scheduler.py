"""Receiver-side STT fallback for voice notes.

When a remote household sends an audio DM, the sender's STT runs and
ships the transcript in ``content``. If that STT was unavailable (no
``adapter.stt`` configured, the provider raised, ...) the audio still
arrives but with empty ``content``. The recipient's bubble renders
the audio without a transcript line, which is fine for a hearing
viewer but leaves a deaf / muted / busy viewer with no text to read.

This scheduler closes that gap. Every ``interval_seconds`` it sweeps
``conversation_messages`` for audio rows older than 1 hour but with
no transcript yet, decodes them locally, runs the local
``AudioTranscriptionService``, and (on success) patches the row +
emits :class:`DmMessageUpdated` so the SPA's ``dm.message_updated``
WS frame swaps the placeholder for the transcript text.

Bounded by a fixed lookback window so a permanently-untranscribable
blob doesn't soak the loop forever — the receiver gives up after 1
hour, and the audio bubble keeps playing without a transcript
indefinitely.

Pattern mirrors :class:`ReplayCachePruneScheduler` —
``_stop: asyncio.Event`` per CLAUDE.md.
"""

from __future__ import annotations

import asyncio
import logging
import pathlib
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING

from ..domain.events import DmMessageUpdated
from ..infrastructure.event_bus import EventBus
from ..repositories.conversation_repo import AbstractConversationRepo
from ..repositories.user_repo import AbstractUserRepo
from ..services.dm_audience import local_audience
from ..services.protection_gate import ProtectionGateMixin

if TYPE_CHECKING:
    from ..services.audio_transcription_service import AudioTranscriptionService

log = logging.getLogger(__name__)


class AudioTranscriptScheduler(ProtectionGateMixin):
    """Periodic receiver-side STT for un-transcribed audio DMs.

    Guardian blocks (§CP.F2, via :class:`ProtectionGateMixin`): a patched
    transcript is never pushed to a member a block separates from the
    voice note's sender.
    """

    __slots__ = (
        "_child_protection",
        "_convos",
        "_users",
        "_transcribe",
        "_bus",
        "_media_dir",
        "_interval",
        "_window",
        "_task",
        "_stop",
    )

    def __init__(
        self,
        conversation_repo: AbstractConversationRepo,
        user_repo: AbstractUserRepo,
        transcribe: AudioTranscriptionService,
        bus: EventBus,
        media_dir: pathlib.Path,
        *,
        interval_seconds: float = 30.0,
        window: timedelta = timedelta(hours=1),
    ) -> None:
        self._convos = conversation_repo
        self._users = user_repo
        self._child_protection = None
        self._transcribe = transcribe
        self._bus = bus
        self._media_dir = media_dir
        self._interval = interval_seconds
        self._window = window
        self._task: asyncio.Task | None = None
        self._stop = asyncio.Event()

    async def start(self) -> None:
        """Start the background loop. Idempotent."""
        if self._task is not None and not self._task.done():
            return
        self._stop.clear()
        self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        """Stop the loop and wait for the task to exit."""
        self._stop.set()
        if self._task is not None:
            try:
                await asyncio.wait_for(self._task, timeout=5.0)
            except asyncio.TimeoutError, asyncio.CancelledError:
                self._task.cancel()
            self._task = None

    async def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                await self._sweep_once()
            except Exception as exc:  # pragma: no cover — defensive
                log.warning("audio-transcript sweep failed: %s", exc)
            try:
                await asyncio.wait_for(
                    self._stop.wait(),
                    timeout=self._interval,
                )
            except asyncio.TimeoutError:
                continue

    async def _sweep_once(self) -> int:
        """Process one batch. Returns the number of rows patched.

        Exposed for tests + manual / scheduled runs. Builds the
        ``since_iso`` cutoff inside the call so each pass picks up
        the window's current trailing edge.
        """
        cutoff_dt = datetime.now(timezone.utc) - self._window
        since_iso = cutoff_dt.strftime("%Y-%m-%d %H:%M:%S")
        rows = await self._convos.find_pending_audio_transcripts(
            since_iso=since_iso,
            limit=50,
        )
        if not rows:
            return 0

        patched = 0
        for msg in rows:
            if self._stop.is_set():
                break
            if not msg.media_url:
                continue
            try:
                filename = msg.media_url.rsplit("/", 1)[-1]
                blob_path = self._media_dir / filename
                audio_bytes = await asyncio.get_running_loop().run_in_executor(
                    None, blob_path.read_bytes
                )
            except FileNotFoundError:
                # Blob hasn't landed yet — the cross-household sync is
                # still in flight. Try again on the next sweep.
                continue
            except Exception as exc:
                log.warning(
                    "audio-transcript: cannot read %s: %s",
                    msg.media_url,
                    exc,
                )
                continue

            transcript = await self._transcribe.transcribe(audio_bytes)
            if transcript is None:
                continue

            try:
                await self._convos.edit_message(msg.id, transcript)
            except Exception as exc:  # pragma: no cover
                log.warning("audio-transcript: persist failed: %s", exc)
                continue

            recipient_ids = await self._collect_recipient_ids(
                conversation_id=msg.conversation_id,
                sender_user_id=msg.sender_user_id,
            )
            await self._bus.publish(
                DmMessageUpdated(
                    conversation_id=msg.conversation_id,
                    message_id=msg.id,
                    sender_user_id=msg.sender_user_id,
                    recipient_user_ids=recipient_ids,
                    content=transcript,
                    edited_at=datetime.now(timezone.utc),
                )
            )
            patched += 1
        return patched

    async def _collect_recipient_ids(
        self,
        *,
        conversation_id: str,
        sender_user_id: str,
    ) -> tuple[str, ...]:
        """Build the WS fan-out list for ``DmMessageUpdated``.

        The receiver-side fallback only patches local rows, so all
        the recipients we care about are local members. We don't
        re-fan over federation — every household runs its own
        fallback independently for messages it received.
        """
        return await local_audience(
            self._convos,
            self._users,
            conversation_id,
            actor_user_id=sender_user_id,
            # §CP.F2: a blocked sender's transcript never reaches the
            # protected member (they never see the voice note either).
            withheld=await self._guardian_block_counterparts(sender_user_id),
        )
