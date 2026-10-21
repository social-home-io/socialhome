"""GFS connection management service (§24).

Handles pairing with Global Federation Servers, disconnecting, and
publishing / unpublishing spaces to paired GFS instances.

The pairing flow (simpler than HFS):
1. Admin scans GFS QR code → extracts ``{gfs_url, token}``.
2. Instance ``GET {gfs_url}/gfs/info`` to fetch the GFS's
   ``{gfs_instance_id, public_key}`` so it can pin them before
   trusting any future relay.
3. Instance POSTs to ``{gfs_url}/gfs/register`` with
   ``{token, instance_id (own), public_key (own), display_name}`` (plus
   the key-wrap fields when provisioned). No household address: the GFS
   relays over the WebSocket the household opens, so it never needs one.
4. GFS validates the token (single-use), registers the client,
   responds ``{status, instance_id}``.
5. Connection saved with ``status=active`` (or ``pending`` if the
   GFS requires admin approval).

Relaying space content (:meth:`GfsConnectionService.publish_space_event`) is
**always identity-free**: a connection server must not learn WHICH household
relayed a public/global-space event. ``GET /gfs/info`` is the capability
channel for the GFS↔HFS leg (it has no ``proto_version`` negotiation), so a
GFS proving ``anonymous_publish`` receives ``{space_id, event_type,
payload}`` and authorizes the relay purely on the space-authority signature
inside the opaque payload. There is no identified fallback any more: a GFS
whose ``/gfs/info`` is unreachable gets the publish later, from the retry
queue, once it proves the capability; a GFS that answers without it (an
older build) gets no space publishes at all until it upgrades, with one
WARNING per connection.

The capability is *authenticated*, not merely read — otherwise an on-path
attacker could fake it toward a GFS that cannot authorize the anonymous
body, or strip it to deny the relay:

* **Only a signed block counts.** The capability is trusted only when
  ``capabilities`` + ``capabilities_sig`` + ``capabilities_sig_suite`` verify
  against the GFS identity key this household pinned at pair time
  (:mod:`socialhome.capabilities_sig`). The bare top-level
  ``anonymous_publish`` mirror is informational and never acted on.
* **The cache ratchets up.** Once verified, a later fetch without the block
  does not downgrade it for the rest of the process — a GFS cannot lose a
  capability its build has.
* **https:// at pair time.** A GFS URL must be ``https://`` unless its host is
  loopback / RFC1918 / link-local, so the pinning fetch itself can't be
  rewritten on-path.

The descriptor is *static*, so it proves nothing about the server answering
it: anyone can replay a real GFS's ``/gfs/info`` from another address. A GFS
whose signed block offers ``proof_of_possession`` therefore also has to sign a
fresh nonce with its KEY, bound to the URL this household used
(:mod:`socialhome.gfs_proof`, ``POST /gfs/proof``), before the household
registers. The first successful proof sets a persisted ratchet
(``gfs_connections.proof_required_at``): from then on every GFS-WS (re)connect
runs the identity-bound reconcile only after a fresh proof
(:meth:`GfsConnectionService.possession_gate`).
:meth:`GfsConnectionService.verify_gfs_possession` is the same check for
callers that must re-bind to a GFS at a new address.
"""

from __future__ import annotations

import base64
import hmac
import json
import logging
import time
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager, nullcontext
from datetime import UTC, datetime
from typing import Literal

import aiohttp

from ..capabilities_sig import UnsupportedCapsSigSuite, verify_capabilities
from ..crypto import b64url_encode, ed25519_public_key, sign_ed25519
from ..domain.errors import SpaceNotPublishableError, TierChangeRequiredError
from ..domain.federation import GfsConnection, GfsSpacePublication
from ..domain.gfs_server import GfsServer
from ..domain.media_constraints import (
    SPACE_IMAGE_DATA_URI_PREFIX,
    SPACE_IMAGE_EMBED_MAX_BYTES,
)
from ..domain.space import (
    SpaceType,
    normalize_category,
    normalize_join_mode,
)
from ..domain.space_place import place_pin_of, place_pin_wire
from ..federation.keywrap_seal import KEM_SUITE_X25519
from ..gfs_proof import (
    PROOF_CAPABILITY,
    PROOF_ROUTE,
    UnsupportedProofSigSuite,
    new_proof_nonce,
    normalize_gfs_base_url,
    verify_possession_proof,
)
from ..outbound_fetch import OutboundFetchRefused, open_pinned_session
from ..peer_url import InvalidPeerUrlError, validate_peer_url
from ..repositories.gfs_connection_repo import AbstractGfsConnectionRepo
from ..repositories.space_repo import AbstractSpaceRepo
from .gfs_http import gfs_server_address
from .gfs_publish_retry import (
    GfsPublish,
    GfsPublishRetryQueue,
    PublishOutcome,
    classify_publish_status,
)
from .gfs_server_targets import refusal_outcome, resolve_server_targets
from .space_authority_pin import owner_authority_cert

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
#: seconds. The answer itself is never cached as ``False``: an unreachable
#: descriptor is "unknown", so publishes wait in the retry queue instead of
#: being skipped for the rest of the process. This TTL only stops the
#: *stall*: a GFS whose ``/gfs/info`` is down otherwise cost a full 10 s
#: connect timeout on EVERY publish. Short enough that a GFS coming back is
#: picked up within seconds.
GFS_INFO_NEGATIVE_TTL_S: float = 30.0

#: At most one on-demand ``/gfs/info`` refresh per connection this often
#: (:meth:`GfsConnectionService.refresh_if_stale`).
GFS_INFO_REFRESH_DEBOUNCE_S: float = 60.0


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
        log.debug(
            "GFS %s: could not read the error body: %s", context, exc, exc_info=True
        )
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


def _require_secure_url(url: str, *, field: str) -> None:
    """Reject a GFS-facing URL that is neither ``https://`` nor LAN-local.

    ``GET /gfs/info`` carries the signed capability block and the TOFU-pinned
    GFS public key; ``POST /gfs/publish`` carries space content. Over plain
    ``http://`` on the public internet an on-path attacker can strip the
    capability block (which stops every relay to that GFS) or swap the pinned
    key on the first fetch. A LAN / loopback GFS — how the
    federation demo harness and most home deployments run — keeps plain HTTP:
    there is no public path to sit on and usually no certificate to serve.

    The structural rules (http(s) only, a host, no credentials) are the
    shared household-address rules in :mod:`socialhome.peer_url`.

    Raises :class:`GfsConnectionError`, which the pairing route maps to a 4xx
    with this message.
    """
    try:
        validate_peer_url(url, field=field, require_tls_unless_private=True)
    except InvalidPeerUrlError as exc:
        raise GfsConnectionError(str(exc)) from exc


class GfsSignupError(GfsConnectionError):
    """Connecting to a GFS failed — a scanned / pasted pairing code
    (:meth:`GfsConnectionService.pair`) or open sign-up
    (:meth:`GfsConnectionService.pair_open_signup`).

    ``reason`` is the machine-readable cause the routes map to a plain
    message — never the GFS's own words:

    * ``invalid_url`` — the GFS URL fails the transport rules;
    * ``already_connected`` — this household already has that GFS;
    * ``unreachable`` — no answer, or a 5xx;
    * ``identity_mismatch`` — ``/gfs/info`` names another id / key than the
      pinned default GFS (open sign-up), or the pairing code's address
      answers but not as a GFS at all (QR: a 4xx, or a 200 that is no
      descriptor — retrying won't help, so not ``unreachable``);
    * ``closed`` — no verified ``open_signup`` capability, or the token
      endpoint said no (403/404);
    * ``busy`` — the GFS rate-limited the token request (429);
    * ``refused`` — the GFS turned the registration down (e.g. an expired
      token);
    * ``unproven`` — the GFS offers proof of possession but could not prove
      it holds the key ``/gfs/info`` names at the address used (an impostor
      replaying a real descriptor, a relay, or a ``base_url`` that differs
      from the address the household was given).
    """

    __slots__ = ("reason",)

    def __init__(self, message: str, *, reason: str, status: int | None = None) -> None:
        super().__init__(message, status=status)
        self.reason = reason


def _contact_reason(exc: GfsConnectionError, *, answered: str) -> str:
    """Why a GFS call failed, as a :class:`GfsSignupError` reason.

    ``unreachable`` when the GFS never answered or answered 5xx — it may be
    back later; otherwise *answered*: what a 4xx (or a 200 of the wrong
    shape) means for that particular call.
    """
    if exc.status is None or exc.status >= 500:
        return "unreachable"
    return answered


def _descriptor_capabilities(info: dict) -> dict | None:
    """The capability block of a ``/gfs/info`` body, verified under its OWN key.

    Before pairing there is no pinned key yet, so the block is verified
    against the ``public_key`` in the same response — the key about to be
    pinned (TOFU, the trust :meth:`GfsConnectionService.pair` already
    extends to the key itself). Missing block, bad signature or an unknown
    suite all answer ``None``; the unsigned top-level flags never count.
    """
    caps = info.get("capabilities")
    sig = info.get("capabilities_sig")
    suite = info.get("capabilities_sig_suite")
    if not isinstance(caps, dict) or not isinstance(sig, str) or not sig:
        return None
    if not isinstance(suite, str):
        return None
    try:
        ok = verify_capabilities(
            str(info.get("public_key") or ""),
            str(info.get("gfs_instance_id") or ""),
            caps,
            sig,
            suite,
        )
    except UnsupportedCapsSigSuite:
        log.warning(
            "GFS /gfs/info signed its capability block with the unknown suite"
            " %r — treating it as unsigned",
            suite,
        )
        return None
    return caps if ok else None


def _descriptor_offers_open_signup(info: dict) -> bool:
    """Whether a ``/gfs/info`` body proves ``open_signup`` under its OWN key."""
    caps = _descriptor_capabilities(info)
    return caps is not None and caps.get("open_signup") is True


#: Outcome of one ``POST /gfs/proof`` challenge. ``failed`` is the only
#: identity verdict: a 200 whose signature does not verify against the key at
#: this address (an impostor, a relay, a ``base_url`` that differs from the
#: stored address), or whose proof is malformed / names an unknown suite.
#: ``busy`` (429) and ``unreachable`` (no answer, a 5xx, a 404 / 405 from a
#: node that does not know the route yet — a rolling deploy behind a balancer
#: — or any other unexpected status: a 3xx, a WAF's 403, a 400) are retryable
#: and say nothing about identity.
ProofOutcome = Literal["proven", "failed", "busy", "unreachable"]

#: What :meth:`GfsConnectionService.possession_gate` answers: ``pass`` (run the
#: reconnect reconcile), ``retry`` (hold it — retryable; schedule a retry) or
#: ``refuse`` (a verified failure on a connection that must prove its key).
GateResult = Literal["pass", "retry", "refuse"]

#: How long a successful proof is reused by the reconnect gate before it
#: challenges again — a flapping socket costs one challenge per window.
PROOF_REUSE_S: float = 300.0

#: v_57 — how long a listing target's proof of possession is trusted before
#: the next capability question proves again.
LISTING_PROOF_TTL_S: float = 3600.0

#: v_57 — a listing target no space's list has named for this long has its
#: RAM caches evicted.
LISTING_TARGET_IDLE_S: float = 24 * 3600.0


def _possession_key(conn: GfsConnection) -> tuple[str, str]:
    """(connection id, normalized address) — so a check of a candidate copy
    at another URL never overwrites the connection's own answer."""
    return (conn.id, normalize_gfs_base_url(conn.inbox_url))


def _replaces_of(caps: dict) -> frozenset[str]:
    """The former ids a verified capability block says it ``replaces``."""
    raw = caps.get("replaces")
    if not isinstance(raw, list):
        return frozenset()
    return frozenset(r for r in raw if isinstance(r, str) and r)


