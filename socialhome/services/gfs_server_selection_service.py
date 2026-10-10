"""The owner's GFS list for a space it hosts (``spaces.gfs_servers``).

A global space is listed on GFSs its owner chose. Members must reach every one
of them — including GFSs their own household never connected to — so the
owner hands them the list inside the encrypted, authority-signed
``space_meta`` (``SPACE_CONFIG_CHANGED``). This service keeps the owner's copy
(migration 0089, :mod:`socialhome.domain.gfs_server`) in step with the truth:
the space's ACTIVE ``gfs_space_publications`` rows on ACTIVE connections.

:meth:`GfsServerSelectionService.sync_snapshot` re-derives the list and, only
when it changed, stores it, raises ``config_sequence`` and publishes
:class:`SpaceConfigChanged` — :class:`SpaceConfigOutbound` then broadcasts the
new snapshot to every member household. It runs after every successful
publish / unpublish and after a disconnect cascaded publications away
(:meth:`GfsConnectionService.attach_on_listings_changed`).

Only spaces this household OWNS, and only the listed tiers (public / global):
a private space's list is the owner's channel choice, handled elsewhere.
"""

from __future__ import annotations

import logging

from ..domain.events import SpaceConfigChanged
from ..domain.federation import GfsConnection
from ..domain.gfs_server import (
    GFS_KEY_SUITE_ED25519,
    MAX_GFS_SERVERS,
    GfsServer,
)
from ..domain.space import PUBLIC_SPACE_TIERS, SpaceConfigEventType
from ..infrastructure.event_bus import EventBus
from ..repositories.gfs_connection_repo import AbstractGfsConnectionRepo
from ..repositories.space_repo import AbstractSpaceRepo

log = logging.getLogger(__name__)


class GfsServerSelectionService:
    """Derives and distributes the owner's ``spaces.gfs_servers``."""

    __slots__ = ("_spaces", "_gfs", "_bus", "_own_instance_id")

    def __init__(
        self,
        *,
        space_repo: AbstractSpaceRepo,
        gfs_repo: AbstractGfsConnectionRepo,
        bus: EventBus,
        own_instance_id: str,
    ) -> None:
        self._spaces = space_repo
        self._gfs = gfs_repo
        self._bus = bus
        self._own_instance_id = own_instance_id

    async def sync_snapshot(self, space_id: str) -> bool:
        """Re-derive the owner's GFS list for *space_id*; ``True`` when it
        changed (stored, sequence raised, members told)."""
        space = await self._spaces.get(space_id)
        if (
            space is None
            or not self._own_instance_id
            or space.owner_instance_id != self._own_instance_id
            or space.space_type not in PUBLIC_SPACE_TIERS
        ):
            return False
        servers = self._servers_from(
            space_id, await self._gfs.list_active_listings_for_space(space_id)
        )
        if space.gfs_servers == servers:
            return False
        await self._spaces.set_gfs_servers(space_id, servers)
        sequence = await self._spaces.increment_config_sequence(space_id)
        await self._bus.publish(
            SpaceConfigChanged(
                space_id=space_id,
                event_type=SpaceConfigEventType.GFS_SERVERS_CHANGED.value,
                payload={"gfs_server_count": len(servers)},
                sequence=sequence,
            )
        )
        log.info(
            "space %s: GFS list now has %d server(s) — telling members",
            space_id,
            len(servers),
        )
        return True

    @staticmethod
    def _servers_from(
        space_id: str, conns: list[GfsConnection]
    ) -> tuple[GfsServer, ...]:
        """One :class:`GfsServer` per connection, validated through the same
        codec a member applies. A connection that does not pass (a public
        plain-http URL, a malformed key) is left out and logged — a member
        would drop it anyway."""
        out: list[GfsServer] = []
        for conn in conns:
            if len(out) >= MAX_GFS_SERVERS:
                log.warning(
                    "space %s: listed on more than %d GFSs — members get the first %d",
                    space_id,
                    MAX_GFS_SERVERS,
                    MAX_GFS_SERVERS,
                )
                break
            try:
                server = GfsServer.from_wire(
                    {
                        "gfs_instance_id": conn.gfs_instance_id,
                        "inbox_url": conn.inbox_url,
                        "public_key": conn.public_key,
                        "gfs_key_suite": GFS_KEY_SUITE_ED25519,
                        "display_name": conn.display_name,
                    }
                )
            except ValueError as exc:
                log.warning(
                    "space %s: GFS connection %s left out of the members' GFS "
                    "list — %s",
                    space_id,
                    conn.id,
                    exc,
                )
                continue
            out.append(server)
        return tuple(out)
