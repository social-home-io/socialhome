"""Hourly retention prune for space content (§27181, §47092).

For every space THIS household hosts that has a non-NULL
``retention_days`` setting, content older than that horizon is soft-deleted unless the post's
``type`` appears in ``spaces.retention_exempt_json`` (e.g. an admin
might exempt ``"poll"`` so historic decisions stay readable).

Retention settings also federate to member households (so a co-admin on
another household sees and re-saves the real values), but only the host
enforces them: a mirrored space — ``owner_instance_id`` is another
household — is skipped, so a member's copy never deletes posts on its own.

Soft-delete sets ``space_posts.deleted = 1`` so the existing
moderation/feed-rendering paths keep working unchanged. Comments on a
purged post cascade via the row's foreign key.

The space's **chat** (v_55) is pruned by the same horizon, on EVERY
household that holds a chat for the space — not only the host: each
household keeps its own copy of the chat (no household re-streams old
messages beyond the catch-up window), so a member's copy would otherwise
outlive the space's retention forever. Expired chat messages are
soft-deleted exactly like ``DmService.delete_message`` does (content and
media cleared, the row kept for reply links); ``retention_exempt_json``
names post types and does not apply to chat.

Mirrors the start/stop pattern of :class:`PageLockExpiryScheduler` so
it plugs into the existing app-startup hook list.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta, timezone

import orjson

from ..db import AsyncDatabase
from ..repositories.conversation_repo import AbstractConversationRepo

log = logging.getLogger(__name__)


class SpaceRetentionScheduler:
    """Background loop that prunes expired space content per space."""

    __slots__ = (
        "_db",
        "_own_instance_id",
        "_interval",
        "_task",
        "_stop",
        "_convos",
        "_on_chat_pruned",
    )

    def __init__(
        self,
        db: AsyncDatabase,
        *,
        own_instance_id: str,
        interval_seconds: float = 3600.0,
        conversation_repo: AbstractConversationRepo | None = None,
        on_chat_pruned: Callable[[list[str]], Awaitable[None]] | None = None,
    ) -> None:
        self._db = db
        #: Where the space chats live; ``None`` prunes no chat.
        self._convos = conversation_repo
        #: Told the ids of pruned chat messages (drops them from search).
        self._on_chat_pruned = on_chat_pruned
        self._own_instance_id = own_instance_id
        self._interval = interval_seconds
        self._task: asyncio.Task | None = None
        self._stop = asyncio.Event()

    async def start(self) -> None:
        if self._task is not None and not self._task.done():
            return
        self._stop.clear()
        self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
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
                pruned = await self._prune_once()
                if pruned:
                    log.info(
                        "space-retention: soft-deleted %d posts / chat messages",
                        pruned,
                    )
            except Exception as exc:  # pragma: no cover
                log.warning("space-retention loop failed: %s", exc)
            try:
                await asyncio.wait_for(
                    self._stop.wait(),
                    timeout=self._interval,
                )
            except asyncio.TimeoutError:
                continue

    async def _prune_once(self) -> int:
        """Run one prune pass over every hosted space with retention configured.

        Returns the total number of soft-deleted posts. Exposed for tests.
        """
        spaces = await self._db.fetchall(
            "SELECT id, retention_days, retention_exempt_json "
            "FROM spaces WHERE retention_days IS NOT NULL AND owner_instance_id=?",
            (self._own_instance_id,),
        )
        total = 0
        for s in spaces:
            try:
                exempt = set(orjson.loads(s["retention_exempt_json"] or "[]"))
            except ValueError, TypeError:
                exempt = set()
            # ``space_posts.created_at`` is always written as tz-aware
            # ISO 8601 (``SpacePost.created_at`` → ``_iso_or_none``), so
            # every comparison below wraps both sides in ``datetime()``:
            # SQLite compares TEXT lexicographically and "T" (0x54) sorts
            # above " " (0x20), so a raw compare against this naive cutoff
            # judged same-day posts "not old enough" and never expired them.
            cutoff = (
                datetime.now(timezone.utc) - timedelta(days=int(s["retention_days"]))
            ).strftime("%Y-%m-%d %H:%M:%S")
            # Build the type filter as ``NOT IN``; sqlite needs the
            # placeholder list to match exempt cardinality.
            if exempt:
                placeholders = ",".join("?" for _ in exempt)
                row = await self._db.fetchone(
                    f"""
                    SELECT COUNT(*) AS n FROM space_posts
                     WHERE space_id=? AND deleted=0
                       AND datetime(created_at) < datetime(?)
                       AND type NOT IN ({placeholders})
                    """,
                    (s["id"], cutoff, *exempt),
                )
                await self._db.enqueue(
                    f"""
                    UPDATE space_posts
                       SET deleted=1
                     WHERE space_id=? AND deleted=0
                       AND datetime(created_at) < datetime(?)
                       AND type NOT IN ({placeholders})
                    """,
                    (s["id"], cutoff, *exempt),
                )
            else:
                row = await self._db.fetchone(
                    """
                    SELECT COUNT(*) AS n FROM space_posts
                     WHERE space_id=? AND deleted=0
                       AND datetime(created_at) < datetime(?)
                    """,
                    (s["id"], cutoff),
                )
                await self._db.enqueue(
                    """
                    UPDATE space_posts SET deleted=1
                     WHERE space_id=? AND deleted=0
                       AND datetime(created_at) < datetime(?)
                    """,
                    (s["id"], cutoff),
                )
            total += int(row["n"]) if row else 0
        total += await self._prune_chats()
        return total

    async def _prune_chats(self) -> int:
        """Soft-delete space-chat messages past their space's retention
        horizon, on every space this household holds a chat for, and tell
        ``on_chat_pruned`` (the search index) which ones went."""
        if self._convos is None:
            return 0
        pruned = await self._convos.prune_space_chat_messages(
            now=datetime.now(timezone.utc)
        )
        if pruned and self._on_chat_pruned is not None:
            await self._on_chat_pruned(pruned)
        return len(pruned)