class GfsConnectionService:
    """Service for managing GFS connections and space publications."""

    __slots__ = (
        "_addressee_key",
        "_anon_publish",
        "_anon_warned",
        "_authority_rotation",
        "_caps_warned",
        "_change_lock",
        "_cover_repo",
        "_envelope_relay",
        "_http_client",
        "_icon_repo",
        "_info_failed_at",
        "_invite_links",
        "_listing_failed_at",
        "_listing_proven_at",
        "_listing_seen",
        "_listing_sessions",
        "_member_publish_strict",
        "_member_publish_trusted",
        "_member_servers",
        "_on_disconnect",
        "_on_listings_changed",
        "_on_rebound",
        "_on_removed",
        "_on_repinned",
        "_own_instance_id",
        "_own_signing_key",
        "_place_pin",
        "_possession",
        "_private_channels",
        "_proof_offered",
        "_proven_at",
        "_publish_client",
        "_publish_retry",
        "_rebind_warned",
        "_refreshed_at",
        "_replaces",
        "_repo",
        "_rotation_warned",
        "_sealed_highlight_stream",
        "_space_repo",
        "_target_warned",
        "_targeted_publish",
        "_theme_repo",
    )

    def __init__(
        self,
        repo: AbstractGfsConnectionRepo,
        *,
        http_client: aiohttp.ClientSession | None = None,
        publish_client: aiohttp.ClientSession | None = None,
        listing_sessions: Callable[[str], Awaitable[aiohttp.ClientSession]] = (
            open_pinned_session
        ),
    ) -> None:
        self._repo = repo
        self._http_client = http_client
        # The session ``POST /gfs/publish`` (and its retries) rides — a
        # SEPARATE, cookie-less one (``aiohttp.DummyCookieJar``), so a sticky
        # load-balancer cookie the GFS set on this household's authenticated
        # calls (pairing, WS, signed queries) can never be replayed on an
        # anonymous publish and link it back. No fallback to the shared
        # session: without it, nothing is published.
        self._publish_client = publish_client
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
        # when a publish finds it unknown. Only a verified ``True`` lets a
        # publish go out; anything else sends nothing (never an identified
        # body). Once ``True`` under a verified signature the entry RATCHETS
        # (see :meth:`_apply_capability`).
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
        # …and for ``place_pin``: the GFS folds a global space's ``place``
        # into the canonical bytes it verifies. An older one does not, so a
        # body carrying the pin would 403 there — it gets the body without.
        self._place_pin: dict[str, bool] = {}
        # …and for ``targeted_publish`` (v_61): ``POST /gfs/publish`` takes a
        # ``to`` and delivers to that one subscriber. An older GFS would fan
        # a targeted item out to every subscriber, so nothing targeted is
        # sent to it.
        self._targeted_publish: dict[str, bool] = {}
        # …and for ``sealed_highlight_stream``: the GFS's public highlight
        # viewer page opens the end-to-end sealed stream (``stream_seal`` +
        # ``sealed`` frames, the ``#k=`` link secret). An older page can't,
        # so no public highlight link is minted there — it would be dead.
        self._sealed_highlight_stream: dict[str, bool] = {}
        # Same again for ``member_publish_trusted`` (v_49): whether the GFS
        # carries the identified ``POST /gfs/member-publish`` and the
        # ``/gfs/spaces/{id}/epoch`` notice. Nothing identified is ever sent
        # to a GFS that has not proved it under a valid signature.
        self._member_publish_trusted: dict[str, bool] = {}
        # Same again for ``member_publish_strict`` (v_50): whether the GFS
        # carries the anonymous ``POST /gfs/member-publish-anon`` and takes
        # ``publish_mode`` / ``writer_key_cert`` on the epoch notice (an older
        # server would refuse an owner notice carrying them).
        self._member_publish_strict: dict[str, bool] = {}
        self._private_channels: dict[str, bool] = {}
        # Same discipline for ``addressee_key``: whether the server takes the
        # signed ``gfs_key`` field (its own key) on member publish, epoch
        # notices and channel requests. An older server refuses unknown
        # fields, so the key is bound only where proven (True only).
        self._addressee_key: dict[str, bool] = {}
        # ``time.monotonic()`` of the last on-demand ``/gfs/info`` refresh per
        # connection (:meth:`refresh_if_stale` — the debounce).
        self._refreshed_at: dict[str, float] = {}
        # Same again for ``authority_rotation`` (v_44): whether the GFS
        # re-pins a space's authority key from an owner cert. A GFS without
        # it keeps the OLD key — and so keeps honouring a revoked admin's
        # relays — so a household publishing a cert there warns, once.
        self._authority_rotation: dict[str, bool] = {}
        self._rotation_warned: set[str] = set()
        # Failed ``POST /gfs/publish`` calls wait here for a retry —
        # identity-free bodies only, in memory, bounded (see the module).
        self._publish_retry = GfsPublishRetryQueue(self._retry_publish)
        #: v_49 — called after a publish that carried the owner's authority
        #: cert succeeded (the GFS re-pinned and forgot the space's epoch).
        self._on_repinned: Callable[[str, str], Awaitable[object]] | None = None
        #: Called with a space id after the space's publication rows changed
        #: (a publish or unpublish succeeded, a disconnect removed them) or a
        #: listing GFS was renamed — re-derives the owner's
        #: ``spaces.gfs_servers`` snapshot.
        self._on_listings_changed: Callable[[str], Awaitable[object]] | None = None
        #: Per-space lock factory shared with the owner's GFS choice
        #: (:meth:`attach_change_lock`); a no-op until attached.
        self._change_lock: Callable[[str], AbstractAsyncContextManager[object]] = (
            lambda _space_id: nullcontext()
        )
        #: Run in order with the connection BEFORE its row is deleted: the
        #: seat keeper, the private-channel owner (each only records state
        #: and schedules its requests in the background).
        self._on_disconnect: list[Callable[[GfsConnection], Awaitable[object]]] = []
        #: Called with the removed connection AFTER its row is gone — the
        #: owner's private-channel lists drop that server
        #: (:meth:`GfsServerSelectionService.on_connection_removed`).
        self._on_removed: Callable[[GfsConnection], Awaitable[object]] | None = None
        #: Called in order with the connection id after it adopted the
        #: server's new public id (:meth:`_rebind_to_served_id`): the seat
        #: keeper moves this household's follow seats to the new id, the
        #: private-channel owner re-registers and re-issues grants.
        self._on_rebound: list[Callable[[str], Awaitable[object]]] = []
        #: ``(connection id, served id)`` pairs already WARNed about — one
        #: line per refused rebind per process, not one per reconnect.
        self._rebind_warned: set[tuple[str, str]] = set()
        #: Connection id → the former ids its server SIGNED that it
        #: ``replaces`` (the ``replaces`` list of the latest verified
        #: capability block served under the connection's current id).
        #: Re-derived on every verified ``/gfs/info`` fetch — startup warm-up,
        #: each reconnect, the periodic refresh — so it survives a restart.
        #: What a grant issued before a rebind may still name for this server
        #: (:meth:`known_instance_ids`).
        self._replaces: dict[str, frozenset[str]] = {}
        # Proof of possession per connection, as last observed in this
        # process: ``True`` = the server at the connection's URL signed a fresh
        # nonce with the pinned key, ``False`` = it does not offer the proof
        # (an older GFS) or failed it, absent = not checked yet. What is
        # ENFORCED is the persisted ratchet (``proof_required_at``, see
        # :meth:`possession_gate`); this map only reports.
        self._possession: dict[tuple[str, str], bool] = {}
        # Whether the latest verified capability block of a connection offered
        # ``proof_of_possession`` (RAM, refreshed with every /gfs/info fetch).
        self._proof_offered: dict[str, bool] = {}
        # ``time.monotonic()`` of each connection's latest successful proof —
        # the reconnect gate's reuse window (:data:`PROOF_REUSE_S`).
        self._proven_at: dict[str, float] = {}
        #: v_57 — the owner's signed GFS list as THIS household may use it
        #: (:meth:`GfsServerSelectionService.member_servers`); ``None`` until
        #: wired, and then every caller falls back to its own connections.
        self._member_servers: (
            Callable[[str], Awaitable[tuple[GfsServer, ...] | None]] | None
        ) = None
        #: Opens the cookie-less, address-pinned session a LISTING target (a
        #: listed server we are not registered on) is reached through —
        #: :func:`socialhome.outbound_fetch.open_pinned_session`; a seam for
        #: tests, which cannot reach a loopback server through it.
        self._listing_sessions = listing_sessions
        #: Listing target id → ``time.monotonic()`` it was last named by a
        #: space's list. RAM caches of a target no list has named for
        #: :data:`LISTING_TARGET_IDLE_S` are evicted (:meth:`_evict_idle`).
        self._listing_seen: dict[str, float] = {}
        #: Listing target id → ``time.monotonic()`` of its latest successful
        #: proof of possession (bound to the listed URL). A target whose
        #: server offers the proof is trusted only for
        #: :data:`LISTING_PROOF_TTL_S` after one.
        self._listing_proven_at: dict[str, float] = {}
        #: Listing target id → ``time.monotonic()`` it FAILED its proof (a
        #: verdict, not a blip): unsupported everywhere — the relay too —
        #: until :data:`LISTING_PROOF_TTL_S` passed or the target is evicted.
        self._listing_failed_at: dict[str, float] = {}
        #: Key-mismatch warnings already logged (connection id, listed id).
        self._target_warned: set[tuple[str, str]] = set()

    def attach_member_servers(
        self, getter: Callable[[str], Awaitable[tuple[GfsServer, ...] | None]]
    ) -> None:
        """Wire the read side of the owner's signed GFS list (v_57)."""
        self._member_servers = getter

    async def server_targets(self, space_id: str) -> list[GfsConnection] | None:
        """Every GFS the space's owner listed *space_id* on, as send targets
        (:func:`~socialhome.services.gfs_server_targets
        .resolve_server_targets`): our own active connection where we hold
        one, else an unregistered in-memory listing target. ``None`` when no
        list is known (an older owner, a space never listed, nothing wired)
        — the caller then keeps its pre-v_57 behaviour (its own
        connections). Never raises."""
        if self._member_servers is None:
            return None
        try:
            servers = await self._member_servers(space_id)
            if servers is None:
                return None
            conns = await self._repo.list_active()
        except Exception:
            log.exception("GFS list for space %s unreadable", space_id)
            return None
        targets = resolve_server_targets(
            servers,
            conns,
            known_ids=self.known_instance_ids,
            warned=self._target_warned,
        )
        targets = await self._owner_live_only(space_id, targets)
        now = time.monotonic()
        for t in targets:
            if not t.registered:
                self._listing_seen[t.id] = now
        self._evict_idle(now)
        return targets

    async def _owner_live_only(
        self, space_id: str, targets: list[GfsConnection]
    ) -> list[GfsConnection]:
        """On the OWNER household the signed list trails its listing rows by
        one snapshot: a server it just withdrew from is still on the list
        until ``sync_snapshot`` re-signs it. Its own sends follow the rows —
        a target must still hold an active listing (never a just-withdrawn
        one, never a pending one)."""
        if self._space_repo is None or not self._own_instance_id:
            return targets
        try:
            space = await self._space_repo.get(space_id)
            if space is None or space.owner_instance_id != self._own_instance_id:
                return targets
            live = {
                c.id for c in await self._repo.list_active_listings_for_space(space_id)
            }
        except Exception:
            log.exception("GFS listings for space %s unreadable", space_id)
            return []
        return [t for t in targets if t.id in live]

    def capability_unknown(self, conn: GfsConnection) -> bool:
        """Whether *conn*'s capabilities are merely UNKNOWN right now — its
        ``/gfs/info`` (or a listing target's proof) did not answer within the
        negative TTL and no verdict says otherwise. A retry then waits
        (transient) instead of dropping the item."""
        if self._listing_failed(conn):
            return False
        failed_at = self._info_failed_at.get(conn.id)
        return (
            failed_at is not None
            and time.monotonic() - failed_at < GFS_INFO_NEGATIVE_TTL_S
        )

    def _listing_failed(self, conn: GfsConnection) -> bool:
        """The sticky "failed its proof" verdict of a listing target."""
        if conn.registered:
            return False
        failed = self._listing_failed_at.get(conn.id)
        if failed is None:
            return False
        if time.monotonic() - failed < LISTING_PROOF_TTL_S:
            return True
        del self._listing_failed_at[conn.id]
        return False

    async def _listing_recheck(self, conn: GfsConnection) -> None:
        """Before a listing target's cached capabilities are used: once its
        proof is older than :data:`LISTING_PROOF_TTL_S`, prove again. A
        failed proof forgets everything (and is sticky); a busy / unreachable
        server keeps the previous verified answers until a verdict comes."""
        if conn.registered:
            return
        proven = self._listing_proven_at.get(conn.id)
        if proven is None or time.monotonic() - proven < LISTING_PROOF_TTL_S:
            return
        failed_at = self._info_failed_at.get(conn.id)
        if failed_at is not None and (
            time.monotonic() - failed_at < GFS_INFO_NEGATIVE_TTL_S
        ):
            return
        await self._fetch_gfs_info(conn)

    def _forget_listing(self, target_id: str) -> None:
        """Drop every RAM cache of one listing target."""
        for cache in (
            self._anon_publish,
            self._envelope_relay,
            self._invite_links,
            self._place_pin,
            self._targeted_publish,
            self._sealed_highlight_stream,
            self._member_publish_trusted,
            self._member_publish_strict,
            self._private_channels,
            self._addressee_key,
            self._authority_rotation,
            self._proof_offered,
            self._info_failed_at,
            self._replaces,
            self._refreshed_at,
            self._listing_proven_at,
            self._listing_failed_at,
        ):
            cache.pop(target_id, None)

    def _evict_idle(self, now: float) -> None:
        """Forget listing targets no space's list has named for
        :data:`LISTING_TARGET_IDLE_S` (a delisted server, an owner's list
        that moved on), so the caches stay as small as the lists."""
        for target_id, seen in list(self._listing_seen.items()):
            if now - seen >= LISTING_TARGET_IDLE_S:
                del self._listing_seen[target_id]
                self._forget_listing(target_id)

    @staticmethod
    def require_registered(conn: GfsConnection) -> None:
        """Refuse an IDENTIFIED request to a server this household is not
        registered on (a v_57 listing target): nothing that names this
        household — its instance id, its signature, its cookie — may ever
        reach a server it never connected to."""
        if not conn.registered:
            raise GfsConnectionError(
                f"GFS {conn.gfs_instance_id!r} is not a connection of this "
                "household — no identified request goes there"
            )

    @asynccontextmanager
    async def anon_session(
        self, conn: GfsConnection
    ) -> AsyncIterator[aiohttp.ClientSession | None]:
        """The session an ANONYMOUS request to *conn* rides: the shared
        cookie-less publish session for a server we are registered on; for a
        listing target a fresh cookie-less session pinned to the vetted public
        address its listed URL resolves to (closed on exit).

        Yields ``None`` when there is no session (publish session not wired).
        Raises :class:`~socialhome.outbound_fetch.OutboundFetchRefused` when a
        listing target's address is refused (logged at WARNING)."""
        if conn.registered:
            yield self._publish_client
            return
        try:
            session = await self._listing_sessions(conn.inbox_url)
        except OutboundFetchRefused as exc:
            log.warning(
                "GFS %r (%s) on a space owner's list refused as a send target "
                "(%s) — nothing is sent to it",
                conn.display_name,
                conn.inbox_url,
                exc.reason,
            )
            raise
        try:
            yield session
        finally:
            await session.close()

    def attach_on_disconnect(
        self, hook: Callable[[GfsConnection], Awaitable[object]]
    ) -> None:
        """Add an unpair hook — run in the order attached, before the row is
        deleted (local bookkeeping; any request a hook needs runs in the
        background, never in the route). One failing never skips the next."""
        self._on_disconnect.append(hook)

    def attach_on_removed(
        self, hook: Callable[[GfsConnection], Awaitable[object]]
    ) -> None:
        """Wire the follow-up run after a connection's row was deleted."""
        self._on_removed = hook

    def attach_on_rebound(self, hook: Callable[[str], Awaitable[object]]) -> None:
        """Add a hook run after a connection adopted a new public id (hooks
        run in the order attached; one failing never skips the next)."""
        self._on_rebound.append(hook)

    def known_instance_ids(self, conn: GfsConnection) -> frozenset[str]:
        """*conn*'s pinned public id plus the former ids its server signed
        that it ``replaces`` — what a grant issued before a rebind may still
        name for this server."""
        return frozenset({conn.gfs_instance_id, *self._replaces.get(conn.id, ())})

    def attach_on_repinned(self, hook: Callable[[str, str], Awaitable[object]]) -> None:
        """Wire the v_49 epoch re-announce run after a re-pinning publish."""
        self._on_repinned = hook

    def attach_on_listings_changed(
        self, hook: Callable[[str], Awaitable[object]]
    ) -> None:
        """Wire the owner's GFS-list snapshot, run after a space's listings
        changed (:meth:`GfsServerSelectionService.sync_snapshot`)."""
        self._on_listings_changed = hook

    def attach_change_lock(
        self, factory: Callable[[str], AbstractAsyncContextManager[object]]
    ) -> None:
        """Share the selection service's per-space lock
        (:meth:`GfsServerSelectionService.changing`), so a heal / refresh
        re-publish never re-lists on a GFS the owner is removing."""
        self._change_lock = factory

    async def _relist(self, space_id: str, gfs_id: str) -> bool:
        """Re-publish *space_id* to *gfs_id* under the space's change lock,
        and only while this household still holds that listing — a refresh
        that raced a withdrawal must not undo it. ``False`` when the row is
        gone."""
        async with self._change_lock(space_id):
            listed = {
                p.gfs_connection_id
                for p in await self._repo.list_publications_for_space(space_id)
            }
            if gfs_id not in listed:
                return False
            await self.publish_space(space_id, gfs_id)
            return True

    async def _listings_changed(self, space_id: str) -> None:
        """Run the listing hook, fail-soft: the GFS round-trip already
        happened and its row is written; a failed snapshot is logged and
        re-derived on the next change."""
        if self._on_listings_changed is None:
            return
        try:
            await self._on_listings_changed(space_id)
        except Exception:
            log.exception(
                "GFS listing snapshot failed for space %s after a listing change",
                space_id,
            )

    async def start(self) -> None:
        """Start the GFS publish retry loop (app ``on_startup``)."""
        await self._publish_retry.start()

    async def stop(self) -> None:
        """Stop the GFS publish retry loop (app ``on_cleanup``)."""
        await self._publish_retry.stop()

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

    def attach_publish_session(self, session: aiohttp.ClientSession) -> None:
        """Provide the cookie-less publish session (``app._on_startup``)."""
        if self._publish_client is None:
            self._publish_client = session

    def publish_client(self) -> aiohttp.ClientSession | None:
        """The cookie-less session ``/gfs/publish`` rides (app cleanup closes it)."""
        return self._publish_client

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

        A malformed call (missing payload / identity fields) raises a plain
        :class:`GfsConnectionError`; everything past that raises
        :class:`GfsSignupError` with the same ``reason`` vocabulary as
        :meth:`pair_open_signup` (``invalid_url``, ``already_connected``,
        ``unreachable``, ``identity_mismatch``, ``refused``), so the two
        connect paths answer the same codes for the same causes.
        """
        gfs_url = str(qr_payload.get("gfs_url") or "").rstrip("/")
        token = str(qr_payload.get("token") or "")
        if not gfs_url or not token:
            raise GfsConnectionError(
                "gfs_url and token are required in the QR payload",
            )
        if not own_instance_id or not own_public_key_hex:
            raise GfsConnectionError(
                "own_instance_id and own_public_key_hex"
                " are required for GFS registration",
            )
        await self._require_new_gfs_url(gfs_url)
        try:
            info = await self._fetch_descriptor(gfs_url)
        except GfsConnectionError as exc:
            # Answered, but not as a GFS (a 4xx, or a 200 that is no
            # descriptor): the code's address doesn't point at one.
            raise GfsSignupError(
                str(exc),
                reason=_contact_reason(exc, answered="identity_mismatch"),
                status=exc.status,
            ) from exc
        return await self._register_classified(
            gfs_url,
            info,
            token,
            own_instance_id=own_instance_id,
            own_public_key_hex=own_public_key_hex,
            own_display_name=own_display_name,
            own_keywrap_public_key_hex=own_keywrap_public_key_hex,
            own_keywrap_sig=own_keywrap_sig,
        )

    async def _require_new_gfs_url(self, gfs_url: str) -> None:
        """The checks both pairing paths run BEFORE the first byte leaves.

        Transport first: over public plain HTTP the signed capability block
        on ``/gfs/info`` can be stripped on-path and the TOFU key swapped,
        so there is nothing to pin. LAN / loopback is still fine (the demo
        harness pairs ``http://127.0.0.1:<port>``). Then: a GFS this
        household already has is not registered with twice.

        Raises :class:`GfsSignupError` (``invalid_url`` /
        ``already_connected``).
        """
        try:
            _require_secure_url(gfs_url, field="gfs_url")
        except GfsConnectionError as exc:
            raise GfsSignupError(str(exc), reason="invalid_url") from exc
        wanted = gfs_server_address(gfs_url)
        for existing in await self._repo.list_all():
            if gfs_server_address(existing.inbox_url) == wanted:
                raise GfsSignupError(
                    "This household is already connected to that GFS",
                    reason="already_connected",
                )

    async def _register_classified(
        self,
        gfs_url: str,
        info: dict,
        token: str,
        *,
        own_instance_id: str,
        own_public_key_hex: str,
        own_display_name: str,
        own_keywrap_public_key_hex: str,
        own_keywrap_sig: str,
    ) -> GfsConnection:
        """:meth:`_register`, with its failure classified for the routes.

        Proof of possession runs FIRST: an impostor learns neither the token
        nor this household's identity.
        """
        proven = await self._require_possession(gfs_url, info)
        try:
            conn = await self._register(
                gfs_url,
                info,
                token,
                own_instance_id=own_instance_id,
                own_public_key_hex=own_public_key_hex,
                own_display_name=own_display_name,
                own_keywrap_public_key_hex=own_keywrap_public_key_hex,
                own_keywrap_sig=own_keywrap_sig,
                proven=proven,
            )
        except GfsConnectionError as exc:
            # No status = never answered; a 5xx = the GFS is having trouble.
            # Anything else is the GFS saying no to this registration.
            raise GfsSignupError(
                str(exc),
                reason=_contact_reason(exc, answered="refused"),
                status=exc.status,
            ) from exc
        self._possession[_possession_key(conn)] = proven
        if proven:
            self._proven_at[conn.id] = time.monotonic()
        return conn

    async def _require_possession(self, gfs_url: str, info: dict) -> bool:
        """Demand proof of possession from a GFS that offers it.

        Returns ``True`` when the GFS proved it holds the key in *info* at
        *gfs_url*, ``False`` when its signed block doesn't offer the proof
        (an older GFS — paired as before, recorded unproven, logged at INFO).
        Raises :class:`GfsSignupError`: ``unproven`` when it offers the proof
        and fails it — or offers none while this household already holds a
        PROVEN connection under the same key (the ratchet: a replayed
        pre-proof descriptor at another address) — ``busy`` on a 429,
        ``unreachable`` when the challenge got no usable answer.

        Residual: a replayed pre-proof descriptor for a key this household
        never saw proven still pairs as "older GFS" — recorded unproven, and
        every check that requires the proof (the reconnect gate once a later
        proof ratchets it, :meth:`verify_gfs_possession`) refuses it.
        """
        caps = _descriptor_capabilities(info)
        public_key = str(info.get("public_key") or "")
        if caps is None or caps.get(PROOF_CAPABILITY) is not True:
            if await self._key_requires_proof(public_key):
                log.warning(
                    "GFS %s serves the key of a connection that proved "
                    "possession before, but offers no %s — refusing (a replayed "
                    "old descriptor, or that GFS rolled back)",
                    gfs_url,
                    PROOF_CAPABILITY,
                )
                raise GfsSignupError(
                    "The GFS no longer proves it holds its key",
                    reason="unproven",
                )
            log.info(
                "GFS %s (%r) does not offer %s — pairing without proof that it "
                "holds the key its /gfs/info names (older GFS); the connection "
                "is recorded as unproven",
                gfs_url,
                str(info.get("gfs_instance_id") or ""),
                PROOF_CAPABILITY,
            )
            return False
        outcome = await self._prove(gfs_url, public_key)
        if outcome == "proven":
            return True
        if outcome == "busy":
            raise GfsSignupError(
                "The GFS rate-limited the proof of possession", reason="busy"
            )
        if outcome == "unreachable":
            raise GfsSignupError(
                "GFS unreachable while proving possession of its key",
                reason="unreachable",
            )
        raise GfsSignupError(
            "The GFS could not prove it holds the key its /gfs/info names",
            reason="unproven",
        )

    async def _key_requires_proof(self, public_key: str) -> bool:
        """Whether a connection pinning *public_key* has the proof ratchet."""
        key = public_key.strip().lower()
        return any(
            c.proof_required_at is not None and c.public_key.strip().lower() == key
            for c in await self._repo.list_all()
        )

    async def _prove(
        self,
        gfs_url: str,
        public_key_hex: str,
        *,
        client: aiohttp.ClientSession | None = None,
    ) -> ProofOutcome:
        """One ``POST {gfs_url}/gfs/proof`` challenge. Never raises.

        Verifies against *public_key_hex* (the key being pinned, or the one
        already pinned — the cluster-wide identity; the instance id is a
        per-node label and not part of the proof) and against *gfs_url* — the
        address THIS household used, never one the server reports. Every
        answered-but-wrong case is ``failed`` and logged at WARNING with what
        the server reported. *client* overrides the shared session (a v_57
        listing target's own pinned, cookie-less one).
        """
        if client is None:
            try:
                client = self._client()
            except RuntimeError:
                return "unreachable"
        nonce = new_proof_nonce()
        try:
            async with client.post(
                f"{gfs_url}{PROOF_ROUTE}",
                json={"nonce": nonce},
                allow_redirects=False,
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                if resp.status == 429:
                    log.warning("GFS %s rate-limited %s", gfs_url, PROOF_ROUTE)
                    return "busy"
                if resp.status != 200:
                    # A 5xx, a 404 / 405 from a node that does not know the
                    # route (yet) — a rolling deploy behind a balancer — a
                    # redirect, a WAF's 403, a 400: unexpected, retryable,
                    # never a verdict about the key.
                    log.warning(
                        "GFS %s %s answered HTTP %d — retryable, not a verdict "
                        "about its key",
                        gfs_url,
                        PROOF_ROUTE,
                        resp.status,
                    )
                    return "unreachable"
                try:
                    body = await resp.json()
                except aiohttp.ContentTypeError, ValueError:
                    body = None
        except (TimeoutError, aiohttp.ClientError) as exc:
            log.warning("GFS %s unreachable during %s: %s", gfs_url, PROOF_ROUTE, exc)
            return "unreachable"
        if not isinstance(body, dict):
            log.warning("GFS %s returned a malformed possession proof", gfs_url)
            return "failed"
        sig = body.get("proof_sig")
        suite = body.get("proof_sig_suite")
        if not isinstance(sig, str) or not isinstance(suite, str):
            log.warning("GFS %s returned a malformed possession proof", gfs_url)
            return "failed"
        try:
            ok = verify_possession_proof(public_key_hex, nonce, gfs_url, sig, suite)
        except UnsupportedProofSigSuite:
            log.warning(
                "GFS %s signed its possession proof with the unknown suite %r",
                gfs_url,
                suite[:64],
            )
            return "failed"
        if not ok:
            log.warning(
                "GFS %s FAILED proof of possession: the signature does not "
                "verify against the key %s… at this address. The server "
                "reported instance id %r and base_url %r — an impostor "
                "replaying the descriptor, a relay, or a GFS whose base_url "
                "differs from the address this household uses",
                gfs_url,
                public_key_hex[:16],
                str(body.get("gfs_instance_id") or "")[:128],
                str(body.get("base_url") or "")[:200],
            )
            return "failed"
        return "proven"

    def possession_proven(self, conn: GfsConnection) -> bool | None:
        """Whether *conn*'s server proved possession of the pinned key.

        ``True`` proven at pairing / the latest WS connect, ``False`` an older
        GFS without the proof or a failed proof, ``None`` not checked yet in
        this process. RAM cache only — never a network call.
        """
        return self._possession.get(_possession_key(conn))

    async def verify_gfs_possession(self, conn: GfsConnection) -> bool:
        """Challenge *conn*'s server NOW: does the server at ``conn.inbox_url``
        hold the key this household pinned (``conn.public_key``)?

        The hook for re-binding to a GFS that moved: pass a copy of the
        connection with the candidate address
        (``dataclasses.replace(conn, inbox_url=new_url)``) — ``True`` only
        when the server there signs a fresh nonce with the PINNED key, bound
        to that address. An older GFS without ``/gfs/proof``, an unreachable
        one, or any failure answers ``False``. Never raises. Records the answer
        under (connection id, address) — a candidate copy never overwrites
        the connection's own entry — unless the outcome was retryable; never
        touches the persisted ratchet.
        """
        outcome = await self._prove(conn.inbox_url.rstrip("/"), conn.public_key)
        if outcome in ("failed", "proven"):
            self._possession[_possession_key(conn)] = outcome == "proven"
        return outcome == "proven"

    async def pair_open_signup(
        self,
        gfs_url: str,
        *,
        own_instance_id: str,
        own_public_key_hex: str,
        own_display_name: str = "",
        own_keywrap_public_key_hex: str = "",
        own_keywrap_sig: str = "",
        expect_instance_id: str = "",
        expect_public_key: str = "",
    ) -> GfsConnection:
        """Pair with *gfs_url* through its open sign-up — no QR code.

        The one-click "Connect to the GFS" onboarding step. Same trust as a
        QR scan, one step longer:

        1. ``GET /gfs/info`` ONCE. Its signed capability block is verified
           against the ``public_key`` in that same response — the key about
           to be pinned (TOFU, exactly what :meth:`pair` trusts). A bare,
           unsigned or wrongly-signed ``open_signup`` counts as "closed".
        2. ``POST /gfs/signup-token`` (no body, nothing about this household)
           for a fresh single-use pairing token.
        3. ``POST /gfs/register`` with that token and exactly the fields QR
           pairing sends; the key pinned is the one verified in step 1 — no
           second descriptor fetch an on-path attacker could swap.

        *expect_instance_id* / *expect_public_key* pin the server's identity
        (the configured default GFS): a descriptor naming another id or key
        is refused before anything else is sent. Empty means no pin.

        Raises :class:`GfsSignupError` whose ``reason`` the route maps to a
        plain message: ``invalid_url``, ``already_connected``,
        ``unreachable``, ``identity_mismatch``, ``closed``, ``busy`` or
        ``refused``. Nothing leaves
        this household before the URL check passes.
        """
        gfs_url = str(gfs_url or "").rstrip("/")
        if not own_instance_id or not own_public_key_hex:
            raise GfsSignupError(
                "own_instance_id and own_public_key_hex"
                " are required for GFS registration",
                reason="refused",
            )
        await self._require_new_gfs_url(gfs_url)

        try:
            info = await self._fetch_descriptor(gfs_url)
        except GfsConnectionError as exc:
            raise GfsSignupError(str(exc), reason="unreachable") from exc
        if (
            expect_instance_id
            and str(info.get("gfs_instance_id")) != expect_instance_id
        ) or (
            expect_public_key
            and not hmac.compare_digest(
                str(info.get("public_key") or "").strip().lower().encode(),
                expect_public_key.strip().lower().encode(),
            )
        ):
            log.warning(
                "GFS %s does not match the pinned default GFS identity "
                "(instance id %r) — open sign-up refused",
                gfs_url,
                info.get("gfs_instance_id"),
            )
            raise GfsSignupError(
                "The GFS at that address is not the configured default GFS",
                reason="identity_mismatch",
            )
        if not _descriptor_offers_open_signup(info):
            raise GfsSignupError(
                "The GFS does not offer open sign-up (no verified open_signup"
                " capability on /gfs/info)",
                reason="closed",
            )
        token = await self._request_signup_token(gfs_url)
        return await self._register_classified(
            gfs_url,
            info,
            token,
            own_instance_id=own_instance_id,
            own_public_key_hex=own_public_key_hex,
            own_display_name=own_display_name,
            own_keywrap_public_key_hex=own_keywrap_public_key_hex,
            own_keywrap_sig=own_keywrap_sig,
        )

    async def _request_signup_token(self, gfs_url: str) -> str:
        """``POST {gfs_url}/gfs/signup-token`` → the single-use token.

        Sends no body: the GFS learns nothing here beyond what any visitor
        to its landing page reveals.
        """
        try:
            async with self._client().post(
                f"{gfs_url}/gfs/signup-token",
                allow_redirects=False,
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                if resp.status in (403, 404):
                    raise GfsSignupError(
                        "The GFS has closed open sign-up",
                        reason="closed",
                        status=resp.status,
                    )
                if resp.status == 429:
                    raise GfsSignupError(
                        "The GFS is handing out too many sign-ups right now",
                        reason="busy",
                        status=429,
                    )
                if resp.status != 200:
                    detail = await _remote_detail(resp, context="/gfs/signup-token")
                    raise GfsSignupError(
                        f"GFS /gfs/signup-token failed (HTTP {resp.status}): {detail}",
                        reason="unreachable" if resp.status >= 500 else "refused",
                        status=resp.status,
                    )
                body = await resp.json()
        except (TimeoutError, aiohttp.ClientError) as exc:
            raise GfsSignupError(
                f"GFS unreachable while requesting a sign-up token: {exc}",
                reason="unreachable",
            ) from exc
        token = body.get("token") if isinstance(body, dict) else None
        if not isinstance(token, str) or not token:
            raise GfsSignupError(
                "GFS /gfs/signup-token returned no token",
                reason="refused",
            )
        return token

    async def _fetch_descriptor(self, gfs_url: str) -> dict:
        """``GET {gfs_url}/gfs/info`` for pairing — the key to pin + caps.

        Raises :class:`GfsConnectionError` on any failure, with ``status``
        set whenever the GFS answered — including a 200 that is no
        descriptor (not an object, or lacking ``gfs_instance_id`` /
        ``public_key``), so ``status is None`` strictly means "no answer".
        """
        client = self._client()
        info_url = f"{gfs_url}/gfs/info"
        try:
            async with client.get(
                info_url,
                allow_redirects=False,
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                if resp.status != 200:
                    detail = await _remote_detail(resp, context="/gfs/info")
                    raise GfsConnectionError(
                        f"GFS /gfs/info failed (HTTP {resp.status}): {detail}",
                        status=resp.status,
                    )
                try:
                    info = await resp.json()
                except (aiohttp.ContentTypeError, ValueError) as exc:
                    # A web page (or garbage) where the descriptor should be:
                    # the address answered, it just isn't a GFS.
                    raise GfsConnectionError(
                        f"GFS /gfs/info did not return JSON: {exc}",
                        status=resp.status,
                    ) from exc
        except (TimeoutError, aiohttp.ClientError) as exc:
            raise GfsConnectionError(
                f"GFS unreachable while fetching /gfs/info: {exc}",
            ) from exc
        if not isinstance(info, dict):
            raise GfsConnectionError(
                "GFS /gfs/info did not return an object", status=200
            )
        if not str(info.get("gfs_instance_id") or "") or not str(
            info.get("public_key") or ""
        ):
            raise GfsConnectionError(
                "GFS /gfs/info did not return gfs_instance_id and public_key",
                status=200,
            )
        return info

    async def _register(
        self,
        gfs_url: str,
        info: dict,
        token: str,
        *,
        own_instance_id: str,
        own_public_key_hex: str,
        own_display_name: str,
        own_keywrap_public_key_hex: str,
        own_keywrap_sig: str,
        proven: bool = False,
    ) -> GfsConnection:
        """Register with *token*, pin the key from *info*, save the row
        (with the proof ratchet set when the GFS just proved possession).

        Shared by QR pairing and open sign-up so the two can never send
        different fields: the register body below is the whole of what a
        GFS learns about this household at pairing time. It carries no
        household address — the GFS relays over the WebSocket this
        household opens (``/gfs/ws``), so an External URL is neither
        required nor sent.
        """
        gfs_instance_id = str(info.get("gfs_instance_id") or "")
        gfs_public_key = str(info.get("public_key") or "")
        gfs_display_name = str(info.get("server_name") or gfs_url)
        client = self._client()

        # Register the HFS instance using the pairing token (from the QR
        #    code, or from open sign-up). Publish the local
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
                allow_redirects=False,
                json=register_body,
                timeout=aiohttp.ClientTimeout(total=15),
            ) as resp:
                if resp.status != 200:
                    detail = await _remote_detail(resp, context="/gfs/register")
                    raise GfsConnectionError(
                        f"GFS registration failed (HTTP {resp.status}): {detail}",
                        status=resp.status,
                    )
                body = await resp.json()
        except (TimeoutError, aiohttp.ClientError) as exc:
            raise GfsConnectionError(
                f"GFS unreachable: {exc}",
            ) from exc
        if not isinstance(body, dict):
            body = {}

        # ``status`` is "registered" (auto-accepted) or "pending" (admin
        # review). Pending is still a recorded connection, just inert
        # until the GFS admin flips it.
        registration_status = str(body.get("status") or "registered")
        local_status = "active" if registration_status == "registered" else "pending"

        now = datetime.now(UTC).isoformat()
        conn = GfsConnection(
            id=uuid.uuid4().hex,
            gfs_instance_id=gfs_instance_id,
            display_name=gfs_display_name,
            public_key=gfs_public_key,
            inbox_url=gfs_url,
            status=local_status,
            paired_at=now,
            proof_required_at=now if proven else None,
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

        v_57 — a LISTING target (``registered=False``) is read over its own
        cookie-less, address-pinned session (:meth:`anon_session`), its block
        is verified against the key the owner's signed list names, and it is
        never rebound (there is no row to move).
        """
        if not conn.registered:
            return await self._fetch_listing_info(conn)
        try:
            client = self._client()
        except RuntimeError:
            return None
        info = await self._get_info(client, conn)
        if info is None:
            return None
        current = await self._rebind_to_served_id(conn, info)
        self._note_replaces(conn, current, info)
        self._record_capabilities(conn, info)
        return info

    async def _fetch_listing_info(self, conn: GfsConnection) -> dict | None:
        """``/gfs/info`` of a LISTING target, over its own pinned session.

        Its capability block must verify against the key the owner's list
        names; and when that block offers ``proof_of_possession``, the server
        must also sign a fresh nonce bound to the LISTED URL
        (``POST /gfs/proof``, the same session) before anything in the block
        is trusted — a fresh proof is reused for :data:`LISTING_PROOF_TTL_S`.
        A failed or unavailable proof trusts nothing (WARNING on a failed
        one) and suppresses the target for the negative TTL. A server that
        does not offer the proof (an older build) is trusted on the signed
        block alone, as a registered connection's is. Nothing here touches
        ``proof_required_at`` — a listing target is never stored, but a key
        another connection of ours already ratcheted (``_key_requires_proof``)
        must prove here too: a block that no longer offers the proof is a
        replayed descriptor, a FAILED verdict.

        A failed proof is a sticky verdict (:meth:`_listing_failed`): every
        cache is forgotten and the target is unsupported, the relay included.
        A busy / unreachable proof is no verdict: the previous verified
        answers stay (the negative TTL throttles the next try)."""
        if self._listing_failed(conn):
            return None
        try:
            async with self.anon_session(conn) as session:
                info = await self._get_info(session, conn)
                if info is None or session is None:
                    return None
                caps = self._verified_capabilities(conn, info)
                offered = caps is not None and caps.get(PROOF_CAPABILITY) is True
                ratcheted = await self._key_requires_proof(conn.public_key)
                if caps is not None and ratcheted and not offered:
                    self._fail_listing(
                        conn, "no longer offers the proof its key is known for"
                    )
                    return None
                if offered:
                    proven = self._listing_proven_at.get(conn.id)
                    fresh = (
                        proven is not None
                        and time.monotonic() - proven < LISTING_PROOF_TTL_S
                    )
                    if not fresh:
                        outcome = await self._prove(
                            conn.inbox_url, conn.public_key, client=session
                        )
                        if outcome == "failed":
                            self._fail_listing(
                                conn, "failed proof of possession of the listed key"
                            )
                            return None
                        if outcome != "proven":
                            self._info_failed_at[conn.id] = time.monotonic()
                            return None
                        self._listing_proven_at[conn.id] = time.monotonic()
        except OutboundFetchRefused:
            self._info_failed_at[conn.id] = time.monotonic()
            return None
        self._note_replaces(conn, conn.gfs_instance_id, info)
        self._record_capabilities(conn, info)
        return info

    def _fail_listing(self, conn: GfsConnection, why: str) -> None:
        """Record the sticky FAILED verdict of a listing target."""
        log.warning(
            "GFS %r (%s) on a space owner's list %s — nothing is sent to it",
            conn.display_name,
            conn.inbox_url,
            why,
        )
        self._forget_listing(conn.id)
        self._listing_failed_at[conn.id] = time.monotonic()

    async def _get_info(
        self, client: aiohttp.ClientSession | None, conn: GfsConnection
    ) -> dict | None:
        """One ``GET {conn}/gfs/info`` over *client*; ``None`` (and the
        negative TTL) on any failure."""
        if client is None:
            return None
        try:
            async with client.get(
                f"{conn.inbox_url}/gfs/info",
                allow_redirects=False,
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
        except (TimeoutError, aiohttp.ClientError, ValueError) as exc:
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
        return info

    def _note_replaces(self, conn: GfsConnection, current: str, info: dict) -> None:
        """Remember the ``replaces`` list of a block served under *current*
        (the connection's id after any rebind) and signed by the pinned
        key. A block served under another id (a node still on an old id)
        never touches it."""
        if info.get("gfs_instance_id") != current:
            return
        caps = self._caps_signed_for(conn, current, info)
        if caps is None:
            return
        self._replaces[conn.id] = _replaces_of(caps)

    async def refresh_all_metadata(
        self, *, should_stop: Callable[[], bool] | None = None
    ) -> int:
        """:meth:`refresh_connection_metadata` for every active connection —
        the periodic, jittered pass (``GfsInfoRefreshScheduler``) that lets a
        household whose socket stays up for days still converge on its
        server's new id. Sequential and fail-soft; returns how many were
        refreshed."""
        done = 0
        for conn in await self._repo.list_active():
            if should_stop is not None and should_stop():
                break
            if conn.status != "active" or not conn.inbox_url:
                continue
            try:
                await self.refresh_connection_metadata(conn.id)
            except Exception:
                log.exception("GFS %s: periodic /gfs/info refresh failed", conn.id)
                continue
            done += 1
        return done

    async def _rebind_to_served_id(self, conn: GfsConnection, info: dict) -> str:
        """Adopt the public id ``/gfs/info`` serves when the server SIGNED,
        under the pinned key, that it replaces the pinned one. Returns the
        connection's id after this step.

        The key is the trust anchor, the id only a label: an operator may
        change ``[server] instance_id`` (a cluster whose nodes each served
        their own id moving to one shared id), keeping the old ids in
        ``instance_id_aliases``, which the server signs into its capability
        block as ``replaces``. A new id is adopted only when the served
        ``public_key`` IS the pinned key, the block verifies under (pinned
        key, new id) AND that block's ``replaces`` names the pinned id. A
        node that merely serves ANOTHER id under the same key (today's
        per-node-id cluster, or an old node mid-rollout) is not a reason to
        move — no lateral moves, no flipping back. A different key is
        refused with a WARNING (a new key means re-pairing). The id is
        UNIQUE: when another connection already holds it (the same server
        paired twice), both rows stay untouched. Never raises.
        """
        served = info.get("gfs_instance_id")
        if not isinstance(served, str) or not served:
            return conn.gfs_instance_id
        if served == conn.gfs_instance_id:
            return conn.gfs_instance_id
        served_key = info.get("public_key")
        if (
            not isinstance(served_key, str)
            or served_key.lower() != conn.public_key.lower()
        ):
            self._warn_rebind(
                conn,
                served,
                f"now serves the id {served!r} under a key other than the pinned "
                "key — not adopting it. If the server really changed its key, "
                "remove the connection and pair again",
            )
            return conn.gfs_instance_id
        caps = self._caps_signed_for(conn, served, info)
        if caps is None:
            # The pinned key, but no block it signed over the new id: the
            # capability check warns about the block itself.
            return conn.gfs_instance_id
        try:
            fresh = await self._repo.get(conn.id)
            if fresh is None:
                return conn.gfs_instance_id
            if fresh.gfs_instance_id == served:
                return served
            if fresh.gfs_instance_id not in _replaces_of(caps):
                log.debug(
                    "GFS %s serves %r, which does not replace the pinned %r — staying",
                    conn.id,
                    served,
                    fresh.gfs_instance_id,
                )
                return fresh.gfs_instance_id
            if not await self._repo.update_gfs_instance_id(conn.id, served):
                self._warn_rebind(
                    conn,
                    served,
                    f"now serves the id {served!r}, which another connection "
                    "already holds — the same server paired twice. Keeping both "
                    "connections as they are; remove one of them",
                )
                return fresh.gfs_instance_id
        except Exception:
            log.exception("GFS %s: rebind to the served id failed", conn.id)
            return conn.gfs_instance_id
        log.info(
            "GFS %r (%s) now serves the id %r instead of %r under the pinned key "
            "— adopted",
            conn.display_name,
            conn.inbox_url,
            served,
            fresh.gfs_instance_id,
        )
        self._replaces[conn.id] = _replaces_of(caps)
        for hook in self._on_rebound:
            try:
                await hook(conn.id)
            except Exception:
                log.exception("GFS %s: rebind hook failed", conn.id)
        return served

    @staticmethod
    def _caps_signed_for(
        conn: GfsConnection, gfs_instance_id: str, info: dict
    ) -> dict | None:
        """*info*'s capability block when it verifies under *conn*'s PINNED
        key for *gfs_instance_id*, else ``None``. Never raises."""
        caps = info.get("capabilities")
        sig = info.get("capabilities_sig")
        suite = info.get("capabilities_sig_suite")
        if not isinstance(caps, dict) or not isinstance(sig, str) or not sig:
            return None
        if not isinstance(suite, str):
            return None
        try:
            ok = verify_capabilities(conn.public_key, gfs_instance_id, caps, sig, suite)
        except UnsupportedCapsSigSuite:
            return None
        return caps if ok else None

    def _warn_rebind(self, conn: GfsConnection, served: str, detail: str) -> None:
        if (conn.id, served) in self._rebind_warned:
            return
        self._rebind_warned.add((conn.id, served))
        log.warning("GFS %r (%s) %s.", conn.display_name, conn.inbox_url, detail)

    @staticmethod
    def _signed_id(conn: GfsConnection, info: dict) -> str:
        """The id *info*'s capability block is verified for: the served one
        when ``/gfs/info`` serves the PINNED key (it may have moved to a new
        id, see :meth:`_rebind_to_served_id`), else the pinned one. Always
        verified under the pinned key, so only that key's holder can make a
        block pass either way."""
        served = info.get("gfs_instance_id")
        served_key = info.get("public_key")
        if (
            isinstance(served, str)
            and served
            and isinstance(served_key, str)
            and served_key.lower() == conn.public_key.lower()
        ):
            return served
        return conn.gfs_instance_id

    def _record_capabilities(self, conn: GfsConnection, info: dict) -> None:
        """Update the capability cache for *conn* from a ``/gfs/info`` body.

        The bare top-level ``anonymous_publish`` flag is IGNORED: it rides an
        unauthenticated endpoint, so an on-path attacker could forge or strip
        it. Only the signed block counts.

        A signed ``envelope_relay`` is cached too (True only — the same
        "never a sticky no" rule as :meth:`_signed_capability_supported`),
        so the connections list can say which servers relay pairing
        envelopes without probing every GFS on each read.
        """
        caps = self._verified_capabilities(conn, info)
        self._apply_capability(
            conn, caps is not None and caps.get("anonymous_publish") is True
        )
        if caps is not None and caps.get("envelope_relay") is True:
            self._envelope_relay[conn.id] = True
        if caps is not None and caps.get("addressee_key") is True:
            self._addressee_key[conn.id] = True
        if caps is not None:
            self._proof_offered[conn.id] = caps.get(PROOF_CAPABILITY) is True

    def addressee_key_for(self, conn: GfsConnection) -> str | None:
        """The server key to bind into a request to *conn* (``gfs_key``,
        signed): *conn*'s PINNED key, once its server proved under a valid
        signature that it takes the field; ``None`` otherwise (the field is
        omitted, exactly as an older household sends it). RAM cache only,
        warmed by every ``/gfs/info`` fetch."""
        if not self._addressee_key.get(conn.id) or not conn.public_key:
            return None
        return conn.public_key.lower()

    def forget_addressee_key(self, conn: GfsConnection) -> None:
        """Stop binding ``gfs_key`` for *conn* until the next verified
        ``/gfs/info`` fetch says ``addressee_key`` again. Called when a node
        refused a request carrying it as an unknown field — in a cluster
        mid-upgrade, the balancer may route to an older node that does not
        know the field although a newer one advertised it."""
        if self._addressee_key.pop(conn.id, None):
            log.info(
                "GFS %r (%s) refused the gfs_key field (an older node?) — "
                "sending without it until its /gfs/info says otherwise",
                conn.display_name,
                conn.inbox_url,
            )

    async def refresh_if_stale(
        self,
        conn: GfsConnection,
        *,
        min_interval_s: float = GFS_INFO_REFRESH_DEBOUNCE_S,
    ) -> GfsConnection | None:
        """Re-read *conn*'s ``/gfs/info`` now — at most once per
        *min_interval_s* per connection — and return the connection as it
        stands afterwards (``None`` when it is gone). For a caller that
        found evidence the server moved on (an owner's grant naming an id
        this connection does not know yet): it may rebind before the caller
        gives up."""
        now = time.monotonic()
        last = self._refreshed_at.get(conn.id)
        if last is None or now - last >= min_interval_s:
            self._refreshed_at[conn.id] = now
            await self.refresh_connection_metadata(conn.id)
        return await self._repo.get(conn.id)

    async def warm_capabilities(
        self,
        *,
        should_stop: Callable[[], bool] | None = None,
    ) -> int:
        """Fetch ``/gfs/info`` once for every active connection whose server
        has not yet proved ``envelope_relay`` in this process.

        The cache is RAM-only, so after a restart every connection reads
        cold until something fetches ``/gfs/info`` — until now only the
        GFS WebSocket's (re)connect hook, so the pairing picker and the
        GFS fallback switch stayed hidden for as long as that socket took
        to come up (reconnect back-off, a server refusing the socket while
        its HTTP side answers, a fetch that failed on the one connect).
        The startup warm-up (:class:`~socialhome.infrastructure
        .gfs_capability_warmup.GfsCapabilityWarmup`) calls this once,
        independent of the socket. List reads still never fetch.

        Sequential and fail-soft: a failing server records only the short
        negative TTL, exactly as any other fetch; ``should_stop`` is checked
        before each fetch so a shutdown lands between requests. Returns how
        many connections are known relay-capable afterwards.
        """
        for conn in await self._repo.list_active():
            if should_stop is not None and should_stop():
                break
            if conn.status != "active" or not conn.inbox_url:
                continue
            if self._envelope_relay.get(conn.id):
                continue
            await self._fetch_gfs_info(conn)
        return sum(
            1 for c in await self._repo.list_active() if self._envelope_relay.get(c.id)
        )

    def envelope_relay_known(self, conn: GfsConnection) -> bool:
        """Whether *conn*'s server has already proved ``envelope_relay``
        under a valid signature in this process — RAM cache only, never a
        network probe. Warmed by every ``/gfs/info`` fetch (pair time and
        each WS reconnect) and by :meth:`envelope_relay_supported`.
        """
        return self._envelope_relay.get(conn.id, False)

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
                conn.public_key, self._signed_id(conn, info), caps, sig, suite
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
        ignored for the rest of the process rather than silently stopping
        every future relay to that GFS. The ratchet is RAM-only:
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
            "GFS %r (%s) %s. No space events are relayed to it until a "
            "verifiable capability block appears.",
            conn.display_name,
            conn.inbox_url,
            detail,
        )

    async def _anonymous_publish_supported(self, conn: GfsConnection) -> bool:
        """Whether *conn*'s GFS proved ``anonymous_publish`` on /gfs/info.

        Answers from the cache when it is warm (filled at pair time and on
        every WS reconnect). On a cold miss — the first publish after a boot
        that hasn't seen a reconnect yet — probe ``/gfs/info`` ONCE. An
        unreachable GFS answers ``False`` (and lands in ``_info_failed_at``,
        which :meth:`_publish_capability` reads as "unknown"), and its failure is
        suppressed for :data:`GFS_INFO_NEGATIVE_TTL_S` so a burst of publishes
        doesn't pay the connect timeout each time.
        """
        if self._listing_failed(conn):
            return False
        await self._listing_recheck(conn)
        if self._listing_failed(conn):
            return False
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

    async def member_publish_trusted_supported(self, conn: GfsConnection) -> bool:
        """Whether *conn*'s GFS proved ``member_publish_trusted`` on /gfs/info.

        Gates every identified member publish and every epoch notice
        (:mod:`socialhome.services.gfs_member_publish_service`): a household
        sends its identity to a connection server only after that server's
        signed capability block proved it agreed to the trusted-mode
        contract."""
        return await self._signed_capability_supported(
            conn,
            "member_publish_trusted",
            self._member_publish_trusted,
        )

    async def member_publish_strict_supported(self, conn: GfsConnection) -> bool:
        """Whether *conn*'s GFS proved ``member_publish_strict`` on /gfs/info.

        Gates every anonymous member publish and the v_50 fields of the
        epoch notice (:mod:`socialhome.services.gfs_member_publish_service`).
        Without it a strict space's items take the host path — never the
        identified one."""
        return await self._signed_capability_supported(
            conn,
            "member_publish_strict",
            self._member_publish_strict,
        )

    async def private_channels_supported(self, conn: GfsConnection) -> bool:
        """Whether *conn*'s GFS proved ``private_channels`` on /gfs/info
        (v_51).

        Gates every opaque private-space channel request
        (:mod:`socialhome.services.gfs_channel_service`): a household creates
        a private space's channel, and its members subscribe (identified),
        only against a server whose signed capability block proved it
        carries the ``/gfs/channels/*`` contract. Without it a private
        space's items take the host path."""
        return await self._signed_capability_supported(
            conn,
            "private_channels",
            self._private_channels,
        )

    async def sealed_highlight_stream_supported(self, conn: GfsConnection) -> bool:
        """Whether *conn*'s GFS proved ``sealed_highlight_stream`` on
        /gfs/info.

        Gates minting a public highlight link
        (:meth:`HighlightPublicationService.publish`): this household streams
        only sealed frames, which an older GFS's viewer page cannot open, so a
        link minted there would never play for anyone."""
        return await self._signed_capability_supported(
            conn,
            "sealed_highlight_stream",
            self._sealed_highlight_stream,
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
        if self._listing_failed(conn):
            return False
        await self._listing_recheck(conn)
        if self._listing_failed(conn):
            return False
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
        # The name rides the owner's signed GFS list members hold.
        for space_id in await self._spaces_listed_on(gfs_id):
            await self._listings_changed(space_id)

    async def _spaces_listed_on(self, gfs_id: str) -> list[str]:
        return sorted({p.space_id for p in await self._repo.list_publications(gfs_id)})

    async def possession_gate(self, gfs_id: str) -> GateResult:
        """Whether the GFS-WS (re)connect's BULK reconcile may run now for
        *gfs_id*: pin heal, seat re-subscribe (and seat rebind), member epoch
        notices, the name push and the content-key reconcile — the
        identity-bound requests the household sends on its own on every
        connect.

        What it does not cover: the socket's signed hello has already been
        sent by then (the supervisor connects before this runs), and
        user-driven traffic (a publish, a follow, a report) is not gated.
        The gate withholds only that bulk reconcile.

        Call it right after :meth:`refresh_connection_metadata` (which warms
        the capability cache). Rules:

        * **Proof required** — this connection has the ratchet
          (``proof_required_at``, persisted), or another connection pinning
          the same key has it: a fresh proof (or one within
          :data:`PROOF_REUSE_S`) → ``pass``; a verified failure →
          ``refuse``; anything retryable (no answer, 5xx, 404 / 405, 429,
          another unexpected status) → ``retry`` — the caller retries
          gate + reconcile later, so a rolling deploy never costs the
          reconcile. A server that no longer advertises the capability is
          warned about and challenged anyway.
        * **Not required, capability offered**: one challenge. Proven → the
          ratchet is set, ``pass``. Anything else → ``pass`` (a ``failed``
          proof with a WARNING, no ratchet): the ratchet protects only
          connections that once proved — a self-hosted GFS paired at a LAN
          address or alias other than its ``base_url`` keeps working.
        * **Not offered, not required** (an older GFS): ``pass``, unproven.

        Never raises; an unknown connection answers ``pass`` (nothing to gate).
        """
        conn = await self._repo.get(gfs_id)
        if conn is None:
            return "pass"
        required = conn.proof_required_at is not None or (
            await self._key_requires_proof(conn.public_key)
        )
        offered = self._proof_offered.get(conn.id, False)
        key = _possession_key(conn)
        if not required and not offered:
            self._possession[key] = False
            return "pass"
        if required and not offered:
            log.warning(
                "GFS %r (%s) proved possession of its key before and now offers "
                "no %s — a rollback, a replayed old descriptor, or another "
                "server at that address; challenging anyway",
                conn.display_name,
                conn.inbox_url,
                PROOF_CAPABILITY,
            )
        last = self._proven_at.get(conn.id)
        if last is not None and time.monotonic() - last < PROOF_REUSE_S:
            return "pass"
        outcome = await self._prove(conn.inbox_url.rstrip("/"), conn.public_key)
        if outcome == "proven":
            self._possession[key] = True
            self._proven_at[conn.id] = time.monotonic()
            if conn.proof_required_at is None:
                await self._repo.mark_proof_required(
                    conn.id, datetime.now(UTC).isoformat()
                )
                log.info(
                    "GFS %r (%s) proved possession of its key — required from now on",
                    conn.display_name,
                    conn.inbox_url,
                )
            return "pass"
        self._proven_at.pop(conn.id, None)
        if outcome == "failed":
            self._possession[key] = False
        if not required:
            if outcome == "failed":
                log.warning(
                    "GFS %r (%s) offers proof of possession but did not prove "
                    "its key at this address (often: paired at a LAN address "
                    "or alias other than its base_url) — reconciling anyway, "
                    "nothing proven here yet to enforce",
                    conn.display_name,
                    conn.inbox_url,
                )
            return "pass"
        if outcome == "failed":
            log.warning(
                "GFS %r (%s) FAILED proof of possession of the key it proved "
                "before — skipping the reconnect reconcile",
                conn.display_name,
                conn.inbox_url,
            )
            return "refuse"
        log.warning(
            "GFS %r (%s): proof of possession not available on this connect "
            "(%s) — holding the reconnect reconcile for a retry",
            conn.display_name,
            conn.inbox_url,
            outcome,
        )
        return "retry"

    async def disconnect(self, gfs_id: str) -> None:
        """Remove a GFS connection and all its publications.

        Sends nothing inline: the ``on_disconnect`` hook (the seat keeper)
        only records local state and schedules its unsubscribes in the
        background."""
        conn = await self._repo.get(gfs_id)
        if conn is None:
            raise GfsConnectionError(f"GFS connection {gfs_id} not found")
        for hook in self._on_disconnect:
            try:
                await hook(conn)
            except Exception:
                log.exception("gfs: on-disconnect hook failed for %s", gfs_id)
        # ``repo.delete`` removes this connection's publication rows itself,
        # without passing through :meth:`unpublish_space`, so note the spaces
        # first.
        listed = await self._spaces_listed_on(gfs_id)
        await self._repo.delete(gfs_id)
        for space_id in listed:
            await self._listings_changed(space_id)
        if self._on_removed is not None:
            try:
                await self._on_removed(conn)
            except Exception:
                log.exception("gfs: on-removed hook failed for %s", gfs_id)

    async def list_connections(self) -> list[GfsConnection]:
        """Return ALL GFS connections for this household — active, pending,
        and suspended.

        The UI list distinguishes them by ``status`` and must surface a
        ``pending`` connection (GFS hasn't approved the household yet) or a
        ``suspended`` one; filtering to active-only made a freshly-connected
        GFS invisible until approval.
        """
        return await self._repo.list_all()

    async def publish_space(
        self, space_id: str, gfs_id: str, *, announce: bool = True
    ) -> GfsSpacePublication:
        """Publish a space to a GFS.

        Only a GLOBAL space is ever listed (:meth:`_require_publishable`).
        Callers outside :class:`GfsServerSelectionService` only REFRESH a
        listing that already exists (:meth:`republish_space`,
        :meth:`heal_space_pins`); choosing a new server is the owner's
        call, made through the selection service.

        ``announce=False`` skips the members' GFS-list refresh — a caller
        that changes several listings refreshes once at the end.

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
        await self._require_publishable(space_id)

        # v_57 — whether this is the space's first listing there (then the
        # epoch is announced below, before members hear of the server).
        listed_before = any(
            p.space_id == space_id for p in await self._repo.list_publications(gfs_id)
        )
        # A global space's place pin goes only to a GFS that proved it folds
        # the field into the signed bytes (an older one would 403 the whole
        # publish); elsewhere the space lists without it.
        # Probed only for a pinned space, so an unpinned publish costs no
        # extra ``/gfs/info`` round-trip.
        with_place = await self._has_place_pin(
            space_id
        ) and await self._signed_capability_supported(
            conn, "place_pin", self._place_pin
        )
        body = await self._build_publish_body(space_id, with_place=with_place)
        if "authority_cert" in body and not await self._signed_capability_supported(
            conn, "authority_rotation", self._authority_rotation
        ):
            # An older GFS folds only the fields it knows into the canonical
            # bytes it verifies, so a cert in the body would break the
            # signature and 403 the whole publish. Send it the body without
            # the cert: it keeps the old pin, and we say so.
            self._warn_no_authority_rotation(conn)
            body = await self._build_publish_body(
                space_id, with_cert=False, with_place=with_place
            )
        client = self._client()
        publish_url = f"{conn.inbox_url}/gfs/spaces/{space_id}/publish"
        try:
            async with client.post(
                publish_url,
                allow_redirects=False,
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
                except aiohttp.ClientError, ValueError:
                    data = {}
                if not isinstance(data, dict):
                    data = {}
        except (TimeoutError, aiohttp.ClientError) as exc:
            raise GfsConnectionError(f"Could not reach GFS: {exc}") from exc

        status = data.get("status") or "active"
        publication = await self._repo.publish_space(space_id, gfs_id, status=status)
        if self._on_repinned is not None and (
            "authority_cert" in body or not listed_before
        ):
            # v_49: a publish carrying the owner's cert re-pins the space key
            # at the GFS, which clears its epoch state — re-announce the
            # current epoch right away, whichever path (rotation, a later
            # retry, the reconnect heal) made the re-pin land. v_57: also on
            # a FIRST listing — a server that just listed the space must hold
            # its epoch and writer-key pin BEFORE members are told about it
            # below, or their first anonymous publishes there are refused.
            try:
                await self._on_repinned(space_id, gfs_id)
            except Exception:
                log.exception(
                    "publish_space: epoch re-announce after the re-pin failed "
                    "for space %s on gfs %s",
                    space_id,
                    gfs_id,
                )
        # Last: members learn about this server only once the publish (and
        # any epoch re-announce above) has landed there.
        if announce:
            await self._listings_changed(space_id)
        return publication

    def _warn_no_authority_rotation(self, conn: GfsConnection) -> None:
        """WARN once per connection: this GFS cannot re-pin a rotated key."""
        if conn.id in self._rotation_warned:
            return
        self._rotation_warned.add(conn.id)
        log.warning(
            "GFS %r (%s) does not advertise authority_rotation — it keeps the "
            "old space authority key after a rotation, so a revoked admin's "
            "relays to it stay authorized. Ask its operator to upgrade.",
            conn.display_name,
            conn.inbox_url,
        )

    async def _has_place_pin(self, space_id: str) -> bool:
        if self._space_repo is None:
            return False
        space = await self._space_repo.get(space_id)
        return space is not None and place_pin_of(space) is not None

    async def _build_publish_body(
        self, space_id: str, *, with_cert: bool = True, with_place: bool = False
    ) -> dict:
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
            "ts": datetime.now(UTC).isoformat(),
        }
        # v_44 — once the space authority key rotated, the owner's cert for
        # the current key, inside the signed body: the GFS re-pins only from
        # a cert that verifies against this household's registered key. It
        # names no member and no reason.
        cert = (
            owner_authority_cert(
                space,
                own_instance_id=self._own_instance_id,
                owner_seed=self._own_signing_key,
                owner_pk=ed25519_public_key(self._own_signing_key),
            )
            if with_cert
            else None
        )
        if cert is not None:
            body["authority_cert"] = cert
        # The owner's place pin (a global space's own listing metadata, like
        # its name — never the household's home unless the owner chose
        # exactly that), inside the signed body. Sorting only; omitted when
        # unset or when this GFS cannot verify it.
        pin = place_pin_of(space) if with_place else None
        if pin is not None:
            body["place"] = place_pin_wire(pin)
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

    #: Largest cover/icon (raw WebP bytes) the publish body will embed — the
    #: shared protocol bound the GFS also stores under. Two images at this
    #: size, base64'd, plus metadata stay under the GFS's per-route
    #: ``SPACE_PUBLISH_MAX_BODY_BYTES``, so art can never sink the publish.
    _MAX_EMBEDDED_IMAGE_BYTES: int = SPACE_IMAGE_EMBED_MAX_BYTES

    async def _image_data_uri(self, repo, space, *, icon: bool = False) -> str:
        """A ``data:image/webp;base64,…`` URI for the space's cover/icon, or
        ``""`` when none is set. Self-contained so the GFS page renders it
        without a cross-origin, auth-gated fetch back to the host.

        Bounded: an image whose raw bytes exceed
        :data:`_MAX_EMBEDDED_IMAGE_BYTES` is omitted (``""``) so it cannot
        push the publish body past the GFS request limit and fail the publish;
        the omission is logged at WARNING so an operator can see why the
        listing has no art.
        """
        has = getattr(space, "icon_hash" if icon else "cover_hash", None)
        if repo is None or not has:
            return ""
        got = await repo.get(space.id)
        if got is None:
            return ""
        webp, _hash = got
        if len(webp) > self._MAX_EMBEDDED_IMAGE_BYTES:
            log.warning(
                "GFS publish: %s for space %s is %d bytes (> %d) — omitting "
                "it from the publish body so the space still lists",
                "icon" if icon else "cover",
                space.id,
                len(webp),
                self._MAX_EMBEDDED_IMAGE_BYTES,
            )
            return ""
        return SPACE_IMAGE_DATA_URI_PREFIX + base64.b64encode(webp).decode("ascii")

    async def unpublish_space(
        self, space_id: str, gfs_id: str, *, announce: bool = True
    ) -> None:
        """Unpublish a space from a GFS.

        ``announce=False`` skips the members' GFS-list refresh (see
        :meth:`publish_space`).

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

        ts = datetime.now(UTC).isoformat()
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
                allow_redirects=False,
                json=body,
                timeout=aiohttp.ClientTimeout(total=15),
            ) as resp:
                if resp.status not in (200, 204, 404):
                    detail = await _remote_detail(resp, context="unpublish")
                    raise GfsConnectionError(
                        f"GFS rejected unpublish (HTTP {resp.status}): {detail}",
                    )
        except (TimeoutError, aiohttp.ClientError) as exc:
            raise GfsConnectionError(f"Could not reach GFS: {exc}") from exc

        await self._repo.unpublish_space(space_id, gfs_id)
        if announce:
            await self._listings_changed(space_id)

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
                "this GFS can't host invite links yet",
            )

        ts = datetime.now(UTC).isoformat()
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
                allow_redirects=False,
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
                except aiohttp.ClientError, ValueError:
                    data = {}
                if not isinstance(data, dict):
                    data = {}
        except (TimeoutError, aiohttp.ClientError) as exc:
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

        ts = datetime.now(UTC).isoformat()
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
                allow_redirects=False,
                json=body,
                timeout=aiohttp.ClientTimeout(total=15),
            ) as resp:
                if resp.status not in (200, 204, 404):
                    detail = await _remote_detail(resp, context="invite revoke")
                    raise GfsConnectionError(
                        f"GFS rejected invite revoke (HTTP {resp.status}): {detail}",
                        status=resp.status,
                    )
        except (TimeoutError, aiohttp.ClientError) as exc:
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

        ts = datetime.now(UTC).isoformat()
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
                allow_redirects=False,
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
                except aiohttp.ClientError, ValueError:
                    data = {}
                if not isinstance(data, dict):
                    data = {}
        except (TimeoutError, aiohttp.ClientError) as exc:
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
        conn = await self._repo.get(gfs_id)
        if conn is None:
            raise GfsConnectionError(f"GFS connection {gfs_id} not found")
        return await self.unsubscribe_via(conn, space_id)

    async def unsubscribe_via(self, conn: GfsConnection, space_id: str) -> str:
        """:meth:`unsubscribe_from_gfs_space` over a connection snapshot —
        for the unsubscribes an unpair sends after its row is gone."""
        if not self._own_instance_id or not self._own_signing_key:
            raise GfsConnectionError(
                "cannot unsubscribe from a GFS space without a wired signing identity",
            )
        ts = datetime.now(UTC).isoformat()
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
                allow_redirects=False,
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
                except aiohttp.ClientError, ValueError:
                    data = {}
                if not isinstance(data, dict):
                    data = {}
        except (TimeoutError, aiohttp.ClientError) as exc:
            raise GfsConnectionError(f"Could not reach GFS: {exc}") from exc
        return str(data.get("status") or "unsubscribed")

    async def _require_publishable(self, space_id: str) -> None:
        """Refuse to list anything but a GLOBAL space on a GFS.

        * public → :class:`TierChangeRequiredError` (409
          ``TIER_CHANGE_REQUIRED``): a public space is discoverable only by
          the households we are connected with; listing it on a GFS *is*
          making it global — a quorum-gated tier change, never a side
          effect of a publish;
        * private / household → :class:`SpaceNotPublishableError` (409
          ``SPACE_NOT_PUBLIC``): it would put a private space's name,
          description and cover in a GFS directory.

        Checked before any request is built, so nothing reaches the GFS. An
        unknown space is left to :meth:`_build_publish_body`, which already
        refuses it.
        """
        if self._space_repo is None:
            return
        space = await self._space_repo.get(space_id)
        if space is None or space.space_type is SpaceType.GLOBAL:
            return
        if space.space_type is SpaceType.PUBLIC:
            raise TierChangeRequiredError()
        raise SpaceNotPublishableError()

    async def republish_space(self, space_id: str) -> int:
        """Re-publish ``space_id`` to every GFS it is ALREADY published to.

        The v_44 authority rotation calls this so each GFS that lists the
        space re-pins from the owner's cert, and a directory-setting change
        (``join_mode``, ``allow_subscribers``) so each one shows the truth.
        Never a server that doesn't list it: a key rotation or a settings
        edit must never be how a connection server first learns that a
        space exists. Fail-soft per GFS; returns how many accepted the
        publish.
        """
        if not await self._owns(space_id):
            return 0
        done = 0
        for conn in await self._repo.list_gfs_for_space(space_id):
            if conn.status != "active":
                continue
            try:
                if await self._relist(space_id, conn.id):
                    done += 1
            except (GfsConnectionError, SpaceNotPublishableError) as exc:
                log.warning(
                    "republish_space: failed for gfs %s: %s",
                    conn.id,
                    exc,
                )
        return done

    async def publish_space_event(
        self,
        *,
        space_id: str,
        event_type: str,
        payload: dict,
    ) -> int:
        """Relay a single space-content event to a space's GFS subscribers.

        POSTs to ``POST /gfs/publish`` on every GFS on the owner's signed list
        (v_57, including servers this household is not registered on — the
        body names nobody, and a listing target is reached over its own
        pinned cookie-less session), or without a list on every GFS this
        household published the space to.
        The ``payload`` is the caller-built wire envelope — for the Phase-5a2
        public-post relay it is the already-encrypted + authority-signed
        ``{space_id, epoch, encrypted_payload, authority_sig, ...}`` dict, so
        the GFS stays content-blind and authorizes the relay via the embedded
        space-authority signature (see :class:`SpacePublicOutbound`).

        **The body is always identity-free** — exactly ``{space_id,
        event_type, payload}``, never ``from_instance`` or a household
        signature: a connection server must not learn WHICH household relayed
        a public/global-space event. There is no identified fallback, so the
        method takes no household identity at all. What a GFS gets depends
        on what its SIGNED ``GET /gfs/info`` capability block proves
        (:meth:`_publish_capability`):

        * ``anonymous_publish`` proven → the POST.
        * ``/gfs/info`` unreachable (cold cache, GFS down) → nothing yet;
          the publish waits in the retry queue, which re-checks the
          capability before every attempt.
        * reachable but not proven (an older build, or a stripped / unsigned
          block) → skipped, with one WARNING per connection per process
          (:meth:`_warn_publish_unsupported`). Such a GFS gets no space
          publishes until it upgrades.

        A per-GFS failure never aborts the fan-out. A *transient* one
        (transport error, timeout, 408, 429, 5xx) is queued for a backed-off
        retry, honouring a 429's ``Retry-After``
        (:class:`~socialhome.services.gfs_publish_retry.GfsPublishRetryQueue`);
        a *permanent* one (any other 4xx) is logged and dropped. While a GFS
        has retries pending, a new publish to it queues behind them so it
        cannot overtake. Returns the number of GFS instances the event was
        accepted by on this first attempt.
        """
        if (
            self._http_client is None
            or self._publish_client is None
            or not self._own_signing_key
        ):
            # No publish context (``attach_publish_context`` / the cookie-less
            # publish session) — nothing to relay with. Nothing is signed
            # with the identity here.
            log.warning(
                "publish_space_event: publish context not wired — dropping"
                " relay for space %s",
                space_id,
            )
            return 0
        if not await self._relayable(space_id):
            return 0
        conns = await self._relay_targets(space_id)
        if not conns:
            return 0
        item = GfsPublish(space_id=space_id, event_type=event_type, payload=payload)
        delivered = 0
        for conn in conns:
            if conn.status != "active":
                continue
            if self._publish_retry.pending(conn.id):
                # Earlier publishes to this GFS are waiting for a retry:
                # join them rather than overtake (subscribers see order).
                self._queue_publish_retry(conn, item, None)
                continue
            capability = await self._publish_capability(conn)
            if capability == "unknown":
                self._queue_publish_retry(conn, item, None)
                continue
            if capability == "unsupported":
                self._warn_publish_unsupported(conn)
                continue
            outcome = await self._post_publish(conn, item.body(), space_id=space_id)
            if outcome.kind == "delivered":
                delivered += 1
            elif outcome.kind == "transient":
                self._queue_publish_retry(conn, item, outcome.retry_after_s)
        return delivered

    async def publish_space_event_to(
        self,
        gfs_id: str,
        *,
        space_id: str,
        event_type: str,
        payload: dict,
        to_instance_id: str,
    ) -> bool | None:
        """Relay one event to ONE subscriber of *space_id* on the GFS
        *gfs_id* (``targeted_publish``, v_61) — the subscriber that GFS
        itself named in its ``new_subscriber`` notify.

        ``None`` when that server has not proved ``targeted_publish`` (and
        ``anonymous_publish``) under a valid signature: nothing was sent, and
        the caller falls back (or skips). Otherwise ``True`` when the GFS
        accepted it. No retry queue: the subscriber's next connect asks for
        it again (``new_subscriber`` is re-sent on reconnect). The body is
        ``{space_id, event_type, payload, to}`` — still no household
        identity.
        """
        if self._publish_client is None or not to_instance_id:
            return None
        conn = await self._repo.get(gfs_id)
        if conn is None or conn.status != "active" or not conn.inbox_url:
            return None
        if not await self._anonymous_publish_supported(conn):
            return None
        if not await self._signed_capability_supported(
            conn, "targeted_publish", self._targeted_publish
        ):
            return None
        body = {
            **GfsPublish(
                space_id=space_id, event_type=event_type, payload=payload
            ).body(),
            "to": to_instance_id,
        }
        outcome = await self._post_publish(conn, body, space_id=space_id)
        return outcome.kind == "delivered"

    async def _relay_targets(self, space_id: str) -> list[GfsConnection]:
        """Where a relay of *space_id* goes: every server on the owner's
        signed list (v_57 — a delegated admin relaying while the owner is
        offline reaches the servers it never connected to, anonymously), or,
        without a list, the servers THIS household published it to."""
        targets = await self.server_targets(space_id)
        if targets is not None:
            return targets
        return await self._repo.list_active_listings_for_space(space_id)

    async def _publish_capability(
        self, conn: GfsConnection
    ) -> Literal["anonymous", "unsupported", "unknown"]:
        """Whether ``conn`` may be sent the identity-free relay body.

        ``anonymous`` — its signed capability block proved
        ``anonymous_publish``. ``unknown`` — ``/gfs/info`` could not be
        reached (the negative TTL is live), so ask again later.
        ``unsupported`` — it answered without the proof.
        """
        if await self._anonymous_publish_supported(conn):
            return "anonymous"
        if self._listing_failed(conn):
            return "unsupported"
        # "unknown" only while there is no answer at all: a GFS already seen
        # without the proof stays "unsupported" even if a later refresh
        # blipped, or its publishes would wait in the queue forever.
        if self._anon_publish.get(conn.id) is None and conn.id in self._info_failed_at:
            return "unknown"
        return "unsupported"

    async def _post_publish(
        self, conn: GfsConnection, body: dict, *, space_id: str
    ) -> PublishOutcome:
        """One ``POST /gfs/publish`` of ``body`` to ``conn``, classified —
        over the cookie-less session (a listing target's own pinned one)."""
        url = f"{conn.inbox_url}/gfs/publish"
        try:
            async with self.anon_session(conn) as session:
                if session is None:
                    return PublishOutcome.permanent()
                return await self._post_publish_once(
                    session, url, body, conn=conn, space_id=space_id
                )
        except OutboundFetchRefused as exc:
            return refusal_outcome(exc)

    async def _post_publish_once(
        self,
        session: aiohttp.ClientSession,
        url: str,
        body: dict,
        *,
        conn: GfsConnection,
        space_id: str,
    ) -> PublishOutcome:
        try:
            async with session.post(
                url,
                allow_redirects=False,
                json=body,
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                outcome = classify_publish_status(
                    resp.status, resp.headers.get("Retry-After")
                )
                if outcome.kind != "delivered":
                    log.warning(
                        "publish_space_event: GFS %s rejected relay for space"
                        " %s — HTTP %d (%s)",
                        conn.id,
                        space_id,
                        resp.status,
                        outcome.kind,
                    )
                return outcome
        except (TimeoutError, aiohttp.ClientError) as exc:
            log.warning(
                "publish_space_event: GFS %s relay failed for space %s: %s",
                conn.id,
                space_id,
                exc or type(exc).__name__,
            )
            return PublishOutcome.transient()

    def _queue_publish_retry(
        self,
        conn: GfsConnection,
        item: GfsPublish,
        retry_after_s: float | None,
    ) -> None:
        if not self._publish_retry.enqueue(conn.id, item, retry_after_s=retry_after_s):
            log.warning(
                "publish_space_event: %s@%s to GFS %s is lost — the retry"
                " queue refused it",
                item.event_type,
                item.space_id,
                conn.id,
            )

    async def _retry_publish(self, conn_id: str, item: GfsPublish) -> PublishOutcome:
        """One retry of ``item`` to ``conn_id`` — the retry queue's sender.

        Re-reads before every attempt: the server must still be on the
        owner's list (without one: the space still published to that
        connection) and active, or the item is dropped (the owner withdrew
        it). The body is the identity-free
        ``{space_id, event_type, payload}`` and goes only to a GFS that has
        proved ``anonymous_publish`` (:meth:`_publish_capability`): while
        ``/gfs/info`` is unreachable the item waits; a GFS that answers
        without the proof has it dropped.
        """
        if self._http_client is None or self._publish_client is None:
            return PublishOutcome.permanent()
        # Re-resolved now: a server the owner has since delisted (or, without
        # a list, one whose listing we withdrew) gets nothing more.
        conn = (
            next(
                (
                    c
                    for c in await self._relay_targets(item.space_id)
                    if c.id == conn_id
                ),
                None,
            )
            if await self._relayable(item.space_id)
            else None
        )
        if conn is None or conn.status != "active":
            log.info(
                "publish retry: %s@%s is no longer published to GFS %s — dropping it",
                item.event_type,
                item.space_id,
                conn_id,
            )
            return PublishOutcome.permanent()
        capability = await self._publish_capability(conn)
        if capability == "unknown":
            return PublishOutcome.transient()
        if capability == "unsupported":
            self._warn_publish_unsupported(conn)
            return PublishOutcome.permanent()
        return await self._post_publish(conn, item.body(), space_id=item.space_id)

    def _warn_publish_unsupported(self, conn: GfsConnection) -> None:
        """Warn ONCE per connection per process that a GFS gets no publishes.

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
            "GFS %r (%s) does not prove anonymous_publish in a signed"
            " capability block — space events are NOT relayed to it (that"
            " would tell the connection server which household relayed"
            " them). Ask its operator to upgrade.",
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
        on the GFS side (the pin is COALESCE-guarded and only moves on an
        owner-certified rotation, v_44 — which this publish also carries),
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
            if not await self._owns(pub.space_id):
                # Only the owner household lists a space (owner model): a
                # legacy row for a space hosted elsewhere is never refreshed
                # (a household admin can withdraw it by hand).
                continue
            if not await self._holds_space_seed(pub.space_id):
                continue
            try:
                if await self._relist(pub.space_id, gfs_id):
                    healed += 1
            except SpaceNotPublishableError as exc:
                # A listing left over from before the space left the global
                # tier (or a hand-published public space the startup
                # reconcile has not converted yet): never re-advertise it.
                log.info(
                    "gfs.heal_space_pins: space %s is not global, not "
                    "re-publishing it to gfs %s: %s",
                    pub.space_id,
                    gfs_id,
                    exc.code,
                )
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

    async def _owns(self, space_id: str) -> bool:
        """Whether this household hosts *space_id* (a live local row)."""
        if self._space_repo is None or not self._own_instance_id:
            return False
        space = await self._space_repo.get(space_id)
        return (
            space is not None
            and not space.dissolved
            and space.owner_instance_id == self._own_instance_id
        )

    async def _relayable(self, space_id: str) -> bool:
        """Whether content of *space_id* may go to the GFSs listing it: only a
        GLOBAL space is on a GFS. A leftover row of a space that left the
        tier (its withdrawal failed) must not keep the relay going — the
        withdrawal retry removes the row itself."""
        if self._space_repo is None:
            return True
        space = await self._space_repo.get(space_id)
        return space is not None and space.space_type is SpaceType.GLOBAL

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
                exc_info=True,
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
                exc_info=True,
            )
            return False
        if seed is None:
            log.debug(
                "gfs.heal_space_pins: no seed held for space %s — skipping",
                space_id,
            )
            return False
        return True

    async def unpublish_space_from_listed(self, space_id: str) -> int:
        """Unpublish a space from every GFS that currently lists it.

        Used when a space leaves the global tier or is dissolved. Only the
        GFSes that hold a publication row are contacted, so a GFS that never
        listed the space never learns its id from the withdrawal. Fail-soft
        per GFS; returns how many withdrawals succeeded.
        """
        done = 0
        for conn in await self._repo.list_gfs_for_space(space_id):
            try:
                await self.unpublish_space(space_id, conn.id)
                done += 1
            except GfsConnectionError as exc:
                log.warning(
                    "unpublish_space_from_listed: failed for gfs %s: %s",
                    conn.id,
                    exc,
                )
        return done

    def _build_instance_name_body(self, display_name: str) -> dict | None:
        """Build the signed ``/gfs/instance`` body for a household-name push.

        Returns ``None`` when there's no publish context wired (early boot /
        tests) — nothing to sign with, so the caller skips the push.
        """
        if not self._own_instance_id or not self._own_signing_key:
            return None
        ts = datetime.now(UTC).isoformat()
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
                allow_redirects=False,
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
        except (TimeoutError, aiohttp.ClientError) as exc:
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
        from datetime import datetime

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
            "created_at": datetime.now(UTC).isoformat(),
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
                allow_redirects=False,
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
                allow_redirects=False,
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
