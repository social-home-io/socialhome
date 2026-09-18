"""Opaque household-to-household envelope relay (§D2b).

The connection server shields households from each other. Two households
introduced by an invite link never learn each other's network address:
the sender seals a self-authenticating body to the recipient's published
key-wrap key (``socialhome.federation.keywrap_seal``) and hands this
server an OPAQUE blob addressed only by instance id. This module is the
server half of the :class:`socialhome.federation.invite_bootstrap
.RelayEnvelopeSender` seam.

What the GFS sees is a recipient id, a blob size and a timing. What it
must never do — and what the tests in ``tests/global_server
/test_envelope_relay.py`` pin — is parse or log the sealed content, store
or log any sender attribute (there is none on the wire), return anything
that distinguishes one recipient from another, or forward a frame to
anybody but ``to_instance``.

**Uniform response.** ``POST /gfs/envelope`` answers every well-formed
request with the same ``202 {"status": "accepted"}`` — recipient online,
recipient offline, recipient not registered here at all. Any other
answer is a presence/existence oracle: an anonymous caller could walk
instance ids and learn which households use this server and which are
awake. An envelope for a household this server does not know is dropped
server-side, logged at DEBUG, and stored nowhere.

**Store and forward.** A live ``/gfs/ws`` socket takes the frame
immediately. Otherwise the blob waits in ``gfs_envelope_queue`` (GFS
migration 0011) for :data:`ENVELOPE_QUEUE_TTL_SECONDS` and is drained, in
order, on that household's next authenticated hello. Dropping instead
would make an invite link fail whenever the issuing household happens to
be asleep — which is most of the night.

Ordering note: envelopes that arrive DURING a drain go straight to the
now-live socket and can therefore overtake a queued one. The bootstrap
protocol is a nonce-matched request/reply, so this is harmless; what is
guaranteed is that queued envelopes are delivered in queue order.
"""

from __future__ import annotations

import logging
import re
import time
from typing import TYPE_CHECKING, Any

import orjson

from .public import ClientIpResolver

# The window-limiter factory is module-private in ``.public`` and every
# limiter on this server is built from it; importing it beats a fourth
# hand-rolled copy of the same middleware.
from .public import _build_window_limiter

if TYPE_CHECKING:
    from .repositories import AbstractGfsEnvelopeQueueRepo, AbstractGfsFederationRepo
    from .ws_registry import GfsWebSocketRegistry

log = logging.getLogger(__name__)


#: Route this relay is mounted at. One constant so the limiter, the router
#: and the docs can't drift apart.
ENVELOPE_ROUTE_PATH: str = "/gfs/envelope"

#: WebSocket frame type pushed to the recipient. The frame carries this and
#: the sealed dict — nothing else, and in particular nothing about who sent
#: it (the server does not know).
ENVELOPE_FRAME_TYPE: str = "envelope"

#: The exact key set a ``sealed`` dict must carry. This is the wire shape
#: ``keywrap_seal.seal_to_keywrap`` produces; the VALUES are opaque here (the
#: suite tag is validated by the recipient, which is the only party that can
#: act on it — a relay that enforced a suite allow-list would have to be
#: redeployed before households could migrate to the Phase-2 hybrid suite).
SEALED_KEYS: frozenset[str] = frozenset({"kem_suite", "eph_pk", "ciphertext"})

#: Exact shape of the routing ``to_instance`` field.
#:
#: An instance id is not free text: ``socialhome.crypto.derive_instance_id``
#: produces the unpadded lowercase base32 of a truncated SHA-256 — 32
#: characters from ``[a-z2-7]``, always. Accepting "any string up to 128
#: chars" instead let a caller post an id containing a NEWLINE and have the
#: server's own log lines render an attacker-authored second line — log
#: forgery, from an endpoint that is anonymous on purpose. It also let
#: unbounded junk reach a SQLite bind and sit in the queue table.
#:
#: Anchored (``fullmatch``) so nothing rides in front of or behind the id,
#: which also makes the length bound exact rather than an upper limit.
ENVELOPE_INSTANCE_ID_CHARS: int = 32
ENVELOPE_INSTANCE_ID_RE = re.compile(rf"[a-z2-7]{{{ENVELOPE_INSTANCE_ID_CHARS}}}")

#: Hard body cap on ``POST /gfs/envelope``, enforced before the bytes are
#: buffered or parsed (the endpoint is unauthenticated by design — the sender
#: is deliberately anonymous, so there is nothing to authenticate).
#:
#: Sized from the largest legitimate envelope the household side can produce.
#: ``invite_bootstrap.MAX_SEALED_BLOB_BYTES`` caps the sealed ``ciphertext``
#: string at 256 KiB, and that cap is itself sized from the ACK, which
#: carries ``space_meta`` from ``build_space_snapshot_for_federation``: the
#: base64'd space cover WebP (bounded by ``SPACE_COVER_MAX_DIMENSION`` =
#: 1200 px), the base64'd icon WebP (256 px) and the member roster. On top of
#: the ciphertext the body carries a 44-char ``eph_pk``, a 32-char
#: ``to_instance``, the suite tag and ~100 bytes of JSON framing. 320 KiB
#: therefore accepts every envelope the household side will ever send (a
#: receiver-side cap rejects anything larger anyway) with 64 KiB of headroom,
#: while keeping one anonymous request's memory cost trivial.
ENVELOPE_MAX_BODY_BYTES: int = 320 * 1024

