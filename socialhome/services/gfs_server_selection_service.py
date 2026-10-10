"""The GFSs a global space is listed on: the owner's choice, and the signed
list (``spaces.gfs_servers``) members receive.

Tiers (owner model, 2026-10-10): a PUBLIC space is discoverable only by the
households we are connected with and never touches a GFS; a GLOBAL space is
listed on the GFSs its OWNER picks (at most :data:`MAX_GFS_SERVERS`).
Nothing is ever published to "every active GFS".

Choosing (:meth:`GfsServerSelectionService.set_servers`, ``add_server``,
``remove_server``): the space OWNER, on the owner household, of a GLOBAL
space, among this household's ACTIVE connections. The tier change itself
stays quorum-gated (``SpaceApprovalService``); picking servers for an
already-global space is the owner's alone. Each change publishes to the added
servers, withdraws from the removed ones, and re-signs the members' list ONCE
at the end (``apply``). ``reconcile_on_startup`` brings an upgraded
household in line: an owned PUBLIC space that was hand-published to a GFS
becomes GLOBAL (its existing listings are recorded, not re-decided), and an
owned GLOBAL space with no list yet gets one, keeping its listings.

A global space is listed on GFSs its owner chose. Members must reach every one
of them — including GFSs their own household never connected to — so the
owner hands them the list inside the encrypted ``space_meta``
(``SPACE_CONFIG_CHANGED``), signed with the owner household's identity key
(:mod:`socialhome.gfs_server_list_sig`). This service keeps the owner's copy
(migration 0093, :mod:`socialhome.domain.gfs_server`) in step with the truth:
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
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime, timezone

from dataclasses import replace

from ..domain.errors import (
    GfsSelectionInvalidError,
    GfsSelectionRequiredError,
    SpaceNotPublishableError,
    TierChangeRequiredError,
)
from ..domain.events import SpaceConfigChanged
from ..domain.federation import GfsConnection, GfsSpacePublication
from ..domain.gfs_server import (
    GFS_KEY_SUITE_ED25519,
    MAX_GFS_SERVERS,
    GfsSelectionOutcome,
    GfsServer,
    canonical_json,
    gfs_servers_from_wire,
    gfs_servers_to_wire,
)
from ..domain.space import (
    Space,
    SpaceConfigEventType,
    SpacePermissionError,
    SpaceRole,
    SpaceType,
)
from ..gfs_server_list_sig import sign_gfs_server_list
from ..infrastructure.event_bus import EventBus
from ..space_owner_statement import MAX_STATEMENT_FUTURE_SKEW_MS
from ..repositories.gfs_connection_repo import AbstractGfsConnectionRepo
from ..repositories.space_repo import AbstractSpaceRepo
from ..repositories.user_repo import AbstractUserRepo
from .gfs_connection_service import GfsConnectionError, GfsConnectionService

log = logging.getLogger(__name__)


class GfsServerSelectionService:
    """Derives, signs and distributes the owner's ``spaces.gfs_servers``."""

    __slots__ = (
        "_spaces",
        "_gfs",
        "_bus",
        "_users",
        "_publisher",
        "_own_instance_id",
        "_own_signing_key",
        "_locks",
        "_change_locks",
    )

    def __init__(
        self,
        *,
        space_repo: AbstractSpaceRepo,
        gfs_repo: AbstractGfsConnectionRepo,
        bus: EventBus,
        user_repo: AbstractUserRepo | None = None,
        gfs_connection_service: GfsConnectionService | None = None,
    ) -> None:
        self._spaces = space_repo
        self._gfs = gfs_repo
        self._bus = bus
        # Who may choose (the space OWNER) and how a listing is made / taken
        # down. Optional so the snapshot side works on its own.
        self._users = user_repo
        self._publisher = gfs_connection_service
        # The household identity, attached once it is loaded at startup
        # (:meth:`attach_identity`). Until then nothing is derived.
        self._own_instance_id = ""
        self._own_signing_key = b""
        #: space id → [lock, holders + waiters]; pruned when unused.
        self._locks: dict[str, list] = {}
        #: The same for changes of the owner's choice (publish / withdraw):
        #: one at a time per space, so the cap and the diff hold. Separate
        #: from ``_locks`` because a change ends with a snapshot.
        self._change_locks: dict[str, list] = {}

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
        async with _space_lock(self._locks, space_id):
            return await self._sync_locked(space_id)

    def _changing(self, space_id: str):
        """Serialise changes of one space's listings."""
        return _space_lock(self._change_locks, space_id)

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
        if space.space_type is SpaceType.GLOBAL:
            servers = await self._derive(space_id)
        elif current is None or current.servers_json == "[]":
            # Never listed, or already signed empty: nothing to say.
            return False
        else:
            # Not global (a public space is never on a GFS): members must
            # stop using every server now, even if a withdrawal could not
            # reach one of them.
            servers = ()
        servers_json = canonical_json(gfs_servers_to_wire(servers))
        if current is not None and current.servers_json == servers_json:
            return False
        now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
        issued_at = max(now_ms, current.issued_at + 1 if current else 0)
        if issued_at > now_ms + MAX_STATEMENT_FUTURE_SKEW_MS:
            # Members refuse a list more than a day ahead; a runaway stored
            # value must not wedge the owner. They keep their (higher) copy
            # until the clock passes it, at most a day.
            log.warning(
                "space %s: stored GFS list is dated far ahead — signing at now",
                space_id,
            )
            issued_at = now_ms
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

    # ── The owner's choice ────────────────────────────────────────────────

    async def check_selection(
        self, gfs_ids: object, *, required: bool = False
    ) -> tuple[str, ...]:
        """Validate an owner's choice of GFSs; returns the connection ids,
        de-duplicated, in the order given.

        Raises :class:`GfsSelectionInvalidError` for a malformed value, more
        than :data:`MAX_GFS_SERVERS` servers or an id that is not one of
        this household's ACTIVE connections; with ``required``, raises
        :class:`GfsSelectionRequiredError` for an empty choice while there is
        an active connection to choose (with none, a global space is simply
        "global, not listed" until the owner connects one)."""
        if gfs_ids is None:
            ids: tuple[str, ...] = ()
        elif isinstance(gfs_ids, (list, tuple)) and all(
            isinstance(g, str) and g for g in gfs_ids
        ):
            ids = tuple(dict.fromkeys(gfs_ids))
        else:
            raise GfsSelectionInvalidError("malformed")
        if len(ids) > MAX_GFS_SERVERS:
            raise GfsSelectionInvalidError("too_many", max_servers=MAX_GFS_SERVERS)
        active = {c.id for c in await self._gfs.list_active()}
        if any(g not in active for g in ids):
            raise GfsSelectionInvalidError("unknown")
        if required and not ids and active:
            raise GfsSelectionRequiredError()
        return ids

    async def still_active(self, gfs_ids: tuple[str, ...]) -> tuple[str, ...]:
        """*gfs_ids* minus any no longer an ACTIVE connection, capped at
        :data:`MAX_GFS_SERVERS` — for a choice made earlier (a tier proposal
        approved days later)."""
        active = {c.id for c in await self._gfs.list_active()}
        return tuple(g for g in dict.fromkeys(gfs_ids) if g in active)[:MAX_GFS_SERVERS]

    async def set_servers(
        self, space_id: str, *, actor_username: str, gfs_ids: object
    ) -> GfsSelectionOutcome:
        """The OWNER replaces the set of GFSs a global space is listed on.

        Publishes to every added server, withdraws from every removed one,
        then re-signs the members' list once. An empty set on a space that
        is no longer global is allowed (it only withdraws)."""
        await self._require_owner(space_id, actor_username)
        if not isinstance(gfs_ids, (list, tuple)):
            # ``None`` here would read as "no servers" and unlist the space
            # everywhere: a replacement set must be spelled out.
            raise GfsSelectionInvalidError("malformed")
        ids = await self.check_selection(gfs_ids)
        async with self._changing(space_id):
            if ids:
                # Re-read under the lock: a tier change may have landed.
                _require_global(await self._require_owned(space_id))
            return await self._apply_locked(space_id, ids)

    async def add_server(
        self, space_id: str, *, actor_username: str, gfs_id: str
    ) -> GfsSpacePublication:
        """The OWNER lists a global space on one more GFS (or refreshes a
        listing it already has). Raises :class:`GfsConnectionError` when the
        GFS refuses or can't be reached — nothing is listed then."""
        await self._require_owner(space_id, actor_username)
        publisher = self._require_publisher()
        async with self._changing(space_id):
            _require_global(await self._require_owned(space_id))
            # The cap is checked under the lock, so two concurrent adds
            # can't both pass it.
            current = {c.id for c in await self._gfs.list_gfs_for_space(space_id)}
            if gfs_id not in current and len(current) >= MAX_GFS_SERVERS:
                raise GfsSelectionInvalidError("too_many", max_servers=MAX_GFS_SERVERS)
            conn = await self._gfs.get(gfs_id)
            if conn is None or conn.status != "active":
                raise GfsConnectionError(f"GFS connection {gfs_id} not found")
            try:
                return await publisher.publish_space(space_id, gfs_id, announce=False)
            finally:
                await self.sync_snapshot(space_id)

    async def remove_server(
        self,
        space_id: str,
        *,
        actor_username: str,
        gfs_id: str,
        household_admin: bool = False,
    ) -> None:
        """Withdraw a space's listing from one GFS.

        The OWNER may, for a space it hosts. So may a household admin
        (``household_admin``), keyed on the publication ROW this household
        holds rather than on a live owned space — a leftover listing of a
        dissolved space, or one published before an ownership change, can
        still be withdrawn; withdrawing only narrows what a GFS learns, like
        disconnecting the GFS (also admin-only) does for every space.

        A GFS with no publication row for the space is never contacted
        (``KeyError`` → 404): it would learn the space id from the request."""
        if not household_admin:
            await self._require_owner(space_id, actor_username)
        publisher = self._require_publisher()
        async with self._changing(space_id):
            rows = {
                p.gfs_connection_id
                for p in await self._gfs.list_publications_for_space(space_id)
            }
            if gfs_id not in rows:
                raise KeyError(f"space {space_id!r} is not listed on {gfs_id!r}")
            try:
                await publisher.unpublish_space(space_id, gfs_id, announce=False)
            finally:
                await self.sync_snapshot(space_id)

    async def apply(
        self, space_id: str, gfs_ids: tuple[str, ...]
    ) -> GfsSelectionOutcome:
        """Make *gfs_ids* the servers listing *space_id* — no actor check;
        the caller authorised the change (:meth:`set_servers`, or
        ``SpaceService.on_tier_change`` after the owner / a quorum moved the
        space into the global tier).

        Fail-soft per server: one GFS that can't be reached is reported in
        ``failed`` and never aborts the rest. Members are told once, after
        every publish landed (:meth:`sync_snapshot`), so a server is never
        in their list before it holds the space's pin."""
        async with self._changing(space_id):
            return await self._apply_locked(space_id, gfs_ids)

    async def _apply_locked(
        self, space_id: str, gfs_ids: tuple[str, ...]
    ) -> GfsSelectionOutcome:
        publisher = self._require_publisher()
        gfs_ids = tuple(dict.fromkeys(gfs_ids))[:MAX_GFS_SERVERS]
        current = [c.id for c in await self._gfs.list_gfs_for_space(space_id)]
        failed: list[tuple[str, str]] = []
        try:
            for gfs_id in gfs_ids:
                if gfs_id in current:
                    continue
                try:
                    await publisher.publish_space(space_id, gfs_id, announce=False)
                except (GfsConnectionError, SpaceNotPublishableError) as exc:
                    log.warning(
                        "space %s: listing on gfs %s failed: %s",
                        space_id,
                        gfs_id,
                        exc,
                    )
                    failed.append((gfs_id, "publish_failed"))
            for gfs_id in current:
                if gfs_id in gfs_ids:
                    continue
                try:
                    await publisher.unpublish_space(space_id, gfs_id, announce=False)
                except GfsConnectionError as exc:
                    log.warning(
                        "space %s: withdrawing the listing on gfs %s failed: %s",
                        space_id,
                        gfs_id,
                        exc,
                    )
                    failed.append((gfs_id, "unpublish_failed"))
        finally:
            await self.sync_snapshot(space_id)
        listed = tuple(c.id for c in await self._gfs.list_gfs_for_space(space_id))
        return GfsSelectionOutcome(listed=listed, failed=tuple(failed))

    async def retry_withdrawals(self, gfs_id: str | None = None) -> int:
        """Withdraw every listing this household still holds that must not
        be on a GFS — at startup and on each GFS (re)connect; returns how
        many withdrawals succeeded.

        A row is withdrawn when its space is gone or dissolved (a
        dissolve whose withdrawal failed), or when this household OWNS the
        space and it is no longer global (a quorum-approved tier change
        whose withdrawal could not reach the GFS). A row for a space hosted
        elsewhere is left alone (the household admin can withdraw it by
        hand); the reconnect heal no longer re-publishes it either.
        ``gfs_id`` limits it to one connection."""
        if not self._own_instance_id or self._publisher is None:
            return 0
        rows = (
            await self._gfs.list_publications(gfs_id)
            if gfs_id is not None
            else [
                p
                for c in await self._gfs.list_all()
                for p in await self._gfs.list_publications(c.id)
            ]
        )
        done = 0
        for pub in rows:
            space = await self._spaces.get(pub.space_id)
            if space is not None and not space.dissolved:
                if space.owner_instance_id != self._own_instance_id:
                    continue
                if space.space_type is SpaceType.GLOBAL:
                    continue
            async with self._changing(pub.space_id):
                try:
                    await self._publisher.unpublish_space(
                        pub.space_id, pub.gfs_connection_id, announce=False
                    )
                    done += 1
                except GfsConnectionError as exc:
                    log.warning(
                        "space %s: withdrawing the leftover listing on gfs %s "
                        "failed again (retried at the next start / reconnect): %s",
                        pub.space_id,
                        pub.gfs_connection_id,
                        exc,
                    )
                if space is not None and not space.dissolved:
                    await self.sync_snapshot(pub.space_id)
        return done

    async def describe(self, space: Space) -> tuple[list[dict] | None, bool]:
        """Read-only view of where *space* is listed, for any member:
        ``(servers, unlisted)``.

        ``servers`` — ``[{display_name, inbox_url, connected}]`` from the
        owner's signed list (``connected``: this household holds an active
        connection to that GFS), ``None`` when the space isn't global or no
        list is known yet. ``unlisted`` — the space is global but listed on
        no GFS ("global, not listed": a remote admin's approved proposal, or
        every chosen server refused), so the SPA can say so."""
        if space.space_type is not SpaceType.GLOBAL:
            return None, False
        active = await self._gfs.list_active()
        own = {(c.gfs_instance_id, c.inbox_url) for c in active}
        if space.gfs_servers is None:
            if space.owner_instance_id != self._own_instance_id:
                return None, False
            # Owned, list not signed yet (startup reconcile pending): the
            # listings are the truth.
            listings = await self._gfs.list_active_listings_for_space(space.id)
            return (
                [
                    {
                        "display_name": c.display_name,
                        "inbox_url": c.inbox_url,
                        "connected": True,
                    }
                    for c in listings
                ],
                not listings,
            )
        servers, _ = space.gfs_servers.servers(own_routes=own)
        view = [
            {
                "display_name": s.display_name,
                "inbox_url": s.inbox_url,
                "connected": s.route in own,
            }
            for s in servers or ()
        ]
        # "Global, not listed" means the OWNER's signed list is empty — not
        # that this household can't use any entry of it (a private-network
        # GFS it isn't connected to is dropped from ``view`` above).
        return view, space.gfs_servers.servers_json == "[]"

    async def owner_view(self, space_id: str, *, actor_username: str) -> dict:
        """``GET /api/spaces/{id}/gfs-servers`` — the owner's picker.

        ``listings``: EVERY connection holding a publication row for the
        space, whatever the connection's state (a suspended / pending one
        included), so the owner can see and withdraw each. ``available``:
        the ACTIVE connections a listing can be added on. Plus the cap and
        the members' signed list."""
        space = await self._require_owner(space_id, actor_username)
        listed = {
            p.gfs_connection_id: p.status
            for p in await self._gfs.list_publications_for_space(space_id)
        }
        servers, unlisted = await self.describe(space)
        listings = []
        for gfs_id in sorted(listed):
            conn = await self._gfs.get(gfs_id)
            if conn is None:
                continue
            listings.append(
                {
                    "gfs_id": conn.id,
                    "display_name": conn.display_name,
                    "inbox_url": conn.inbox_url,
                    "connection_status": conn.status,
                    "listing_status": listed[gfs_id],
                }
            )
        return {
            "space_type": space.space_type.value,
            "gfs_ids": sorted(listed),
            "max": MAX_GFS_SERVERS,
            "unlisted": unlisted,
            "servers": servers,
            "listings": listings,
            "available": [
                {
                    "gfs_id": c.id,
                    "display_name": c.display_name,
                    "inbox_url": c.inbox_url,
                    "listed": c.id in listed,
                    "listing_status": listed.get(c.id),
                }
                for c in await self._gfs.list_active()
            ],
        }

    async def reconcile_on_startup(self) -> int:
        """Bring the spaces this household owns in line with the owner model;
        idempotent, returns how many spaces / listings it changed.

        * an owned PUBLIC space with an ACTIVE GFS listing and NO signed GFS
          list (``gfs_servers IS NULL``) becomes GLOBAL. That combination
          exists only on a household upgraded from before the owner model,
          where public spaces were hand-published: every space that has been
          global since is signed a list (an empty one when it leaves the
          tier), so a quorum-approved global → public whose withdrawal
          failed is never promoted back — its listing is withdrawn instead.
          This records the state the owner created — it is listed there —
          so it skips the quorum, and keeps every listing;
        * an owned GLOBAL space with no signed list yet gets one, derived
          from the listings it keeps (:meth:`sync_snapshot`);
        * every listing that must not be on a GFS any more is withdrawn again
          (:meth:`retry_withdrawals`)."""
        if not self._own_instance_id or not self._own_signing_key:
            return 0
        changed = 0
        for space in await self._spaces.list_by_type(SpaceType.PUBLIC):
            if (
                space.owner_instance_id != self._own_instance_id
                or space.gfs_servers is not None
            ):
                continue
            if not any(
                p.status == "active"
                for p in await self._gfs.list_publications_for_space(space.id)
            ):
                continue
            await self._promote_to_global(space)
            changed += 1
        for space in await self._spaces.list_by_type(SpaceType.GLOBAL):
            if (
                space.owner_instance_id == self._own_instance_id
                and space.gfs_servers is None
                and await self.sync_snapshot(space.id)
            ):
                changed += 1
        changed += await self.retry_withdrawals()
        return changed

    async def _promote_to_global(self, space: Space) -> None:
        log.info(
            "space %s: public space listed on a GFS — it is global now "
            "(public spaces are never on a GFS)",
            space.id,
        )
        await self._spaces.save(replace(space, space_type=SpaceType.GLOBAL))
        sequence = await self._spaces.increment_config_sequence(space.id)
        # Same LWW bookkeeping as a local ``update_config``.
        await self._spaces.set_config_author(space.id, self._own_instance_id)
        await self._bus.publish(
            SpaceConfigChanged(
                space_id=space.id,
                event_type=SpaceConfigEventType.PUBLIC_MODE_CHANGED.value,
                payload={"space_type": SpaceType.GLOBAL.value},
                sequence=sequence,
            )
        )
        await self.sync_snapshot(space.id)

    # ── Guards ────────────────────────────────────────────────────────────

    def _require_publisher(self) -> GfsConnectionService:
        if self._publisher is None:
            raise RuntimeError("gfs connection service not attached")
        return self._publisher

    async def _require_owned(self, space_id: str) -> Space:
        space = await self._spaces.get(space_id)
        if space is None or space.dissolved:
            raise KeyError(f"space {space_id!r} not found")
        if (
            not self._own_instance_id
            or space.owner_instance_id != self._own_instance_id
        ):
            raise SpacePermissionError(
                "only the space's owner household chooses its GFSs"
            )
        return space

    async def _require_owner(self, space_id: str, actor_username: str) -> Space:
        space = await self._require_owned(space_id)
        user = await self._users.get(actor_username) if self._users else None
        member = (
            await self._spaces.get_member(space_id, user.user_id)
            if user is not None
            else None
        )
        if member is None or member.role != SpaceRole.OWNER:
            raise SpacePermissionError("only the space's owner chooses its GFSs")
        return space


@asynccontextmanager
async def _space_lock(table: dict[str, list], space_id: str) -> AsyncIterator[None]:
    """One :class:`asyncio.Lock` per space in *table*, pruned once nobody
    holds or waits for it (the dict stays as small as the work in flight)."""
    entry = table.setdefault(space_id, [asyncio.Lock(), 0])
    entry[1] += 1
    try:
        async with entry[0]:
            yield
    finally:
        entry[1] -= 1
        if entry[1] == 0:
            table.pop(space_id, None)


def _require_global(space: Space) -> None:
    """Only a global space is listed; listing a public one would make it
    global, which is a tier change (409 ``TIER_CHANGE_REQUIRED``)."""
    if space.space_type is SpaceType.GLOBAL:
        return
    if space.space_type is SpaceType.PUBLIC:
        raise TierChangeRequiredError()
    raise SpaceNotPublishableError()
