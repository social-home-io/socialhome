"""Daily background scheduler that checks for Social Home App updates.

Runs :meth:`AppService.list_updates` once per ``interval`` seconds (default
24 hours, matching :data:`AppCatalogService.CATALOG_TTL_S`). The call uses
``force=True`` so the catalog cache is refreshed on every check tick.

Mirrors the ``_stop: asyncio.Event`` lifecycle of
:class:`~socialhome.infrastructure.replay_cache_scheduler.ReplayCachePruneScheduler`:
- ``start()`` creates a background task and clears ``_stop``.
- ``stop()`` sets ``_stop``, waking the sleeping interval wait.
- ``start()`` is idempotent — a second call while the task is running is a
  no-op.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

from .app_catalog_service import CATALOG_TTL_S

if TYPE_CHECKING:
    from .app_service import AppService

log = logging.getLogger(__name__)


class AppUpdateScheduler:
    """Background task that checks for available app updates once per interval.

    Parameters
    ----------
    app_service:
        The :class:`AppService` instance to call :meth:`~AppService.list_updates`
        on.
    interval:
        How many seconds to sleep between checks.  Defaults to
        :data:`~socialhome.services.app_catalog_service.CATALOG_TTL_S`
        (24 hours).
    """

    __slots__ = ("_app_service", "_interval", "_stop", "_task")

    def __init__(
        self,
        app_service: AppService,
        *,
        interval: float = CATALOG_TTL_S,
    ) -> None:
        self._app_service = app_service
        self._interval = interval
        self._task: asyncio.Task | None = None
        self._stop = asyncio.Event()

    async def start(self) -> None:
        """Start the background loop.  Idempotent — safe to call more than once."""
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
                updates = await self._app_service.list_updates(force=True)
                if updates:
                    log.info(
                        "app-update check: %d update(s) available: %s",
                        len(updates),
                        [u["app_id"] for u in updates],
                    )
                else:
                    log.debug("app-update check: all apps are up to date")
            except Exception as exc:  # pragma: no cover
                log.warning("app-update check failed: %s", exc, exc_info=True)
            try:
                await asyncio.wait_for(
                    self._stop.wait(),
                    timeout=self._interval,
                )
            except TimeoutError:
                continue
