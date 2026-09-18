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

Relaying space content (:meth:`GfsConnectionService.publish_space_event`) is
**identity-free by default**: a connection server must not learn WHICH
household relayed a public/global-space event. ``GET /gfs/info`` is the
capability channel for the GFS↔HFS leg (it has no ``proto_version``
negotiation), so a GFS advertising ``anonymous_publish`` receives
``{space_id, event_type, payload}`` and authorizes the relay purely on the
space-authority signature inside the opaque payload. A GFS that did not
advertise it — an older build, or one whose ``/gfs/info`` was unreachable —
still gets the legacy identified body, once per connection with a WARNING:
unknown → legacy is the safe default, because the legacy body is accepted by
both an old and a new GFS while the identity-free one would 403 on an old one.

That fallback is also the attack surface, so the capability must be
*authenticated*, not merely read:

* **Only a signed block counts.** The capability is trusted only when
  ``capabilities`` + ``capabilities_sig`` + ``capabilities_sig_suite`` verify
  against the GFS identity key this household pinned at pair time
  (:mod:`socialhome.capabilities_sig`). The bare top-level
  ``anonymous_publish`` mirror is informational — acting on it would let an
  on-path attacker strip the flag and force the legacy body, whose household
  transport signature is a third-party-provable "household X relayed into
  space Y" artefact.
* **The cache ratchets up.** Once verified, a later fetch without the block
  does not downgrade it for the rest of the process — a GFS cannot lose a
  capability its build has.
* **https:// at pair time.** A GFS URL must be ``https://`` unless its host is
  loopback / RFC1918 / link-local, so the pinning fetch itself can't be
  rewritten on-path.
