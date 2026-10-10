"""Mirror a GFS-discovered space onto a local ``spaces`` stub row.

This is the on-ramp between GFS *discovery* (``PublicSpaceDiscoveryService``
fills ``public_space_cache`` from ``GET /gfs/spaces``) and GFS *content*
(``SpacePublicInbound`` / ``SpaceSubscriberKeyInbound`` consume relayed
frames). Both ends already exist; neither works for a space this household
has never met, because:

* the local ``spaces`` row that carries ``identity_public_key`` — the pinned
  space-authority verify key every relayed frame is checked against — is
  never created, so every inbound frame is dropped; and
* ``SpaceService.subscribe_to_space`` 404s on an id with no local row, so the
  GFS-side ``space_subscribers`` set is never populated and no relay fans out.

:meth:`GfsSpaceMirrorService.ensure_mirror` closes that gap: it fetches
``GET {gfs}/gfs/spaces/{space_id}`` (see
``global_server/routes/relay.py::SpaceDetailView``) and seats a remote stub
via the canonical :func:`~socialhome.services.space_service.stub_space_from_metadata`.

Trust boundary — read before changing anything here
---------------------------------------------------
The mirrored ``identity_public_key`` is **served by the GFS**. It is not
self-certifying: a space id is a plain ``uuid4`` minted by
``SpaceService.create_space``, never derived from the authority key, so
there is no ``derive_space_id(pk) == space_id`` check to make. The pin is
therefore **TOFU at the household**, which the repository layer enforces:
``SqliteSpaceRepo.save`` deliberately leaves ``identity_public_key`` out of
its ``ON CONFLICT DO UPDATE SET`` clause, so a later refresh — from this
GFS or any other — can never move a pin once seated. The one exception is an
owner-signed authority cert (v_44, :mod:`.space_authority_pin`): it re-pins
only when it verifies against the space's owner instance and carries a
higher epoch, which a GFS cannot forge.

Consequences, stated plainly:

* A household that pairs with a **hostile GFS** can be served a fabricated
  space whose authority key that GFS controls, and would then accept relayed
  "space content" signed by it. An already-pinned space cannot be hijacked,
  and an owned space is never touched.
* **The §D1b crossing is not protected, and this is a real gap.** "Already
  pinned" cuts both ways: a hostile GFS can list a *real* space id (ids are
  harvestable from any public directory) with that space's real
  ``owning_instance`` but an attacker-controlled ``identity_public_key``. If
  a local user subscribes before this household has ever met the space, the
  stub is seated with the attacker's pin. A later, perfectly legitimate
  §D1b ``SPACE_PRIVATE_INVITE`` from the *real* host then passes
  ``can_seat_remote_stub`` (the owner matches) and re-``save``s the row — but
  ``save`` excludes ``identity_public_key`` from its upsert, so the
  attacker's pin stays **permanently**. Genuine space-authority frames then
  fail verification (a permanent denial of service for that space at this
  household) while the hostile GFS's forged frames verify. So the blast
  radius is "spaces this household first learned about through that GFS" —
  which is *not* the same as "spaces only that GFS knows about".
  Fixing it means recording mirror provenance on the row so an
  authenticated §D1b envelope sender may outrank a GFS listing when
  re-pinning; that touches the §D1b handler path and is deliberately left to
  its own reviewed change (see the TODO at the seating site).
* ``owner_instance_id`` here comes from the GFS listing
  (``owning_instance``) and is **not** an authenticated envelope sender, unlike
  the §D1b callers of ``stub_space_from_metadata``. That is precisely why every
  inbound relay verifies against the *pinned key*, never against the claimed
  owner.

Fail-closed: a listing that is not ``status == "active"``, or whose
``identity_public_key`` is missing or not 32 bytes of hex, is skipped — a row
seated with an unverifiable pin would be strictly worse than no row at all.
"""

from __future__ import annotations

import asyncio
import logging
import re
import secrets
import time
from datetime import datetime, timedelta, timezone
from collections.abc import Awaitable, Callable, Coroutine, Iterable
from typing import Any

import aiohttp

from ..domain.events import (
    RemoteSpaceMemberBanned,
    RemoteSpaceMemberRemoved,
    SpaceConfigChanged,
    SpaceMemberLeft,
)
from ..domain.gfs_space_seat import GfsSpaceSeat
from ..domain.federation import GfsConnection
from ..domain.space import Space, normalize_join_mode
from ..infrastructure.event_bus import EventBus
from ..repositories.gfs_connection_repo import AbstractGfsConnectionRepo
from ..repositories.gfs_space_seat_repo import AbstractGfsSpaceSeatRepo
from ..repositories.public_space_repo import AbstractPublicSpaceRepo
from ..repositories.space_repo import AbstractSpaceRepo
from .gfs_connection_service import GfsConnectionError, GfsConnectionService
from .gfs_directory import GfsDirectoryCache
from .gfs_http import MAX_GFS_BODY_BYTES, gfs_server_address, read_json_capped
from ..authority_cert import MAX_AUTHORITY_KEY_EPOCH
from ..domain.space import PUBLIC_SPACE_TIERS, SpaceConfigEventType, SpaceRole
from .space_service import can_seat_remote_stub, stub_space_from_metadata

log = logging.getLogger(__name__)

#: Characters a space id may contain before it is interpolated into a GFS
#: URL. ``space_id`` arrives from ``POST /api/spaces/{space_id}/subscribe``,
#: and aiohttp percent-DECODES the path before populating ``match_info`` — so
#: a request for ``..%2F..%2Fadmin`` yields the literal ``../../admin``, which
#: yarl then normalises away the ``/gfs/spaces/`` prefix entirely, turning the
#: metadata fetch into a GET against an arbitrary path on the paired GFS.
#: Validate before building the URL rather than trusting the router.
_SAFE_SPACE_ID = re.compile(r"\A[A-Za-z0-9_.-]{1,128}\Z")

#: Length in hex characters of an Ed25519 public key (32 raw bytes).
_PIN_HEX_LEN = 64

#: Per-GFS timeout for the metadata fetch. Deliberately short: this is a
#: small metadata GET, never a bulk transfer, and ``ensure_mirror`` asks the
#: servers whose directory lists the id **serially** (first hit wins, which
#: keeps the ordering deterministic and the code simple; the directories
#: themselves are read concurrently). The worst case therefore bounds a
#: single ``POST /api/spaces/{id}/subscribe`` at one directory read plus
#: ``_MIRROR_FETCH_TIMEOUT_S × len(listing servers)`` of held request slot —
#: and any authenticated local user can drive that against an arbitrary
#: unknown id, so the per-connection budget stays small rather than the
#: 15 s used for the interactive GFS calls.
_MIRROR_FETCH_TIMEOUT_S = 5.0

#: A seat taken this recently is never torn down as "unwanted": the
#: follower's local member row is written only AFTER the GFS subscribe
#: succeeds, so a relay frame (or a reconnect self-heal) landing in between
#: must not unsubscribe the seat being taken.
SEAT_GRACE_S = 120.0

#: Minimum seconds between two reactive unsubscribes of one (server, space)
#: — a server that keeps relaying a space we hold no seat in gets one
#: unsubscribe per interval, not one per frame.
UNWANTED_RELAY_RETRY_S = 600.0

#: Most (server, space) keys remembered by each of the two maps above.
_MAX_TRACKED_SEATS = 1024

#: Background seat releases in flight at once; past it one is skipped
#: (logged) — the next reconnect self-heal reconciles it.
MAX_PENDING_SEAT_TASKS = 64

#: Deferred post-grace re-checks (one per first subscribe) in flight at
#: once — a separate budget, so subscribes never crowd out releases.
MAX_PENDING_RECHECKS = 256

