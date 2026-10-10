"""The owner's signed GFS list for a space it hosts (``spaces.gfs_servers``).

A global space is listed on GFSs its owner chose. Members must reach every one
of them — including GFSs their own household never connected to — so the
owner hands them the list inside the encrypted ``space_meta``
(``SPACE_CONFIG_CHANGED``), signed with the owner household's identity key
(:mod:`socialhome.gfs_server_list_sig`). This service keeps the owner's copy
(migration 0091, :mod:`socialhome.domain.gfs_server`) in step with the truth:
the space's ACTIVE ``gfs_space_publications`` rows on ACTIVE connections.

:meth:`GfsServerSelectionService.sync_snapshot` re-derives the list and, only
when it changed, signs it with a fresh ``issued_at``, stores it, raises
``config_sequence`` and publishes :class:`SpaceConfigChanged` —
:class:`SpaceConfigOutbound` then broadcasts the new snapshot to every member
household. It runs:

* after every successful publish / unpublish, after a disconnect removed a
  connection's publications and after a listing GFS was renamed
  (:meth:`GfsConnectionService.attach_on_listings_changed`);
* after every local config change of a space we own (:meth:`wire`) — so a
  space that leaves the public tiers signs an EMPTY list at once, even when
  a GFS could not be reached to withdraw the listing.

Only spaces this household OWNS. A private / household space never keeps a
list (a private space's channel servers are a later change). One lock per
space keeps two concurrent snapshots from signing out of order.

:meth:`GfsServerSelectionService.member_servers` is the read side: the
servers of a space THIS household may contact, with private-host entries
dropped unless we hold the same connection ourselves. Whoever sends to them
(federation v_56) must still go through the SSRF-guarded
:class:`socialhome.outbound_fetch.OutboundFetcher` path, since a DNS name is
not resolved here.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

from ..domain.events import SpaceConfigChanged
from ..domain.federation import GfsConnection
from ..domain.gfs_server import (
    GFS_KEY_SUITE_ED25519,
    MAX_GFS_SERVERS,
    GfsServer,
    canonical_json,
    gfs_servers_from_wire,
    gfs_servers_to_wire,
)
from ..domain.space import PUBLIC_SPACE_TIERS, SpaceConfigEventType
from ..gfs_server_list_sig import sign_gfs_server_list
from ..infrastructure.event_bus import EventBus
from ..repositories.gfs_connection_repo import AbstractGfsConnectionRepo
from ..repositories.space_repo import AbstractSpaceRepo

log = logging.getLogger(__name__)


class GfsServerSelectionService:
    """Derives, signs and distributes the owner's ``spaces.gfs_servers``."""

    __slots__ = (
        "_spaces",
        "_gfs",
        "_bus",
        "_own_instance_id",
        "_own_signing_key",
        "_locks",
    )

    def __init__(
        self,
        *,
        space_repo: AbstractSpaceRepo,
        gfs_repo: AbstractGfsConnectionRepo,
        bus: EventBus,
    ) -> None:
        self._spaces = space_repo
        self._gfs = gfs_repo
        self._bus = bus
        # The household identity, attached once it is loaded at startup
        # (:meth:`attach_identity`). Until then nothing is derived.
        self._own_instance_id = ""
        self._own_signing_key = b""
        self._locks: dict[str, asyncio.Lock] = {}

    def attach_identity(self, *, own_instance_id: str, own_signing_key: bytes) -> None:
        """The household's instance id and Ed25519 identity seed — the key
        the list is signed with."""
        self._own_instance_id = own_instance_id
        self._own_signing_key = own_signing_key

    def wire(self) -> None:
        """Re-check the list after every config change (a tier change above
        all). Its own ``gfs_servers_changed`` is skipped."""
        self._bus.subscribe(SpaceConfigChanged, self._on_config_changed)

    async def _on_config_changed(self, event: SpaceConfigChanged) -> None:
        if event.event_type == SpaceConfigEventType.GFS_SERVERS_CHANGED.value:
            return
        try:
            await self.sync_snapshot(event.space_id)
        except Exception:
            log.exception("GFS list snapshot failed for space %s", event.space_id)

    async def sync_snapshot(self, space_id: str) -> bool:
        """Re-derive the owner's GFS list for *space_id*; ``True`` when it
        changed (signed, stored, sequence raised, members told)."""
        lock = self._locks.setdefault(space_id, asyncio.Lock())
        async with lock:
            return await self._sync_locked(space_id)

    async def _sync_locked(self, space_id: str) -> bool:
        if not self._own_instance_id or not self._own_signing_key:
            return False
        space = await self._spaces.get(space_id)
        if (
            space is None
            or space.dissolved
            or space.owner_instance_id != self._own_instance_id
        ):
            return False
        current = space.gfs_servers
        if space.space_type in PUBLIC_SPACE_TIERS:
            servers = await self._derive(space_id)
        elif current is None or current.servers_json == "[]":
            # Never listed, or already signed empty: nothing to say.
            return False
        else:
            # Left the public tiers: members must stop using every server
            # now, even if a withdrawal could not reach one of them.
            servers = ()
        servers_json = canonical_json(gfs_servers_to_wire(servers))
        if current is not None and current.servers_json == servers_json:
            return False
        now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
        issued_at = max(now_ms, current.issued_at + 1 if current else 0)
        signed = sign_gfs_server_list(
            space_id=space_id,
            servers=servers,
            issued_at=issued_at,
            owner_seed=self._own_signing_key,
        )
        await self._spaces.set_gfs_servers(space_id, signed)
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

    async def _derive(self, space_id: str) -> tuple[GfsServer, ...]:
        """One entry per active listing, run through the same codec (and
        dedupe / cap) a member applies. A connection that does not pass (a
        public plain-http URL, a malformed key) is left out and logged — a
        member would drop it anyway. Our own private-network connections
        stay in: a member drops them unless it holds the same connection."""
        conns = await self._gfs.list_active_listings_for_space(space_id)
        raw = [self._entry(conn) for conn in conns]
        if len(raw) > MAX_GFS_SERVERS:
            log.warning(
                "space %s: listed on %d GFSs — members get the first %d",
                space_id,
                len(raw),
                MAX_GFS_SERVERS,
            )
        servers, dropped = gfs_servers_from_wire(
            raw, own_routes={(c.gfs_instance_id, c.inbox_url) for c in conns}
        )
        overflow = max(0, len(raw) - MAX_GFS_SERVERS)
        if dropped > overflow:
            log.warning(
                "space %s: %d GFS connection(s) left out of the members' GFS "
                "list (unusable URL or key, or a duplicate)",
                space_id,
                dropped - overflow,
            )
        return servers or ()

    @staticmethod
    def _entry(conn: GfsConnection) -> dict:
        return {
            "gfs_instance_id": conn.gfs_instance_id,
            "inbox_url": conn.inbox_url,
            "public_key": conn.public_key,
            "gfs_key_suite": GFS_KEY_SUITE_ED25519,
            "display_name": conn.display_name,
        }

    async def member_servers(self, space_id: str) -> tuple[GfsServer, ...] | None:
        """The owner's listed servers THIS household may contact for
        *space_id*; ``None`` when no list is known.

        A private-host entry (loopback / RFC1918 / link-local / ULA) is kept
        only when we hold an active connection to the same
        ``gfs_instance_id`` at the same ``inbox_url``. Dropped entries are
        logged once per call."""
        space = await self._spaces.get(space_id)
        if space is None or space.gfs_servers is None:
            return None
        own = {(c.gfs_instance_id, c.inbox_url) for c in await self._gfs.list_active()}
        servers, dropped = space.gfs_servers.servers(own_routes=own)
        if dropped:
            log.warning(
                "space %s: %d entr(y/ies) of the owner's GFS list unusable here "
                "(bad field, private network address, duplicate or over the cap)",
                space_id,
                dropped,
            )
        return servers
