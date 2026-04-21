"""Hourly retention prune for space content (§27181, §47092).

For every space that has a non-NULL ``retention_days`` setting,
content older than that horizon is soft-deleted unless the post's
``type`` appears in ``spaces.retention_exempt_json`` (e.g. an admin
might exempt ``"poll"`` so historic decisions stay readable).

Soft-delete sets ``space_posts.deleted = 1`` so the existing
moderation/feed-rendering paths keep working unchanged. Comments on a
purged post cascade via the row's foreign key.

Mirrors the start/stop pattern of :class:`PageLockExpiryScheduler` so
it plugs into the existing app-startup hook list.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timedelta, timezone

from ..db import AsyncDatabase

log = logging.getLogger(__name__)


class SpaceRetentionScheduler:
    """Background loop that prunes expired space content per space."""

    __slots__ = ("_db", "_interval", "_task", "_stop")

    def __init__(
        self,
        db: AsyncDatabase,
        *,
        interval_seconds: float = 3600.0,
    ) -> None:
        self._db = db
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
                        "space-retention: soft-deleted %d posts",
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
        """Run one prune pass over every space with retention configured.

        Returns the total number of soft-deleted posts. Exposed for tests.
        """
        spaces = await self._db.fetchall(
            "SELECT id, retention_days, retention_exempt_json "
            "FROM spaces WHERE retention_days IS NOT NULL",
        )
        total = 0
        for s in spaces:
            try:
                exempt = set(json.loads(s["retention_exempt_json"] or "[]"))
            except ValueError, TypeError:
                exempt = set()
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
                       AND created_at < ?
                       AND type NOT IN ({placeholders})
                    """,
                    (s["id"], cutoff, *exempt),
                )
                await self._db.enqueue(
                    f"""
                    UPDATE space_posts
                       SET deleted=1
                     WHERE space_id=? AND deleted=0
                       AND created_at < ?
                       AND type NOT IN ({placeholders})
                    """,
                    (s["id"], cutoff, *exempt),
                )
            else:
                row = await self._db.fetchone(
                    """
                    SELECT COUNT(*) AS n FROM space_posts
                     WHERE space_id=? AND deleted=0
                       AND created_at < ?
                    """,
                    (s["id"], cutoff),
                )
                await self._db.enqueue(
                    """
                    UPDATE space_posts SET deleted=1
                     WHERE space_id=? AND deleted=0
                       AND created_at < ?
                    """,
                    (s["id"], cutoff),
                )
            total += int(row["n"]) if row else 0
        return total
