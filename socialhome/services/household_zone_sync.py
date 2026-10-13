"""Household-zone sync — mirror the platform's zones into the household.

On a platform with a :class:`~socialhome.platform.adapter.ZoneSource`
(Home Assistant: the ``zone.*`` entities) the household's zones follow
the platform's, read-only in Social Home. The option lives in
``instance_config`` under :data:`SYNC_ENABLED_KEY` and defaults to on.

* **On** — once at startup, then every :data:`SYNC_INTERVAL_SECONDS`:
  :meth:`HouseholdZoneService.mirror_external` with the source's zones.
  A source that couldn't be read
  (:class:`~socialhome.domain.household_zone.ZoneSourceUnavailableError`)
  skips the run — the synced zones stay, never wiped by a flaky
  connection — and the status carries the error.
* **Off** — ``mirror_external([])`` once (the synced zones go, the
  household's own zones stay), then the loop idles until it's turned on.

Nothing is published here: ``mirror_external`` emits
``HouseholdZonesChanged`` itself, only when something changed. Runs are
serialised by a lock, so the periodic tick and an admin's toggle never
mirror concurrently. The loop follows the ``_stop: asyncio.Event``
lifecycle (template: ``infrastructure/replay_cache_scheduler.py``).
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from ..domain.household_zone import HouseholdZoneSyncStatus, ZoneSourceUnavailableError
from ..repositories.instance_config_repo import AbstractInstanceConfigRepo
from .household_zone_service import HouseholdZoneService

if TYPE_CHECKING:
    from ..platform.adapter import ZoneSource

__all__ = ["SYNC_ENABLED_KEY", "SYNC_INTERVAL_SECONDS", "HouseholdZoneSync"]

log = logging.getLogger(__name__)

#: ``instance_config`` key of the household's sync option (``"1"`` / ``"0"``;
#: missing = on).
SYNC_ENABLED_KEY = "household_zones.sync_enabled"

#: How often the sync re-reads the platform's zones.
SYNC_INTERVAL_SECONDS = 300.0


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class HouseholdZoneSync:
    """Periodic platform → household zone mirror with an on/off option."""

    __slots__ = (
        "_zones",
        "_config",
        "_source",
        "_interval",
        "_task",
        "_stop",
        "_lock",
        "_cleared",
        "_last_synced_at",
        "_last_error",
    )

    def __init__(
        self,
        zone_service: HouseholdZoneService,
        instance_config: AbstractInstanceConfigRepo,
        source: "ZoneSource | None",
        *,
        interval_seconds: float = SYNC_INTERVAL_SECONDS,
    ) -> None:
        self._zones = zone_service
        self._config = instance_config
        self._source = source
        self._interval = interval_seconds
        self._task: asyncio.Task | None = None
        self._stop = asyncio.Event()
        self._lock = asyncio.Lock()
        # The off-path already removed the synced zones since it was
        # turned off — later ticks idle.
        self._cleared = False
        self._last_synced_at: str | None = None
        self._last_error: str | None = None

    @property
    def available(self) -> bool:
        """``True`` when the platform has zones to sync."""
        return self._source is not None

    # ── Option ─────────────────────────────────────────────────────────

    async def is_enabled(self) -> bool:
        """The household's option; on unless explicitly turned off."""
        raw = await self._config.get(SYNC_ENABLED_KEY)
        return raw is None or raw.strip() not in ("0", "false")

    async def set_enabled(self, enabled: object) -> HouseholdZoneSyncStatus:
        """Store the option and sync right away (on: pull the platform's
        zones; off: remove the synced ones). Returns the new status."""
        if not isinstance(enabled, bool):
            raise ValueError("enabled must be true or false")
        if not self.available:
            raise ValueError("This household has no platform zones to sync.")
        await self._config.set(SYNC_ENABLED_KEY, "1" if enabled else "0")
        self._cleared = False
        await self.sync_now()
        return await self.status()

    async def status(self) -> HouseholdZoneSyncStatus:
        enabled = await self.is_enabled()
        return HouseholdZoneSyncStatus(
            available=self.available,
            enabled=enabled,
            last_synced_at=self._last_synced_at if enabled else None,
            last_error=self._last_error if enabled else None,
        )

    # ── One run ────────────────────────────────────────────────────────

    async def sync_now(self) -> None:
        """One sync run (serialised). Never raises — a failure is logged
        at WARNING and kept in the status."""
        if self._source is None:
            return
        async with self._lock:
            try:
                if await self.is_enabled():
                    await self._pull(self._source)
                elif not self._cleared:
                    result = await self._zones.mirror_external([])
                    self._cleared = True
                    # Turned back on later, "last synced" means that run.
                    self._last_synced_at = None
                    self._last_error = None
                    if result.removed:
                        log.info(
                            "household zones: sync off, %d synced zones removed",
                            result.removed,
                        )
            except Exception as exc:
                self._last_error = str(exc) or type(exc).__name__
                log.warning("household zones: sync failed: %s", exc, exc_info=True)

    async def _pull(self, source: "ZoneSource") -> None:
        try:
            zones = await source.list_zones()
        except ZoneSourceUnavailableError as exc:
            # Not "zero zones" — keep the synced rows and try next tick.
            self._last_error = str(exc) or "zone source unavailable"
            log.warning("household zones: platform zones unavailable: %s", exc)
            return
        result = await self._zones.mirror_external(zones)
        self._last_synced_at = _now_iso()
        self._last_error = None
        if result.changed:
            log.info(
                "household zones: synced %d zones (%d removed, %d skipped)",
                result.upserted,
                result.removed,
                result.skipped,
            )

    # ── Lifecycle ──────────────────────────────────────────────────────

    async def start(self) -> None:
        """Start the loop (first run right away). Idempotent; a no-op
        without a zone source."""
        if self._source is None:
            return
        if self._task is not None and not self._task.done():
            return
        self._stop.clear()
        self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        """Stop the loop and wait for the in-flight run to finish."""
        self._stop.set()
        if self._task is not None:
            try:
                await asyncio.wait_for(self._task, timeout=5.0)
            except asyncio.TimeoutError, asyncio.CancelledError:
                self._task.cancel()
            self._task = None

    async def _loop(self) -> None:
        while not self._stop.is_set():
            await self.sync_now()
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self._interval)
            except asyncio.TimeoutError:
                continue
