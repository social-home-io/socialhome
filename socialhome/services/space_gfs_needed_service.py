"""A bell for the household admins while their household stands in for a
space's host but isn't on every GFS of that space.

A household holding a delegated-admin seat (it has the space's signing
seed, :meth:`SpaceService.stands_in_for_host`) acts for the host while the
host is offline — and that needs every GFS on the owner's signed list
(``spaces.gfs_servers``), or members reached only through a GFS it isn't on
lose it. The space's feed shows a "needed" notice for each missing GFS
(``SpaceGfsNotice``); this service makes sure the household's admins hear
about it without opening the space.

Local only: no federation message, no table. The row itself
(``space_gfs_needed``, Needs you, one per space and admin) remembers which
GFSs it told of (``params.gfs``), so the same GFS is told of once until it
is connected or leaves the list — across restarts too — and again only if
it goes missing later (:meth:`NotificationService.reconcile_space_gfs_needed`).

When it checks a space:

* this household gets the seat (:class:`SpaceAdminSeedStored`);
* a config change from the owner side was applied
  (:class:`SpaceFeaturesApplied`) — a newer owner-signed GFS list, or
  delegation turned off (which drops the seat);
* a GFS connection was added (:meth:`on_connection_added`, every space);
* at startup (:meth:`check_all`, every space).

Nothing missing, or no seat any more → the bell is resolved.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING

from ..domain.events import SpaceAdminSeedStored, SpaceFeaturesApplied
from ..domain.space import Space
from ..infrastructure.event_bus import EventBus
from ..repositories.space_repo import AbstractSpaceRepo

if TYPE_CHECKING:
    from .notification_service import NotificationService

log = logging.getLogger(__name__)

#: The bell's type (files under Needs you).
NOTIF_TYPE: str = "space_gfs_needed"

StandsIn = Callable[[Space], Awaitable[bool]]
Describe = Callable[[Space], Awaitable[tuple[list[dict] | None, bool]]]


class SpaceGfsNeededService:
    """Keeps the ``space_gfs_needed`` bell in line with the truth."""

    __slots__ = ("_bus", "_describe", "_notifications", "_spaces", "_stands_in")

    def __init__(
        self,
        *,
        space_repo: AbstractSpaceRepo,
        notifications: NotificationService,
        bus: EventBus,
        stands_in: StandsIn,
        describe: Describe,
    ) -> None:
        self._spaces = space_repo
        self._notifications = notifications
        self._bus = bus
        #: :meth:`SpaceService.stands_in_for_host`.
        self._stands_in = stands_in
        #: :meth:`GfsServerSelectionService.describe` — the owner's list
        #: with ``connected`` per GFS, as the space page shows it.
        self._describe = describe

    def wire(self) -> None:
        self._bus.subscribe(SpaceAdminSeedStored, self._on_space_event)
        self._bus.subscribe(SpaceFeaturesApplied, self._on_space_event)

    async def _on_space_event(
        self, event: SpaceAdminSeedStored | SpaceFeaturesApplied
    ) -> None:
        await self.check(event.space_id)

    async def on_connection_added(self, _conn: object) -> None:
        """A GFS connection was added: the spaces it was missing for are
        fine now (:meth:`GfsConnectionService.attach_on_connected`)."""
        await self.check_all()

    async def check_all(self) -> None:
        """Check every space (startup, a new connection). Fail-soft per
        space."""
        for space in await self._spaces.list_all():
            try:
                await self._check_space(space)
            except Exception:
                log.exception("GFS-needed bell: check failed for %s", space.id)

    async def check(self, space_id: str) -> None:
        space = await self._spaces.get(space_id)
        if space is None:
            return
        await self._check_space(space)

    async def _check_space(self, space: Space) -> None:
        await self._notifications.reconcile_space_gfs_needed(
            space, await self._missing(space)
        )

    async def _missing(self, space: Space) -> list[tuple[str, str]]:
        """``(address, name)`` of each listed GFS we aren't connected to —
        none unless this household stands in for the host."""
        if space.dissolved or not await self._stands_in(space):
            return []
        servers, _ = await self._describe(space)
        return [
            (str(s["inbox_url"]), str(s.get("display_name") or ""))
            for s in servers or ()
            if not s.get("connected") and s.get("inbox_url")
        ]
