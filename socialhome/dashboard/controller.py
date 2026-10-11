"""Keeps the dashboard listener in step with its settings.

:class:`DashboardController` is what the admin API and app startup talk
to: a settings change that moves the listener (``enabled`` / ``port``) is
saved, applied and — when the listener can't follow — rolled back as one
critical section, so concurrent admin edits can't interleave a save with
another edit's rollback.
"""

from __future__ import annotations

import asyncio
import logging
import socket
from collections.abc import Mapping
from typing import Protocol
from urllib.parse import urlsplit

from ..domain.household_dashboard import (
    DashboardNotRunningError,
    DashboardPortInUseError,
    DashboardSettings,
)
from ..services.dashboard_settings_service import (
    DashboardSettingsService,
    require_admin,
)
from .app import host_allowed, normalise_host

log = logging.getLogger(__name__)

__all__ = [
    "LISTENER_FIELDS",
    "DashboardController",
    "DashboardListener",
    "address_hosts",
    "local_hostnames",
]

#: Settings the listener follows; edits to anything else never touch it.
LISTENER_FIELDS = frozenset({"enabled", "port"})


class DashboardListener(Protocol):
    """What the controller needs of :class:`~.server.DashboardServer`."""

    @property
    def running_port(self) -> int | None: ...

    async def apply(self, settings: DashboardSettings) -> None: ...

    async def stop(self) -> None: ...


def local_hostnames() -> frozenset[str]:
    """This machine's own names for the dashboard ``Host`` allow-list:
    the hostname, lower-cased, plus its first label when it is dotted."""
    name = normalise_host(socket.gethostname())
    if not name:
        return frozenset()
    return frozenset({name, name.split(".", 1)[0]})


def address_hosts(settings: DashboardSettings) -> frozenset[str]:
    """The host name of the admin-set dashboard address, if any."""
    if not settings.address:
        return frozenset()
    host = urlsplit(settings.address).hostname
    return frozenset({host}) if host else frozenset()


class DashboardController:
    """Settings + listener, changed together."""

    __slots__ = ("_settings", "_server", "_local_hostnames", "_lock")

    def __init__(
        self,
        settings: DashboardSettingsService,
        server: DashboardListener,
        *,
        local_hostnames: frozenset[str],
    ) -> None:
        self._settings = settings
        self._server = server
        self._local_hostnames = frozenset(normalise_host(h) for h in local_hostnames)
        self._lock = asyncio.Lock()

    @property
    def settings(self) -> DashboardSettingsService:
        return self._settings

    @property
    def running_port(self) -> int | None:
        return self._server.running_port

    async def update(
        self, *, actor_is_admin: bool, patch: Mapping[str, object]
    ) -> DashboardSettings:
        """Save ``patch``; when it touches the listener, move it to match.

        If the listener can't follow (any error — a taken port is 409
        ``DASHBOARD_PORT_IN_USE``) the whole patch is rolled back to the
        stored settings from before it, and the error is re-raised; the
        listener keeps its previous state. Edits that don't touch
        ``enabled`` / ``port`` are saved without touching the listener,
        so they work even while its port is taken.
        """
        async with self._lock:
            previous = await self._settings.snapshot()
            updated = await self._settings.update(
                actor_is_admin=actor_is_admin, patch=patch
            )
            if LISTENER_FIELDS.isdisjoint(patch):
                return updated
            try:
                await self._server.apply(updated)
            except Exception:
                await self._settings.restore(actor_is_admin=True, previous=previous)
                raise
            return updated

    async def start(self) -> None:
        """Start an enabled dashboard (app startup). A taken port is
        logged, never raised: the main app must start regardless."""
        async with self._lock:
            settings = await self._settings.get()
            if not settings.enabled:
                return
            try:
                await self._server.apply(settings)
            except DashboardPortInUseError:
                log.warning(
                    "household dashboard is enabled but port %d is taken — not started",
                    settings.port,
                )

    async def stop(self) -> None:
        await self._server.stop()

    async def issue_setup_token(
        self, *, actor_is_admin: bool, now: float | None = None
    ) -> str:
        """A one-time tablet sign-in token — only while the dashboard
        listens (the link would lead nowhere otherwise)."""
        require_admin(actor_is_admin)
        # Under the controller lock so a concurrent PUT can't switch the
        # listener off between this check and the token being issued.
        async with self._lock:
            if self._server.running_port is None:
                raise DashboardNotRunningError()
            return await self._settings.issue_setup_token(
                actor_is_admin=actor_is_admin, now=now
            )

    async def url_host_allowed(self, url: str) -> bool:
        """Would the dashboard answer a request for ``url``'s host?

        The dashboard's own ``Host`` allow-list (:func:`host_allowed`) with
        this machine's names and the admin-set address — so the admin UI
        can say "set the address" when the guessed URL would be refused.
        """
        extra = self._local_hostnames | frozenset(
            normalise_host(h) for h in address_hosts(await self._settings.get())
        )
        return host_allowed(urlsplit(url).netloc, extra)