#: A detached seat (its server was unpaired) whose unsubscribe the server
#: confirmed is dropped this long after the unpair — locally, no request.
#: An unconfirmed one stays as an unsubscribe-only tombstone.
ORPHAN_SEAT_DAYS = 90

#: The sweep only trusts the wall clock past this instant (a device that
#: booted without a real-time clock reads 1970) — before it nothing is
#: stamped or aged.
CLOCK_SANE_AFTER = datetime(2026, 1, 1, tzinfo=timezone.utc)

#: A row past its age is dropped only by a sweep at least this long after
#: the one that first saw it expired — one clock jump never drops anything.
EXPIRY_CONFIRM_S = 24 * 3600.0

#: How long ``stop()`` lets a purge already running finish.
STOP_PURGE_GRACE_S = 5.0

#: How long a "a local user still wants this seat" answer is reused by the
#: relay-frame path, so a busy followed space costs one check per window
#: rather than one per frame.
WANTED_CACHE_S = 60.0

#: Minimum seconds between two lazy pin refreshes of one space (v_44). A
#: relayed frame that fails the authority check triggers a refresh; a burst
#: of them (or a hostile relay replaying old-key frames) must not turn into
#: a GFS request per frame.
PIN_REFRESH_INTERVAL_S = 60.0

#: Most spaces whose last refresh time is remembered at once.
_PIN_REFRESH_MAX_TRACKED = 1024


def _listing_pin(body: dict) -> tuple[str, int] | None:
    """``(identity_public_key, authority_rotation_seq)`` off a GFS listing,
    or ``None`` when either is missing or malformed (fail-closed)."""
    pk = body.get("identity_public_key")
    epoch = body.get("authority_rotation_seq", 0)
    if not isinstance(pk, str) or len(pk) != _PIN_HEX_LEN:
        return None
    try:
        if len(bytes.fromhex(pk)) != 32:
            return None
    except ValueError:
        return None
    if (
        isinstance(epoch, bool)
        or not isinstance(epoch, int)
        or epoch < 0
        or epoch > MAX_AUTHORITY_KEY_EPOCH
    ):
        return None
    return pk.lower(), epoch


def _as_text(value: object) -> str | None:
    """Coerce a GFS-supplied field to ``str``, preserving ``None``.

    The GFS response is remote input with no schema guarantee; a non-string
    (list / dict / int) passed through to the ``spaces`` upsert raises
    ``sqlite3.ProgrammingError`` from inside ``ensure_mirror``, which is not
    a mapped domain exception and surfaces as an HTTP 500.
    """
    return None if value is None else str(value)


