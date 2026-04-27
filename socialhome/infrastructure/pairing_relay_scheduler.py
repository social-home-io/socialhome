"""Periodic retention sweep for ``pairing_relay`` (§11.9).

Without retention the durable :class:`PairingRelayQueue` table grows
forever — every relay request leaves a row behind even after it has
been approved or declined. This scheduler runs once per
``interval_seconds`` and prunes:

* ``approved`` / ``declined`` rows older than ``resolved_window``
  (default 7 days) — they exist purely as audit trail, the request
  itself has long-since been actioned.
* ``pending`` rows older than ``pending_window`` (default 30 days) —
  an admin who hasn't acted in a month isn't going to; the requester
  can re-submit.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone

from ..repositories.pairing_relay_repo import AbstractPairingRelayRepo

log = logging.getLogger(__name__)


class PairingRelayRetentionScheduler:
    """Background task that prunes stale pairing_relay rows."""

    __slots__ = (
        "_repo",
        "_interval",
        "_pending_window",
        "_resolved_window",
        "_task",
        "_stop",
    )

    def __init__(
        self,
        repo: AbstractPairingRelayRepo,
        *,
        interval_seconds: float = 3600.0,  # hourly
        pending_window: timedelta = timedelta(days=30),
        resolved_window: timedelta = timedelta(days=7),
    ) -> None:
        self._repo = repo
        self._interval = interval_seconds
        self._pending_window = pending_window
        self._resolved_window = resolved_window
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
                pruned = await self._prune_once()
                if pruned:
                    log.debug("pairing-relay: pruned %d stale rows", pruned)
            except Exception as exc:  # pragma: no cover
                log.warning("pairing-relay prune failed: %s", exc)
            try:
                await asyncio.wait_for(
                    self._stop.wait(),
                    timeout=self._interval,
                )
            except asyncio.TimeoutError:
                continue

    async def _prune_once(self) -> int:
        """Run one prune pass. Exposed for tests."""
        now = datetime.now(timezone.utc)
        pending_cutoff = (now - self._pending_window).isoformat()
        resolved_cutoff = (now - self._resolved_window).isoformat()
        purged = 0
        purged += await self._repo.delete_older_than(
            status="pending", cutoff_iso=pending_cutoff
        )
        purged += await self._repo.delete_older_than(
            status="approved", cutoff_iso=resolved_cutoff
        )
        purged += await self._repo.delete_older_than(
            status="declined", cutoff_iso=resolved_cutoff
        )
        return purged
