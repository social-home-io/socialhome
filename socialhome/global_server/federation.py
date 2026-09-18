"""GFS federation service — instance registration, event relay, subscriptions.

Business logic only — all SQL lives in :mod:`.repositories`. Crypto
helpers are reused from :mod:`socialhome.crypto` (no duplication).

Fan-out delivery is **WebSocket-primary, HTTPS-fallback** (spec §24.12):
if a paired SH instance has an open ``/gfs/ws`` WebSocket, the event is
pushed over that connection; otherwise it falls back to an HTTPS POST
to the subscriber's inbox URL.
"""

from __future__ import annotations

import asyncio
import hashlib
import itertools
import json
import logging
import time
from datetime import datetime, timezone
from typing import TYPE_CHECKING

import aiohttp

from ..authority_sig import (
    AUTHORITY_EVENT_SPACE_SUBSCRIBERS_QUERY,
    AUTHORITY_RELAY_EVENT_TYPES,
    UnsupportedAuthoritySuite,
    authority_signing_bytes,
    strip_authority_sig_fields,
    verify_authority_event,
)
from ..crypto import b64url_decode, verify_ed25519
from ..domain.space import normalize_category
from .domain import (
    ClientInstance,
    GfsSubscriber,
    GfsSubscriberWithKeys,
    GlobalSpace,
)
from .repositories import AbstractGfsFederationRepo

if TYPE_CHECKING:
    from .ws_registry import GfsWebSocketRegistry

log = logging.getLogger(__name__)

#: Storage cap for a published space's ``about_markdown``. Generous for a
#: real "about" blurb; bounds DB growth + the public-page render cost from
#: an oversized publish. The public renderer applies its own (smaller) cap.
MAX_ABOUT_MARKDOWN_CHARS: int = 8000

#: Max display_name length accepted by ``update_instance`` (chars). Mirrors
#: the household-name bound on the HFS side.
MAX_DISPLAY_NAME_CHARS: int = 80

#: Max simultaneous in-flight subscriber deliveries for ONE relayed event.
#: Sequential fan-out let a single accepted (and anonymously replayable)
#: publish pin a request handler for ``len(subscribers) ×
#: FAN_OUT_TIMEOUT_SECONDS``; unbounded concurrency would instead let it open a
#: socket per subscriber. 8 keeps a large space's fan-out an order of magnitude
#: faster than sequential while capping the sockets and memory one publish can
#: claim.
FAN_OUT_CONCURRENCY: int = 8

#: Per-target delivery timeout for the HTTPS-inbox fallback (seconds).
FAN_OUT_TIMEOUT_SECONDS: int = 10

#: Wall-clock ceiling on ONE relay's whole fan-out (seconds). Bounded
#: concurrency alone still lets an accepted publish pin the request handler for
#: ``ceil(len(subscribers) / FAN_OUT_CONCURRENCY) × FAN_OUT_TIMEOUT_SECONDS``
#: when every target blackholes — minutes for a space with a few hundred
#: subscribers, and the caller of ``/gfs/publish`` is anonymous, so an attacker
#: who registers N instances and subscribes them with blackhole inbox URLs can
#: hold hundreds of handlers at the per-IP publish limit. With the deadline the
#: handler returns whatever was delivered by then and the stragglers are
#: cancelled: the relay is at-least-once and subscribers dedupe by the post id
#: inside the payload, so a cancelled HTTPS-inbox delivery is indistinguishable
#: from one that was simply lost. 8 s leaves a healthy fan-out (a WS push is
#: sub-millisecond; a live inbox answers well inside the per-target timeout)
#: entirely untouched.
#:
#: It MUST stay below the household's publish client timeout — the relay POST
#: in ``socialhome.services.gfs_connection_service`` runs under
#: ``aiohttp.ClientTimeout(total=10)`` — so the GFS, not the client, decides
#: when a slow fan-out ends. Two reasons. The client otherwise gives up on a
#: relay the server is still completing and reports a failure that did not
#: happen. And the payload digest is recorded BEFORE the fan-out starts, so a
#: client-side timeout would leave the sender believing it must retry while the
#: GFS suppresses the identical bytes for :data:`PUBLISH_REPLAY_TTL_S`. No
#: retry loop exists today and real payloads are byte-unique per send (each
#: carries its own post id), so nothing is lost in practice — but the ordering
#: is the invariant that keeps it that way.
FAN_OUT_DEADLINE_SECONDS: float = 8.0

#: How long a relayed payload's digest is remembered for replay suppression
#: (seconds). Matches the ±300 s freshness window the §24.11 inbound pipeline
#: and every other replay guard in this codebase use, so "recent" means the
#: same thing everywhere. A capture replayed after the window fans out again —
#: the guard bounds the amplification burst an attacker can drive from one
#: captured frame, it is not a permanent content-id store (the GFS is
#: content-blind and cannot see a post id).
PUBLISH_REPLAY_TTL_S: float = 300.0

#: Hard cap on remembered payload digests. 10 000 × (32-byte key + float) is
#: well under a megabyte and far more distinct publishes than a GFS sees in
#: five minutes; past the cap the oldest entries are evicted, which is also the
#: closest-to-expiring order.
PUBLISH_REPLAY_MAX_ENTRIES: int = 10_000

#: Verify key used on the unknown-instance branch of the LEGACY transport-
#: signature check so that branch does the same Ed25519 verification work as
#: the registered-instance branch — otherwise an unknown instance returns
#: measurably sooner and the endpoint becomes a timing oracle for "is this
#: household registered here?". The Ed25519 public key for the all-zero seed;
#: nothing is ever signed with it, and a signature can never verify under it.
_TIMING_UNIFORM_DUMMY_KEY_HEX: str = (
    "3b6a27bcceb6a42d62a3a8d02a6f0d73653215771de243a63ac048a18b59da29"
)

#: Fixed, well-formed (64-byte) Ed25519 signature fed to the dummy verify on
#: every early rejection. A real Ed25519 verification of a wrong-length
#: signature fails on the length check and costs nothing, so the burn has to
#: use a full-length value to match the work a genuine bad-signature rejection
#: does. It can never verify under any key.
_TIMING_UNIFORM_DUMMY_SIG: bytes = bytes(64)

#: Freshness window for the signed instance-update timestamp (seconds). A
#: ``ts`` further than this from now is treated as a replay and rejected —
#: same ±300 s tolerance the §24.11 inbound pipeline uses.
INSTANCE_UPDATE_TS_SKEW_SECONDS: int = 300

#: Max ``new_subscriber`` re-notifies triggered by a SINGLE subscriber
#: (re)connect (Phase 5b-d). A household subscribed to hundreds of spaces must
#: not turn its own reconnect into an unbounded fan-out of owner notifies (each
#: one makes a seed-holder re-seal a key). Spaces beyond the cap are simply not
#: re-notified on this connect — the Phase-5b-c reconcile (run by every
#: seed-holder on ITS reconnect) still backstops them, so the cap costs latency,
#: never correctness.
MAX_RECONNECT_NOTIFIES: int = 50


