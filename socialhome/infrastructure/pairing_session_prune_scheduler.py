"""Periodic pruning for ``pending_pairings`` past their TTL (§11).

A QR-issued pairing session has :data:`PAIRING_TTL_SECONDS` (300s) to
complete. Past that window the row is just clutter — and the matching
``remote_instances`` row in PENDING_SENT / PENDING_RECEIVED status is
worse, because the SPA's "pending handshake" UI keeps surfacing it to
the admin as something they should act on.

This scheduler runs once per ``interval_seconds`` (60s by default,
twice the polling granularity of any UI that lists pending pairs) and
delegates to
:meth:`socialhome.repositories.federation_repo.AbstractFederationRepo.cleanup_expired_pairings`,
which deletes both the expired session row and any orphan PENDING
``remote_instances`` row in a single transaction.

Mirrors the canonical scheduler shape used by
:class:`socialhome.infrastructure.replay_cache_scheduler.ReplayCachePruneScheduler`
and :class:`socialhome.infrastructure.pairing_relay_scheduler.PairingRelayRetentionScheduler`.
"""

from __future__ import annotations

import asyncio
import logging

from ..repositories.federation_repo import AbstractFederationRepo

log = logging.getLogger(__name__)


class PairingSessionPruneScheduler:
    """Background task that prunes stale ``pending_pairings`` rows."""

    __slots__ = ("_interval", "_repo", "_stop", "_task")

    def __init__(
        self,
        repo: AbstractFederationRepo,
        *,
        interval_seconds: float = 60.0,
    ) -> None:
        self._repo = repo
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
                pruned = await self._prune_once()
                if pruned:
                    log.info("pairing-session: pruned %d expired rows", pruned)
            except Exception as exc:  # pragma: no cover
                log.warning("pairing-session prune failed: %s", exc, exc_info=True)
            try:
                await asyncio.wait_for(
                    self._stop.wait(),
                    timeout=self._interval,
                )
            except TimeoutError:
                continue

    async def _prune_once(self) -> int:
        """Run one prune pass. Exposed for tests."""
        return await self._repo.cleanup_expired_pairings()