class GfsSpaceMirrorService:
    """Seats + tears down local stub rows for GFS-discovered spaces."""

    __slots__ = (
        "_spaces",
        "_gfs_conn_repo",
        "_public_spaces",
        "_gfs",
        "_http_client",
        "_own_instance_id",
        "_last_pin_refresh",
        "_directories",
        "_seats",
        "_seated_at",
        "_unwanted_at",
        "_tasks",
        "_stopping",
        "_rechecks",
        "_wanted_at",
        "_purging",
        "_teardown",
    )

    def __init__(
        self,
        *,
        space_repo: AbstractSpaceRepo,
        gfs_connection_repo: AbstractGfsConnectionRepo,
        gfs_connection_service: GfsConnectionService,
        seat_repo: AbstractGfsSpaceSeatRepo,
        public_space_repo: AbstractPublicSpaceRepo | None = None,
        directories: GfsDirectoryCache | None = None,
    ) -> None:
        self._spaces = space_repo
        self._gfs_conn_repo = gfs_connection_repo
        # The ``public_space_cache`` directory — the household's only local
        # evidence that a GFS ever advertised a given space id. Optional so
        # older wiring keeps working; without it :meth:`was_gfs_listed`
        # answers ``False``, which is the fail-safe answer (no destructive
        # teardown on an unprovable mirror).
        self._public_spaces = public_space_repo
        self._gfs = gfs_connection_service
        self._http_client: aiohttp.ClientSession | None = None
        self._own_instance_id = ""
        self._last_pin_refresh: dict[str, float] = {}
        # Each server's whole public directory. ``app`` shares one cache
        # with member publish (over the cookie-less publish session);
        # standalone wiring reads it over the mirror's own session.
        self._directories = directories or GfsDirectoryCache(lambda: self._http_client)
        # The servers holding a subscriber seat of ours, per space (0092) —
        # the only servers an identity-bound (un)subscribe may go to.
        self._seats = seat_repo
        # (gfs_instance_id, space_id) → monotonic time of the last seat taken
        # / the last reactive teardown sent; both bounded.
        self._seated_at: dict[tuple[str, str], float] = {}
        self._unwanted_at: dict[tuple[str, str], float] = {}
        # Background seat releases (strong refs) and the stop flag.
        self._tasks: set[asyncio.Task[object]] = set()
        self._stopping = False
        # Post-grace re-checks (a separate, larger budget).
        self._rechecks: set[asyncio.Task[object]] = set()
        # (server, space) → when a relay-frame check last found it wanted.
        self._wanted_at: dict[tuple[str, str], float] = {}
        # Teardowns started by a re-check: ``stop()`` lets them finish.
        self._purging: set[asyncio.Task[object]] = set()
        # ``SpaceService`` teardown of an unused GFS mirror (stub purge).
        self._teardown: Callable[[str], Awaitable[object]] | None = None

    def attach_identity(self, *, own_instance_id: str) -> None:
        """Our instance id — an owner never re-pins its own space (v_44)."""
        self._own_instance_id = own_instance_id

    def attach_session(self, session: aiohttp.ClientSession) -> None:
        """Provide the shared aiohttp session after construction.

        Called from ``app._on_startup`` beside the sibling GFS services.
        Tests inject a stub session the same way.
        """
        if self._http_client is None:
            self._http_client = session

    async def ensure_mirror(self, space_id: str) -> tuple[Space, str] | None:
        """Mirror *space_id*'s metadata from the first paired GFS that serves
        it, returning ``(seated_space, gfs_connection_id)``. A GFS is asked
        for the space's detail only when its whole directory lists the id
        (:class:`GfsDirectoryCache`) — never a per-space probe of the others.

        Returns ``None`` when no active GFS knows the space, when every
        candidate listing fails validation (fail-closed — see the module
        docstring), or when a local row already exists under a different host
        (``can_seat_remote_stub``).
        """
        if not _SAFE_SPACE_ID.fullmatch(space_id) or space_id in {".", ".."}:
            # Never interpolate an unvalidated id into the GFS URL — see
            # ``_SAFE_SPACE_ID``. Fail closed: a malformed id is not a space
            # any GFS could legitimately serve.
            log.warning("gfs_space_mirror: refusing unsafe space id %r", space_id)
            return None
        client = self._http_client
        if client is None:
            log.debug(
                "gfs_space_mirror: no HTTP session wired — cannot mirror %s",
                space_id,
            )
            return None
        # The detail GET names the space: sent to a GFS that doesn't list it,
        # it would tell that operator (and give it our address) which space
        # this household is after. Ask only a server whose WHOLE directory
        # lists the id; an unreadable directory proves nothing. The
        # directories are read concurrently (each is cached and coalesced);
        # a cached copy lacking the id is re-read when it is a few seconds
        # old, so a space published a moment ago is followable at once.
        conns = await self._gfs_conn_repo.list_active()
        listing_flags = await asyncio.gather(
            *(
                self._directories.lists(conn, space_id, refresh_on_miss=True)
                for conn in conns
            )
        )
        for conn, lists_it in zip(conns, listing_flags):
            if not lists_it:
                continue
            url = f"{conn.inbox_url.rstrip('/')}/gfs/spaces/{space_id}"
            try:
                async with client.get(
                    url,
                    allow_redirects=False,
                    timeout=aiohttp.ClientTimeout(total=_MIRROR_FETCH_TIMEOUT_S),
                ) as resp:
                    if resp.status == 404:
                        continue
                    if resp.status != 200:
                        log.warning(
                            "gfs_space_mirror: %s returned HTTP %d",
                            url,
                            resp.status,
                        )
                        continue
                    # Bounded read — a GFS body is remote input and aiohttp
                    # caps nothing by default. An over-large or unparsable
                    # body reads as a failed fetch (fail-closed: nothing is
                    # seated from it).
                    body = await read_json_capped(
                        resp,
                        url=url,
                        limit=MAX_GFS_BODY_BYTES,
                    )
            except (aiohttp.ClientError, asyncio.TimeoutError, OSError) as exc:
                log.warning("gfs_space_mirror: fetch failed for %s: %s", url, exc)
                continue
            if body is None:
                # Already logged by ``read_json_capped`` (over cap / not
                # JSON). Try the next paired GFS.
                continue
            if not isinstance(body, dict):
                log.warning("gfs_space_mirror: %s returned a non-object body", url)
                continue

            meta = self._validated_metadata(space_id, body, gfs_id=conn.id)
            if meta is None:
                continue
            owning_instance = str(body.get("owning_instance") or "")
            # CONTRACT DEVIATION — read before reusing this call.
            # ``can_seat_remote_stub`` documents that its third argument MUST
            # be the *authenticated envelope sender* (§D1b), never a claimed
            # owner. Here it is the ``owning_instance`` string straight out
            # of the GFS response body: unauthenticated, attacker-controlled
            # if the GFS is hostile.
            # It is safe **only** because of the precondition upstream:
            # ``SpaceService.subscribe_to_space`` calls ``ensure_mirror``
            # exclusively when ``await self._spaces.get(space_id) is None``,
            # so the guard's "no local row → always seatable" branch is the
            # only one that can be taken; the claimed owner is never compared
            # against an existing row and so can never win against one.
            # WARNING: calling ``ensure_mirror`` to *refresh* an existing
            # mirror would turn this into a direct overwrite of the claimed
            # owner — a hostile GFS could then re-home any space we already
            # hold. Any such call site needs the authenticated-sender
            # contract restored first.
            if not await can_seat_remote_stub(
                self._spaces,
                space_id,
                owning_instance,
            ):
                log.warning(
                    "gfs_space_mirror: refusing to mirror %s from GFS %s — "
                    "a local row is already held under another host",
                    space_id,
                    conn.id,
                )
                return None
            # TODO(gfs-mirror-provenance): record that this row's pin came
            # from a GFS listing (a provenance column / side table on the
            # space row) and let an *authenticated* envelope sender — a §D1b
            # invite from the space's real host — outrank that GFS listing
            # when re-pinning ``identity_public_key``. Without it a hostile
            # GFS that wins the race on a real space id pins its own key
            # permanently; see the "§D1b crossing" bullet in the module
            # docstring. Deliberately out of scope here: the fix touches the
            # §D1b invite handler path and needs its own review.
            space = stub_space_from_metadata(
                space_id,
                host_instance_id=owning_instance,
                meta=meta,
            )
            await self._spaces.save(space)
            # v_44 — remember WHICH connection server seated this mirror and
            # the re-pin counter it showed: a later pin heal is accepted from
            # this GFS only, and only to a higher counter.
            listed = _listing_pin(body)
            await self._spaces.set_mirror_provenance(
                space_id,
                gfs_id=conn.id,
                rotation_seq=listed[1] if listed is not None else 0,
            )
            log.info(
                "gfs_space_mirror: seated stub for space %s from GFS %s",
                space_id,
                conn.id,
            )
            return space, conn.id
        return None

    async def _fetch_listing(self, conn, space_id: str) -> dict | None:
        """``GET {gfs}/gfs/spaces/{space_id}`` from one connection, or None."""
        client = self._http_client
        if client is None or not _SAFE_SPACE_ID.fullmatch(space_id):
            return None
        url = f"{conn.inbox_url.rstrip('/')}/gfs/spaces/{space_id}"
        try:
            async with client.get(
                url,
                allow_redirects=False,
                timeout=aiohttp.ClientTimeout(total=_MIRROR_FETCH_TIMEOUT_S),
            ) as resp:
                if resp.status != 200:
                    return None
                body = await read_json_capped(resp, url=url, limit=MAX_GFS_BODY_BYTES)
        except (aiohttp.ClientError, asyncio.TimeoutError, OSError) as exc:
            log.warning("gfs_space_mirror: fetch failed for %s: %s", url, exc)
            return None
        return body if isinstance(body, dict) else None

    async def refresh_authority_pin(
        self, space_id: str, *, force: bool = False
    ) -> bool:
        """Heal a FOLLOWER's pin from the GFS listing after a rotation (v_44).

        Called when a relayed frame fails the authority check (the owner may
        have rotated the key), and on every GFS-WS reconnect. The listing
        carries only the current ``identity_public_key`` and the GFS's own
        ``authority_rotation_seq`` — the GFS re-pinned after verifying the
        owner's cert against the owner's registered key, and keeps the cert
        (and its wall-clock epoch) private. Trust model: the same trust a
        follower already places in the GFS that seated its mirror for its
        first (TOFU) pin — and only that GFS — bounded by a strictly HIGHER
        seq than the one held, so an older key never comes back.

        Only for a PUBLIC / GLOBAL space this household merely FOLLOWS (at
        least one local ``subscriber`` seat, nothing else): a household with
        a real seat, or a private stub, trusts the owner's own cert,
        delivered over federation, never a connection server's word.
        Rate-limited to one fetch per :data:`PIN_REFRESH_INTERVAL_S` per
        space unless ``force``. Returns whether the pin moved.
        """
        now = time.monotonic()
        last = self._last_pin_refresh.get(space_id)
        if not force and last is not None and now - last < PIN_REFRESH_INTERVAL_S:
            return False
        self._remember_refresh(space_id, now)
        return await self._refresh_from(
            await self._gfs_conn_repo.list_active(), space_id
        )

    def _remember_refresh(self, space_id: str, now: float) -> None:
        """Record a refresh; prune entries past the interval so the map is
        bounded by the spaces refreshed in the last minute."""
        if len(self._last_pin_refresh) >= _PIN_REFRESH_MAX_TRACKED:
            cutoff = now - PIN_REFRESH_INTERVAL_S
            for sid, at in list(self._last_pin_refresh.items()):
                if at < cutoff:
                    del self._last_pin_refresh[sid]
            if len(self._last_pin_refresh) >= _PIN_REFRESH_MAX_TRACKED:
                self._last_pin_refresh.clear()
        self._last_pin_refresh[space_id] = now

    async def refresh_authority_pins(self, gfs_id: str) -> int:
        """Re-check every mirrored subscription's pin against *gfs_id*.

        Run on each GFS-WS (re)connect beside :meth:`resubscribe_all`, scoped
        by the same mirror provenance. Returns how many pins moved.
        """
        conn = await self._gfs_conn_repo.get(gfs_id)
        if conn is None:
            return 0
        await self.rebind_mirrors(conn)
        moved = 0
        for space_id in await self._spaces.list_subscribed_space_ids():
            if not await self.was_gfs_listed(space_id):
                continue
            self._remember_refresh(space_id, time.monotonic())
            if await self._refresh_from([conn], space_id):
                moved += 1
        return moved

    async def _refresh_from(self, conns, space_id: str) -> bool:
        """Heal from the listing of the ONE GFS that seated this mirror.

        Refused unless every condition of the follower trust model holds:
        not our space; a public / global tier; at least one local
        ``subscriber`` seat and no other local seat; the connection is the
        recorded ``mirror_gfs_id``; the listed ``authority_rotation_seq`` is
        strictly higher than the one held.
        """
        space = await self._spaces.get(space_id)
        if space is None or (
            self._own_instance_id and space.owner_instance_id == self._own_instance_id
        ):
            return False
        if space.space_type not in PUBLIC_SPACE_TIERS:
            return False
        members = await self._spaces.list_members(space_id)
        if not members or any(m.role != SpaceRole.SUBSCRIBER for m in members):
            return False  # a real seat heals from the owner's cert only
        mirror_gfs, held_seq = await self._spaces.get_mirror_provenance(space_id)
        if mirror_gfs is None:
            return False  # provenance unknown (a pre-v44 mirror)
        for conn in conns:
            if conn.id != mirror_gfs:
                continue
            body = await self._fetch_listing(conn, space_id)
            listed = _listing_pin(body) if body is not None else None
            if listed is None or listed[1] <= held_seq:
                continue
            if await self._spaces.adopt_gfs_pin(
                space_id,
                gfs_id=conn.id,
                public_key_hex=listed[0],
                rotation_seq=listed[1],
            ):
                log.info(
                    "gfs_space_mirror: space %s re-pinned from GFS %s (seq %d)",
                    space_id,
                    conn.id,
                    listed[1],
                )
                return True
        return False

    def _validated_metadata(
        self,
        space_id: str,
        body: dict,
        *,
        gfs_id: str,
    ) -> dict | None:
        """Turn a ``GET /gfs/spaces/{id}`` body into stub metadata, or
        ``None`` when the listing must not be mirrored.

        Fail-closed on anything we cannot verify locally: the listing must be
        ``active`` and must carry a well-formed (32-byte hex) space-authority
        pin — that key is the ONLY thing standing between this household and
        a forged relay frame, so an absent or malformed one is fatal, never
        defaulted.
        """
        status = body.get("status")
        if not isinstance(status, str) or status != "active":
            log.warning(
                "gfs_space_mirror: GFS %s lists space %s as %r — not mirroring",
                gfs_id,
                space_id,
                status,
            )
            return None
        # Validated, not coerced: an instance id is an identifier the rest
        # of the stack compares for equality, so a non-string here is a
        # malformed listing rather than something to stringify.
        owning = body.get("owning_instance")
        if not isinstance(owning, str) or not owning:
            log.warning(
                "gfs_space_mirror: GFS %s listing for %s has no owning_instance",
                gfs_id,
                space_id,
            )
            return None
        pin_raw = body.get("identity_public_key")
        pin = pin_raw if isinstance(pin_raw, str) else ""
        if len(pin) != _PIN_HEX_LEN:
            log.warning(
                "gfs_space_mirror: GFS %s listing for %s has no usable "
                "space-authority key — refusing to seat an unverifiable stub",
                gfs_id,
                space_id,
            )
            return None
        try:
            raw = bytes.fromhex(pin)
        except ValueError:
            raw = b""
        if len(raw) != 32:
            log.warning(
                "gfs_space_mirror: GFS %s listing for %s carries a malformed "
                "space-authority key — refusing to seat it",
                gfs_id,
                space_id,
            )
            return None
        return {
            "name": _as_text(body.get("name")) or "Untitled space",
            # Every string-shaped field is coerced, not passed through: a
            # GFS may answer ``{"about_markdown": [1, 2, 3]}``, and a list
            # reaching SQLite raises ``ProgrammingError`` out of
            # ``ensure_mirror`` → ``subscribe_to_space`` → an unmapped HTTP
            # 500. Fail-closed means "the hostile body cannot crash us"
            # just as much as "the hostile body cannot seat a bad pin".
            "description": _as_text(body.get("description")),
            "about_markdown": _as_text(body.get("about_markdown")),
            "identity_public_key": pin,
            # A GFS mirror is a *subscription* stub, never a locally joinable
            # space: joining still goes through
            # ``POST /api/public_spaces/{id}/join-request``.
            "space_type": "global",
            # The owner's real join mode, as the GFS directory reports it —
            # this used to be hardcoded ``invite_only`` because the field
            # never arrived, which left the household unable to tell an open
            # space from a closed one. Normalised, so a missing field (an
            # older GFS) or a hostile value fails closed to ``invite_only``.
            "join_mode": normalize_join_mode(body.get("join_mode")),
            # The owner's readability opt-in, likewise straight off the
            # directory body. It rides in the ``features`` block because that
            # is where ``allow_subscribers`` lives on the space
            # (``SpaceFeatures``), and ``stub_space_from_metadata`` feeds the
            # block through ``SpaceFeatures.from_wire_dict``. Every other
            # feature keeps its dataclass default, exactly as before this key
            # existed. Fail-closed: an older GFS sends nothing ⇒ False ⇒
            # ``subscribe_to_space`` refuses rather than seating a member on a
            # space whose content can never arrive. Strict ``is True``, like
            # every other read of this hostile body: a GFS that answers
            # ``{"allow_subscribers": "nope"}`` must not widen access through
            # Python truthiness.
            "features": {
                "allow_subscribers": body.get("allow_subscribers") is True,
            },
            # The owning household's local username means nothing here (the
            # GFS listing carries no such field) — leave it empty.
            "owner_username": "",
            # Both are clamped to their allowed set inside
            # ``stub_space_from_metadata`` (``normalize_min_age`` /
            # ``normalize_category``) — but ``normalize_category`` does a
            # set membership test, which *raises* on an unhashable value, so
            # the category is stringified first.
            "min_age": body.get("min_age"),
            "category": _as_text(body.get("category")),
        }

    async def was_gfs_listed(self, space_id: str) -> bool:
        """Whether a paired GFS directory actually advertised *space_id*.

        Positive evidence only, cheapest first:

        * mirror provenance (v_44) — :meth:`ensure_mirror` seats a stub only
          off a GFS listing, and records which connection served it;
        * a recorded GFS seat of the space — we subscribed through a GFS;
        * a ``public_space_cache`` row — that table's one writer is the GFS
          directory poll (which truncates what it imports per tick, so a
          missing row proves nothing);
        * a paired server's WHOLE directory listing it (never truncated),
          read from the cache only — this runs on the unsubscribe request
          path, which never waits on a download — and not for an owner the
          admin blocked from discovery (the poll's own filter).

        Used as the teardown guard: destructive or GFS-visible work on a
        space we cannot prove is a mirror is skipped. Answers ``False``
        whenever the evidence is absent *or* unavailable, which is the
        fail-safe direction (a peer-discovered public/global stub has none).
        """
        mirror_gfs, _ = await self._spaces.get_mirror_provenance(space_id)
        if mirror_gfs is not None:
            return True
        if await self._seats.list_for_space(space_id):
            return True
        if self._public_spaces is not None and (
            await self._public_spaces.get(space_id) is not None
        ):
            return True
        for conn in await self._gfs_conn_repo.list_active():
            owner = self._directories.peek_owner(conn, space_id)
            if owner is not None and not await self._owner_blocked(owner):
                return True
        return False

    async def _owner_blocked(self, owner: str) -> bool:
        """The admin blocked this owner from discovery — its listings are
        no evidence (the directory poll skips them the same way)."""
        return (
            bool(owner)
            and self._public_spaces is not None
            and (await self._public_spaces.is_instance_blocked(owner))
        )

    async def subscribe_to_gfs(self, space_id: str, gfs_id: str) -> None:
        """Register this household on *gfs_id*'s subscriber set for *space_id*.

        A thin seam over :meth:`take_seat` so ``SpaceService.subscribe_to_space``
        can sequence mirror → local refusals → GFS subscribe, and a
        locally-refused user (banned / under-age) never reaches the GFS.
        Raises :class:`GfsConnectionError` (unknown connection, refusal).
        """
        conn = await self._gfs_conn_repo.get(gfs_id)
        if conn is None:
            raise GfsConnectionError(f"GFS connection {gfs_id} not found")
        # A FIRST subscribe: its member row is written only after this
        # returns, so the seat is spared by every teardown for a moment —
        # and re-checked once that moment has passed (a subscribe → quick
        # unsubscribe inside it would otherwise leave seat and stub).
        key = (conn.gfs_instance_id, space_id)
        _remember(self._seated_at, key)
        await self.take_seat(space_id, conn)
        self._schedule_recheck(space_id, key)

    def attach_teardown(self, teardown: Callable[[str], Awaitable[object]]) -> None:
        """Wire ``SpaceService``'s unused-mirror teardown (unsubscribe +
        stub purge) for the post-grace re-check."""
        self._teardown = teardown

    def _schedule_recheck(self, space_id: str, key: tuple[str, str]) -> None:
        if self._stopping or len(self._rechecks) >= MAX_PENDING_RECHECKS:
            log.debug("gfs_space_mirror: post-grace re-check of %s skipped", space_id)
            return
        marked = self._seated_at.get(key)
        task = asyncio.create_task(
            self._recheck_after_grace(space_id, key, marked),
            name=f"gfs-seat-recheck-{space_id}",
        )
        self._rechecks.add(task)
        task.add_done_callback(self._recheck_done)

    def _recheck_done(self, task: asyncio.Task[object]) -> None:
        self._rechecks.discard(task)
        if not task.cancelled() and task.exception() is not None:
            log.error(
                "gfs_space_mirror: post-grace re-check failed",
                exc_info=task.exception(),
            )

    async def _recheck_after_grace(
        self, space_id: str, key: tuple[str, str], marked: float | None
    ) -> None:
        await asyncio.sleep(SEAT_GRACE_S)
        # This subscribe's grace is over (a newer one keeps its own).
        if marked is not None and self._seated_at.get(key) == marked:
            del self._seated_at[key]
        if await self._wants_seat(space_id):
            return
        # The teardown runs as its own task: a ``stop()`` that cancels this
        # (sleeping) re-check lets a teardown already under way finish
        # rather than cutting it between the unsubscribe and the purge.
        purge = asyncio.create_task(self._teardown_unused(space_id))
        self._purging.add(purge)
        purge.add_done_callback(self._purging.discard)
        await asyncio.shield(purge)

    async def _teardown_unused(self, space_id: str) -> None:
        if self._teardown is not None:
            # ``SpaceService`` re-checks the members under the space's
            # subscribe lock, so a concurrent subscribe can't be purged away.
            await self._teardown(space_id)
        await self.release_unused_seats(space_id)

    async def wait_rechecks(self) -> None:
        """Wait for the post-grace re-checks in flight (tests)."""
        while self._rechecks:
            await asyncio.gather(*list(self._rechecks), return_exceptions=True)

    async def take_seat(self, space_id: str, conn: GfsConnection) -> None:
        """Subscribe on *conn* and record the seat under the server's own id
        (``gfs_instance_id`` — stable across a re-pair, unlike ``conn.id``),
        bound to the key and URL it was taken over (:func:`_belongs`).

        Every subscribe this household sends goes through here — the
        follower's, the reconnect self-heal's and member publish's
        auto-subscribe — so the seat table names every server that seats us,
        and a teardown can reach exactly those. Raises
        :class:`GfsConnectionError`; nothing is recorded then.
        """
        await self._gfs.subscribe_to_gfs_space(space_id, conn.id)
        await self._seats.record(_seat_on(space_id, conn))

    # ── Seat lifecycle hooks ─────────────────────────────────────────────

    def wire(self, bus: EventBus) -> None:
        """Release a space's seats once no local user is seated there: on a
        leave / removal, and on the bans that drop a local seat without one
        (a local ban, a host's federated ban, the §25.6 sync's bans)."""
        bus.subscribe(SpaceMemberLeft, self._on_seat_maybe_gone)
        bus.subscribe(RemoteSpaceMemberBanned, self._on_seat_maybe_gone)
        bus.subscribe(RemoteSpaceMemberRemoved, self._on_seat_maybe_gone)
        bus.subscribe(SpaceConfigChanged, self._on_config_changed)

    async def _on_seat_maybe_gone(
        self,
        event: SpaceMemberLeft | RemoteSpaceMemberBanned | RemoteSpaceMemberRemoved,
    ) -> None:
        # Never block the leave / ban on a GFS round-trip: cheap check
        # first, the signed unsubscribes in the background.
        if await self._seats.list_for_space(event.space_id):
            self._spawn(self.release_unused_seats(event.space_id), "release")

    async def _on_config_changed(self, event: SpaceConfigChanged) -> None:
        if event.event_type == SpaceConfigEventType.MEMBER_BANNED.value:
            if await self._seats.list_for_space(event.space_id):
                self._spawn(self.release_unused_seats(event.space_id), "release")

    def _spawn(
        self, coro: Coroutine[Any, Any, object], name: str, *, quiet: bool = False
    ) -> None:
        if self._stopping or len(self._tasks) >= MAX_PENDING_SEAT_TASKS:
            coro.close()
            # The relay-frame path is hot: never a WARNING per frame there.
            (log.debug if quiet else log.warning)(
                "gfs_space_mirror: background %s skipped (busy / stopping)", name
            )
            return
        task = asyncio.create_task(coro, name=f"gfs-seat-{name}")
        self._tasks.add(task)
        task.add_done_callback(self._task_done)

    def _task_done(self, task: asyncio.Task[object]) -> None:
        self._tasks.discard(task)
        if not task.cancelled() and task.exception() is not None:
            log.error(
                "gfs_space_mirror: background seat task failed",
                exc_info=task.exception(),
            )

    async def wait_idle(self) -> None:
        """Wait for the background seat tasks in flight (tests, shutdown)."""
        while self._tasks:
            await asyncio.gather(*list(self._tasks), return_exceptions=True)

    async def stop(self) -> None:
        """Cancel the background seat tasks (app ``on_cleanup``)."""
        self._stopping = True
        tasks = [*self._tasks, *self._rechecks]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        if self._purging:
            await asyncio.wait(list(self._purging), timeout=STOP_PURGE_GRACE_S)

    # ── Wanting / releasing seats ────────────────────────────────────────

    async def _wants_seat(self, space_id: str) -> bool:
        """A seat is wanted while the space is not dissolved and any local
        user — follower or member — is still seated there."""
        space = await self._spaces.get(space_id)
        if space is None or space.dissolved:
            return False
        return bool(await self._spaces.list_local_member_user_ids(space_id))

    def _in_grace(self, gfs_instance_id: str, space_id: str) -> bool:
        at = self._seated_at.get((gfs_instance_id, space_id))
        return at is not None and time.monotonic() - at < SEAT_GRACE_S

    async def _releasable(self, space_id: str, gfs_instance_id: str) -> bool:
        """Checked right before each release: nobody local wants the seat
        and it was not just taken (its member row may not be written yet)."""
        if self._in_grace(gfs_instance_id, space_id):
            return False
        return not await self._wants_seat(space_id)

    async def seat_in_grace(self, space_id: str) -> bool:
        """Whether a seat of *space_id* was taken moments ago — a subscribe
        in progress whose member row is not written yet."""
        return any(
            self._in_grace(seat.gfs_instance_id, space_id)
            for seat in await self._seats.list_for_space(space_id)
        )

    async def release_unused_seats(self, space_id: str) -> int:
        """Tear down every recorded seat of *space_id* nobody local wants
        any more (see :meth:`_releasable`). Returns how many went."""
        return await self.release_seats(space_id, only_unwanted=True)

    async def release_seats(self, space_id: str, *, only_unwanted: bool = False) -> int:
        """Unsubscribe from every server recorded as seating us for
        *space_id*, and forget each seat the server confirmed. Only an
        active connection whose id, key and URL match the seat is
        contacted (:func:`_belongs`); a server not connected right now keeps
        its row for the reconnect self-heal of a genuine re-pair. A seat
        taken in the last :data:`SEAT_GRACE_S` is never released. Returns
        how many were released."""
        seats = await self._seats.list_for_space(space_id)
        if not seats:
            return 0
        active = await self._gfs_conn_repo.list_active()
        released = 0
        for seat in seats:
            if self._in_grace(seat.gfs_instance_id, space_id):
                continue
            # Re-checked per seat, before anything is sent OR forgotten: a
            # row a local follower still wants is what a re-pair re-takes
            # (and what carries the pin anchor) — never drop it.
            if only_unwanted and await self._wants_seat(space_id):
                return released
            conn = next((c for c in active if _belongs(seat, c)), None)
            if conn is None and seat.detached and seat.released:
                # Unpaired, and the server confirmed the unsubscribe then.
                await self._seats.forget(space_id, seat.gfs_instance_id)
                continue
            if conn is None:
                log.info(
                    "gfs_space_mirror: seat of %s on server %s kept until it "
                    "reconnects (no matching active connection)",
                    space_id,
                    seat.gfs_instance_id,
                )
                continue
            if await self._release(space_id, conn):
                released += 1
        return released

    async def _release(self, space_id: str, conn: GfsConnection) -> bool:
        """Unsubscribe on *conn* (a server that seats us) and forget the
        seat. Fail-soft: a failure keeps the row for the next self-heal."""
        try:
            await self._gfs.unsubscribe_from_gfs_space(space_id, conn.id)
        except GfsConnectionError as exc:
            log.warning(
                "gfs_space_mirror: unsubscribe of %s from GFS %s failed: %s",
                space_id,
                conn.id,
                exc,
            )
            return False
        await self._seats.forget(space_id, conn.gfs_instance_id)
        return True

    async def on_disconnect(self, conn: GfsConnection) -> int:
        """An unpair of *conn*: its seats are kept but marked detached
        (local, inline — the unpair request waits on nothing else), and
        their unsubscribes go out in the background over a snapshot of the
        connection. A confirmed one marks the row released; a failed one
        leaves an unsubscribe-only tombstone that any later matching
        connection retries. A re-pair of the same server (same id, key and
        address) re-takes what a local user still wants. Returns how many
        seats were detached."""
        others = [c for c in await self._gfs_conn_repo.list_all() if c.id != conn.id]
        seats = [
            s
            for s in await self._seats.list_for_gfs(conn.gfs_instance_id)
            if _belongs(s, conn) and not any(_belongs(s, o) for o in others)
        ]
        now = _utcnow()
        at = _stamp(now) if now >= CLOCK_SANE_AFTER else None
        for seat in seats:
            await self._seats.mark_detached(seat.space_id, seat.gfs_instance_id, at=at)
        if seats:
            self._spawn(
                self._unsubscribe_detached(conn, [s.space_id for s in seats]),
                "unpair",
            )
        return len(seats)

    async def _unsubscribe_detached(
        self, conn: GfsConnection, space_ids: list[str]
    ) -> None:
        for space_id in space_ids:
            # Right before sending: a quick re-pair may have re-taken the
            # seat meanwhile (the row is live again) — then leave it be.
            seat = await self._seats.get(space_id, conn.gfs_instance_id)
            if seat is None or not seat.detached or seat.released:
                continue
            if seat.gfs_connection_id != conn.id:
                continue
            try:
                await self._gfs.unsubscribe_via(conn, space_id)
            except GfsConnectionError as exc:
                log.info(
                    "gfs_space_mirror: unpair — unsubscribe of %s from %s failed "
                    "(kept as a tombstone): %s",
                    space_id,
                    conn.id,
                    exc,
                )
                continue
            await self._seats.mark_released(space_id, conn.gfs_instance_id)

    async def sweep_orphan_seats(self) -> int:
        """Local housekeeping, no request.

        * A seat matching a paired connection is left to the reconnect
          self-heal (which re-takes or releases it).
        * One matching none that is not yet detached (a connection row gone
          without an unpair) is detached now — kept, unreleased.
        * A detached row is dropped only when its server confirmed the
          unsubscribe (``released``) and it is :data:`ORPHAN_SEAT_DAYS` old —
          and only on a sweep at least :data:`EXPIRY_CONFIRM_S` after the one
          that first saw it expired. Nothing is stamped or aged while the
          clock reads before :data:`CLOCK_SANE_AFTER`, and a stamp in the
          future (the clock went back) is reset. An unreleased row stays as
          an unsubscribe-only tombstone.
        * A seat whose server id and key reappear at a DIFFERENT address is
          not re-bound (a proof of possession, planned in a follow-up, can
          tell a move from an impostor): it is logged once per seat as
          needing a re-follow.

        Returns how many rows were dropped.
        """
        now = _utcnow()
        sane = now >= CLOCK_SANE_AFTER
        conns = await self._gfs_conn_repo.list_all()
        dropped = 0
        for seat in await self._seats.list_all():
            key = (seat.space_id, seat.gfs_instance_id)
            matching = next((c for c in conns if _belongs(seat, c)), None)
            if matching is not None:
                # An unsubscribe-only tombstone whose server is reachable
                # again (an unpair unsubscribe that failed or was dropped):
                # send it now. Wanted / live seats are the reconnect
                # self-heal's to re-take.
                if (
                    seat.detached
                    and not seat.released
                    and matching.status == "active"
                    and await self._releasable(*key)
                ):
                    await self._release(seat.space_id, matching)
                continue
            await self._warn_refollow(seat, conns)
            if not seat.detached:
                await self._seats.mark_detached(*key, at=_stamp(now) if sane else None)
                continue
            if not sane:
                continue
            detached_at = _parse_stamp(seat.detached_at)
            if detached_at is None or detached_at > now + timedelta(days=1):
                await self._seats.set_detached_at(*key, _stamp(now))
                continue
            if not seat.released:
                continue  # an unsubscribe-only tombstone
            if await self._wants_seat(seat.space_id):
                # A local user still follows it: it is what a re-pair
                # re-takes — never aged out.
                if seat.expiry_seen_at is not None:
                    await self._seats.set_expiry_seen(*key, None)
                continue
            if now - detached_at < timedelta(days=ORPHAN_SEAT_DAYS):
                if seat.expiry_seen_at is not None:
                    await self._seats.set_expiry_seen(*key, None)
                continue
            seen = _parse_stamp(seat.expiry_seen_at)
            if seen is None or seen > now:
                await self._seats.set_expiry_seen(*key, _stamp(now))
                continue
            if (now - seen).total_seconds() >= EXPIRY_CONFIRM_S:
                await self._seats.forget(*key)
                dropped += 1
        return dropped

    async def _warn_refollow(
        self, seat: GfsSpaceSeat, conns: list[GfsConnection]
    ) -> None:
        """One WARNING per seat (flag on the row) when its server's id
        reappears at another address, or under another key at the same
        address: neither is re-bound without the proof of possession planned
        in a follow-up, so the user has to re-follow."""
        if seat.refollow_warned:
            return
        moved = next((c for c in conns if _moved(seat, c)), None)
        rekeyed = next((c for c in conns if _rekeyed(seat, c)), None)
        if moved is None and rekeyed is None:
            return
        await self._seats.mark_refollow_warned(seat.space_id, seat.gfs_instance_id)
        if moved is not None:
            log.warning(
                "gfs_space_mirror: follow of %s on %s needs re-follow: "
                "server address changed (%s → %s)",
                seat.space_id,
                moved.display_name or seat.gfs_instance_id,
                _address(seat.gfs_inbox_url or ""),
                _address(moved.inbox_url),
            )
        else:
            assert rekeyed is not None
            log.warning(
                "gfs_space_mirror: follow of %s on %s needs re-follow: "
                "server key changed at %s",
                seat.space_id,
                rekeyed.display_name or seat.gfs_instance_id,
                _address(rekeyed.inbox_url),
            )

    async def _seats_on(self, conn: GfsConnection) -> list[GfsSpaceSeat]:
        """The recorded seats that belong to *conn*: same server id, key and
        URL. A seat recorded under the id but bound to another key / URL is
        not this server's — an impostor claiming the id gets nothing."""
        return [
            seat
            for seat in await self._seats.list_for_gfs(conn.gfs_instance_id)
            if _belongs(seat, conn)
        ]

    # ── Reconnect self-heal ──────────────────────────────────────────────

    async def resubscribe_all(self, gfs_id: str, *, also: Iterable[str] = ()) -> int:
        """Reconcile this household's seats on *gfs_id*. Returns the count the
        GFS accepted.

        ``SpaceService.subscribe_to_space`` POSTs ``/gfs/subscribe`` only on
        the FIRST-ever mirror (it returns early once a local member row
        exists), so a seat the GFS dropped is never re-taken: the household
        keeps showing "subscribed" while receiving nothing, forever. The GFS
        drops seats on its own — a publish that withdraws readability purges
        them — so the local row and the remote seat do drift apart in normal
        operation. Re-POSTing on every GFS-WS (re)connect closes that gap; the
        GFS's ``add_subscriber`` is an upsert, so a seat we already hold is a
        no-op.

        Only seats this server holds are re-taken: those recorded under its
        ``gfs_instance_id`` AND bound to its key and URL (so a genuine
        re-pair, which mints a new local connection id, keeps them, and an
        id-claiming impostor gets none), plus a pre-v44 follower mirror with
        no recorded seat anywhere that this server's whole directory lists
        (:meth:`_legacy_seated_on`). A signed, identity-bound subscribe sent
        anywhere else would disclose our interest in a space to an operator
        who never seated us. A recorded seat no local user wants any more
        (a leave that happened while this server was unreachable) is torn
        down here instead — re-checked right before each release.

        Fail-soft per space: this is a background self-heal, so one space's
        failure must never skip the rest or break the caller's reconnect
        sequence.

        ``also`` (v_50): further space ids to subscribe in the SAME batch —
        the spaces this household writes in (member auto-subscribe, already
        scoped to servers listing them). The merged batch is de-duplicated
        and SHUFFLED, so the order and timing of the identical signed
        subscribes never tell the server which seats are writers' and which
        are followers'.
        """
        also_ids = list(also)
        conn = await self._gfs_conn_repo.get(gfs_id)
        if conn is None:
            # Nothing is recorded or reconciled without the server's id —
            # but the member seats the caller asked for are still attempted.
            batch = list(dict.fromkeys(also_ids))
            secrets.SystemRandom().shuffle(batch)
            restored = 0
            for space_id in batch:
                if await self._subscribe_logged(space_id, gfs_id):
                    restored += 1
            return restored
        # First: carry the pin-heal anchor over a genuine re-pair — before
        # the re-take below rebinds the seats to this connection — and run
        # the local orphan-seat housekeeping.
        await self.rebind_mirrors(conn)
        await self.sweep_orphan_seats()
        keep: list[str] = []
        stale: list[str] = []
        recorded = await self._seats_on(conn)
        for seat in recorded:
            if await self._releasable(seat.space_id, conn.gfs_instance_id):
                stale.append(seat.space_id)
            else:
                keep.append(seat.space_id)
        legacy: list[str] = []
        for space_id in await self._spaces.list_subscribed_space_ids():
            if not await self.was_gfs_listed(space_id):
                continue
            if await self._legacy_seated_on(space_id, conn):
                legacy.append(space_id)
        batch = list(dict.fromkeys([*keep, *legacy, *also_ids]))
        secrets.SystemRandom().shuffle(batch)
        restored = 0
        for space_id in batch:
            if await self._subscribe_logged(space_id, gfs_id, conn=conn):
                restored += 1
        for space_id in stale:
            # Re-checked now: a subscribe may have landed meanwhile.
            if await self._releasable(space_id, conn.gfs_instance_id):
                await self._release(space_id, conn)
        return restored

    async def _subscribe_logged(
        self, space_id: str, gfs_id: str, *, conn: GfsConnection | None = None
    ) -> bool:
        try:
            if conn is not None:
                await self.take_seat(space_id, conn)
            else:
                await self._gfs.subscribe_to_gfs_space(space_id, gfs_id)
        except GfsConnectionError as exc:
            # A 403 is an EXPECTED outcome, not an incident: the space may
            # not live on this GFS at all, or its owner may have withdrawn
            # readability for good. Either way there is nothing to fix and
            # nothing an operator should act on — keep it out of the
            # warning log, which would otherwise fill up once per reconnect
            # per space. ``GfsConnectionError`` carries no status field, so
            # match the message ``subscribe_to_gfs_space`` formats.
            if "HTTP 403" in str(exc):
                log.debug(
                    "gfs_space_mirror: GFS %s refused re-subscribe of %s: %s",
                    gfs_id,
                    space_id,
                    exc,
                )
            else:
                log.warning(
                    "gfs_space_mirror: re-subscribe of %s on GFS %s failed: %s",
                    space_id,
                    gfs_id,
                    exc,
                )
            return False
        return True

    async def unsubscribe(self, space_id: str) -> None:
        """Best-effort removal from every server that seats us for
        *space_id* — the proven-mirror teardown of
        ``SpaceService._maybe_purge_gfs_mirror``.

        The (un)subscribe is signed and identity-bound, so sending it to a
        GFS that never seated us would tell that operator this household
        follows the space: the recorded seats are released
        (:meth:`release_seats`); with none recorded, only a pre-v44 mirror's
        directory fallback (:meth:`_legacy_seated_on`) is tried. The GFS
        treats an unknown unsubscribe as success (404 → idempotent). Errors
        are logged and swallowed — a GFS that is down must never block a
        local unsubscribe.
        """
        if await self._seats.list_for_space(space_id):
            await self.release_seats(space_id)
            return
        mirror_gfs, _ = await self._spaces.get_mirror_provenance(space_id)
        if mirror_gfs is not None:
            return  # v_44+: its seat was recorded; none left means none held
        # A pre-v44 mirror: finding its server means reading directories —
        # never on the unsubscribe request path. Read before the caller
        # purges the stub, so the background task needs no row.
        self._spawn(self._legacy_unsubscribe(space_id), "legacy-unsubscribe")

    async def _legacy_unsubscribe(self, space_id: str) -> None:
        for conn in await self._gfs_conn_repo.list_active():
            if not await self._legacy_listed(conn, space_id):
                continue
            try:
                await self._gfs.unsubscribe_from_gfs_space(space_id, conn.id)
            except GfsConnectionError as exc:
                log.warning(
                    "gfs_space_mirror: unsubscribe of %s from GFS %s failed: %s",
                    space_id,
                    conn.id,
                    exc,
                )

    async def _legacy_seated_on(self, space_id: str, conn: GfsConnection) -> bool:
        """Whether *conn* is where a pre-v44 follower mirror of *space_id*
        subscribed — only for a space with NO recorded seat on ANY server.

        A recorded seat anywhere means the household knows exactly where it
        is seated (the fallback must not add seats on further servers). A
        mirror carrying provenance (v_44, ``spaces.mirror_gfs_id``) had its
        seat recorded — by :meth:`take_seat`, or by the 0092 backfill when
        its connection still existed — so no recorded seat means it is gone
        or its connection was re-paired away; neither is knowable. Only a
        mirror with NO provenance falls back to *conn*'s WHOLE public
        directory (``GET /gfs/spaces`` — never a space-specific probe): a
        server that lists the space already knows it, and is where the
        subscribe went. Its first successful re-subscribe records the seat,
        so the fallback runs once per legacy mirror. Fail closed on an
        unreadable directory.
        """
        if await self._seats.list_for_space(space_id):
            return False
        mirror_gfs, _ = await self._spaces.get_mirror_provenance(space_id)
        if mirror_gfs is not None:
            return False
        return await self._legacy_listed(conn, space_id)

    async def _legacy_listed(self, conn: GfsConnection, space_id: str) -> bool:
        """*conn*'s whole directory lists *space_id* under an owner the
        admin did not block (downloads the directory when needed)."""
        owner = await self._directories.owner(conn, space_id)
        return owner is not None and not await self._owner_blocked(owner)

    async def rebind_mirrors(self, conn: GfsConnection) -> int:
        """Move the v_44 pin-heal anchor of every mirror seated from an
        earlier pairing of *conn*'s server onto *conn* (a disconnect +
        re-pair mints a new local connection id). Returns how many moved.

        No trust widening: only a mirror whose ``mirror_gfs_id`` is the
        connection its seat on this server was taken over, and only when
        *conn* has the same server id, pins the SAME key and answers at the
        SAME URL (:func:`_belongs`). Pairing reads id and key off an
        unauthenticated ``/gfs/info``, so the URL is what an impostor cannot
        copy. A re-pair under a different key or URL — or a mirror seated
        from another server, or one with no recorded seat — keeps its old
        anchor, and so never heals from this connection.
        """
        moved = 0
        for seat in await self._seats_on(conn):
            if seat.gfs_connection_id is None or seat.gfs_connection_id == conn.id:
                continue
            mirror_gfs, _ = await self._spaces.get_mirror_provenance(seat.space_id)
            if (
                mirror_gfs == seat.gfs_connection_id
                and await self._spaces.rebind_mirror_provenance(
                    seat.space_id,
                    from_gfs_id=seat.gfs_connection_id,
                    to_gfs_id=conn.id,
                )
            ):
                moved += 1
                log.info(
                    "gfs_space_mirror: mirror %s re-anchored to re-paired GFS %s",
                    seat.space_id,
                    conn.id,
                )
            # The seat now lives on this pairing (same server, key and URL).
            await self._seats.record(_seat_on(seat.space_id, conn))
        return moved

    # ── Reactive teardown ────────────────────────────────────────────────

    async def on_relay_frame(self, frame: dict, *, gfs_id: str) -> None:
        """Reactive teardown of a seat this household recorded on THIS
        server but no longer wants (a leave missed while it was reachable
        only through its relay): unsubscribe there, at most once per
        :data:`UNWANTED_RELAY_RETRY_S` per (server, space), in the
        background — never inline in the socket's read loop.

        Uniform and silent otherwise: a frame for a space this server holds
        no recorded seat of ours in (wanted or not, whatever other servers
        hold) does nothing, so a server cannot use made-up frames to ask
        "do you follow X". The supervisor calls this only for frames its
        consumer accepted without raising.
        """
        space_id = frame.get("space_id")
        if "channel_id" in frame or not isinstance(space_id, str):
            return
        if not _SAFE_SPACE_ID.fullmatch(space_id):
            return
        conn = await self._gfs_conn_repo.get(gfs_id)
        if conn is None or conn.status != "active":
            return
        key = (conn.gfs_instance_id, space_id)
        now = time.monotonic()
        last = self._unwanted_at.get(key)
        if last is not None and now - last < UNWANTED_RELAY_RETRY_S:
            return
        wanted_at = self._wanted_at.get(key)
        if wanted_at is not None and now - wanted_at < WANTED_CACHE_S:
            return  # a followed space's frames: one check per window
        seat = await self._seats.get(space_id, conn.gfs_instance_id)
        if seat is None or not _belongs(seat, conn):
            return
        # Cheap check inline; only a release goes to the background, so a
        # busy followed space never fills the task budget.
        if not await self._releasable(space_id, conn.gfs_instance_id):
            _remember(self._wanted_at, key)
            return
        _remember(self._unwanted_at, key)
        self._spawn(self._release_unwanted(space_id, conn), "reactive", quiet=True)

    async def _release_unwanted(self, space_id: str, conn: GfsConnection) -> None:
        if not await self._releasable(space_id, conn.gfs_instance_id):
            return
        log.info(
            "gfs_space_mirror: GFS %s relays space %s that no local user is "
            "seated in — unsubscribing there",
            conn.id,
            space_id,
        )
        await self._release(space_id, conn)