def _burn_dummy_verify(event_type: str, space_id: str, payload: object) -> None:
    """Do one throw-away Ed25519 verification on an early-rejection path.

    ``publish_event``'s authority branch returns the SAME opaque 403 for every
    failure, but "same body" is not "same cost": the early rejects (space not
    published, banned, event type not in the allow-set, payload not a signed
    dict, no pinned key, unknown suite, unparseable pinned key) do one DB read
    and no signature verification, while a merely-invalid signature does one
    verification. An anonymous caller could time the difference and learn which
    spaces exist, which are banned and which carry a pinned authority key.

    So each early reject burns one verification over the bytes it WOULD have
    verified, against the same fixed dummy key the legacy branch uses
    (:data:`_TIMING_UNIFORM_DUMMY_KEY_HEX`) and a fixed full-length signature.
    The result is discarded — the caller raises regardless.
    """
    bare = strip_authority_sig_fields(payload) if isinstance(payload, dict) else {}
    message = authority_signing_bytes(
        event_type=event_type,
        space_id=space_id,
        payload=bare,
    )
    verify_ed25519(
        bytes.fromhex(_TIMING_UNIFORM_DUMMY_KEY_HEX),
        message,
        _TIMING_UNIFORM_DUMMY_SIG,
    )


class SeenPayloadCache:
    """Bounded, TTL'd set of recently-relayed payload digests.

    The content-blind GFS cannot dedupe on a post id — that id lives inside the
    encrypted payload. It CAN dedupe on the payload bytes it already holds:
    hashing them leaks nothing it does not already have, and the digest is over
    the SAME canonical JSON encoding the space-authority signature is computed
    against, so two payloads with equal bytes have equal signing input. A
    forged or tampered payload therefore never collides with a legitimate one
    — mutating any field (the signature included) changes the digest.

    In-memory and per-process on purpose: no table, no migration. A GFS restart
    or a second cluster node simply forgets, which costs one extra fan-out of a
    replayed frame and never correctness (the relay is at-least-once and
    subscribers dedupe by post id).
    """

    __slots__ = ("_cap", "_seen", "_ttl")

    def __init__(
        self,
        *,
        ttl_s: float = PUBLISH_REPLAY_TTL_S,
        cap: int = PUBLISH_REPLAY_MAX_ENTRIES,
    ) -> None:
        self._ttl = ttl_s
        self._cap = cap
        # key → monotonic expiry. Insertion order IS expiry order because the
        # TTL is constant, which is what lets the prune stop at the first
        # unexpired entry.
        self._seen: dict[bytes, float] = {}

    @staticmethod
    def digest(payload: object) -> bytes:
        """Return the 32-byte BLAKE2b digest of *payload*'s canonical JSON."""
        canonical = json.dumps(
            payload,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        return hashlib.blake2b(canonical, digest_size=32).digest()

    def seen(self, key: bytes, *, now: float | None = None) -> bool:
        """Return whether *key* was recorded within the TTL."""
        stamp = time.monotonic() if now is None else now
        expires = self._seen.get(key)
        if expires is None:
            return False
        if expires <= stamp:
            del self._seen[key]
            return False
        return True

    def record(self, key: bytes, *, now: float | None = None) -> None:
        """Remember *key* for the TTL, pruning expired + overflowing entries."""
        stamp = time.monotonic() if now is None else now
        self._seen.pop(key, None)  # re-insert at the end to keep expiry order
        self._seen[key] = stamp + self._ttl
        self._prune(stamp)

    def _prune(self, stamp: float) -> None:
        # Front-pop rather than a scan over a COPY of the dict: this runs on
        # every ``record`` (i.e. every accepted publish) and copying up to
        # ``_cap`` items each time made the common case — nothing expired —
        # O(n) in the cache size. Insertion order is expiry order (constant
        # TTL), so the first unexpired entry ends the loop.
        while self._seen:
            key = next(iter(self._seen))
            if self._seen[key] > stamp:
                break
            del self._seen[key]
        overflow = len(self._seen) - self._cap
        if overflow > 0:
            # Only the overflowing prefix is materialised, never the whole dict.
            for key in list(itertools.islice(self._seen, overflow)):
                del self._seen[key]

    def __len__(self) -> int:
        return len(self._seen)


class GfsFederationService:
    """Lightweight federation relay for the GFS process.

    Responsible for:
    * Registering/updating client household instances.
    * Verifying Ed25519 signatures on inbound publish requests.
    * Fanning out events to all subscribers (WS push, HTTPS fallback).
    * Managing space subscription lists.
    * Listing all known global spaces.
    """

    __slots__ = ("_reconnect_tasks", "_repo", "_seen_payloads", "_ws_registry")

    def __init__(
        self,
        repo: AbstractGfsFederationRepo,
        ws_registry: "GfsWebSocketRegistry | None" = None,
    ) -> None:
        self._repo = repo
        self._ws_registry = ws_registry
        self._seen_payloads = SeenPayloadCache()
        # Strong refs to in-flight reconnect-notify tasks — an asyncio task
        # with no reference can be garbage-collected mid-await.
        self._reconnect_tasks: set[asyncio.Task[None]] = set()

    async def register_instance(
        self,
        instance_id: str,
        public_key: str,
        inbox_url: str,
        *,
        display_name: str = "",
        auto_accept: bool = False,
        keywrap_public_key: str = "",
        kem_suite: str = "",
        keywrap_sig: str = "",
    ) -> None:
        """Register or update a client household instance.

        ``keywrap_public_key`` + ``kem_suite`` carry the household's published
        X25519 key-wrap pubkey (Phase 5b foundation) — empty for an older HFS
        that ships none, in which case that household simply can't be
        sealed-to yet. ``keywrap_sig`` is the household's self-signature over
        that key-wrap pubkey so a remote sealer can bind it to the household
        identity end-to-end (``verify_keywrap_binding``) and never trust this
        GFS-served value; empty for an older HFS (unsealable, graceful).
        """
        await self._repo.upsert_instance(
            ClientInstance(
                instance_id=instance_id,
                display_name=display_name,
                public_key=public_key,
                inbox_url=inbox_url,
                status="active" if auto_accept else "pending",
                auto_accept=auto_accept,
                keywrap_public_key=keywrap_public_key,
                kem_suite=kem_suite,
                keywrap_sig=keywrap_sig,
            )
        )
        log.debug("GFS: registered instance %s inbox=%s", instance_id, inbox_url)

    async def publish_event(
        self,
        space_id: str,
        event_type: str,
        payload: object,
        from_instance: str = "",
        signature: str = "",
        *,
        session: aiohttp.ClientSession | None = None,
    ) -> list[str]:
        """Relay an event to all subscribers of *space_id* — **anonymously**.

        The canonical request body is ``{space_id, event_type, payload}``: the
        relaying identity is not part of the contract, so the GFS does not
        REQUIRE, STORE, LOG or FORWARD which household relayed a public/global
        space event.

        That is the honest scope of the guarantee — it is NOT "the GFS cannot
        learn it". A household that relays here usually also holds an
        authenticated ``/gfs/ws`` socket to the same server from the same
        address, so network-level correlation (source IP, timing, body size)
        stays available to whoever operates the GFS. Removing the identity from
        the protocol removes it from the GFS's records and from anything the
        GFS forwards to subscribers; it does not anonymise the TCP connection.

        A second consequence of the anonymity: INSTANCE-level moderation
        (``client_instances.status = 'banned'``) cannot gate this path at all —
        a banned household simply omits the legacy fields and is
        indistinguishable from any other caller. The SPACE-level ban checked
        below is the only moderation lever on the relay path; per-IP shedding
        (``build_publish_rate_limit``) is the only other handle.

        The only authenticator is the **space-authority signature** carried
        inside the (opaque) ``payload``, verified against the space's TOFU-pinned
        ``identity_public_key`` — see :meth:`_authorize_authority_relay`. Any
        seed-holder (owner OR delegated admin) can produce one, so the space
        keeps working while the owner is offline, and the GFS stays blind to
        the content: it verifies a signature over opaque bytes, never decrypts.

        *from_instance* / *signature* are **legacy** fields an older household
        still sends. They are TOLERATED but never trusted: when either is
        present the household transport signature is verified exactly as before
        (a garbage legacy field must not be a free pass), and then the value is
        discarded — it authorizes nothing, never enters the fan-out frame, and
        is never logged.

        Returns the instance_ids successfully notified. Fails closed with
        :class:`PermissionError` on a bad/unverifiable legacy field, an
        unpublished or moderator-``banned`` space, an event type outside
        ``AUTHORITY_RELAY_EVENT_TYPES``, a space with no pinned authority key,
        or a missing / invalid / unknown-suite authority signature.
        """
        # Legacy transport-signature check. Verified when present so a forged
        # legacy field is rejected rather than ignored; the identity it names
        # is NEVER used to authorize the relay.
        if from_instance or signature:
            await self._verify_legacy_publish_sig(
                space_id,
                event_type,
                payload,
                from_instance,
                signature,
            )

        # The space must already be published (no auto-mint of an ownership
        # row from an event — mirrors subscribe).
        existing = await self._repo.get_space(space_id)
        if existing is None:
            _burn_dummy_verify(event_type, space_id, payload)
            raise PermissionError("space not published")
        # A moderator ban is fail-closed: banned content is not relayed, and
        # the check runs BEFORE any fan-out.
        if existing.status == "banned":
            _burn_dummy_verify(event_type, space_id, payload)
            raise PermissionError("space is banned")
        # A withdrawn space is delisted from the DIRECTORY only (see
        # ``unpublish_space``) — households that already subscribed keep
        # receiving the relay, so ``withdrawn`` is deliberately not a reject.

        self._authorize_authority_relay(space_id, event_type, payload, existing)

        # Replay suppression — AFTER authorization, never before. Recording a
        # rejected payload would let a forged frame mute the legitimate relay
        # of the same bytes (e.g. one sent before the space healed its pin).
        # A forged payload can't pre-poison a legitimate one either: any change
        # to the payload — the signature field included — changes the digest.
        digest = SeenPayloadCache.digest(payload)
        if self._seen_payloads.seen(digest):
            log.debug(
                "GFS: suppressing replayed %s for space %s (identical payload)",
                event_type,
                space_id,
            )
            # Idempotent no-op: the route answers 200 with ``delivered_to: 0``,
            # so a replayer learns nothing a first publish wouldn't also show.
            return []
        self._seen_payloads.record(digest)

        subscribers = await self._repo.list_subscribers(space_id)

        # Identity-free fan-out frame: routing fields only. No ``from_instance``
        # and no GFS-added target id — a subscriber learns nothing about which
        # household relayed the event, and dedupes on the post id inside the
        # encrypted payload.
        event_body = {
            "space_id": space_id,
            "event_type": event_type,
            "payload": payload,
        }
        log.debug(
            "GFS: relaying %s for space %s to %d subscriber(s)",
            event_type,
            space_id,
            len(subscribers),
        )

        return await self._fan_out(subscribers, event_body, session)

    async def _verify_legacy_publish_sig(
        self,
        space_id: str,
        event_type: str,
        payload: object,
        from_instance: str,
        signature: str,
    ) -> None:
        """Verify the LEGACY household transport signature on a publish.

        Older households POST ``{space_id, event_type, payload, from_instance,
        signature}`` where *signature* is that household's Ed25519 signature
        over the canonical JSON of the first four fields. Those requests must
        keep working, but a present-but-bogus legacy field must not be a free
        pass, so the signature is verified exactly as it used to be — and then
        the identity is dropped on the floor (it authorizes nothing).

        Fails closed with :class:`PermissionError` when one legacy field is
        supplied without the other, the named instance is unregistered or
        banned, or the signature is malformed / doesn't verify. Every failure
        raises the SAME message and never echoes *from_instance*, so the
        endpoint is not an instance-existence oracle by MESSAGE. It is not an
        oracle by TIMING either: the unknown / banned-instance branch verifies
        the supplied signature against a fixed dummy key
        (:data:`_TIMING_UNIFORM_DUMMY_KEY_HEX`) so it performs the same one DB
        read plus one Ed25519 verification as the registered-instance branch,
        instead of returning as soon as the lookup misses.
        """
        if not from_instance or not signature:
            raise PermissionError("Invalid Ed25519 signature")
        inst = await self._repo.get_instance(from_instance)
        known = inst is not None and inst.status != "banned"
        canonical = json.dumps(
            {
                "space_id": space_id,
                "event_type": event_type,
                "payload": payload,
                "from_instance": from_instance,
            },
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        key_hex = (
            inst.public_key
            if known and inst is not None
            else _TIMING_UNIFORM_DUMMY_KEY_HEX
        )
        try:
            # A malformed stored pubkey is unverifiable — fail closed as a
            # 403 rather than escaping the handler as a 500.
            raw_key = bytes.fromhex(key_hex)
            raw_sig = b64url_decode(signature)
        except (ValueError, TypeError) as exc:
            raise PermissionError("Invalid Ed25519 signature") from exc
        verified = verify_ed25519(raw_key, canonical, raw_sig)
        if not known or not verified:
            raise PermissionError("Invalid Ed25519 signature")

    def _authorize_authority_relay(
        self,
        space_id: str,
        event_type: str,
        payload: object,
        existing: GlobalSpace,
    ) -> None:
        """Authorize a relay via the space-authority signature — the ONLY
        authenticator ``publish_event`` accepts.

        Raises :class:`PermissionError` unless the ``payload`` carries a valid
        space-authority signature verifiable against the space's TOFU-pinned
        public key. There is no owner exemption: the retired
        ``from_instance == owning_instance`` path let an owner relay ANY event
        type with no authority signature at all, and it also required the GFS
        to know who was relaying. Fail-closed in every case:

        * wire ``event_type`` isn't one the authority sig authorizes (the
          ``AUTHORITY_RELAY_EVENT_TYPES`` set — ``space_post_public`` or
          ``space_subscriber_key_handoff``) → reject;
        * payload isn't a signed dict / no ``authority_sig`` → reject;
        * no pinned pubkey on the space → reject (GFS can't verify authority;
          the owner re-publishes the space metadata to heal the pin);
        * unknown authority suite → reject (no default fallback);
        * signature present but doesn't verify → reject.

        The GFS never decrypts ``payload`` — it only verifies the Ed25519
        authority signature over the payload bytes (with the two signature
        fields stripped, mirroring the signer).

        Replay/dedupe contract: the authority signature binds the space id +
        payload but NO timestamp / nonce / epoch, so a captured authority-signed
        payload stays valid forever and can be re-POSTed by anyone who saw it.
        The GFS can't dedupe on a post id — that lives inside the encrypted
        payload — but it CAN dedupe on the payload BYTES it already holds:
        ``publish_event`` keeps a :class:`SeenPayloadCache` of BLAKE2b digests
        over the same canonical JSON the authority signature is computed
        against, and a digest seen within :data:`PUBLISH_REPLAY_TTL_S` makes the
        relay an idempotent no-op (200, ``delivered_to: 0``, no fan-out). That
        is what bounds the amplification a single captured frame can drive at
        the per-IP publish limit — each replay would otherwise cost every
        subscriber another copy of the body.

        The cache is in-memory, TTL'd and capped, so it is a burst bound, not a
        permanent content-id store: past the TTL (or after a restart, or on a
        second cluster node) the same bytes fan out once more. The standing
        backstop is therefore still SUBSCRIBER-side dedupe by the post id
        carried inside the payload — enforced by the HFS
        ``space_public_inbound`` consumer (the same way moments dedupe by
        moment_id). The relay stays at-least-once.
        See ``docs/protocol/discovery.md``.
        """
        # The authority signature authorizes only the event types in
        # ``AUTHORITY_RELAY_EVENT_TYPES`` (``space_post_public`` and the
        # Phase-5b ``space_subscriber_key_handoff``). Bind the caller-supplied
        # WIRE event_type to one of those AND verify the signature under that
        # SAME type below — a relayer holding one valid payload can't relay it
        # under an arbitrary type (e.g. ``space_admin_action``), and a payload
        # signed for one allowed type can't be replayed under the other (the
        # signing bytes bind the event type).
        if event_type not in AUTHORITY_RELAY_EVENT_TYPES:
            _burn_dummy_verify(event_type, space_id, payload)
            raise PermissionError(
                "authority relay only permits the "
                f"{sorted(AUTHORITY_RELAY_EVENT_TYPES)!r} event types",
            )
        if not isinstance(payload, dict) or "authority_sig" not in payload:
            _burn_dummy_verify(event_type, space_id, payload)
            raise PermissionError("missing space-authority signature")
        if not existing.identity_public_key:
            # No TOFU-pinned key → the GFS cannot verify a space-authority
            # signature, so nothing may be relayed for this space until the
            # owner re-publishes its metadata and pins one.
            _burn_dummy_verify(event_type, space_id, payload)
            raise PermissionError(
                "no pinned authority key for this space",
            )
        authority_sig = payload.get("authority_sig") or ""
        authority_sig_suite = payload.get("authority_sig_suite") or ""
        try:
            ok = verify_authority_event(
                event_type=event_type,
                space_id=space_id,
                payload=strip_authority_sig_fields(payload),
                authority_sig=str(authority_sig),
                authority_sig_suite=str(authority_sig_suite),
                space_public_key=bytes.fromhex(existing.identity_public_key),
            )
        except UnsupportedAuthoritySuite as exc:
            # The suite check fires before any verification — burn one so an
            # unknown suite costs what a bad signature costs.
            _burn_dummy_verify(event_type, space_id, payload)
            raise PermissionError(
                f"unknown authority signature suite: {exc}",
            ) from exc
        except ValueError as exc:
            # Malformed pinned pubkey hex — treat as unverifiable, fail-closed.
            # ``bytes.fromhex`` raises while building the call arguments, so
            # nothing was verified: burn one.
            _burn_dummy_verify(event_type, space_id, payload)
            raise PermissionError("invalid authority key") from exc
        if not ok:
            raise PermissionError("invalid authority signature")

    @staticmethod
    def _assert_fresh_ts(ts: object) -> None:
        """Raise ``PermissionError`` unless *ts* is a fresh timestamp.

        Fresh means a tz-aware ISO 8601 string within ±300 s of now. A
        missing, non-string, unparseable or naive value is untrusted and
        rejected — a missing UTC offset can't be interpreted safely, so it
        is treated exactly like a stale one.
        """
        if not isinstance(ts, str) or not ts:
            raise PermissionError("Stale timestamp")
        try:
            parsed = datetime.fromisoformat(ts)
        except (ValueError, TypeError) as exc:
            raise PermissionError("Stale timestamp") from exc
        if parsed.tzinfo is None:
            raise PermissionError("Stale timestamp")
        now = datetime.now(timezone.utc)
        if abs((now - parsed).total_seconds()) > INSTANCE_UPDATE_TS_SKEW_SECONDS:
            raise PermissionError("Stale timestamp")

    async def _verify_signed_request(
        self,
        instance_id: str,
        payload: dict[str, object],
        *,
        signature: str,
    ) -> ClientInstance:
        """Verify a self-signed, replay-guarded request from *instance_id*.

        Shared by every ``{instance_id, ..., ts}``-shaped GFS request
        (``update_instance``, ``subscribe``, ``unsubscribe``). The Ed25519
        *signature* is checked over the canonical JSON of *payload* against
        the instance's REGISTERED public key — because ``instance_id`` is
        part of the signed payload, a caller can only act as **itself**.
        The freshness check reads ``payload["ts"]`` — the very timestamp
        the signature covers, so a caller can never have a signature
        verified over one timestamp and freshness-checked against another.
        The signed ``ts`` must be a fresh, tz-aware ISO 8601 timestamp
        within ±300 s of now; unparseable / naive timestamps are rejected
        (a missing offset is treated as untrusted).

        Fails closed with :class:`PermissionError` on an unknown instance,
        a missing / malformed / non-verifying signature, or a stale ``ts``.
        Returns the looked-up :class:`ClientInstance` so callers can reuse
        it without a second read.
        """
        inst = await self._repo.get_instance(instance_id)
        if inst is None:
            raise PermissionError(f"Unknown instance: {instance_id}")

        # Signature is REQUIRED here (unlike publish_space's optional-sig
        # branch): an empty or invalid signature is a hard PermissionError.
        if not signature:
            raise PermissionError("Invalid Ed25519 signature")
        canonical = json.dumps(
            payload,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        try:
            # A malformed stored pubkey is just as unverifiable as a
            # malformed signature — fail closed with the same
            # PermissionError instead of escaping the handler as a 500.
            raw_key = bytes.fromhex(inst.public_key)
            raw_sig = b64url_decode(signature)
        except (ValueError, TypeError) as exc:
            raise PermissionError("Invalid Ed25519 signature") from exc
        if not verify_ed25519(raw_key, canonical, raw_sig):
            raise PermissionError("Invalid Ed25519 signature")

        # ``.get`` (not ``[...]``): a future caller that forgets ``ts`` must
        # fail closed as a 403, never escape the handler as a KeyError/500 —
        # the freshness contract enforces itself.
        self._assert_fresh_ts(payload.get("ts"))
        return inst

    async def subscribe(
        self,
        instance_id: str,
        space_id: str,
        ts: str,
        signature: str,
    ) -> None:
        """Add *instance_id* as a subscriber of *space_id*.

        Authenticated the same way as :meth:`update_instance`: the request
        is signed by *instance_id* over the canonical ``{action:
        "subscribe", instance_id, space_id, ts}`` JSON and verified against
        the instance's registered public key. The ``action`` discriminator
        is part of the signed bytes (domain separation), so a captured
        subscribe signature can never be replayed as an unsubscribe. Because the signature binds to the *instance_id* in the
        body, a caller can only subscribe **itself** — it can't sign as
        another household. The signed ``ts`` is replay-guarded (±300 s).

        Rejects (``PermissionError``) an unknown instance, a missing /
        malformed / invalid signature, a stale timestamp, and a space the
        GFS has never seen published (no auto-creation of a pending row from
        an unauthenticated demand signal — a subscription must target a real
        published space).
        """
        inst = await self._verify_signed_request(
            instance_id,
            {
                "action": "subscribe",
                "instance_id": instance_id,
                "space_id": space_id,
                "ts": ts,
            },
            signature=signature,
        )

        # A subscription must target a space the GFS already knows about.
        # Auto-minting a row from an (un)authenticated subscribe let any
        # caller seed arbitrary space ids — reject unknown ids instead.
        existing = await self._repo.get_space(space_id)
        if existing is None:
            raise PermissionError("space not published")

        await self._repo.add_subscriber(
            space_id=space_id,
            instance_id=instance_id,
        )
        log.debug("GFS: %s subscribed to space %s", instance_id, space_id)

        # Phase 5b-b — best-effort notify the space OWNER so a seed-holder can
        # seal the per-space content key to this new subscriber (the GFS stays
        # blind: it only forwards the subscriber's already-published identity +
        # key-wrap material, never the key). Only the owner is notified here
        # because the GFS authoritatively knows ``owning_instance``; a
        # delegated admin catches up via the 5b-c reconcile (pulling the
        # subscriber list). If the owner is offline (no WS), the frame is
        # dropped — that, too, is the reconcile's job, NOT a subscribe failure.
        await self._notify_owner_new_subscriber(
            existing.owning_instance, space_id, inst
        )

    async def _notify_owner_new_subscriber(
        self,
        owning_instance: str,
        space_id: str,
        subscriber: ClientInstance,
    ) -> None:
        """Push a ``new_subscriber`` frame to the space owner's WS (best-effort).

        Carries the subscriber's registered Ed25519 identity ``public_key`` +
        its published key-wrap pubkey/suite/self-signature so the owner can
        verify the key-wrap binding end-to-end (``verify_keywrap_binding``,
        anti-GFS-substitution) before sealing. Never raises — a missing socket
        or a send error is logged and swallowed (the 5b-c reconcile backstops
        an offline owner)."""
        if self._ws_registry is None:
            return
        try:
            await self._ws_registry.send(
                owning_instance,
                {
                    "type": "new_subscriber",
                    "space_id": space_id,
                    "subscriber": {
                        "instance_id": subscriber.instance_id,
                        "identity_public_key": subscriber.public_key,
                        "keywrap_public_key": subscriber.keywrap_public_key,
                        "kem_suite": subscriber.kem_suite,
                        "keywrap_sig": subscriber.keywrap_sig,
                    },
                },
            )
        except Exception as exc:  # defensive — never fail a subscribe on notify
            log.warning(
                "GFS: new_subscriber notify to owner %s for space %s failed: %s",
                owning_instance,
                space_id,
                exc,
            )

    def schedule_subscriber_connected(
        self,
        instance_id: str,
    ) -> None:
        """Fire-and-forget :meth:`on_subscriber_connected` for *instance_id*.

        Called from the ``/gfs/ws`` handler AFTER the hello is authenticated
        and the socket registered. Deliberately synchronous + detached: the
        notify fan-out must never delay (or fail) the WebSocket handshake.
        """
        task = asyncio.create_task(self.on_subscriber_connected(instance_id))
        self._reconnect_tasks.add(task)
        task.add_done_callback(self._reconnect_tasks.discard)

    async def on_subscriber_connected(self, instance_id: str) -> None:
        """Re-emit the ``new_subscriber`` notify for every space *instance_id*
        subscribes to (Phase 5b-d — subscriber-side reconnect repair).

        The 5b-b handoff is relayed back to the subscriber over ITS OWN GFS
        socket. If that socket was down when the seal was fanned out, the key
        is simply lost: the HTTPS-inbox fallback cannot deliver a relay frame
        to a household (wrong path shape + unsigned body — see ``_fan_out``),
        nothing retries, and the 5b-c reconcile only fires when a SEED-HOLDER
        reconnects, not the subscriber. So when the subscriber's own socket
        comes up we ask each space owner to run the exact same verified
        seal-and-relay again — this time with the socket up to receive it.

        The GFS is content-blind: it never saw the sealed payload and cannot
        store or replay it, so asking the owner to re-seal is the only repair
        available here. It needs no new table, event type or key, and is
        idempotent — the subscriber's ``apply_space_content_key_from_metadata``
        import is per-epoch idempotent, so a duplicate handoff is a no-op.

        Best-effort and **never raises**: this runs on a connect path, where a
        missing instance, a repo error, an owner with no socket or a send
        failure must be logged and skipped, never break the handshake.
        """
        if self._ws_registry is None:
            return
        try:
            subscriber = await self._repo.get_instance(instance_id)
            if subscriber is None:
                return
            spaces = await self._repo.list_subscribed_spaces(instance_id)
        except Exception as exc:  # defensive — connect path, never raise
            log.warning(
                "GFS: reconnect notify lookup for %s failed: %s",
                instance_id,
                exc,
            )
            return

        sent = 0
        for space in spaces:
            # An owner needs no handoff to itself.
            if space.owning_instance == instance_id:
                continue
            if sent >= MAX_RECONNECT_NOTIFIES:
                log.info(
                    "GFS: reconnect notify for %s capped at %d spaces "
                    "(the 5b-c reconcile covers the rest)",
                    instance_id,
                    MAX_RECONNECT_NOTIFIES,
                )
                break
            # Already best-effort / never-raises (offline owner, send error).
            await self._notify_owner_new_subscriber(
                space.owning_instance,
                space.space_id,
                subscriber,
            )
            sent += 1
        if sent:
            log.debug(
                "GFS: re-notified %d space owner(s) after %s connected",
                sent,
                instance_id,
            )

    async def list_subscribers_with_keys(
        self,
        space_id: str,
        *,
        ts: str,
        authority_sig: str,
        authority_sig_suite: str,
    ) -> list[GfsSubscriberWithKeys]:
        """Release the subscriber list to a verified SEED-HOLDER (Phase-5b-c).

        Authorization mirrors the space-authority relay path: the caller proves
        it holds the space seed (owner OR delegated admin) by signing
        ``{space_id, ts}`` under :data:`AUTHORITY_EVENT_SPACE_SUBSCRIBERS_QUERY`
        with the seed; the GFS verifies it against the TOFU-pinned space public
        key it already stores (``global_spaces.identity_public_key``) — no new
        roster or key. Fail-closed (:class:`PermissionError`) on every failure:

        * unknown space, or a space with no pinned authority pubkey (the GFS
          can't verify a seed-holder, so it releases nothing);
        * stale ``ts`` (±300 s replay guard, same window as ``subscribe``);
        * missing / malformed / unknown-suite / non-verifying signature.

        The signing bytes bind the event type AND the space id, so a query
        signed for another space (or under the relay event type) can't be
        replayed here. Returns the subscriber rows (instance ids + their
        already-registered identity / key-wrap pubkeys) on success.
        """
        existing = await self._repo.get_space(space_id)
        if existing is None or not existing.identity_public_key:
            # No row, or no pinned key → nothing verifiable → release nothing.
            raise PermissionError("no pinned authority key for this space")

        # Replay guard FIRST (cheap, and independent of the signature): a
        # fresh, tz-aware ISO 8601 ``ts`` within ±300 s of now. Naive /
        # unparseable timestamps are rejected (a missing offset is untrusted).
        self._assert_fresh_ts(ts)

        if not authority_sig:
            raise PermissionError("invalid authority signature")
        try:
            ok = verify_authority_event(
                event_type=AUTHORITY_EVENT_SPACE_SUBSCRIBERS_QUERY,
                space_id=space_id,
                payload={"space_id": space_id, "ts": ts},
                authority_sig=authority_sig,
                authority_sig_suite=authority_sig_suite,
                space_public_key=bytes.fromhex(existing.identity_public_key),
            )
        except UnsupportedAuthoritySuite as exc:
            raise PermissionError(
                f"unknown authority signature suite: {exc}",
            ) from exc
        except ValueError as exc:
            raise PermissionError("invalid authority key") from exc
        if not ok:
            raise PermissionError("invalid authority signature")

        return await self._repo.list_subscribers_with_keys(space_id)

    async def unsubscribe(
        self,
        instance_id: str,
        space_id: str,
        ts: str,
        signature: str,
    ) -> None:
        """Remove *instance_id* from the subscribers of *space_id*.

        Authenticated exactly like :meth:`subscribe`: an Ed25519 signature
        over the canonical ``{action: "unsubscribe", instance_id, space_id,
        ts}`` JSON — the ``action`` is inside the signed bytes, so an
        unsubscribe signature can't be replayed as a subscribe — verified
        against the instance's registered public key and replay-guarded
        (±300 s). The signature binds the request to *instance_id*, so a
        caller can only unsubscribe **itself** — without this, any caller
        could evict any household from any space's relay fan-out.

        Fails closed (``PermissionError``) on an unknown instance, a
        missing / malformed / invalid signature, or a stale / naive
        timestamp. Unlike :meth:`subscribe` it deliberately does NOT
        require the space to still exist: unsubscribing from an
        already-removed space stays idempotent.
        """
        await self._verify_signed_request(
            instance_id,
            {
                "action": "unsubscribe",
                "instance_id": instance_id,
                "space_id": space_id,
                "ts": ts,
            },
            signature=signature,
        )
        await self._repo.remove_subscriber(
            space_id=space_id,
            instance_id=instance_id,
        )
        log.debug("GFS: %s unsubscribed from space %s", instance_id, space_id)

    async def list_spaces(
        self,
        *,
        status: str | None = None,
    ) -> list[GlobalSpace]:
        """Return global/public spaces known to this GFS node.

        The public ``GET /gfs/spaces`` endpoint passes ``status='active'``
        to hide pending + banned rows. Internal callers (admin, tests)
        can pass ``status=None`` to see everything.
        """
        return await self._repo.list_spaces(status=status)

    async def get_space(self, space_id: str) -> GlobalSpace | None:
        """Single-space lookup — used by ``GET /gfs/spaces/{id}`` so SH
        clients fetching the metadata for a discovery-link can mirror
        the space row locally before subscribing."""
        return await self._repo.get_space(space_id)

    async def hide_space(
        self,
        space_id: str,
        owning_instance: str,
        ts: str,
        signature: str,
    ) -> None:
        """Withdraw a space listing at its OWNER's signed request.

        Drives ``POST|DELETE /gfs/spaces/{id}/unpublish``. Authenticated
        exactly like :meth:`subscribe` / :meth:`unsubscribe`: an Ed25519
        signature over the canonical ``{action: "unpublish", owning_instance,
        space_id, ts}`` JSON, verified against *owning_instance*'s registered
        public key and replay-guarded (±300 s). The ``action`` is inside the
        signed bytes, so a captured subscribe/unsubscribe signature can never
        be replayed as a delisting.

        Authentication alone is not enough: a signature only proves WHICH
        registered household is calling, so the caller must additionally BE
        the space's ``owning_instance`` — otherwise any paired household that
        learned a space id (they travel in discovery links) could delist
        someone else's space.

        Sets the reversible ``withdrawn`` flag and NEVER touches ``status``:
        ``banned`` is the GFS moderator's verdict and is deliberately sticky
        against re-publish, so writing it here permanently locked an owner out
        of its own listing. The row itself survives (audit trail, subscriber
        list, TOFU-pinned authority pubkey) and the owner's next signed
        publish clears the flag. Withdrawal affects DISCOVERY only — the relay
        and existing subscribers are untouched (``docs/protocol/discovery.md``).

        Fails closed with :class:`PermissionError` on an unknown instance, a
        missing / malformed / invalid signature, a stale ``ts``, or a caller
        that is not the owner. An unknown space stays a silent no-op (the
        unpublish fan-out must be idempotent) — but only AFTER the signature
        verifies, so space existence is never leaked to an unsigned caller.
        """
        await self._verify_signed_request(
            owning_instance,
            {
                "action": "unpublish",
                "owning_instance": owning_instance,
                "space_id": space_id,
                "ts": ts,
            },
            signature=signature,
        )
        existing = await self._repo.get_space(space_id)
        if existing is None:
            return
        if existing.owning_instance != owning_instance:
            raise PermissionError("not the owner of this space")
        await self._repo.set_space_withdrawn(space_id, True)
        log.info("GFS: owner %s withdrew space %s", owning_instance, space_id)

    async def publish_space(
        self,
        *,
        space_id: str,
        owning_instance: str,
        name: str,
        description: str | None = None,
        about_markdown: str | None = None,
        cover_url: str | None = None,
        icon_url: str | None = None,
        min_age: int = 0,
        category: str = "general",
        accent_color: str = "#D2542A",
        primary_color: str = "#D2542A",
        identity_public_key: str = "",
        signature: str = "",
        ts: str = "",
    ) -> GlobalSpace:
        """Register / refresh a space row from the owning instance.

        Drives the ``POST /gfs/spaces/{id}/publish`` route. The publish
        body is signed by the owning HFS so a malicious peer can't
        flip another household's space metadata; signature is verified
        against the registered ``ClientInstance.public_key``.
        Auto-accepted clients land as ``status='active'`` (visible on
        ``GET /gfs/spaces``); pending clients stay pending until the
        GFS admin flips them.

        ``ts`` is an OPTIONAL tz-aware ISO 8601 timestamp. When present it is
        part of the signed canonical body and replay-guarded (±300 s), which
        makes the publish a *fresh* statement of intent — only such a publish
        may clear an owner's earlier ``withdrawn`` flag. A publish without
        ``ts`` (an older household) still registers and refreshes metadata,
        but CANNOT restore a withdrawn listing: its body is replayable
        forever, so honouring it would let anyone holding one historical
        publish body re-list a space its owner deliberately delisted.
        """
        inst = await self._repo.get_instance(owning_instance)
        if inst is None:
            raise PermissionError(
                f"Unknown owning_instance: {owning_instance}",
            )
        # Signature is MANDATORY (same trust model as update_instance): an
        # empty / malformed / invalid signature is a hard PermissionError, so
        # a registered peer can't overwrite another household's space listing.
        if not signature:
            raise PermissionError("Invalid Ed25519 signature")
        signed: dict[str, object] = {
            "space_id": space_id,
            "owning_instance": owning_instance,
            "name": name,
            "description": description or "",
            "about_markdown": about_markdown or "",
            "cover_url": cover_url or "",
            "icon_url": icon_url or "",
            "min_age": min_age,
            "category": category,
            "accent_color": accent_color,
            "primary_color": primary_color,
            "identity_public_key": identity_public_key or "",
        }
        # The signed ``ts`` is OPTIONAL, for backward compatibility: unlike
        # subscribe/unsubscribe, ``publish_space`` has shipped production
        # callers, so hard-requiring ``ts`` would 403 every older household
        # against an upgraded GFS during a mixed-version window. This is the
        # documented "first-revision payloads missing the field default to the
        # single supported value" shape — with the security-relevant half
        # (clearing ``withdrawn``) gated on the replay-guarded variant.
        #
        # MIGRATION TRIPWIRE: once every household ships ``ts`` (it has been
        # sent by ``GfsConnectionService._build_publish_body`` since this
        # revision), delete the no-``ts`` branch below and make ``ts``
        # mandatory — verify it the way ``_verify_signed_request`` does.
        has_ts = bool(ts)
        if has_ts:
            signed["ts"] = ts
        canonical = json.dumps(
            signed,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        try:
            # A malformed stored pubkey is just as unverifiable as a malformed
            # signature — fail closed with the same PermissionError instead of
            # escaping the handler as a 500 (``register_instance`` never
            # validates that ``public_key`` is hex).
            raw_key = bytes.fromhex(inst.public_key)
            raw_sig = b64url_decode(signature)
        except (ValueError, TypeError) as exc:
            raise PermissionError("Invalid Ed25519 signature") from exc
        if not verify_ed25519(raw_key, canonical, raw_sig):
            raise PermissionError("Invalid Ed25519 signature")
        if has_ts:
            # Freshness is checked over the very ``ts`` the signature covers,
            # so a captured publish body can't be replayed to un-do the
            # owner's later withdrawal.
            self._assert_fresh_ts(ts)
        # Bound the stored ``about_markdown`` (verified above against the
        # full value, so the signature still holds). The public page caps
        # rendering too; capping at storage avoids DB bloat from a paired
        # instance publishing an oversized blob. Truncate rather than
        # reject so a slightly-long About still publishes.
        if about_markdown and len(about_markdown) > MAX_ABOUT_MARKDOWN_CHARS:
            about_markdown = about_markdown[:MAX_ABOUT_MARKDOWN_CHARS]
        existing = await self._repo.get_space(space_id)
        # Owner is immutable after first publish. space_id is a public,
        # owner-chosen UUID that travels in discovery links, so a registered
        # peer that learns it could otherwise re-publish the row with
        # owning_instance=itself — validly signed by its OWN key — and seize
        # the listing. Reject any publish whose owner differs from the stored
        # one (first-publisher wins; only that instance can refresh it).
        if existing is not None and existing.owning_instance != owning_instance:
            raise PermissionError("space already owned by another instance")
        # TOFU-pin the space's authority public key. The FIRST publish that
        # carries one pins it; thereafter it is IMMUTABLE — a later publish
        # offering a DIFFERENT pubkey keeps the pinned one (and logs a warning
        # so a swap attempt is diagnosable). This is what lets the GFS trust an
        # authority-signed relay from a non-owner seed-holder: the verify key
        # is established once by the owner and can't be silently rotated by a
        # later (possibly compromised-transport) publish. A first publish with
        # an empty pubkey leaves it NULL — that space can't use authority-signed
        # relay until a pubkey is pinned. The owner is already immutable
        # (checked above), so only the legit owner could ever pin/refresh.
        pinned_pubkey = identity_public_key or ""
        if existing is not None and existing.identity_public_key:
            if pinned_pubkey and pinned_pubkey != existing.identity_public_key:
                log.warning(
                    "GFS: ignoring attempt to change pinned authority pubkey "
                    "for space %s (pinned=%s…, offered=%s…)",
                    space_id,
                    existing.identity_public_key[:8],
                    pinned_pubkey[:8],
                )
            pinned_pubkey = existing.identity_public_key
        # Preserve subscriber_count / posts_per_week / published_at from
        # the existing row — those are GFS-side bookkeeping, not the
        # owner's to declare. Only the owner's name / description /
        # cover travel with the publish.
        next_status = "active" if inst.auto_accept else "pending"
        if existing is not None and existing.status == "banned":
            next_status = "banned"
        space = GlobalSpace(
            space_id=space_id,
            owning_instance=owning_instance,
            name=name,
            description=description,
            about_markdown=about_markdown,
            cover_url=cover_url,
            icon_url=icon_url,
            min_age=min_age,
            category=normalize_category(category),
            accent_color=accent_color,
            primary_color=primary_color,
            status=next_status,
            subscriber_count=existing.subscriber_count if existing else 0,
            posts_per_week=existing.posts_per_week if existing else 0.0,
            published_at=existing.published_at if existing else "",
            identity_public_key=pinned_pubkey,
            # A FRESH signed publish from the owner (one carrying a signed,
            # replay-guarded ``ts``) is the RECOVERY path for an earlier
            # withdrawal — it restores discoverability. A legacy publish with
            # no ``ts`` is replayable, so it preserves the current flag
            # instead. A moderator ``banned`` status is handled above and
            # stays sticky either way.
            withdrawn=(existing.withdrawn if existing and not has_ts else False),
        )
        if existing is not None and existing.withdrawn and not has_ts:
            log.info(
                "GFS: publish for withdrawn space %s carried no signed ts — "
                "listing stays withdrawn (upgrade the household so its "
                "publish body includes a timestamp)",
                space_id,
            )
        await self._repo.upsert_space(space)
        log.info(
            "GFS: published space %s (owner=%s, status=%s)",
            space_id,
            owning_instance,
            next_status,
        )
        return space

    async def update_instance(
        self,
        instance_id: str,
        display_name: str,
        ts: str,
        signature: str,
    ) -> None:
        """Update a registered instance's display_name. Signed by the
        instance and verified against its registered public key (same
        trust model as publish_space — a peer can't rename another
        household). Rejects unknown instances, bad signatures, and stale
        timestamps (replay guard)."""
        await self._verify_signed_request(
            instance_id,
            {
                "instance_id": instance_id,
                "display_name": display_name,
                "ts": ts,
            },
            signature=signature,
        )

        cleaned = display_name.strip()
        if not cleaned or len(cleaned) > MAX_DISPLAY_NAME_CHARS:
            raise ValueError("display_name must be 1-80 chars")

        await self._repo.set_instance_display_name(instance_id, cleaned)
        log.info("GFS: instance %s renamed to %r", instance_id, cleaned)

    # ── Fan-out ──────────────────────────────────────────────────────────

    async def _fan_out(
        self,
        subscribers: list[GfsSubscriber],
        event_body: dict,
        session: aiohttp.ClientSession | None,
    ) -> list[str]:
        """Deliver *event_body* to each subscriber.

        Tries the SH↔GFS WebSocket first (push frame ``{type:"relay", ...}``).
        If no socket is registered for the subscriber or the send fails,
        falls back to an HTTPS POST to the subscriber's inbox URL.

        Delivery is CONCURRENT but bounded by :data:`FAN_OUT_CONCURRENCY`.
        Sequentially, one accepted publish held its request handler for up to
        ``len(subscribers) × FAN_OUT_TIMEOUT_SECONDS`` — an amplification
        handle for an anonymous caller — while unbounded concurrency would let
        one publish open a socket per subscriber. Returns the ids actually
        reached, in subscriber order.

        Bounded concurrency caps the sockets, not the WALL CLOCK: with every
        target blackholed the handler is still pinned for
        ``ceil(N / FAN_OUT_CONCURRENCY) × FAN_OUT_TIMEOUT_SECONDS``. So the
        whole fan-out also runs under a :data:`FAN_OUT_DEADLINE_SECONDS`
        deadline — on expiry the stragglers are cancelled and the PARTIAL list
        of ids reached so far is returned. Cancelling an in-flight HTTPS-inbox
        POST is equivalent to that delivery being lost, which the at-least-once
        relay (plus subscriber-side dedupe by post id) already tolerates.
        """
        own_session = session is None
        active: aiohttp.ClientSession = (
            session if session is not None else aiohttp.ClientSession()
        )
        push_frame = {"type": "relay", **event_body}
        limit = asyncio.Semaphore(FAN_OUT_CONCURRENCY)
        # Slot-per-subscriber rather than gather()'s return value: on the
        # deadline path gather is cancelled and yields nothing, so the results
        # have to be recorded as they land — and by index, to keep the partial
        # list in subscriber order.
        reached: list[str | None] = [None] * len(subscribers)

        async def _deliver(index: int, sub: GfsSubscriber) -> None:
            async with limit:
                reached[index] = await self._deliver_one(
                    sub, push_frame, event_body, active
                )

        tasks = [
            asyncio.create_task(_deliver(i, sub)) for i, sub in enumerate(subscribers)
        ]
        try:
            await asyncio.wait_for(
                asyncio.gather(*tasks),
                timeout=FAN_OUT_DEADLINE_SECONDS,
            )
        except TimeoutError:
            log.info(
                "GFS fan-out: deadline of %.1fs hit — delivered to %d of %d "
                "subscriber(s), cancelling the rest",
                FAN_OUT_DEADLINE_SECONDS,
                sum(1 for r in reached if r is not None),
                len(subscribers),
            )
        finally:
            pending = [task for task in tasks if not task.done()]
            for task in pending:
                task.cancel()
            if pending:
                # Await the cancellations so no delivery is still touching the
                # session when it closes below.
                await asyncio.gather(*pending, return_exceptions=True)
            if own_session:
                await active.close()
        return [instance_id for instance_id in reached if instance_id is not None]

    async def _deliver_one(
        self,
        sub: GfsSubscriber,
        push_frame: dict,
        event_body: dict,
        session: aiohttp.ClientSession,
    ) -> str | None:
        """Deliver to ONE subscriber; return its id iff it was reached."""
        # WebSocket push first.
        if self._ws_registry is not None and await self._ws_registry.send(
            sub.instance_id,
            push_frame,
        ):
            return sub.instance_id

        # HTTPS-inbox fallback.
        try:
            async with session.post(
                sub.inbox_url,
                json=event_body,
                timeout=aiohttp.ClientTimeout(total=FAN_OUT_TIMEOUT_SECONDS),
            ) as resp:
                if resp.status < 400:
                    return sub.instance_id
                # DEBUG, not WARNING, on purpose: a household's registered
                # ``inbox_url`` is ``<base>/federation/inbox`` while its actual
                # route is ``/federation/inbox/{inbox_id}``, and the body posted
                # here is a bare relay frame rather than a signed §24.11
                # envelope — so this fallback is STRUCTURALLY guaranteed to
                # 401/404 for an offline subscriber. Keeping it at WARNING
                # spammed operator logs with a non-actionable error on every
                # offline peer. The fallback itself stays (other inbox shapes do
                # accept it); fixing the URL / envelope mismatch is a separate
                # design change.
                log.debug(
                    "GFS fan-out: %s returned HTTP %s (subscriber likely offline)",
                    sub.inbox_url,
                    resp.status,
                )
        except Exception as exc:
            log.warning(
                "GFS fan-out: failed to deliver to %s: %s",
                sub.inbox_url,
                exc,
            )
        return None
