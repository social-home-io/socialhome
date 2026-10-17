"""Periodic pruning for ``dm_relay_seen`` (§12.5.3).

The DM-relay dedup ring keeps inbound ``DM_RELAY`` envelopes from
bouncing through the same instance twice. The on-disk
``dm_relay_seen`` table is the dedup source-of-truth that survives
restarts. Without pruning it grows by every relayed envelope forever,
the same way ``federation_replay_cache`` does — and for the same
reason it needs a periodic GC.

Mirrors :class:`~socialhome.infrastructure.replay_cache_scheduler.\
ReplayCachePruneScheduler` so the two have identical lifecycle semantics.
"""

from __future__ import annotations

import asyncio
import logging

from ..services.dm_routing_service import DmRoutingService

log = logging.getLogger(__name__)


class DmRelaySeenPruneScheduler:
    """Background task that prunes stale ``dm_relay_seen`` rows."""

    __slots__ = ("_interval", "_service", "_stop", "_task")

    def __init__(
        self,
        service: DmRoutingService,
        *,
        interval_seconds: float = 600.0,  # every 10 min
    ) -> None:
        self._service = service
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
                    log.debug("dm-relay-seen: pruned %d stale rows", pruned)
            except Exception as exc:  # pragma: no cover
                log.warning("dm-relay-seen prune failed: %s", exc, exc_info=True)
            try:
                await asyncio.wait_for(
                    self._stop.wait(),
                    timeout=self._interval,
                )
            except TimeoutError:
                continue

    async def _prune_once(self) -> int:
        """Run one prune pass. Exposed for tests.

        Delegates to :meth:`DmRoutingService.prune_seen`, which uses
        the service's ``DEDUP_TTL_SECONDS`` (default 1 h) for the cutoff.
        """
        return await self._service.prune_seen()
