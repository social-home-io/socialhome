"""Periodic shared-GFS route discovery + route expiry (v_53).

Drives :class:`~socialhome.services.gfs_route_discovery_service
.GfsRouteDiscoveryService`:

* every :data:`~socialhome.services.gfs_route_discovery_service
  .ROUTE_DISCOVERY_INTERVAL_S` (24 h) ± a jitter of up to 1 h, it expires
  stale routes and re-probes every eligible peer. The jitter keeps a
  restarted fleet of households from probing in lock-step, which would
  hand a GFS a synchronised burst to correlate;
* :meth:`GfsRouteDiscoveryScheduler.trigger` asks for an early round — our
  own GFS connection just came up. Triggers inside one
  :data:`TRIGGER_COALESCE_S` window collapse into a single round, so a
  reconnect storm (every connection reconnecting after a network blip)
  costs one probe round, not one per socket. The per-peer throttle in the
  service bounds anything that slips through.

Follows the ``asyncio.Event`` lifecycle of
:mod:`socialhome.infrastructure.replay_cache_scheduler`.
"""

from __future__ import annotations

import asyncio
import logging
import random
from collections.abc import Callable

from ..services.gfs_route_discovery_service import (
    ROUTE_DISCOVERY_INTERVAL_S,
    ROUTE_DISCOVERY_JITTER_S,
    GfsRouteDiscoveryService,
)

log = logging.getLogger(__name__)

#: Triggers that land within this window of the first one share its round.
TRIGGER_COALESCE_S: float = 30.0


class GfsRouteDiscoveryScheduler:
    """Background loop: periodic + triggered probe rounds, route expiry."""

    __slots__ = (
        "_service",
        "_interval",
        "_jitter",
        "_coalesce",
        "_uniform",
        "_task",
        "_stop",
        "_wake",
    )

    def __init__(
        self,
        service: GfsRouteDiscoveryService,
        *,
        interval_seconds: float = ROUTE_DISCOVERY_INTERVAL_S,
        jitter_seconds: float = ROUTE_DISCOVERY_JITTER_S,
        coalesce_seconds: float = TRIGGER_COALESCE_S,
        uniform: Callable[[float, float], float] = random.uniform,
    ) -> None:
        self._service = service
        self._interval = interval_seconds
        self._jitter = jitter_seconds
        self._coalesce = coalesce_seconds
        self._uniform = uniform
        self._task: asyncio.Task | None = None
        self._stop = asyncio.Event()
        #: Set by :meth:`trigger` (and by :meth:`stop`, to cut a wait short).
        self._wake = asyncio.Event()

    def next_delay(self) -> float:
        """Seconds until the next periodic round: interval ± jitter."""
        return max(0.0, self._interval + self._uniform(-self._jitter, self._jitter))

    def trigger(self) -> None:
        """Ask for an early round (coalesced; never blocks, never raises)."""
        self._wake.set()

    async def start(self) -> None:
        """Start the background loop. Idempotent."""
        if self._task is not None and not self._task.done():
            return
        self._stop.clear()
        self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        """Stop the loop and wait for the task to exit."""
        self._stop.set()
        self._wake.set()
        if self._task is not None:
            try:
                await asyncio.wait_for(self._task, timeout=5.0)
            except asyncio.TimeoutError, asyncio.CancelledError:
                self._task.cancel()
            self._task = None

    async def _loop(self) -> None:
        while not self._stop.is_set():
            triggered = True
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=self.next_delay())
            except asyncio.TimeoutError:
                triggered = False
            if self._stop.is_set():
                break
            if triggered:
                # Let the rest of a reconnect burst land, then run once.
                try:
                    await asyncio.wait_for(self._stop.wait(), timeout=self._coalesce)
                except asyncio.TimeoutError:
                    pass
                if self._stop.is_set():
                    break
            self._wake.clear()
            await self.run_once()

    async def run_once(self) -> None:
        """One round: expire stale routes, then probe. Exposed for tests."""
        try:
            await self._service.expire_stale_routes()
        except Exception as exc:  # noqa: BLE001 — the loop must survive
            log.warning("gfs routes: expiry failed: %s", exc)
        try:
            sent = await self._service.probe_all()
            if sent:
                log.debug("gfs routes: sent %d probe(s)", sent)
        except Exception as exc:  # noqa: BLE001 — the loop must survive
            log.warning("gfs routes: probe round failed: %s", exc)
