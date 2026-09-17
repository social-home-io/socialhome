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
GFS or any other — can never move a pin once seated.

Consequences, stated plainly:

* A household that pairs with a **hostile GFS** can be served a fabricated
  space whose authority key that GFS controls, and would then accept relayed
  "space content" signed by it. The blast radius is confined to spaces
  discovered *through that GFS*; every other trust path — direct paired
  peers, §D1b invites, owned spaces — is unaffected, and an already-pinned
  space cannot be hijacked.
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

import aiohttp

from ..domain.space import Space
from ..repositories.gfs_connection_repo import AbstractGfsConnectionRepo
from ..repositories.space_repo import AbstractSpaceRepo
from .gfs_connection_service import GfsConnectionError, GfsConnectionService
from .space_service import can_seat_remote_stub, stub_space_from_metadata

log = logging.getLogger(__name__)

#: Length in hex characters of an Ed25519 public key (32 raw bytes).
_PIN_HEX_LEN = 64


class GfsSpaceMirrorService:
    """Seats + tears down local stub rows for GFS-discovered spaces."""

    __slots__ = ("_spaces", "_gfs_conn_repo", "_gfs", "_http_client")

    def __init__(
        self,
        *,
        space_repo: AbstractSpaceRepo,
        gfs_connection_repo: AbstractGfsConnectionRepo,
        gfs_connection_service: GfsConnectionService,
    ) -> None:
        self._spaces = space_repo
        self._gfs_conn_repo = gfs_connection_repo
        self._gfs = gfs_connection_service
        self._http_client: aiohttp.ClientSession | None = None

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
                    timeout=aiohttp.ClientTimeout(total=15),
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
                    body = await resp.json()
            except (aiohttp.ClientError, asyncio.TimeoutError, OSError) as exc:
                log.warning("gfs_space_mirror: fetch failed for %s: %s", url, exc)
                continue
            if not isinstance(body, dict):
                log.warning("gfs_space_mirror: %s returned a non-object body", url)
                continue

            meta = self._validated_metadata(space_id, body, gfs_id=conn.id)
            if meta is None:
                continue
            owning_instance = str(body.get("owning_instance") or "")
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
            space = stub_space_from_metadata(
                space_id,
                host_instance_id=owning_instance,
                meta=meta,
            )
            await self._spaces.save(space)
            log.info(
                "gfs_space_mirror: seated stub for space %s from GFS %s",
                space_id,
                conn.id,
            )
            return space, conn.id
        return None

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
        if str(body.get("status") or "") != "active":
            log.warning(
                "gfs_space_mirror: GFS %s lists space %s as %r — not mirroring",
                gfs_id,
                space_id,
                body.get("status"),
            )
            return None
        if not str(body.get("owning_instance") or ""):
            log.warning(
                "gfs_space_mirror: GFS %s listing for %s has no owning_instance",
                gfs_id,
                space_id,
            )
            return None
        pin = str(body.get("identity_public_key") or "")
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
            "name": body.get("name") or "Untitled space",
            "description": body.get("description"),
            "about_markdown": body.get("about_markdown"),
            "identity_public_key": pin,
            # A GFS mirror is a *subscription* stub, never a locally joinable
            # space: joining still goes through
            # ``POST /api/public_spaces/{id}/join-request``.
            "space_type": "global",
            "join_mode": "invite_only",
            # The owning household's local username means nothing here (the
            # GFS listing carries no such field) — leave it empty.
            "owner_username": "",
            # Clamped to the allowed set inside ``stub_space_from_metadata``.
            "min_age": body.get("min_age"),
            "category": body.get("category"),
        }

    async def subscribe_to_gfs(self, space_id: str, gfs_id: str) -> None:
        """Register this household on *gfs_id*'s subscriber set for *space_id*.

        A thin seam over :meth:`GfsConnectionService.subscribe_to_gfs_space`
        so ``SpaceService.subscribe_to_space`` can sequence
        mirror → local refusals → GFS subscribe, and a locally-refused user
        (banned / under-age) never reaches the GFS.
        """
        await self._gfs.subscribe_to_gfs_space(space_id, gfs_id)

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