"""

from __future__ import annotations

import asyncio
import base64
import ipaddress
import json
import logging
import time
import uuid
from datetime import datetime, timezone
from urllib.parse import urlparse

import aiohttp

from ..capabilities_sig import UnsupportedCapsSigSuite, verify_capabilities
from ..crypto import b64url_encode, sign_ed25519
from ..domain.federation import GfsConnection, GfsSpacePublication
from ..domain.space import normalize_category, normalize_join_mode
from ..federation.keywrap_seal import KEM_SUITE_X25519
from ..repositories.gfs_connection_repo import AbstractGfsConnectionRepo
from ..repositories.space_repo import AbstractSpaceRepo

log = logging.getLogger(__name__)

#: Characters of a *remote-authored* error body that may be interpolated
#: into a :class:`GfsConnectionError`. That message travels to the SPA as
#: the 502 ``GFS_UNAVAILABLE`` detail (``routes/base.py``), so an unbounded
#: body would let a hostile GFS author arbitrary — and arbitrarily long —
#: copy inside the household's own error toast. The full body still reaches
#: the operator, in the log.
MAX_REMOTE_DETAIL_CHARS = 200

#: Bytes read from a remote error body at all. Beyond this even the log
#: line is not worth the memory.
_REMOTE_DETAIL_READ_BYTES = 8192

#: How long a FAILED ``GET /gfs/info`` probe suppresses the next one, in
#: seconds. The answer itself is still never cached as ``False`` — the privacy
#: reasoning holds: an unreachable descriptor must not downgrade a household
#: to the identified relay body for the rest of the process. This TTL only
#: stops the *stall*: a GFS whose ``/gfs/info`` is down while ``/gfs/publish``
#: is up otherwise cost a full 10 s connect timeout on EVERY publish. Short
#: enough that a GFS coming back is picked up within seconds.
GFS_INFO_NEGATIVE_TTL_S: float = 30.0


async def _remote_detail(resp, *, context: str) -> str:
    """Read a GFS error body for display, bounded and truncated.

    Returns at most :data:`MAX_REMOTE_DETAIL_CHARS` characters. Anything
    longer is logged in full (up to :data:`_REMOTE_DETAIL_READ_BYTES`) and
    elided in the returned string — never trust a remote peer with the text
    a local user reads.
    """
    try:
        raw = await resp.content.read(_REMOTE_DETAIL_READ_BYTES)
    except Exception as exc:  # pragma: no cover - transport-level failure
        log.debug("GFS %s: could not read the error body: %s", context, exc)
        return ""
    body = raw.decode("utf-8", "replace") if isinstance(raw, bytes) else str(raw)
    if len(body) <= MAX_REMOTE_DETAIL_CHARS:
        return body
    log.warning(
        "GFS %s returned a %d-char error body (truncated for display): %s",
        context,
        len(body),
        body,
    )
    return body[:MAX_REMOTE_DETAIL_CHARS] + "… (truncated)"


class GfsConnectionError(Exception):
    """Raised when a GFS operation fails.

    ``status`` is the upstream HTTP status when there was one, and
    ``None`` when the failure happened before an answer (unreachable,
    timeout, TLS). Callers that turn this into a user-facing message map
    on the status CLASS rather than reading ``str(exc)``: the message
    holds the server's own words, which are useful in a log and useless
    — sometimes misleading — in a toast.
    """

    __slots__ = ("status",)

    def __init__(self, message: str, *, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


def _is_private_host(host: str) -> bool:
    """Whether *host* is loopback / link-local / RFC1918-private.

    A literal IP is classified by :mod:`ipaddress`; the bare name
    ``localhost`` counts as loopback. Everything else — every DNS name — is
    treated as public. DNS is deliberately NOT resolved: a resolver answer is
    attacker-influenced and would turn a TLS check into a rebinding oracle.
    """
    name = host.strip("[]").lower()
    if name in {"localhost", "localhost."}:
        return True
    try:
        addr = ipaddress.ip_address(name)
    except ValueError:
        return False
    return bool(addr.is_loopback or addr.is_private or addr.is_link_local)


def _require_secure_url(url: str, *, field: str) -> None:
    """Reject a GFS-facing URL that is neither ``https://`` nor LAN-local.

    ``GET /gfs/info`` carries the signed capability block and the TOFU-pinned
    GFS public key; ``POST /gfs/publish`` carries space content. Over plain
    ``http://`` on the public internet an on-path attacker can strip the
    capability block (forcing every relay back to the identified body, which
    carries a household-signed "household X relayed into space Y" artefact)
    or swap the pinned key on the first fetch. A LAN / loopback GFS — how the
    federation demo harness and most home deployments run — keeps plain HTTP:
    there is no public path to sit on and usually no certificate to serve.

    Raises :class:`GfsConnectionError`, which the pairing route maps to a 4xx
    with this message.
    """
    parsed = urlparse(url)
    scheme = parsed.scheme.lower()
    if scheme == "https":
        return
    if scheme == "http" and _is_private_host(parsed.hostname or ""):
        return
    raise GfsConnectionError(
        f"{field} must use https:// — {url!r} is not. Plain http:// is only "
        "allowed for a GFS on loopback or a private network (RFC1918, "
        "fc00::/7, fe80::/10, localhost).",
    )


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
        "_anon_publish",
        "_anon_warned",
        "_caps_warned",
        "_info_failed_at",
        "_envelope_relay",
        "_invite_links",
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
        self._space_repo: AbstractSpaceRepo | None = None
        self._theme_repo = None
        self._cover_repo = None
        self._icon_repo = None
        self._own_instance_id = ""
        self._own_signing_key = b""
        # Per-connection GFS capability cache, RAM-only and deliberately
        # NOT persisted: ``anonymous_publish`` is a property of the REMOTE
        # server's build, not of this household's data, so a column would
        # go stale the moment an operator upgrades their GFS. It is
        # (re)learned from the SIGNED capability block on ``GET /gfs/info``
        # at pair time and on every GFS-WS (re)connect, plus once on demand
        # when a publish finds it unknown. Missing key = unknown → the legacy
        # body, which BOTH an old and a new GFS accept, so the safe default
        # can never strand a household. Once ``True`` under a verified
        # signature the entry RATCHETS (see :meth:`_apply_capability`).
        self._anon_publish: dict[str, bool] = {}
        # Connections already warned about the privacy downgrade — one
        # WARNING per connection per process, not one per publish.
        self._anon_warned: set[str] = set()
        # Same one-shot discipline for the capability-block warning (missing
        # block / failed verification / unknown suite): a household publishing
        # fifty times must not emit fifty copies.
        self._caps_warned: set[str] = set()
        # ``time.monotonic()`` of the last FAILED ``/gfs/info`` probe per
        # connection — the negative TTL (:data:`GFS_INFO_NEGATIVE_TTL_S`).
        # Cleared on the next successful fetch.
        self._info_failed_at: dict[str, float] = {}
        # Same RAM-only, ratchet-up discipline as ``_anon_publish``, for
        # the ``envelope_relay`` capability the §D2b invite bootstrap
        # needs (:class:`socialhome.services.gfs_envelope_sender
        # .GfsEnvelopeSender`). A build cannot lose the capability, so a
        # verified ``True`` sticks for the process; anything else stays
        # unknown and is re-probed on the next attempt rather than
        # cached as a permanent "no".
        self._envelope_relay: dict[str, bool] = {}
        # Same discipline again, for the ``invite_links`` capability
        # (``POST /gfs/spaces/{id}/invite`` + the public ``/join`` page).
        self._invite_links: dict[str, bool] = {}

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

    def client(self) -> aiohttp.ClientSession:
        """The shared session, for the sibling services that relay through a
        GFS (:mod:`socialhome.services.gfs_envelope_sender`).

        Handing out this one session — rather than letting each service
        build its own — keeps every GFS-facing call on the same connection
        pool and the same lifecycle as the rest of the app.
        """
        return self._client()

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
        # Transport check BEFORE the first byte leaves: over public plain
        # HTTP the signed capability block below can be stripped on-path and
        # the TOFU key swapped, so there is nothing to pin. LAN / loopback is
        # still fine (the demo harness pairs ``http://127.0.0.1:<port>``).
        _require_secure_url(gfs_url, field="gfs_url")
        _require_secure_url(own_inbox_url, field="own_inbox_url")

        client = self._client()

        # 1. Fetch the GFS's public-key descriptor so we can pin it.
        info_url = f"{gfs_url}/gfs/info"
        try:
            async with client.get(
                info_url,
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                if resp.status != 200:
                    detail = await _remote_detail(resp, context="/gfs/info")
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
                    detail = await _remote_detail(resp, context="/gfs/register")
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
        # Seed the capability cache from the descriptor we already fetched, so
        # the very first relay to a freshly-paired GFS is identity-free without
        # a second round-trip. The block is verified against the key from that
        # SAME response — the one being pinned right now (TOFU, exactly as the
        # key itself is trusted); an unsigned or bad block seeds ``False``.
        self._record_capabilities(conn, info)
        return conn

    async def _fetch_gfs_info(self, conn: GfsConnection) -> dict | None:
        """``GET {conn.inbox_url}/gfs/info``, or ``None`` on any failure.

        Never raises — every caller is on a best-effort path (a reconnect
        hook or a relay fan-out). A successful fetch refreshes the RAM-only
        ``anonymous_publish`` capability cache for *conn* (from the SIGNED
        block only, see :meth:`_record_capabilities`); a failure records only
        a short retry suppression (:data:`GFS_INFO_NEGATIVE_TTL_S`), never a
        capability answer, so one blip can't downgrade this household's
        privacy for the rest of the process.
        """
        try:
            client = self._client()
        except RuntimeError:
            return None
        info_url = f"{conn.inbox_url}/gfs/info"
        try:
            async with client.get(
                info_url,
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                if resp.status != 200:
                    log.debug(
                        "GFS %s /gfs/info returned HTTP %d — skipping",
                        conn.id,
                        resp.status,
                    )
                    self._info_failed_at[conn.id] = time.monotonic()
                    return None
                info = await resp.json()
        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            log.debug(
                "GFS %s unreachable during /gfs/info fetch: %s",
                conn.id,
                exc,
            )
            self._info_failed_at[conn.id] = time.monotonic()
            return None
        if not isinstance(info, dict):
            self._info_failed_at[conn.id] = time.monotonic()
            return None
        self._info_failed_at.pop(conn.id, None)
        self._record_capabilities(conn, info)
        return info

    def _record_capabilities(self, conn: GfsConnection, info: dict) -> None:
        """Update the capability cache for *conn* from a ``/gfs/info`` body.

        The bare top-level ``anonymous_publish`` flag is IGNORED: it rides an
        unauthenticated endpoint, so acting on it would let an on-path
        attacker strip it and force every relay back to the identified legacy
        body — the household-signed, third-party-provable artefact the
        anonymous relay exists to avoid. Only the signed block counts.
        """
        self._apply_capability(conn, self._verified_anonymous_publish(conn, info))

    def _verified_anonymous_publish(self, conn: GfsConnection, info: dict) -> bool:
        """Whether *info* carries a VALID capability block granting anonymous
        publish, verified against the GFS key pinned for *conn*.

        Returns ``False`` — with one WARNING per connection — for a missing
        block, a bad signature, or a suite this build can't verify. The
        warning text distinguishes them so an operator can tell "old GFS"
        from "someone is rewriting my /gfs/info".
        """
        caps = self._verified_capabilities(conn, info)
        return caps is not None and caps.get("anonymous_publish") is True

    def _verified_capabilities(self, conn: GfsConnection, info: dict) -> dict | None:
        """The capability block from *info*, or ``None`` if it isn't trustworthy.

        One verification for every capability this household reads off
        ``/gfs/info`` — the signature discipline (and its warnings) must not
        drift between ``anonymous_publish`` and later additions such as
        ``envelope_relay``.
        """
        caps = info.get("capabilities")
        sig = info.get("capabilities_sig")
        suite = info.get("capabilities_sig_suite")
        if (
            not isinstance(caps, dict)
            or not isinstance(sig, str)
            or not sig
            or not isinstance(suite, str)
        ):
            self._warn_capabilities(
                conn,
                "served no signed capability block on /gfs/info — it is "
                "either an older build or its response was stripped in "
                "transit",
            )
            return None
        try:
            ok = verify_capabilities(
                conn.public_key, conn.gfs_instance_id, caps, sig, suite
            )
        except UnsupportedCapsSigSuite:
            self._warn_capabilities(
                conn,
                f"signed its capability block with the unknown suite {suite!r},"
                " which this build cannot verify",
            )
            return None
        if not ok:
            self._warn_capabilities(
                conn,
                "served a capability block that FAILED verification against "
                "the pinned key — that is tampering or a key mismatch, not "
                "an old build",
            )
            return None
        return caps

    def _apply_capability(self, conn: GfsConnection, verified: bool) -> None:
        """Write the verified capability into the cache, ratcheting UP only.

        A GFS cannot legitimately lose ``anonymous_publish`` — the capability
        is a property of its build, and builds don't travel backwards. So once
        a connection has been seen advertising it under a VALID signature,
        a later fetch that lacks it is an attack (or a broken proxy) and is
        ignored for the rest of the process rather than silently downgrading
        every future relay to the identified body. The ratchet is RAM-only:
        a restart legitimately starts from "unknown" again.
        """
        if verified:
            self._anon_publish[conn.id] = True
            return
        if self._anon_publish.get(conn.id) is True:
            log.warning(
                "GFS %r (%s) previously advertised anonymous_publish under a "
                "valid signature and no longer does — capability downgrade "
                "ignored, relays stay identity-free. A connection server "
                "cannot lose this capability, so suspect tampering or a "
                "proxy rewriting /gfs/info.",
                conn.display_name,
                conn.inbox_url,
            )
            return
        self._anon_publish[conn.id] = False

    def _warn_capabilities(self, conn: GfsConnection, detail: str) -> None:
        """WARN once per connection about an untrusted capability block.

        Names the connection (label + URL) and the consequence, never the
        space or its content — an operator needs to know WHICH server to look
        at, not what was posted to it.
        """
        if conn.id in self._caps_warned:
            return
        self._caps_warned.add(conn.id)
        log.warning(
            "GFS %r (%s) %s. Relays to it keep carrying this household's "
            "instance id until a verifiable capability block appears.",
            conn.display_name,
            conn.inbox_url,
            detail,
        )

    async def _anonymous_publish_supported(self, conn: GfsConnection) -> bool:
        """Whether *conn*'s GFS proved ``anonymous_publish`` on /gfs/info.

        Answers from the cache when it is warm (filled at pair time and on
        every WS reconnect). On a cold miss — the first publish after a boot
        that hasn't seen a reconnect yet — probe ``/gfs/info`` ONCE rather
        than spuriously downgrading to the identified body. An unreachable
        GFS answers ``False`` for this publish only, and its failure is
        suppressed for :data:`GFS_INFO_NEGATIVE_TTL_S` so a burst of publishes
        doesn't pay the connect timeout each time.
        """
        cached = self._anon_publish.get(conn.id)
        if cached is not None:
            return cached
        failed_at = self._info_failed_at.get(conn.id)
        if failed_at is not None and time.monotonic() - failed_at < (
            GFS_INFO_NEGATIVE_TTL_S
        ):
            return False
        await self._fetch_gfs_info(conn)
        return self._anon_publish.get(conn.id, False)

    async def envelope_relay_supported(self, conn: GfsConnection) -> bool:
        """Whether *conn*'s GFS proved ``envelope_relay`` on /gfs/info.

        The §D2b invite bootstrap hands a connection server an opaque
        household-to-household blob (``POST /gfs/envelope``). A server that
        doesn't carry those has no such route, so the sender asks here first
        and fails the redeem with a sentence a human can act on instead of a
        404 behind a ten-second timeout.
        """
        return await self._signed_capability_supported(
            conn,
            "envelope_relay",
            self._envelope_relay,
        )

    async def invite_links_supported(self, conn: GfsConnection) -> bool:
        """Whether *conn*'s GFS proved ``invite_links`` on /gfs/info.

        Gates :meth:`publish_invite` / :meth:`revoke_invite`: an older
        connection server has no ``/gfs/spaces/{id}/invite`` route and no
        ``/join`` page, so minting there would hand the owner a link that
        404s for everyone they send it to.
        """
        return await self._signed_capability_supported(
            conn,
            "invite_links",
            self._invite_links,
        )

    async def _signed_capability_supported(
        self,
        conn: GfsConnection,
        name: str,
        cache: dict[str, bool],
    ) -> bool:
        """Whether *conn*'s GFS proved capability *name* under a VALID signature.

        Same shape as :meth:`_anonymous_publish_supported`: answered from the
        RAM cache when warm, probed once on a cold miss, and suppressed for
        :data:`GFS_INFO_NEGATIVE_TTL_S` after an unreachable probe. Only a
        ``True`` under a valid signature is cached — an unsigned or stripped
        block is "unknown", re-probed next time, never a sticky "no". One
        implementation for every per-capability gate so a new one cannot
        accidentally trust the unsigned top-level flag.
        """
        if cache.get(conn.id):
            return True
        failed_at = self._info_failed_at.get(conn.id)
        if failed_at is not None and time.monotonic() - failed_at < (
            GFS_INFO_NEGATIVE_TTL_S
        ):
            return False
        info = await self._fetch_gfs_info(conn)
        if info is None:
            return False
        caps = self._verified_capabilities(conn, info)
        if caps is None or caps.get(name) is not True:
            return False
        cache[conn.id] = True
        return True

    async def refresh_connection_metadata(self, gfs_id: str) -> None:
        """Re-fetch the GFS descriptor from GET /gfs/info and refresh what
        this household caches about that server.

        Two things ride the descriptor: the operator-settable ``server_name``
        (persisted as the connection's ``display_name`` when it changed) and
        the signed ``anonymous_publish`` capability (RAM-only — see
        :meth:`_fetch_gfs_info`). Best-effort: a transport error / missing
        field is logged and ignored (the next reconnect retries). Called on
        each GFS WS (re)connect, so an operator's rename — and an operator's
        GFS upgrade — both propagate to this client.
        """
        conn = await self._repo.get(gfs_id)
        if conn is None:
            return
        info = await self._fetch_gfs_info(conn)
        if info is None:
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
                    detail = await _remote_detail(resp, context="publish")
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

        The body carries a signed ``ts`` so the GFS can replay-guard it — the
        publish that restores an owner-withdrawn listing must be fresh.
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
            # How people become MEMBERS. Shown on the directory listing so a
            # browser can say "open to join" / "ask to join" / "invite only";
            # it says nothing about readability. Inside the already-signed
            # canonical body — no new signing step, and a relay can't flip it
            # in transit.
            "join_mode": normalize_join_mode(
                getattr(space, "join_mode", None),
            ),
            # The readability opt-in. OFF ⇒ this listing is discoverable but
            # not publicly readable: the GFS refuses ``POST /gfs/subscribe``
            # for it and purges any seat it already had. Signed alongside
            # ``join_mode`` so a relay cannot flip a private space open.
            "allow_subscribers": bool(space.features.allow_subscribers),
            "accent_color": accent,
            "primary_color": primary,
            # Phase 5a: ship the space's Ed25519 authority verify key so the GFS
            # can TOFU-pin it on first publish and later authorize a
            # space-authority-signed relay from any seed-holder (owner or
            # delegated admin) without learning the space content. Inside the
            # already-signed canonical body — no new signing step.
            "identity_public_key": space.identity_public_key or "",
            # A signed, tz-aware timestamp inside the canonical body: it makes
            # this publish a FRESH statement of intent, replay-guarded ±300 s
            # on the GFS side. Only a publish carrying one may clear an
            # earlier owner withdrawal — without it a captured body could
            # re-list a space its owner deliberately delisted. The GFS still
            # accepts a body without ``ts`` (older households keep publishing
            # and refreshing metadata); see ``GfsFederationService.publish_space``.
            "ts": datetime.now(timezone.utc).isoformat(),
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
                    detail = await _remote_detail(resp, context="unpublish")
                    raise GfsConnectionError(
                        f"GFS rejected unpublish (HTTP {resp.status}): {detail}",
                    )
        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            raise GfsConnectionError(f"Could not reach GFS: {exc}") from exc

        await self._repo.unpublish_space(space_id, gfs_id)

    async def publish_invite(
        self,
        space_id: str,
        gfs_id: str,
        blob: str,
        expires_at: int,
    ) -> tuple[str, str]:
        """Park an invite *blob* for *space_id* on *gfs_id*'s bulletin board.

        Returns ``(gfs_token, url)`` — the URL is the shareable
        ``{base}/join/{token}`` page that renders the blob back as a
        ``socialhome://invite#…`` code for a visitor to copy into their own
        Social Home.

        *blob* is this household's business: the connection server never
        parses it, and it MUST NOT contain a household address — the page is
        served to anyone holding the link. Signed like
        :meth:`unpublish_space`, over the canonical ``{action: "mint_invite",
        owning_instance, space_id, ts}`` body, so the ``action`` is inside the
        signed bytes and a captured mint signature can't be replayed as a
        revoke (or a publish, or a subscribe).

        Capability-gated: a server whose SIGNED ``/gfs/info`` block lacks
        ``invite_links`` has no such route, so this raises with a sentence an
        operator can act on rather than minting a link that 404s for every
        person it is sent to.
        """
        if not self._own_instance_id or not self._own_signing_key:
            raise GfsConnectionError(
                "cannot mint an invite without a wired signing identity",
            )
        conn = await self._repo.get(gfs_id)
        if conn is None:
            raise GfsConnectionError(f"GFS connection {gfs_id} not found")
        if not await self.invite_links_supported(conn):
            raise GfsConnectionError(
                "this connection server can't host invite links yet",
            )

        ts = datetime.now(timezone.utc).isoformat()
        body = {
            "owning_instance": self._own_instance_id,
            "blob": blob,
            "expires_at": int(expires_at),
            "ts": ts,
            "signature": self._sign_invite_action(
                "mint_invite",
                space_id=space_id,
                ts=ts,
            ),
        }
        client = self._client()
        url = f"{conn.inbox_url}/gfs/spaces/{space_id}/invite"
        try:
            async with client.post(
                url,
                json=body,
                timeout=aiohttp.ClientTimeout(total=15),
            ) as resp:
                if resp.status not in (200, 201):
                    detail = await _remote_detail(resp, context="invite")
                    raise GfsConnectionError(
                        f"GFS rejected invite (HTTP {resp.status}): {detail}",
                        status=resp.status,
                    )
                try:
                    data = await resp.json()
                except Exception:
                    data = {}
                if not isinstance(data, dict):
                    data = {}
        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            raise GfsConnectionError(f"Could not reach GFS: {exc}") from exc

        gfs_token = str(data.get("gfs_token") or "")
        invite_url = str(data.get("url") or "")
        if not gfs_token or not invite_url:
            # Without both there is nothing to share and nothing to revoke
            # later — fail loudly rather than hand the caller a half-answer.
            raise GfsConnectionError("GFS returned no invite token")
        return gfs_token, invite_url

    async def revoke_invite(
        self,
        space_id: str,
        gfs_id: str,
        gfs_token: str,
    ) -> None:
        """Take one invite link down. Idempotent.

        Signed over the canonical ``{action: "revoke_invite", gfs_token,
        owning_instance, space_id, ts}`` body — both the action AND the token
        are inside the signed bytes, so a mint signature can't be replayed as
        a revoke and a revoke can't be redirected at another token.

        ``200`` / ``204`` / ``404`` are all success: the link is gone either
        way, and a revoke that races a sweep or a second revoke must not
        surface as an error. Unlike :meth:`publish_invite` this does NOT
        capability-gate — a server that never had the route also never has the
        link, so refusing here would strand an owner trying to clean up after
        a downgrade.
        """
        if not self._own_instance_id or not self._own_signing_key:
            raise GfsConnectionError(
                "cannot revoke an invite without a wired signing identity",
            )
        conn = await self._repo.get(gfs_id)
        if conn is None:
            raise GfsConnectionError(f"GFS connection {gfs_id} not found")

        ts = datetime.now(timezone.utc).isoformat()
        body = {
            "owning_instance": self._own_instance_id,
            "ts": ts,
            "signature": self._sign_invite_action(
                "revoke_invite",
                space_id=space_id,
                ts=ts,
                gfs_token=gfs_token,
            ),
        }
        client = self._client()
        url = f"{conn.inbox_url}/gfs/spaces/{space_id}/invite/{gfs_token}"
        try:
            # POST, not DELETE: the GFS route accepts both identically, and
            # some proxies strip a DELETE request body — which would turn the
            # signed revoke into a permanent 400.
            async with client.post(
                url,
                json=body,
                timeout=aiohttp.ClientTimeout(total=15),
            ) as resp:
                if resp.status not in (200, 204, 404):
                    detail = await _remote_detail(resp, context="invite revoke")
                    raise GfsConnectionError(
                        f"GFS rejected invite revoke (HTTP {resp.status}): {detail}",
                        status=resp.status,
                    )
        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            raise GfsConnectionError(f"Could not reach GFS: {exc}") from exc

    def _sign_invite_action(
        self,
        action: str,
        *,
        space_id: str,
        ts: str,
        gfs_token: str | None = None,
    ) -> str:
        """Sign the canonical body for an invite *action*.

        ONE place builds the signed bytes for both invite verbs, so the
        ``action`` discriminator and the token binding can't drift apart
        between mint and revoke — the drift being exactly what would let one
        signature be replayed as the other.
        """
        payload: dict[str, object] = {
            "action": action,
            "owning_instance": self._own_instance_id,
            "space_id": space_id,
            "ts": ts,
        }
        if gfs_token is not None:
            payload["gfs_token"] = gfs_token
        canonical = json.dumps(
            payload,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        return b64url_encode(sign_ed25519(self._own_signing_key, canonical))

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
                    detail = await _remote_detail(resp, context="subscribe")
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

    async def unsubscribe_from_gfs_space(self, space_id: str, gfs_id: str) -> str:
        """Unsubscribe this household from a GFS-listed space's relay fan-out.

        Mirror of :meth:`subscribe_to_gfs_space`: the GFS mandates an
        Ed25519 signature on every (un)subscribe (so a caller can only
        unsubscribe **itself** — without it any household could evict any
        other from a space's relay), so this signs the canonical
        ``{action: "unsubscribe", instance_id, space_id, ts}`` body — the
        ``action`` is inside the signed bytes so the signature can't be
        replayed as a subscribe — and POSTs it to ``/gfs/subscribe``.
        Fail-closed: with no signing identity wired it raises rather than
        send an unsigned body the GFS would reject with a 403.

        ``404`` counts as success alongside ``200``/``204`` — mirroring
        :meth:`unpublish_space`, removing an already-absent subscription is
        idempotent. Returns the GFS-reported status.
        """
        if not self._own_instance_id or not self._own_signing_key:
            raise GfsConnectionError(
                "cannot unsubscribe from a GFS space without a wired signing identity",
            )
        conn = await self._repo.get(gfs_id)
        if conn is None:
            raise GfsConnectionError(f"GFS connection {gfs_id} not found")

        ts = datetime.now(timezone.utc).isoformat()
        canonical = json.dumps(
            {
                "action": "unsubscribe",
                "instance_id": self._own_instance_id,
                "space_id": space_id,
                "ts": ts,
            },
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        body = {
            "action": "unsubscribe",
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
                if resp.status not in (200, 204, 404):
                    detail = await _remote_detail(resp, context="unsubscribe")
                    raise GfsConnectionError(
                        f"GFS rejected unsubscribe (HTTP {resp.status}): {detail}",
                    )
                try:
                    data = await resp.json()
                except Exception:
                    data = {}
                if not isinstance(data, dict):
                    data = {}
        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            raise GfsConnectionError(f"Could not reach GFS: {exc}") from exc
        return str(data.get("status") or "unsubscribed")

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

        POSTs to ``POST /gfs/publish`` on every GFS the space is published to.
        The ``payload`` is the caller-built wire envelope — for the Phase-5a2
        public-post relay it is the already-encrypted + authority-signed
        ``{space_id, epoch, encrypted_payload, authority_sig, ...}`` dict, so
        the GFS stays content-blind and authorizes the relay via the embedded
        space-authority signature (see :class:`SpacePublicOutbound`).

        **The body shape depends on what the GFS advertised.** A GFS whose
        ``GET /gfs/info`` carries ``anonymous_publish: true`` authorizes the
        relay on that embedded space-authority signature ALONE, so it gets the
        identity-free body — exactly ``{space_id, event_type, payload}``, with
        no ``from_instance`` and no household transport signature. That is the
        point of the change: a connection server must not learn WHICH
        household relayed a public/global-space event.

        A GFS that did not advertise it (an older build, or one whose
        ``/gfs/info`` we couldn't reach) still gets the legacy
        ``{space_id, event_type, payload, from_instance, signature}`` body,
        where ``signature`` is THIS household's Ed25519 *transport* signature
        over the canonical body. Unknown → legacy is the safe default: the
        legacy body is accepted by BOTH an old and a new GFS, while the
        identity-free one would 403 on an old server. The privacy downgrade is
        logged once per connection (:meth:`_warn_identified_publish`).

        *from_instance* is therefore only used for the legacy body; on the
        anonymous path it is never serialized.

        Fail-closed: with no signing identity wired, nothing is sent (returns
        ``0`` — the legacy fallback would be unsignable, and a household with
        no identity has nothing to relay). A per-GFS transport/HTTP failure is
        logged and skipped so one unreachable server doesn't abort the
        fan-out. Returns the number of GFS instances the event was accepted by.
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
        anonymous_body = {
            "space_id": space_id,
            "event_type": event_type,
            "payload": payload,
        }
        legacy_body: dict | None = None
        delivered = 0
        for conn in conns:
            if conn.status != "active":
                continue
            if await self._anonymous_publish_supported(conn):
                body = anonymous_body
            else:
                self._warn_identified_publish(conn)
                if legacy_body is None:
                    legacy_body = self._legacy_publish_body(
                        space_id,
                        event_type,
                        payload,
                        from_instance,
                    )
                body = legacy_body
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

    def _legacy_publish_body(
        self,
        space_id: str,
        event_type: str,
        payload: dict,
        from_instance: str,
    ) -> dict:
        """The pre-anonymous-publish relay body, byte-for-byte as before.

        ``{space_id, event_type, payload, from_instance}`` plus this
        household's Ed25519 transport ``signature`` over their canonical JSON.
        Only sent to a GFS that did not advertise ``anonymous_publish`` — that
        server can't authorize the relay without it.
        """
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
        return body

    def _warn_identified_publish(self, conn: GfsConnection) -> None:
        """Warn ONCE per connection per process about the privacy downgrade.

        A household posting fifty times must not emit fifty warnings, so the
        connection id lands in ``_anon_warned`` on the first one. The message
        names the connection (label + URL) and deliberately nothing about the
        space or its content — an operator needs to know WHICH server to
        upgrade, not what was posted to it.
        """
        if conn.id in self._anon_warned:
            return
        self._anon_warned.add(conn.id)
        log.warning(
            "GFS %r (%s) does not advertise anonymous_publish — relays to it "
            "still carry this household's instance id, so that connection "
            "server learns which household relayed each public-space event. "
            "Ask its operator to upgrade.",
            conn.display_name,
            conn.inbox_url,
        )

    async def heal_space_pins(self, gfs_id: str) -> int:
        """Re-publish every space this household published to *gfs_id*.

        Run on each GFS-WS (re)connect. ``POST /gfs/publish`` now authorizes
        purely on the space's TOFU-pinned authority key, so a GFS row that
        pinned NO key (published by a household predating the pin) rejects
        every relay for that space with 403 until the metadata is published
        again — and nothing else re-publishes it. Re-publishing is idempotent
        on the GFS side (the pin is COALESCE-guarded and immutable once set),
        so this heals a NULL pin without disturbing a healthy one.

        Skipped, at DEBUG (both are expected, not faults): a space whose local
        row is gone, and one this household holds no seed for — the pin must
        come from a seed-holder, and a household with no KEK wired can't read
        a seed at all. Sequential and fail-soft per space: this runs on a
        connect hook, so one unreachable GFS or one rejected publish must never
        raise out of it. Returns how many spaces were re-published.
        """
        if self._space_repo is None:
            return 0
        try:
            conn = await self._repo.get(gfs_id)
            if conn is None or conn.status != "active":
                return 0
            publications = await self._repo.list_publications(gfs_id)
        except Exception:
            log.exception("gfs.heal_space_pins: lookup failed for gfs %s", gfs_id)
            return 0
        healed = 0
        for pub in publications:
            if not await self._holds_space_seed(pub.space_id):
                continue
            try:
                await self.publish_space(pub.space_id, gfs_id)
                healed += 1
            except GfsConnectionError as exc:
                log.info(
                    "gfs.heal_space_pins: re-publishing space %s to gfs %s failed: %s",
                    pub.space_id,
                    gfs_id,
                    exc,
                )
            except Exception:
                log.exception(
                    "gfs.heal_space_pins: re-publishing space %s to gfs %s failed",
                    pub.space_id,
                    gfs_id,
                )
        return healed

    async def _holds_space_seed(self, space_id: str) -> bool:
        """Whether this household can author a metadata publish for *space_id*.

        True only when the local space row still exists AND this household
        holds the space's Ed25519 seed (owner or delegated admin). Never
        raises: ``get_space_seed`` raises :class:`RuntimeError` when no
        household KEK is wired, which is a "can't check → skip", not a fault.
        """
        repo = self._space_repo
        if repo is None:
            return False
        try:
            space = await repo.get(space_id)
        except Exception:
            log.debug(
                "gfs.heal_space_pins: space lookup failed for %s — skipping",
                space_id,
            )
            return False
        if space is None:
            log.debug(
                "gfs.heal_space_pins: no local row for space %s — skipping",
                space_id,
            )
            return False
        try:
            seed = await repo.get_space_seed(space_id)
        except RuntimeError:
            log.debug(
                "gfs.heal_space_pins: no key manager wired — cannot read the "
                "seed for space %s, skipping",
                space_id,
            )
            return False
        except Exception:
            log.debug(
                "gfs.heal_space_pins: seed lookup failed for space %s — skipping",
                space_id,
            )
            return False
        if seed is None:
            log.debug(
                "gfs.heal_space_pins: no seed held for space %s — skipping",
                space_id,
            )
            return False
        return True

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
                        await _remote_detail(resp, context="name sync"),
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
                    await _remote_detail(resp, context="report_fraud"),
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
                    await _remote_detail(resp, context="send_appeal"),
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
