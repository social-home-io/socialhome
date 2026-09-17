"""GFS connection management service (§24).

Handles pairing with Global Federation Servers, disconnecting, and
publishing / unpublishing spaces to paired GFS instances.

The pairing flow (simpler than HFS):
1. Admin scans GFS QR code → extracts ``{gfs_url, token}``.
2. Instance ``GET {gfs_url}/gfs/info`` to fetch the GFS's
   ``{gfs_instance_id, public_key}`` so it can pin them before
   trusting any future relay.
3. Instance POSTs to ``{gfs_url}/gfs/register`` with
   ``{token, instance_id (own), public_key (own), inbox_url,
   display_name}``.
4. GFS validates the token (single-use), registers the client,
   responds ``{status, instance_id}``.
5. Connection saved with ``status=active`` (or ``pending`` if the
   GFS requires admin approval).
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import uuid
from datetime import datetime, timezone

import aiohttp

from ..crypto import b64url_encode, sign_ed25519
from ..domain.federation import GfsConnection, GfsSpacePublication
from ..domain.space import normalize_category
from ..federation.keywrap_seal import KEM_SUITE_X25519
from ..repositories.gfs_connection_repo import AbstractGfsConnectionRepo

log = logging.getLogger(__name__)


class GfsConnectionError(Exception):
    """Raised when a GFS operation fails."""

    __slots__ = ()


class GfsConnectionService:
    """Service for managing GFS connections and space publications."""

    __slots__ = (
        "_repo",
        "_http_client",
        "_space_repo",
        "_theme_repo",
        "_cover_repo",
        "_icon_repo",
        "_own_instance_id",
        "_own_signing_key",
    )

    def __init__(
        self,
        repo: AbstractGfsConnectionRepo,
        *,
        http_client: aiohttp.ClientSession | None = None,
    ) -> None:
        self._repo = repo
        self._http_client = http_client
        # Attached lazily after construction (the space repo + identity
        # aren't available at the same wiring step as the GFS-connection
        # repo). When unset, ``publish_space`` falls back to a metadata-
        # less ``{space_id}`` body and the GFS lands a pending row.
        self._space_repo = None
        self._theme_repo = None
        self._cover_repo = None
        self._icon_repo = None
        self._own_instance_id = ""
        self._own_signing_key = b""

    def attach_publish_context(
        self,
        *,
        space_repo,
        own_instance_id: str,
        own_signing_key: bytes,
        theme_repo=None,
        cover_repo=None,
        icon_repo=None,
    ) -> None:
        """Wire the dependencies needed to ship full space metadata (and
        an Ed25519 signature) on publish. Optional — without it,
        ``publish_space`` no-ops the body, the GFS sits at
        ``status='pending'`` until an admin completes it manually.

        ``theme_repo`` / ``cover_repo`` / ``icon_repo`` let the publish
        body carry the space's real brand — theme colours + the cover and
        icon images as self-contained data URIs, so the GFS public page
        renders them on its own origin (no cross-origin / auth fetch)."""
        self._space_repo = space_repo
        self._own_instance_id = own_instance_id
        self._own_signing_key = own_signing_key
        self._theme_repo = theme_repo
        self._cover_repo = cover_repo
        self._icon_repo = icon_repo

    def attach_session(self, session: aiohttp.ClientSession) -> None:
        """Provide the shared aiohttp session after construction.

        Called from ``app._on_startup`` once the app-wide
        :class:`aiohttp.ClientSession` is available. Tests can inject a
        session at construction time via the ``http_client`` kwarg.
        """
        if self._http_client is None:
            self._http_client = session

    def _client(self) -> aiohttp.ClientSession:
        if self._http_client is None:
            raise RuntimeError(
                "GfsConnectionService used before attach_session — "
                "no aiohttp client wired",
            )
        return self._http_client

    async def pair(
        self,
        qr_payload: dict,
        *,
        own_instance_id: str,
        own_public_key_hex: str,
        own_inbox_url: str,
        own_display_name: str = "",
        own_keywrap_public_key_hex: str = "",
        own_keywrap_sig: str = "",
    ) -> GfsConnection:
        """Pair with a GFS using a scanned QR payload.

        ``qr_payload`` carries ``{gfs_url, token}`` — the QR no longer
        embeds the GFS's public key (it would bloat the QR for a value
        any client can pull from ``GET /gfs/info``). The own-identity
        fields come from the calling SH adapter so the GFS sees the
        registering household, not a generic blob.
        """
        gfs_url = str(qr_payload.get("gfs_url") or "").rstrip("/")
        token = str(qr_payload.get("token") or "")
        if not gfs_url or not token:
            raise GfsConnectionError(
                "gfs_url and token are required in the QR payload",
            )
        if not own_instance_id or not own_public_key_hex or not own_inbox_url:
            raise GfsConnectionError(
                "own_instance_id, own_public_key_hex, and own_inbox_url"
                " are required for GFS registration",
            )

        client = self._client()

        # 1. Fetch the GFS's public-key descriptor so we can pin it.
        info_url = f"{gfs_url}/gfs/info"
        try:
            async with client.get(
                info_url,
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                if resp.status != 200:
                    detail = await resp.text()
                    raise GfsConnectionError(
                        f"GFS /gfs/info failed (HTTP {resp.status}): {detail}",
                    )
                info = await resp.json()
        except aiohttp.ClientError as exc:
            raise GfsConnectionError(
                f"GFS unreachable while fetching /gfs/info: {exc}",
            ) from exc

        gfs_instance_id = str(info.get("gfs_instance_id") or "")
        gfs_public_key = str(info.get("public_key") or "")
        gfs_display_name = str(info.get("server_name") or gfs_url)
        if not gfs_instance_id or not gfs_public_key:
            raise GfsConnectionError(
                "GFS /gfs/info did not return gfs_instance_id and public_key",
            )

        # 2. Register the HFS instance using the QR token. Publish the local
        #    X25519 key-wrap pubkey + KEM suite (Phase 5b foundation) plus the
        #    identity's self-signature over that pubkey (``keywrap_sig``) so a
        #    future content-key handoff can seal to this household AND a remote
        #    sealer can bind the key-wrap key to this identity end-to-end (never
        #    trusting the GFS-served value). An HFS without a provisioned
        #    key-wrap key ships none → the GFS stores empty fields and this
        #    household just can't be sealed-to yet.
        register_body: dict[str, str] = {
            "token": token,
            "instance_id": own_instance_id,
            "public_key": own_public_key_hex,
            "inbox_url": own_inbox_url,
            "display_name": own_display_name,
        }
        if own_keywrap_public_key_hex:
            register_body["keywrap_public_key"] = own_keywrap_public_key_hex
            register_body["kem_suite"] = KEM_SUITE_X25519
            # The self-signature binding the key-wrap pubkey to this household's
            # identity (so a remote sealer verifies it end-to-end, never the
            # GFS-served value). Only meaningful alongside the pubkey it signs.
            if own_keywrap_sig:
                register_body["keywrap_sig"] = own_keywrap_sig
        register_url = f"{gfs_url}/gfs/register"
        try:
            async with client.post(
                register_url,
                json=register_body,
                timeout=aiohttp.ClientTimeout(total=15),
            ) as resp:
                if resp.status != 200:
                    detail = await resp.text()
                    raise GfsConnectionError(
                        f"GFS registration failed (HTTP {resp.status}): {detail}",
                    )
                body = await resp.json()
        except aiohttp.ClientError as exc:
            raise GfsConnectionError(
                f"GFS unreachable: {exc}",
            ) from exc

        # ``status`` is "registered" (auto-accepted) or "pending" (admin
        # review). Pending is still a recorded connection, just inert
        # until the GFS admin flips it.
        registration_status = str(body.get("status") or "registered")
        local_status = "active" if registration_status == "registered" else "pending"

        now = datetime.now(timezone.utc).isoformat()
        conn = GfsConnection(
            id=uuid.uuid4().hex,
            gfs_instance_id=gfs_instance_id,
            display_name=gfs_display_name,
            public_key=gfs_public_key,
            inbox_url=gfs_url,
            status=local_status,
            paired_at=now,
        )
        await self._repo.save(conn)
        return conn

    async def refresh_connection_metadata(self, gfs_id: str) -> None:
        """Re-fetch the GFS's current server_name from GET /gfs/info and
        update the stored display_name if it changed. Best-effort: a
        transport error / missing field is logged and ignored (the next
        reconnect retries). Called on each GFS WS (re)connect so a server
        rename propagates to this client."""
        conn = await self._repo.get(gfs_id)
        if conn is None:
            return
        try:
            client = self._client()
        except RuntimeError:
            return
        info_url = f"{conn.inbox_url}/gfs/info"
        try:
            async with client.get(
                info_url,
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                if resp.status != 200:
                    log.debug(
                        "GFS %s /gfs/info refresh returned HTTP %d — skipping",
                        gfs_id,
                        resp.status,
                    )
                    return
                info = await resp.json()
        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            log.debug(
                "GFS %s unreachable during name refresh: %s",
                gfs_id,
                exc,
            )
            return

        new_name = str(info.get("server_name") or "")
        if not new_name or new_name == conn.display_name:
            return
        log.info(
            "GFS %s renamed %r -> %r",
            gfs_id,
            conn.display_name,
            new_name,
        )
        await self._repo.update_display_name(gfs_id, new_name)

    async def disconnect(self, gfs_id: str) -> None:
        """Remove a GFS connection and all its publications."""
        conn = await self._repo.get(gfs_id)
        if conn is None:
            raise GfsConnectionError(f"GFS connection {gfs_id} not found")
        await self._repo.delete(gfs_id)

    async def list_connections(self) -> list[GfsConnection]:
        """Return ALL GFS connections for this household — active, pending,
        and suspended.

        The UI list distinguishes them by ``status`` and must surface a
        ``pending`` connection (GFS hasn't approved the household yet) or a
        ``suspended`` one; filtering to active-only made a freshly-connected
        GFS invisible until approval.
        """
        return await self._repo.list_all()

    async def publish_space(self, space_id: str, gfs_id: str) -> GfsSpacePublication:
        """Publish a space to a GFS.

        Builds the metadata payload from the local ``Space`` row, signs
        it with the household identity key, and POSTs to
        ``/gfs/spaces/{space_id}/publish`` so the GFS can list the space
        on ``GET /gfs/spaces``.

        The local publication row is recorded **only** on a successful
        GFS round-trip — there's no outbox/retry layer, so writing the
        row on a failed publish would make a lost publish look like a
        success. On a non-2xx response or a transport error this raises
        :class:`GfsConnectionError` (mapped to 422 by the route) and
        leaves no local row. The persisted ``status`` is whatever the
        GFS returned (``active`` / ``pending`` / ``banned``), defaulting
        to ``active`` when the body carries none.
        """
        conn = await self._repo.get(gfs_id)
        if conn is None:
            raise GfsConnectionError(f"GFS connection {gfs_id} not found")

        body = await self._build_publish_body(space_id)
        client = self._client()
        publish_url = f"{conn.inbox_url}/gfs/spaces/{space_id}/publish"
        try:
            async with client.post(
                publish_url,
                json=body,
                timeout=aiohttp.ClientTimeout(total=15),
            ) as resp:
                if resp.status not in (200, 201):
                    detail = await resp.text()
                    raise GfsConnectionError(
                        f"GFS rejected publish (HTTP {resp.status}): {detail}",
                    )
                try:
                    data = await resp.json()
                except Exception:
                    data = {}
                if not isinstance(data, dict):
                    data = {}
        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            raise GfsConnectionError(f"Could not reach GFS: {exc}") from exc

        status = data.get("status") or "active"
        return await self._repo.publish_space(space_id, gfs_id, status=status)

    async def _build_publish_body(self, space_id: str) -> dict:
        """Compose + sign the publish body.

        Fail-closed: the GFS now mandates an Ed25519 signature on every
        publish, so an instance with no signing key (publish context not
        wired) or no local space row to describe MUST NOT send an
        unsigned / metadata-less body — it raises :class:`GfsConnectionError`
        instead. A signed body is the only thing the GFS will accept.
        """
        if (
            self._space_repo is None
            or not self._own_instance_id
            or not self._own_signing_key
        ):
            raise GfsConnectionError(
                "cannot publish to GFS without a wired signing identity",
            )
        space = await self._space_repo.get(space_id)
        if space is None:
            raise GfsConnectionError(
                f"cannot publish unknown space {space_id!r} to GFS",
            )
        # Brand: the GFS public page is unauthenticated and on a different
        # origin, so a host-relative ``/api/spaces/{id}/cover`` path can't
        # load there. Ship the cover + icon as self-contained data URIs
        # (read straight from the blob repos) and the real theme colours, so
        # the page renders the space's brand on the GFS's own origin.
        primary, accent = await self._brand_colors(space_id)
        cover_uri = await self._image_data_uri(self._cover_repo, space)
        icon_uri = await self._image_data_uri(self._icon_repo, space, icon=True)
        body: dict = {
            "space_id": space.id,
            "owning_instance": self._own_instance_id,
            "name": space.name,
            "description": space.description or "",
            "about_markdown": getattr(space, "about_markdown", "") or "",
            "cover_url": cover_uri,
            "icon_url": icon_uri,
            "min_age": 0,
            "category": normalize_category(space.category),
            "accent_color": accent,
            "primary_color": primary,
            # Phase 5a: ship the space's Ed25519 authority verify key so the GFS
            # can TOFU-pin it on first publish and later authorize a
            # space-authority-signed relay from any seed-holder (owner or
            # delegated admin) without learning the space content. Inside the
            # already-signed canonical body — no new signing step.
            "identity_public_key": space.identity_public_key or "",
        }
        canonical = json.dumps(
            body,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        body["signature"] = b64url_encode(
            sign_ed25519(self._own_signing_key, canonical),
        )
        return body

    async def _brand_colors(self, space_id: str) -> tuple[str, str]:
        """The space's (primary, accent) theme colours, or the defaults."""
        primary, accent = "#D2542A", "#C8902F"
        if self._theme_repo is not None:
            theme = await self._theme_repo.get_space(space_id)
            if theme is not None:
                primary = theme.primary_color or primary
                accent = theme.accent_color or accent
        return primary, accent

    async def _image_data_uri(self, repo, space, *, icon: bool = False) -> str:
        """A ``data:image/webp;base64,…`` URI for the space's cover/icon, or
        ``""`` when none is set. Self-contained so the GFS page renders it
        without a cross-origin, auth-gated fetch back to the host."""
        has = getattr(space, "icon_hash" if icon else "cover_hash", None)
        if repo is None or not has:
            return ""
        got = await repo.get(space.id)
        if got is None:
            return ""
        webp, _hash = got
        return "data:image/webp;base64," + base64.b64encode(webp).decode("ascii")

    async def unpublish_space(self, space_id: str, gfs_id: str) -> None:
        """Unpublish a space from a GFS.

        The GFS authenticates the withdrawal (it is the owner's retraction of
        a public listing, not an anonymous delete), so this signs the
        canonical ``{action: "unpublish", owning_instance, space_id, ts}``
        body with the household identity key — ``action`` inside the signed
        bytes, so the signature can't be replayed as a subscribe — and ships
        ``{owning_instance, ts, signature}``. Fail-closed: with no signing
        identity wired it raises rather than send a body the GFS rejects.

        Symmetric with :meth:`publish_space`: the local row is removed
        **only** on a successful GFS round-trip. A ``404`` is treated as
        success — the space was already absent on the GFS, so the delete
        is idempotent. Any other non-2xx, or a transport error, raises
        :class:`GfsConnectionError` and keeps the local row (the GFS
        still believes the space is published).
        """
        if not self._own_instance_id or not self._own_signing_key:
            raise GfsConnectionError(
                "cannot unpublish a space without a wired signing identity",
            )
        conn = await self._repo.get(gfs_id)
        if conn is None:
            raise GfsConnectionError(f"GFS connection {gfs_id} not found")

        ts = datetime.now(timezone.utc).isoformat()
        canonical = json.dumps(
            {
                "action": "unpublish",
                "owning_instance": self._own_instance_id,
                "space_id": space_id,
                "ts": ts,
            },
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        body = {
            "owning_instance": self._own_instance_id,
            "ts": ts,
            "signature": b64url_encode(sign_ed25519(self._own_signing_key, canonical)),
        }

        client = self._client()
        unpublish_url = f"{conn.inbox_url}/gfs/spaces/{space_id}/unpublish"
        try:
            # POST, not DELETE: the GFS route accepts both identically, and
            # some proxies strip a DELETE request body — which would turn the
            # signed unpublish into a permanent 400.
            async with client.post(
                unpublish_url,
                json=body,
                timeout=aiohttp.ClientTimeout(total=15),
            ) as resp:
                if resp.status not in (200, 204, 404):
                    detail = await resp.text()
                    raise GfsConnectionError(
                        f"GFS rejected unpublish (HTTP {resp.status}): {detail}",
                    )
        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            raise GfsConnectionError(f"Could not reach GFS: {exc}") from exc

        await self._repo.unpublish_space(space_id, gfs_id)

    async def subscribe_to_gfs_space(self, space_id: str, gfs_id: str) -> str:
        """Subscribe this household to a GFS-listed space's relay fan-out.

        The GFS mandates an Ed25519 signature on every subscribe (so a
        caller can only subscribe itself), so this signs the canonical
        ``{action: "subscribe", instance_id, space_id, ts}`` body — the
        ``action`` is inside the signed bytes so the GFS can't have the
        signature replayed as an unsubscribe — with the household identity
        key and POSTs it to ``/gfs/subscribe``. Fail-closed: with no
        signing identity wired, it raises rather than sending an unsigned
        body the GFS would reject. Returns the GFS-reported status.
        """
        if not self._own_instance_id or not self._own_signing_key:
            raise GfsConnectionError(
                "cannot subscribe to a GFS space without a wired signing identity",
            )
        conn = await self._repo.get(gfs_id)
        if conn is None:
            raise GfsConnectionError(f"GFS connection {gfs_id} not found")

        ts = datetime.now(timezone.utc).isoformat()
        canonical = json.dumps(
            {
                "action": "subscribe",
                "instance_id": self._own_instance_id,
                "space_id": space_id,
                "ts": ts,
            },
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        body = {
            "action": "subscribe",
            "instance_id": self._own_instance_id,
            "space_id": space_id,
            "ts": ts,
            "signature": b64url_encode(sign_ed25519(self._own_signing_key, canonical)),
        }
        client = self._client()
        url = f"{conn.inbox_url}/gfs/subscribe"
        try:
            async with client.post(
                url,
                json=body,
                timeout=aiohttp.ClientTimeout(total=15),
            ) as resp:
                if resp.status not in (200, 201):
                    detail = await resp.text()
                    raise GfsConnectionError(
                        f"GFS rejected subscribe (HTTP {resp.status}): {detail}",
                    )
                try:
                    data = await resp.json()
                except Exception:
                    data = {}
                if not isinstance(data, dict):
                    data = {}
        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            raise GfsConnectionError(f"Could not reach GFS: {exc}") from exc
        return str(data.get("status") or "subscribed")

    async def publish_space_to_all(self, space_id: str) -> int:
        """Publish a space to every active GFS connection.

        Used by :class:`SpaceService` when a space flips to
        ``space_type=global``. The per-GFS :meth:`publish_space` now
        raises on failure; here a failing GFS is logged and skipped so
        one unreachable server doesn't abort the auto-publish fan-out.
        Returns the number of GFS instances the space was successfully
        published to.
        """
        conns = await self._repo.list_active()
        published = 0
        for conn in conns:
            try:
                await self.publish_space(space_id, conn.id)
                published += 1
            except GfsConnectionError as exc:
                log.warning(
                    "publish_space_to_all: failed for gfs %s: %s",
                    conn.id,
                    exc,
                )
        return published

    async def publish_space_event(
        self,
        *,
        space_id: str,
        event_type: str,
        payload: dict,
        from_instance: str,
    ) -> int:
        """Relay a single space-content event to a space's GFS subscribers.

        POSTs ``{space_id, event_type, payload, from_instance, signature}``
        to ``POST /gfs/publish`` on every GFS the space is published to.
        The ``payload`` is the caller-built wire envelope — for the Phase-5a2
        public-post relay it is the already-encrypted + authority-signed
        ``{space_id, epoch, encrypted_payload, authority_sig, ...}`` dict, so
        the GFS stays content-blind and authorizes the relay via the embedded
        space-authority signature (see :class:`SpacePublicOutbound`).

        ``signature`` is THIS household's Ed25519 *transport* signature over
        the canonical body — the GFS verifies it against the registered
        instance pubkey to authenticate ``from_instance`` (a separate concern
        from the content-authority signature inside ``payload``).

        Fail-closed: with no signing identity wired, nothing is sent (returns
        ``0``). A per-GFS transport/HTTP failure is logged and skipped so one
        unreachable server doesn't abort the fan-out. Returns the number of
        GFS instances the event was accepted by.
        """
        if self._http_client is None or not self._own_signing_key:
            log.warning(
                "publish_space_event: no signing identity wired — "
                "dropping relay for space %s",
                space_id,
            )
            return 0
        conns = await self._repo.list_gfs_for_space(space_id)
        if not conns:
            return 0
        body = {
            "space_id": space_id,
            "event_type": event_type,
            "payload": payload,
            "from_instance": from_instance,
        }
        canonical = json.dumps(
            body,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        body["signature"] = b64url_encode(
            sign_ed25519(self._own_signing_key, canonical),
        )
        delivered = 0
        for conn in conns:
            if conn.status != "active":
                continue
            url = f"{conn.inbox_url}/gfs/publish"
            try:
                async with self._http_client.post(
                    url,
                    json=body,
                    timeout=aiohttp.ClientTimeout(total=10),
                ) as resp:
                    if resp.status < 300:
                        delivered += 1
                    else:
                        log.warning(
                            "publish_space_event: GFS %s rejected relay "
                            "for space %s — HTTP %d",
                            conn.id,
                            space_id,
                            resp.status,
                        )
            except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
                log.warning(
                    "publish_space_event: GFS %s relay failed for space %s: %s",
                    conn.id,
                    space_id,
                    exc,
                )
        return delivered

    async def unpublish_space_from_all(self, space_id: str) -> int:
        """Unpublish a space from every GFS it was published to.

        Mirrors :meth:`publish_space_to_all`: a failing per-GFS
        unpublish is logged and skipped. Returns the number of GFS
        instances the space was successfully unpublished from.
        """
        conns = await self._repo.list_active()
        unpublished = 0
        for conn in conns:
            try:
                await self.unpublish_space(space_id, conn.id)
                unpublished += 1
            except GfsConnectionError as exc:
                log.warning(
                    "unpublish_space_from_all: failed for gfs %s: %s",
                    conn.id,
                    exc,
                )
        return unpublished

    def _build_instance_name_body(self, display_name: str) -> dict | None:
        """Build the signed ``/gfs/instance`` body for a household-name push.

        Returns ``None`` when there's no publish context wired (early boot /
        tests) — nothing to sign with, so the caller skips the push.
        """
        if not self._own_instance_id or not self._own_signing_key:
            return None
        ts = datetime.now(timezone.utc).isoformat()
        canonical = json.dumps(
            {
                "instance_id": self._own_instance_id,
                "display_name": display_name,
                "ts": ts,
            },
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        sig = b64url_encode(sign_ed25519(self._own_signing_key, canonical))
        return {
            "instance_id": self._own_instance_id,
            "display_name": display_name,
            "ts": ts,
            "signature": sig,
        }

    async def _post_instance_name(self, conn: GfsConnection, body: dict) -> bool:
        """POST a pre-signed name body to one GFS's ``/gfs/instance``.

        Best-effort: a 404 (old server, no endpoint), any other non-200, or a
        transport error is logged and turned into ``False`` — never raises.
        Returns ``True`` only on HTTP 200.
        """
        url = f"{conn.inbox_url}/gfs/instance"
        try:
            async with self._client().post(
                url,
                json=body,
                timeout=aiohttp.ClientTimeout(total=15),
            ) as resp:
                if resp.status == 200:
                    return True
                if resp.status == 404:
                    log.debug(
                        "GFS %s has no /gfs/instance (older server) —"
                        " skipping name sync",
                        conn.id,
                    )
                else:
                    log.warning(
                        "GFS %s rejected name sync (HTTP %d): %s",
                        conn.id,
                        resp.status,
                        await resp.text(),
                    )
        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            log.warning(
                "GFS %s unreachable during name sync: %s",
                conn.id,
                exc,
            )
        return False

    async def update_display_name_to_all(self, display_name: str) -> int:
        """Push the household's new display_name to every active GFS so the
        GFS-side client_instances row stays in sync. Best-effort: a GFS that's
        unreachable, errors, or is too old (404 — no /gfs/instance) is logged
        and skipped, never aborting the rename. Returns the success count."""
        body = self._build_instance_name_body(display_name)
        if body is None:
            # Early boot / tests without publish context wired — nothing
            # to sign with, so there's nothing to push.
            return 0
        updated = 0
        for conn in await self._repo.list_active():
            if await self._post_instance_name(conn, body):
                updated += 1
        return updated

    async def push_display_name(self, gfs_id: str, display_name: str) -> bool:
        """Re-push the household display_name to ONE GFS (reconnect self-heal).

        Used by the WS (re)connect hook so a GFS that missed an earlier rename
        (or re-created our client row) converges back to the current name.
        Best-effort: returns ``True`` only on HTTP 200; ``False`` for an
        unknown/inactive connection, a missing publish context, a non-200, or
        any transport error. Never raises.
        """
        body = self._build_instance_name_body(display_name)
        if body is None:
            return False
        conn = await self._repo.get(gfs_id)
        if conn is None or conn.status != "active":
            return False
        return await self._post_instance_name(conn, body)

    # ── Fraud report outbound ─────────────────────────────────────────

    async def report_fraud(
        self,
        gfs_id: str,
        *,
        target_type: str,
        target_id: str,
        category: str,
        notes: str | None,
        reporter_instance_id: str,
        reporter_user_id: str | None,
        signing_key: bytes,
    ) -> bool:
        """Sign + POST a fraud report to a single paired GFS.

        Returns ``True`` on a 2xx response, ``False`` on any failure
        (logged, never raised). Called by :class:`ReportService` in the
        background; the local report is always the source of truth.
        """
        import json
        from datetime import datetime, timezone

        from ..crypto import b64url_encode, sign_ed25519

        conn = await self._repo.get(gfs_id)
        if conn is None or conn.status != "active":
            return False

        body = {
            "target_type": target_type,
            "target_id": target_id,
            "category": category,
            "notes": notes,
            "reporter_instance_id": reporter_instance_id,
            "reporter_user_id": reporter_user_id,
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        canonical = json.dumps(body, separators=(",", ":"), sort_keys=True).encode(
            "utf-8"
        )
        body["signature"] = b64url_encode(
            sign_ed25519(signing_key, canonical),
        )

        try:
            client = self._client()
        except RuntimeError:
            # No HTTP session attached (test harness without network). Skip.
            return False
        url = f"{conn.inbox_url}/gfs/report"
        try:
            async with client.post(
                url,
                json=body,
                timeout=aiohttp.ClientTimeout(total=15),
            ) as resp:
                if 200 <= resp.status < 300:
                    return True
                log.warning(
                    "GFS report_fraud returned HTTP %d: %s",
                    resp.status,
                    await resp.text(),
                )
                return False
        except aiohttp.ClientError as exc:
            log.warning("GFS report_fraud request failed: %s", exc)
            return False

    async def send_appeal(
        self,
        gfs_id: str,
        *,
        target_type: str,
        target_id: str,
        message: str,
        from_instance: str,
        signing_key: bytes,
    ) -> bool:
        """Sign + POST an appeal to a GFS that banned us.

        Returns ``True`` on a 2xx response. Logs + drops on any failure.
        """
        import json

        from ..crypto import b64url_encode, sign_ed25519

        conn = await self._repo.get(gfs_id)
        if conn is None or conn.status != "active":
            return False

        body = {
            "target_type": target_type,
            "target_id": target_id,
            "message": message,
            "from_instance": from_instance,
        }
        canonical = json.dumps(
            body,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        body["signature"] = b64url_encode(sign_ed25519(signing_key, canonical))

        try:
            client = self._client()
        except RuntimeError:
            return False
        try:
            async with client.post(
                f"{conn.inbox_url}/gfs/appeal",
                json=body,
                timeout=aiohttp.ClientTimeout(total=15),
            ) as resp:
                if 200 <= resp.status < 300:
                    return True
                log.warning(
                    "GFS send_appeal returned HTTP %d: %s",
                    resp.status,
                    await resp.text(),
                )
                return False
        except aiohttp.ClientError as exc:
            log.warning("GFS send_appeal request failed: %s", exc)
            return False

    # ── Sync-signaling round-robin (spec §24.10.7) ────────────────────────

    async def _first_active_gfs(self) -> GfsConnection | None:
        """Return the first ``status='active'`` GFS connection, or ``None``.

        v1 picks any active GFS; multi-GFS deployments will route per
        space in a follow-up.
        """
        conns = await self._repo.list_active()
        return conns[0] if conns else None

    async def request_signaling_node(
        self,
        sync_id: str,
        *,
        from_instance: str,
        signing_key: bytes,
    ) -> str | None:
        """Ask the paired GFS for a least-loaded signaling node URL.

        Returns the URL the SH provider should embed in
        ``SPACE_SYNC_OFFER`` as ``signaling_node`` (spec §24.10.7).
        Returns ``None`` for any of:

        * No active GFS connection (HFS-only deployment, or none paired).
        * GFS replied ``signaling_node: null`` (single-node mode).
        * GFS replied ``503 {reason: "node_capacity"}`` (S-8 cap hit) —
          the caller should still send the OFFER without the field; the
          requester will fall back to its connected node and ICE may
          ultimately fail with ``DIRECT_FAILED``, which is the expected
          back-pressure path.
        * Any transport error (logged + treated as "no signaling node").
        """
        conn = await self._first_active_gfs()
        if conn is None:
            return None
        try:
            client = self._client()
        except RuntimeError:
            return None
        body = {"from_instance": from_instance, "sync_id": sync_id}
        canonical = json.dumps(body, separators=(",", ":"), sort_keys=True).encode(
            "utf-8"
        )
        body["signature"] = b64url_encode(sign_ed25519(signing_key, canonical))

        url = f"{conn.inbox_url}/cluster/signaling-session"
        try:
            async with client.post(
                url,
                json=body,
                timeout=aiohttp.ClientTimeout(total=5),
            ) as resp:
                if resp.status == 503:
                    return None
                if resp.status != 200:
                    log.warning(
                        "GFS signaling-session returned HTTP %d",
                        resp.status,
                    )
                    return None
                payload = await resp.json()
                node = payload.get("signaling_node")
                return str(node) if node else None
        except aiohttp.ClientError as exc:
            log.debug("GFS signaling-session request failed: %s", exc)
            return None

    async def release_signaling_node(
        self,
        sync_id: str,
        signaling_node: str,
        *,
        from_instance: str,
        signing_key: bytes,
    ) -> None:
        """Decrement the GFS-side counter for a previously-picked node.

        Called when ``SPACE_SYNC_DIRECT_READY`` or
        ``SPACE_SYNC_DIRECT_FAILED`` fires. Idempotent on the GFS side —
        safe to call twice. Errors are logged + swallowed; failing to
        release leaves a stale counter at most until the GFS restarts.
        """
        if not signaling_node:
            return
        conn = await self._first_active_gfs()
        if conn is None:
            return
        try:
            client = self._client()
        except RuntimeError:
            return
        body = {
            "from_instance": from_instance,
            "sync_id": sync_id,
            "signaling_node": signaling_node,
        }
        canonical = json.dumps(body, separators=(",", ":"), sort_keys=True).encode(
            "utf-8"
        )
        body["signature"] = b64url_encode(sign_ed25519(signing_key, canonical))

        url = f"{conn.inbox_url}/cluster/signaling-session/release"
        try:
            async with client.post(
                url,
                json=body,
                timeout=aiohttp.ClientTimeout(total=5),
            ) as resp:
                if resp.status >= 400:
                    log.debug(
                        "GFS signaling-session release HTTP %d",
                        resp.status,
                    )
        except aiohttp.ClientError as exc:
            log.debug("GFS signaling-session release failed: %s", exc)