#: Per-IP/minute cap on ``POST /gfs/envelope``. The endpoint is anonymous ON
#: PURPOSE — there is no household identity on the wire to account against —
#: so the client address is the only shedding handle, exactly as for
#: ``/gfs/publish``.
#:
#: One invite redeem is two envelopes (sealed request, sealed reply), so 30
#: per minute is ~15 complete redeems a minute from one address: far above a
#: household accepting invite links, and still well above a household that
#: also happens to be answering several. It is deliberately much tighter than
#: ``/gfs/publish``'s 120 because an accepted envelope can cost a DB write of
#: up to :data:`ENVELOPE_MAX_BODY_BYTES`, where a publish costs a fan-out and
#: nothing durable. Worst case from one address is therefore ~9.4 MiB/min of
#: queue writes, itself bounded per household by
#: :data:`ENVELOPE_QUEUE_MAX_PER_RECIPIENT`.
#:
#: Like every limiter in :mod:`.public` this sheds ONE noisy source; it is not
#: DDoS protection (see ``RATE_LIMIT_MAX_TRACKED_IPS``).
ENVELOPE_MAX_PER_MINUTE: int = 30

#: How long an undelivered envelope waits for its recipient, in seconds.
#: 24 h: the issuing household of an invite link may simply be asleep, and an
#: invite that fails because the other family went to bed is the failure this
#: queue exists to prevent. Longer would turn a relay into a mailbox — past a
#: day the redeemer's own 5-minute in-flight state is long gone and the
#: redeem has to be retried from the link anyway.
ENVELOPE_QUEUE_TTL_SECONDS: int = 24 * 60 * 60

#: Per-recipient queue depth. At the cap the NEW envelope is dropped —
#: **tail-drop**, never evict-oldest.
#:
#: Evicting the oldest row looks fairer and is the exact opposite: this
#: endpoint is anonymous by construction, so anybody who learns an instance
#: id can post to it. Under evict-oldest, ``cap`` junk envelopes delete
#: ``cap`` real ones — a stranger silently erases a sleeping household's
#: mail, and the household never learns anything was lost. Tail-drop caps
#: the damage at "the flood itself is refused": what is already queued is
#: exactly what the sender was told was accepted. The uniform ``202`` is
#: unchanged either way — the caller must not be able to tell a queued
#: envelope from a dropped one, or the response becomes a depth oracle.
#:
#: 2000, up from 200. 200 was sized for "the handful of redeems a real
#: household sees in a day", which was true when the only thing on this
#: relay was the invite bootstrap. Since the relay tier carries ALL
#: federation traffic for a household seated from an invite link (posts,
#: comments, roster, rekeys, catch-up sync), a day offline in an active
#: space is thousands of envelopes, and 200 would discard almost all of it.
ENVELOPE_QUEUE_MAX_PER_RECIPIENT: int = 2000

#: Per-recipient queue size in bytes — the bound that actually matters.
#: The row count alone is not one: 2000 × :data:`ENVELOPE_MAX_BODY_BYTES`
#: is 625 MiB, which is a disk-filling attack with extra steps. Whichever
#: ceiling is reached first tail-drops. 64 MiB is generous for a day of
#: real space traffic (typical envelopes are single-digit KiB, so the row
#: cap binds first in normal use) and small enough that a server with many
#: registered households stays bounded.
ENVELOPE_QUEUE_MAX_BYTES_PER_RECIPIENT: int = 64 * 1024 * 1024


class InvalidEnvelope(ValueError):
    """The posted body is not a well-formed routing envelope."""


def validate_envelope(body: Any) -> tuple[str, dict[str, str]]:
    """Return ``(to_instance, sealed)`` or raise :class:`InvalidEnvelope`.

    Validates the OUTER shape only. The ``sealed`` dict is opaque: this
    server checks that it carries exactly the three expected string keys and
    never looks at their values — it cannot open the box and must not behave
    as though it could.

    ``to_instance`` is the exception, and is checked against the real
    identifier shape (:data:`ENVELOPE_INSTANCE_ID_RE`) rather than a length
    bound: it is the one field this server routes on, stores, and writes
    into its own logs, so a value carrying a newline is a log-forgery
    primitive handed to an anonymous caller.
    """
    if not isinstance(body, dict):
        raise InvalidEnvelope("expected a JSON object")
    to_instance = body.get("to_instance")
    if not isinstance(to_instance, str) or not ENVELOPE_INSTANCE_ID_RE.fullmatch(
        to_instance
    ):
        raise InvalidEnvelope("invalid field: to_instance")
    sealed = body.get("sealed")
    if not isinstance(sealed, dict) or set(sealed) != SEALED_KEYS:
        raise InvalidEnvelope("invalid field: sealed")
    for key in SEALED_KEYS:
        if not isinstance(sealed[key], str) or not sealed[key]:
            raise InvalidEnvelope("invalid field: sealed")
    return to_instance, dict(sealed)