def _seat_on(space_id: str, conn: GfsConnection) -> GfsSpaceSeat:
    return GfsSpaceSeat(
        space_id=space_id,
        gfs_instance_id=conn.gfs_instance_id,
        gfs_connection_id=conn.id,
        gfs_public_key=conn.public_key,
        gfs_inbox_url=conn.inbox_url,
    )


def _utcnow() -> datetime:
    """The sweep's wall clock (a seam for tests)."""
    return datetime.now(timezone.utc)


def _stamp(at: datetime) -> str:
    """UTC in SQLite's ``datetime('now')`` form."""
    return at.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _parse_stamp(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%d %H:%M:%S").replace(
            tzinfo=timezone.utc
        )
    except ValueError:
        return None


_address = gfs_server_address


def _belongs(seat: GfsSpaceSeat, conn: GfsConnection) -> bool:
    """Whether *conn* is the server *seat* was taken on: same server id,
    same pinned key, same address (:func:`_address`). Missing binding → no."""
    return (
        seat.gfs_instance_id == conn.gfs_instance_id
        and seat.gfs_public_key is not None
        and seat.gfs_public_key == conn.public_key
        and seat.gfs_inbox_url is not None
        and _address(seat.gfs_inbox_url) == _address(conn.inbox_url)
    )


