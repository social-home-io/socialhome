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
import time

import aiohttp

from ..domain.space import Space, normalize_join_mode
from ..repositories.gfs_connection_repo import AbstractGfsConnectionRepo
from ..repositories.public_space_repo import AbstractPublicSpaceRepo
from ..repositories.space_repo import AbstractSpaceRepo
from .gfs_connection_service import GfsConnectionError, GfsConnectionService
from .gfs_http import MAX_GFS_BODY_BYTES, read_json_capped
from ..authority_cert import MAX_AUTHORITY_KEY_EPOCH
from ..domain.space import PUBLIC_SPACE_TIERS, SpaceRole
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
#: small metadata GET, never a bulk transfer, and ``ensure_mirror`` walks
#: every active GFS connection **serially** (first hit wins, which keeps the
#: ordering deterministic and the code simple). The worst case therefore
#: bounds a single ``POST /api/spaces/{id}/subscribe`` at
#: ``_MIRROR_FETCH_TIMEOUT_S × len(active GFS connections)`` of held request
#: slot — and any authenticated local user can drive that against an
#: arbitrary unknown id, so the per-connection budget stays small rather
#: than the 15 s used for the interactive GFS calls.
_MIRROR_FETCH_TIMEOUT_S = 5.0

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
    )

    def __init__(
        self,
        *,
        space_repo: AbstractSpaceRepo,
        gfs_connection_repo: AbstractGfsConnectionRepo,
        gfs_connection_service: GfsConnectionService,
        public_space_repo: AbstractPublicSpaceRepo | None = None,
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
        it, returning ``(seated_space, gfs_connection_id)``.

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
        for conn in await self._gfs_conn_repo.list_active():
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

        The household caches every directory poll into
        ``public_space_cache`` (:class:`PublicSpaceDiscoveryService`), and
        that table has exactly one writer — the GFS poll — so a row there is
        positive evidence that the space came off a GFS directory rather
        than, say, a peer-discovered public/global stub.

        Used as the teardown guard: destructive or GFS-visible work on a
        space we cannot prove is a mirror is skipped. Answers ``False``
        whenever the evidence is absent *or* unavailable (no directory repo
        wired), which is the fail-safe direction.
        """
        if self._public_spaces is None:
            return False
        return await self._public_spaces.get(space_id) is not None

    async def subscribe_to_gfs(self, space_id: str, gfs_id: str) -> None:
        """Register this household on *gfs_id*'s subscriber set for *space_id*.

        A thin seam over :meth:`GfsConnectionService.subscribe_to_gfs_space`
        so ``SpaceService.subscribe_to_space`` can sequence
        mirror → local refusals → GFS subscribe, and a locally-refused user
        (banned / under-age) never reaches the GFS.
        """
        await self._gfs.subscribe_to_gfs_space(space_id, gfs_id)

    async def resubscribe_all(self, gfs_id: str) -> int:
        """Re-register every local subscription on *gfs_id*. Returns the count
        the GFS accepted.

        ``SpaceService.subscribe_to_space`` POSTs ``/gfs/subscribe`` only on
        the FIRST-ever mirror (it returns early once a local member row
        exists), so a seat the GFS dropped is never re-taken: the household
        keeps showing "subscribed" while receiving nothing, forever. The GFS
        drops seats on its own — a publish that withdraws readability purges
        them — so the local row and the remote seat do drift apart in normal
        operation. Re-POSTing on every GFS-WS (re)connect closes that gap; the
        GFS's ``add_subscriber`` is an upsert, so a seat we already hold is a
        no-op.

        Scoped by mirror provenance (:meth:`was_gfs_listed`) for the same
        reason :meth:`SpaceService._maybe_purge_gfs_mirror` is: a public/global
        stub learned from a direct peer is nobody's mirror, and a signed,
        identity-bound subscribe would disclose our interest in a space to a
        GFS operator who never knew it existed.

        Fail-soft per space: this is a background self-heal, so one space's
        failure must never skip the rest or break the caller's reconnect
        sequence.
        """
        restored = 0
        for space_id in await self._spaces.list_subscribed_space_ids():
            if not await self.was_gfs_listed(space_id):
                continue
            try:
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
                continue
            restored += 1
        return restored

    async def unsubscribe(self, space_id: str) -> None:
        """Best-effort removal from every paired GFS's subscriber set.

        We don't record which GFS a subscription came from, and the GFS treats
        an unknown unsubscribe as success (404 → idempotent), so this fans out
        to every active connection. A :class:`GfsConnectionError` is logged and
        swallowed — a GFS that is down must never block a local unsubscribe.
        """
        for conn in await self._gfs_conn_repo.list_active():
            try:
                await self._gfs.unsubscribe_from_gfs_space(space_id, conn.id)
            except GfsConnectionError as exc:
                log.warning(
                    "gfs_space_mirror: unsubscribe of %s from GFS %s failed: %s",
                    space_id,
                    conn.id,
                    exc,
                )
