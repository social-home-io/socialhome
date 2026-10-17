"""Periodic pruning for ``federation_replay_cache`` (§24.11).

The replay cache prevents an attacker from re-playing a captured
federation envelope, and also dedupes a re-signed outbox redelivery
whose 2xx ack was lost (such a retry carries a fresh §24.11 timestamp,
so the timestamp gate no longer drops it — the replay cache is the only
thing left to stop a double-apply). The in-memory
:class:`~socialhome.crypto.ReplayCache` keeps a sliding window sized by
:data:`~socialhome.crypto.REPLAY_CACHE_WINDOW` (24 h, comfortably past
the outbox's ~5.2 h max jittered redelivery interval); the on-disk
``federation_replay_cache`` table is the source of truth that survives
restarts. Without pruning, the table grows by every signed inbound
event forever — eventually crowding out the rest of the database.

This scheduler runs once per ``interval_seconds`` and deletes rows
older than ``window`` (production passes
:data:`~socialhome.crypto.REPLAY_CACHE_WINDOW`, matching the in-memory
cache).
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime, timedelta

from ..repositories.federation_repo import AbstractFederationRepo

log = logging.getLogger(__name__)


class ReplayCachePruneScheduler:
    """Background task that prunes stale replay-cache rows."""

    __slots__ = ("_interval", "_repo", "_stop", "_task", "_window")

    def __init__(
        self,
        repo: AbstractFederationRepo,
        *,
        interval_seconds: float = 600.0,  # every 10 min
        # The 1 h default is for ad-hoc / test construction only; production
        # passes ``REPLAY_CACHE_WINDOW`` (24 h) so the on-disk prune horizon
        # matches the in-memory cache and outlasts the outbox's re-signed
        # redelivery cadence (see module docstring).
        window: timedelta = timedelta(hours=1),
    ) -> None:
        self._repo = repo
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
            except TimeoutError, asyncio.CancelledError:
                self._task.cancel()
            self._task = None

    async def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                pruned = await self._prune_once()
                if pruned:
                    log.debug("replay-cache: pruned %d stale rows", pruned)
            except Exception as exc:  # pragma: no cover
                log.warning("replay-cache prune failed: %s", exc, exc_info=True)
            try:
                await asyncio.wait_for(
                    self._stop.wait(),
                    timeout=self._interval,
                )
            except TimeoutError:
                continue

    async def _prune_once(self) -> int:
        """Run one prune pass. Exposed for tests.

        ``federation_replay_cache.received_at`` defaults to
        SQLite's ``datetime('now')`` format (``YYYY-MM-DD HH:MM:SS``,
        no timezone, space separator). We format the cutoff the same
        way so the string comparison behaves correctly.
        """
        cutoff_dt = datetime.now(UTC) - self._window
        cutoff = cutoff_dt.strftime("%Y-%m-%d %H:%M:%S")
        return await self._repo.prune_replay_cache(cutoff)
