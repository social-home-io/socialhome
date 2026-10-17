"""The household dashboard's own listener — start, stop, follow settings.

The dashboard is a separate :class:`aiohttp.web.Application` on its own
port (a wall tablet talks to it, not to the main app). This class owns
that app's :class:`~aiohttp.web.AppRunner` and moves it between "off"
and "listening on port N" as the admin changes the settings.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from typing import Protocol

from aiohttp import web

from ..access_log import RedactingAccessLogger
from ..domain.household_dashboard import DashboardPortInUseError, DashboardSettings

log = logging.getLogger(__name__)

__all__ = ["DashboardServer", "PortAdvertiser"]


class PortAdvertiser(Protocol):
    """Announces the listening port on the network
    (:class:`~socialhome.dashboard.advertiser.DisplayAdvertiser`).

    Both methods are fail-soft: they never raise, so a broken multicast
    setup can't take the dashboard down with it. ``advertise`` returns at
    once (the announcement runs in the background, so the listener lock
    never waits for a name probe); ``withdraw`` returns once announcing
    has stopped, after anything still in flight.
    """

    async def advertise(self, port: int) -> None: ...

    async def withdraw(self) -> None: ...


class DashboardServer:
    """Runs the dashboard app on one port at a time.

    ``build_app`` is called inside the async start path, so it must do no
    blocking work (no filesystem reads) — pass it a pre-built
    :class:`~socialhome.dashboard.render.DashboardRenderer`.

    ``advertiser`` (when the platform can announce on the LAN) follows the
    listener: announced on every successful bind, withdrawn on stop.
    """

    __slots__ = ("_advertiser", "_build_app", "_host", "_lock", "_port", "_runner")

    def __init__(
        self,
        build_app: Callable[[], web.Application],
        host: str,
        *,
        advertiser: PortAdvertiser | None = None,
    ) -> None:
        self._build_app = build_app
        self._host = host
        self._advertiser = advertiser
        self._runner: web.AppRunner | None = None
        self._port: int | None = None
        # Serialises start / stop / apply so two settings changes can't
        # interleave a bind with a cleanup.
        self._lock = asyncio.Lock()

    @property
    def running_port(self) -> int | None:
        """The port the dashboard listens on, or ``None`` when stopped."""
        return self._port

    async def start(self, port: int) -> None:
        """Listen on ``port``, moving off the current port if running.

        Raises :class:`DashboardPortInUseError` when ``port`` can't be
        bound; a dashboard that was already running keeps serving on
        its old port.
        """
        async with self._lock:
            await self._listen_unlocked(port)

    async def stop(self) -> None:
        """Stop listening. Safe to call when already stopped."""
        async with self._lock:
            await self._stop_unlocked()

    async def apply(self, settings: DashboardSettings) -> None:
        """Bring the listener in line with ``settings``.

        A port change binds the new port first, so a failed change (port
        taken) leaves the dashboard serving on the old one.
        """
        async with self._lock:
            if not settings.enabled:
                await self._stop_unlocked()
            elif self._port != settings.port:
                await self._listen_unlocked(settings.port)

    async def _listen_unlocked(self, port: int) -> None:
        if self._port == port:
            # Same port: it can't be bound twice, so release it first.
            await self._stop_unlocked()
        runner = await self._bind(port)
        old, self._runner, self._port = self._runner, runner, port
        if old is not None:
            await old.cleanup()
        log.info("household dashboard listening on %s:%d", self._host, port)
        if self._advertiser is not None:
            await self._advertiser.advertise(port)

    async def _bind(self, port: int) -> web.AppRunner:
        """A set-up runner listening on ``port``; nothing changes on failure."""
        runner = web.AppRunner(
            self._build_app(),
            access_log_class=RedactingAccessLogger,
        )
        await runner.setup()
        try:
            await web.TCPSite(runner, self._host, port).start()
        except OSError as exc:
            await runner.cleanup()
            log.warning(
                "household dashboard could not listen on %s:%d: %s",
                self._host,
                port,
                exc,
            )
            raise DashboardPortInUseError(port) from exc
        return runner

    async def _stop_unlocked(self) -> None:
        runner, self._runner, self._port = self._runner, None, None
        if runner is not None:
            if self._advertiser is not None:
                await self._advertiser.withdraw()
            await runner.cleanup()
            log.info("household dashboard stopped")
