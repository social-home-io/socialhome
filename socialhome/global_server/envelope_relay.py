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

#: Upper bound on the routing ``to_instance`` field. Mirrors the identifier
#: cap ``/gfs/publish`` applies: an instance id is 32 hex chars, so 128 is
#: generous, and the bound stops a JSON list/dict or a megabyte of text ever
#: reaching a SQLite bind.
ENVELOPE_INSTANCE_ID_MAX_CHARS: int = 128

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

#: Per-recipient queue depth. Past this the OLDEST row is evicted on insert,
#: so one household's queue can never grow without bound and an anonymous
#: flood aimed at one recipient cannot fill the disk (worst case per
#: household is this many times :data:`ENVELOPE_MAX_BODY_BYTES`). 200 is two
#: orders of magnitude above the handful of redeems a real household sees in
#: a day, so a legitimate envelope is never evicted in practice.
ENVELOPE_QUEUE_MAX_PER_RECIPIENT: int = 200


class InvalidEnvelope(ValueError):
    """The posted body is not a well-formed routing envelope."""


def validate_envelope(body: Any) -> tuple[str, dict[str, str]]:
    """Return ``(to_instance, sealed)`` or raise :class:`InvalidEnvelope`.

    Validates the OUTER shape only. The ``sealed`` dict is opaque: this
    server checks that it carries exactly the three expected string keys and
    never looks at their values — it cannot open the box and must not behave
    as though it could.
    """
    if not isinstance(body, dict):
        raise InvalidEnvelope("expected a JSON object")
    to_instance = body.get("to_instance")
    if (
        not isinstance(to_instance, str)
        or not to_instance
        or len(to_instance) > ENVELOPE_INSTANCE_ID_MAX_CHARS
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

    __slots__ = ("_fed_repo", "_queue_repo", "_ws_registry", "_ttl", "_max_queued")

    def __init__(
        self,
        *,
        fed_repo: "AbstractGfsFederationRepo",
        queue_repo: "AbstractGfsEnvelopeQueueRepo",
        ws_registry: "GfsWebSocketRegistry",
        ttl_seconds: int = ENVELOPE_QUEUE_TTL_SECONDS,
        max_queued_per_recipient: int = ENVELOPE_QUEUE_MAX_PER_RECIPIENT,
    ) -> None:
        self._fed_repo = fed_repo
        self._queue_repo = queue_repo
        self._ws_registry = ws_registry
        self._ttl = ttl_seconds
        self._max_queued = max_queued_per_recipient

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
        await self._queue_repo.enqueue(
            to_instance,
            orjson.dumps(sealed).decode(),
            created_at=now,
            expires_at=now + self._ttl,
            max_per_recipient=self._max_queued,
        )
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