def _rekeyed(seat: GfsSpaceSeat, conn: GfsConnection) -> bool:
    """Same server id and address as the seat, under another key — a
    re-pair after a key change (or an impostor at that address)."""
    return (
        seat.gfs_instance_id == conn.gfs_instance_id
        and seat.gfs_inbox_url is not None
        and _address(seat.gfs_inbox_url) == _address(conn.inbox_url)
        and seat.gfs_public_key != conn.public_key
    )


def _moved(seat: GfsSpaceSeat, conn: GfsConnection) -> bool:
    """Same server id and key as the seat, at another address — a genuine
    move or an impostor; only a proof of possession (planned) can tell."""
    return (
        seat.gfs_instance_id == conn.gfs_instance_id
        and seat.gfs_public_key is not None
        and seat.gfs_public_key == conn.public_key
        and not _belongs(seat, conn)
    )


def _remember(
    store: dict[tuple[str, str], float],
    key: tuple[str, str],
    now: float | None = None,
) -> None:
    """Record *key* at *now* (default: the monotonic clock) in a map bounded
    at :data:`_MAX_TRACKED_SEATS`; entries past the longest window are
    pruned first, then the oldest."""
    if now is None:
        now = time.monotonic()
    if len(store) >= _MAX_TRACKED_SEATS:
        horizon = max(SEAT_GRACE_S, UNWANTED_RELAY_RETRY_S, WANTED_CACHE_S)
        for k, at in list(store.items()):
            if now - at >= horizon:
                del store[k]
        while len(store) >= _MAX_TRACKED_SEATS:
            del store[min(store, key=store.__getitem__)]
    store[key] = now
