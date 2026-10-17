"""Periodic GC for fully-left DM conversations (§23.47c) + media orphans.

Two responsibilities, both hourly:

1. **Conversation GC.** When every local member of a 1:1 / group DM
   has soft-left and no remote members are attached, the
   conversation is empty. ``hard_delete`` cascades through child
   tables (messages, reactions, delivery state, gap rows).
   Federated conversations are skipped by
   ``list_fully_left_conversation_ids`` — their lifecycle is owned
   by the federation peer.
2. **DM media orphan sweep.** v_3 introduces cross-household
   ``DM_MEDIA_BLOB`` chunks that the receiver writes to
   ``<msg_id>.preview.webp`` or ``<msg_id>.part<idx>`` under the
   local media root. If the matching ``conversation_messages`` row
   gets soft-deleted (sender retracted the DM, or the user left
   the convo and the conv GC ran above) or never landed at all
   (sender abandoned mid-flight), those files would otherwise
   live forever. This sweep enumerates the part / preview files
   and drops anything without a backing message row.
"""

from __future__ import annotations

import asyncio
import logging
import pathlib
import re

from ..repositories.conversation_repo import AbstractConversationRepo

log = logging.getLogger(__name__)


#: Match ``<msg_id>.preview.webp`` and ``<msg_id>.part<NNNNN>``
#: filenames the federation inbound handler writes under
#: ``media_dir``. Captured group 1 is the message id; group 2 the
#: file kind (preview vs part) for logging.
_DM_MEDIA_ORPHAN_RE = re.compile(
    r"^(?P<msg>[0-9a-f]{16,})\.(?P<kind>preview\.webp|part\d{4,})$",
    re.IGNORECASE,
)


class DmGcScheduler:
    """Background task that hard-deletes fully-left DM conversations."""

    __slots__ = ("_interval", "_media_dir", "_repo", "_stop", "_task")

    def __init__(
        self,
        repo: AbstractConversationRepo,
        *,
        media_dir: pathlib.Path | None = None,
        interval_seconds: float = 3600.0,  # hourly
    ) -> None:
        self._repo = repo
        # ``None`` disables the media-orphan sweep (test stacks that
        # only exercise the conversation GC); the conversation-side
        # sweep still runs.
        self._media_dir = media_dir
        self._interval = interval_seconds
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
            except TimeoutError, asyncio.CancelledError:
                self._task.cancel()
            self._task = None

    async def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                pruned = await self._sweep_once()
                if pruned:
                    log.debug("dm-gc: hard-deleted %d empty conversations", pruned)
            except Exception as exc:  # pragma: no cover
                log.warning("dm-gc sweep failed: %s", exc, exc_info=True)
            try:
                orphans = await self._sweep_media_orphans()
                if orphans:
                    log.info(
                        "dm-gc: removed %d orphaned media file(s)",
                        orphans,
                    )
            except Exception as exc:  # pragma: no cover
                log.warning("dm-gc media-orphan sweep failed: %s", exc, exc_info=True)
            try:
                await asyncio.wait_for(
                    self._stop.wait(),
                    timeout=self._interval,
                )
            except TimeoutError:
                continue

    async def _sweep_once(self) -> int:
        """Run one conversation-GC pass. Exposed for tests."""
        ids = await self._repo.list_fully_left_conversation_ids()
        for conversation_id in ids:
            await self._repo.hard_delete(conversation_id)
        return len(ids)

    async def _sweep_media_orphans(self) -> int:
        """Drop ``<msg_id>.preview.webp`` and ``.part<idx>`` files
        whose backing ``conversation_messages`` row no longer exists.

        Reasons a file can orphan:

        * The DM was retracted (``soft_delete_message`` wiped the
          ``media_*`` columns + cascade through cleanup) but the
          preview/part files survived the federation race.
        * The conversation was GC'd above and the row vanished.
        * Sender died mid-chunked-send so the receiver has parts
          0..k for a message that will never land here.

        Exposed for tests. Returns the count of files removed.
        """
        if self._media_dir is None or not self._media_dir.is_dir():
            return 0
        # Group filenames by message_id so a single ``get_message``
        # check covers all the parts for one DM at once.
        per_msg: dict[str, list[pathlib.Path]] = {}
        for path in self._media_dir.iterdir():
            if not path.is_file():
                continue
            m = _DM_MEDIA_ORPHAN_RE.match(path.name)
            if m is None:
                continue
            per_msg.setdefault(m.group("msg"), []).append(path)
        removed = 0
        for message_id, files in per_msg.items():
            try:
                msg = await self._repo.get_message(message_id)
            except Exception:  # pragma: no cover
                # Repo errors shouldn't kill the sweep; skip this
                # message and try the rest.
                log.debug(
                    "dm gc: lookup of message %s failed", message_id, exc_info=True
                )
                continue
            if msg is not None and not msg.deleted:
                # Row exists and is live — the part/preview files
                # are still in legitimate use, even if the full
                # blob hasn't landed yet.
                continue
            for path in files:
                try:
                    path.unlink(missing_ok=True)
                except OSError as exc:  # pragma: no cover
                    log.debug(
                        "dm-gc media-orphan: failed to unlink %s: %s",
                        path,
                        exc,
                    )
                    continue
                removed += 1
        return removed
