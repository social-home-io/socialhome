"""Daily prune of stale post_drafts (§30631).

Drafts older than ``ttl_days`` (default 30) with no recent activity get
DELETEd. Drafts are local-only and the user can always start over, so a
stricter cutoff is fine.

Mirrors the start/stop pattern of
:class:`PageLockExpiryScheduler` so it slots into the existing app
startup/cleanup loops.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone

from ..db import AsyncDatabase

log = logging.getLogger(__name__)


class PostDraftCleanupScheduler:
    """Background loop that prunes stale rows from ``post_drafts``."""

    __slots__ = ("_db", "_interval", "_ttl_days", "_task", "_stop")

    def __init__(
        self,
        db: AsyncDatabase,
        *,
        interval_seconds: float = 24 * 3600.0,
        ttl_days: int = 30,
    ) -> None:
        self._db = db
        self._interval = interval_seconds
        self._ttl_days = ttl_days
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
                    log.info("post-drafts: pruned %d stale drafts", pruned)
            except Exception as exc:  # pragma: no cover
                log.warning("post-draft loop failed: %s", exc)
            try:
                await asyncio.wait_for(
                    self._stop.wait(),
                    timeout=self._interval,
                )
            except asyncio.TimeoutError:
                continue

    async def _prune_once(self) -> int:
        """Run one prune pass. Exposed for tests."""
        cutoff = (datetime.now(timezone.utc) - timedelta(days=self._ttl_days)).strftime(
            "%Y-%m-%d %H:%M:%S"
        )
        row = await self._db.fetchone(
            "SELECT COUNT(*) AS n FROM post_drafts WHERE updated_at < ?",
            (cutoff,),
        )
        n = int(row["n"]) if row else 0
        if n:
            await self._db.enqueue(
                "DELETE FROM post_drafts WHERE updated_at < ?",
                (cutoff,),
            )
        return n