class GfsEnvelopeRelay:
    """Deliver-or-queue sealed envelopes addressed by instance id."""

    __slots__ = (
        "_fed_repo",
        "_queue_repo",
        "_ws_registry",
        "_ttl",
        "_max_queued",
        "_max_bytes",
    )

    def __init__(
        self,
        *,
        fed_repo: "AbstractGfsFederationRepo",
        queue_repo: "AbstractGfsEnvelopeQueueRepo",
        ws_registry: "GfsWebSocketRegistry",
        ttl_seconds: int = ENVELOPE_QUEUE_TTL_SECONDS,
        max_queued_per_recipient: int = ENVELOPE_QUEUE_MAX_PER_RECIPIENT,
        max_bytes_per_recipient: int = ENVELOPE_QUEUE_MAX_BYTES_PER_RECIPIENT,
    ) -> None:
        self._fed_repo = fed_repo
        self._queue_repo = queue_repo
        self._ws_registry = ws_registry
        self._ttl = ttl_seconds
        self._max_queued = max_queued_per_recipient
        self._max_bytes = max_bytes_per_recipient

    async def accept(self, to_instance: str, sealed: dict[str, str]) -> None:
        """Push *sealed* to *to_instance*, or queue it for later.

        Returns nothing on purpose: the caller answers a uniform ``202``
        whatever happened here, so there is no outcome for it to branch on
        and nothing it could leak by accident.
        """
        instance = await self._fed_repo.get_instance(to_instance)
        if instance is None or instance.status != "active":
            # Not a registered, active client of this server. Dropped — and
            # the caller still gets the same 202, because "no such recipient"
            # is precisely the fact an anonymous prober would be after.
            log.debug(
                "gfs.envelope: dropping envelope for unknown/inactive recipient %s",
                to_instance,
            )
            return
        if await self._ws_registry.send(
            to_instance,
            {"type": ENVELOPE_FRAME_TYPE, "sealed": sealed},
        ):
            log.debug("gfs.envelope: delivered to live socket %s", to_instance)
            return
        now = int(time.time())
        queued = await self._queue_repo.enqueue(
            to_instance,
            orjson.dumps(sealed).decode(),
            created_at=now,
            expires_at=now + self._ttl,
            max_per_recipient=self._max_queued,
            max_bytes_per_recipient=self._max_bytes,
        )
        if not queued:
            # The caller still gets the same 202 — a different answer here
            # would turn queue depth into an oracle. But a recipient at cap
            # IS an operator signal: either that household has been offline
            # far too long, or somebody is flooding it. WARNING, with the
            # recipient and the depth and nothing about the blob.
            log.warning(
                "gfs.envelope: queue full for %s (%d queued) — dropping the "
                "new envelope; the recipient is offline or being flooded",
                to_instance,
                await self._queue_repo.count_for(to_instance),
            )
            return
        log.debug("gfs.envelope: queued for offline recipient %s", to_instance)

    async def drain(self, to_instance: str) -> int:
        """Flush queued envelopes to a freshly-connected household.

        Delivers oldest first and deletes each row only after its frame went
        out, so a socket that dies mid-drain leaves the rest queued for the
        next hello rather than losing them. Returns the number delivered.
        """
        try:
            pending = await self._queue_repo.list_for(to_instance, now=int(time.time()))
        except Exception as exc:  # fail-soft: a drain must never kill the socket
            log.warning(
                "gfs.envelope: drain lookup failed for %s: %s", to_instance, exc
            )
            return 0
        delivered = 0
        for envelope in pending:
            sent = await self._ws_registry.send(
                to_instance,
                {"type": ENVELOPE_FRAME_TYPE, "sealed": envelope.sealed},
            )
            if not sent:
                break
            await self._queue_repo.delete(envelope.id)
            delivered += 1
        if delivered:
            log.debug(
                "gfs.envelope: drained %d queued envelope(s) to %s",
                delivered,
                to_instance,
            )
        return delivered


def build_envelope_rate_limit(resolver: ClientIpResolver):
    """Per-IP rate limiter middleware for ``POST /gfs/envelope``.

    The relay is anonymous by design, so — exactly as for ``/gfs/publish`` —
    the client address is the only handle available for shedding a flood.
    """
    return _build_window_limiter(
        resolver,
        ENVELOPE_MAX_PER_MINUTE,
        lambda path: path == ENVELOPE_ROUTE_PATH,
    )
