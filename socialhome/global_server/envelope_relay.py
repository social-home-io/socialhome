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

**Uniform timing.** The same holds for HOW LONG the answer takes. The three
outcomes cost different amounts of work (an unknown id is one indexed miss,
an online one is a socket write, an offline one is a DB insert), so the route
never awaits them: it validates the outer shape and hands the envelope to
:meth:`GfsEnvelopeRelay.submit`, which schedules :meth:`GfsEnvelopeRelay.accept`
as a background task and returns at once. At most
:data:`ENVELOPE_MAX_INFLIGHT` such tasks run at a time; past that the envelope
is dropped with a rate-limited WARNING and the caller still gets the same
``202`` — a slower answer under load would be an oracle of its own. Tasks for
one recipient run in submit order (a per-recipient lock), so one sender's
envelopes are not reordered by the hand-off.

**Store and forward.** A live ``/gfs/ws`` socket takes the frame
immediately. Otherwise the blob waits in ``gfs_envelope_queue`` (GFS
migration 0011) for :data:`ENVELOPE_QUEUE_TTL_SECONDS` and is drained, in
order, on that household's next authenticated hello. Dropping instead
would make an invite link fail whenever the issuing household happens to
be asleep — which is most of the night.

**Cross-node drain.** Cluster nodes share one database, so the queue is
shared, but every node only knows its OWN live sockets. An envelope posted to
node A for a household whose socket is on node B is queued by A; A then
calls :meth:`socialhome.global_server.cluster.ClusterService.hint_drain`,
which coalesces recipient ids for a moment and broadcasts one
``NODE_DRAIN_HINT`` frame. Node B drains each hinted household it holds a
socket for, so the envelope arrives now rather than on the household's next
reconnect. Drains are serialised per household (a hello drain and a hint
drain can otherwise race and deliver a row twice).

Ordering note: envelopes that arrive DURING a drain go straight to the
now-live socket and can therefore overtake a queued one. The bootstrap
protocol is a nonce-matched request/reply, so this is harmless; what is
guaranteed is that queued envelopes are delivered in queue order.
"""

from __future__ import annotations

import asyncio
import logging
import math
import re
import time
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, Any

import orjson

from .public import ClientIpResolver, build_window_limiter


if TYPE_CHECKING:
    from .cluster import ClusterService
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
#: base64'd space cover and icon WebP (bounded for this leg by
#: ``SPACE_COVER_BOOTSTRAP_MAX_BYTES`` / ``SPACE_ICON_BOOTSTRAP_MAX_BYTES`` —
#: a 1200 px cover alone is not byte-bounded) and the member roster; the
#: household's ``seal_bootstrap_envelope`` refuses to exceed the cap. On top of
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
#: **This is not an invite-only path.** The relay started life carrying two
#: envelopes per invite redeem, and 30/min was generous for that. It now also
#: carries ALL ongoing federation for a household seated from an invite link
#: (:mod:`socialhome.federation.gfs_relay_transport`): every space post,
#: comment, reaction, roster change and — the big one — every space-sync
#: catch-up chunk, of which one join is routinely several hundred. At 30/min a
#: first catch-up took hours and the provider gave up after three consecutive
#: 429s, so a link-joined household simply never received the space.
#:
#: 600/min (10/s) fits a real space's traffic and a catch-up backfill at the
#: pace the sender can produce it, while still capping what ONE address can
#: push through a relay it does not own: worst case ~187 MiB/min of queue
#: writes from one address, itself bounded per household by
#: :data:`ENVELOPE_QUEUE_MAX_PER_RECIPIENT` and
#: :data:`ENVELOPE_QUEUE_MAX_BYTES_PER_RECIPIENT`, which are the caps that
#: actually protect this server's disk. A sender that does hit the ceiling is
#: no longer dead in the water either — a 429 reaches the household as a
#: *cooldown*, waited out and retried, rather than a terminal chunk failure.
#:
#: Like every limiter in :mod:`.public` this sheds ONE noisy source; it is not
#: DDoS protection (see ``RATE_LIMIT_MAX_TRACKED_IPS``).
ENVELOPE_MAX_PER_MINUTE: int = 600

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


#: WebSocket frame type of a queued member-published space item (migration
#: 0014). The same ``"relay"`` type every GFS space fan-out uses, so a
#: drained item is indistinguishable from a live one.
RELAY_FRAME_TYPE: str = "relay"

#: ``gfs_envelope_queue.frame_type`` values (CHECK-constrained in 0014).
QUEUE_KIND_ENVELOPE: str = "envelope"
QUEUE_KIND_RELAY: str = RELAY_FRAME_TYPE

#: Per-recipient caps for queued RELAY items (member-published space items,
#: ``POST /gfs/member-publish``). Counted separately from the envelope caps
#: above so a busy public space can never crowd out a household's sealed
#: invite / link-federation envelopes. Past either cap the recipient's OWN
#: oldest relay item is evicted (``enqueue_relay``) — only authenticated
#: writers fill this queue, and the newest items matter most; anything
#: evicted is caught up through space sync.
RELAY_QUEUE_MAX_PER_RECIPIENT: int = 250
RELAY_QUEUE_MAX_BYTES_PER_RECIPIENT: int = 4 * 1024 * 1024

#: Server-wide ceiling on bytes held in queued RELAY rows. The per-recipient
#: caps bound one household; this bounds the sum. Past it, room is made by
#: evicting the oldest item of the LARGEST holder (fair share), never by
#: refusing the new one — so a group of recipients cannot capture the cap
#: and starve the rest. Live pushes are unaffected.
RELAY_QUEUE_MAX_TOTAL_BYTES: int = 256 * 1024 * 1024

#: How long a ``/gfs/ws`` session must last before the household counts as
#: "seen" for offline relay queueing (``client_instances.relay_seen_at``). A
#: bare hello is free, so it must not earn a household queued copies of
#: every item; a real household keeps its socket open for hours.
RELAY_SEEN_MIN_SESSION_S: float = 60.0

#: Max simultaneous live pushes for one member-published item.
RELAY_FAN_OUT_CONCURRENCY: int = 8

#: Max background ``accept`` tasks in flight (see "Uniform timing" above).
#: The per-IP limiter caps one source at :data:`ENVELOPE_MAX_PER_MINUTE`, and a
#: healthy ``accept`` finishes in milliseconds, so a real server sits far below
#: this; it is the ceiling that keeps a many-source flood, or a stalled
#: database, from piling up unbounded tasks (each holding up to
#: :data:`ENVELOPE_MAX_BODY_BYTES`). Past it the envelope is dropped — the
#: answer is still the uniform ``202``.
ENVELOPE_MAX_INFLIGHT: int = 256

#: Minimum seconds between two "relay saturated" WARNINGs. Each warning reports
#: how many envelopes were dropped since the previous one — an operator signal,
#: never a per-envelope log flood driven by an anonymous caller.
ENVELOPE_SATURATION_WARN_INTERVAL_S: float = 60.0

#: How long :meth:`GfsEnvelopeRelay.close` waits for in-flight ``accept`` tasks
#: before cancelling the rest at shutdown.
ENVELOPE_CLOSE_TIMEOUT_S: float = 5.0


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


class KeyedLocks:
    """One :class:`asyncio.Lock` per key, dropped once nobody holds or waits.

    ``async with locks.hold(key)`` serialises everything for *key* in arrival
    order (``asyncio.Lock`` wakes waiters FIFO) and leaves other keys
    untouched. The map only holds keys with a current holder or waiter, so it
    cannot grow with the number of households ever seen.
    """

    __slots__ = ("_slots",)

    def __init__(self) -> None:
        #: key → [lock, number of holders + waiters].
        self._slots: dict[str, list[Any]] = {}

    def __len__(self) -> int:
        return len(self._slots)

    @asynccontextmanager
    async def hold(self, key: str) -> AsyncIterator[None]:
        slot = self._slots.get(key)
        if slot is None:
            slot = self._slots[key] = [asyncio.Lock(), 0]
        slot[1] += 1
        try:
            async with slot[0]:
                yield
        finally:
            slot[1] -= 1
            if slot[1] == 0:
                del self._slots[key]


class GfsEnvelopeRelay:
    """Deliver-or-queue sealed envelopes addressed by instance id."""

    __slots__ = (
        "_fed_repo",
        "_queue_repo",
        "_ws_registry",
        "_ttl",
        "_max_queued",
        "_max_bytes",
        "_max_inflight",
        "_inflight",
        "_clock",
        "_saturated_drops",
        "_saturation_warned_at",
        "_cluster",
        "_accept_locks",
        "_drain_locks",
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
        max_inflight: int = ENVELOPE_MAX_INFLIGHT,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._fed_repo = fed_repo
        self._queue_repo = queue_repo
        self._ws_registry = ws_registry
        self._ttl = ttl_seconds
        self._max_queued = max_queued_per_recipient
        self._max_bytes = max_bytes_per_recipient
        self._max_inflight = max_inflight
        self._inflight: set[asyncio.Task[None]] = set()
        self._clock = clock
        #: Envelopes dropped at saturation since the last WARNING.
        self._saturated_drops = 0
        self._saturation_warned_at = -math.inf
        #: Set by :meth:`attach_cluster`; ``None`` on a single node.
        self._cluster: "ClusterService | None" = None
        self._accept_locks = KeyedLocks()
        self._drain_locks = KeyedLocks()

    def attach_cluster(self, cluster: "ClusterService") -> None:
        """Hint *cluster* after every enqueue, so a sibling node holding the
        recipient's socket drains it (see "Cross-node drain" above). A
        setter rather than a constructor argument: the cluster in turn calls
        :meth:`drain`, and neither can be built first."""
        self._cluster = cluster

    @property
    def in_flight(self) -> int:
        """Background ``accept`` tasks currently scheduled or running."""
        return len(self._inflight)

    @property
    def lock_count(self) -> int:
        """Per-recipient locks currently held or waited on."""
        return len(self._accept_locks) + len(self._drain_locks)

    def submit(self, to_instance: str, sealed: dict[str, str]) -> None:
        """Hand *sealed* off to a background :meth:`accept` and return at once.

        Synchronous and constant-cost on purpose (see "Uniform timing" in the
        module docstring): the route must answer before the lookup, the push
        or the enqueue runs, so their different costs never reach the
        response latency. At :data:`ENVELOPE_MAX_INFLIGHT` the envelope is
        dropped — still silently as far as the caller is concerned.
        """
        if len(self._inflight) >= self._max_inflight:
            self._note_saturated()
            return
        task = asyncio.get_running_loop().create_task(
            self._accept_in_background(to_instance, sealed),
            name="gfs-envelope-accept",
        )
        self._inflight.add(task)
        task.add_done_callback(self._inflight.discard)

    def _note_saturated(self) -> None:
        """Count a saturation drop; WARN at most once per interval."""
        self._saturated_drops += 1
        now = self._clock()
        if now - self._saturation_warned_at < ENVELOPE_SATURATION_WARN_INTERVAL_S:
            return
        log.warning(
            "gfs.envelope: relay saturated (%d accepts in flight) — dropped "
            "%d envelope(s) since the last warning; the database is slow or "
            "the relay is being flooded",
            len(self._inflight),
            self._saturated_drops,
        )
        self._saturated_drops = 0
        self._saturation_warned_at = now

    async def _accept_in_background(
        self, to_instance: str, sealed: dict[str, str]
    ) -> None:
        """:meth:`accept` under the recipient's lock, failures logged.

        The lock keeps one recipient's envelopes in submit order: tasks start
        in creation order and ``asyncio.Lock`` hands over FIFO. Nothing awaits
        this task's result, so an exception raised here would otherwise only
        surface as "Task exception was never retrieved" — or not at all.
        """
        try:
            async with self._accept_locks.hold(to_instance):
                await self.accept(to_instance, sealed)
        except asyncio.CancelledError:
            raise
        except Exception:
            # The traceback carries no frame locals, so the sealed blob is
            # never rendered; the recipient id is a validated instance id.
            log.warning(
                "gfs.envelope: background accept failed for %s",
                to_instance,
                exc_info=True,
            )

    async def close(self, timeout: float | None = None) -> None:
        """Let in-flight ``accept`` tasks finish, cancelling any still running
        after *timeout* (default :data:`ENVELOPE_CLOSE_TIMEOUT_S`). Called from
        the server's cleanup hook BEFORE the database shuts down."""
        pending = set(self._inflight)
        if not pending:
            return
        _done, still = await asyncio.wait(
            pending,
            timeout=ENVELOPE_CLOSE_TIMEOUT_S if timeout is None else timeout,
        )
        for task in still:
            task.cancel()
        if still:
            await asyncio.gather(*still, return_exceptions=True)

    def _hint_cluster(self, to_instance: str) -> None:
        """Tell sibling nodes a row for *to_instance* just landed in the
        shared queue — one of them may hold the recipient's socket."""
        if self._cluster is not None:
            self._cluster.hint_drain(to_instance)

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
            frame_type=QUEUE_KIND_ENVELOPE,
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
        self._hint_cluster(to_instance)

    async def fan_out_relay(
        self,
        targets: list[str],
        *,
        queue_ok: set[str],
        frame: dict,
    ) -> int:
        """Deliver a member-published space item to *targets*.

        ``frame`` is the identity-free fan-out frame (``SpaceItemFrame``
        shape); it goes out as ``{type: "relay", **frame}``. Live sockets are
        pushed with at most :data:`RELAY_FAN_OUT_CONCURRENCY` sends in flight.
        A target without a live socket gets the frame queued for
        :data:`ENVELOPE_QUEUE_TTL_SECONDS` ONLY when it is in *queue_ok* (seen
        recently — the caller decides), under the per-recipient RELAY caps
        and the server-wide :data:`RELAY_QUEUE_MAX_TOTAL_BYTES`, with room
        made by fair-share eviction in the same transaction as the insert. Callers pass
        only active subscribers. Returns how many targets were pushed or
        queued."""
        push = {"type": RELAY_FRAME_TYPE, **frame}
        limit = asyncio.Semaphore(RELAY_FAN_OUT_CONCURRENCY)
        offline: list[str] = []

        async def _push(target: str) -> bool:
            async with limit:
                if await self._ws_registry.send(target, push):
                    return True
            offline.append(target)
            return False

        results = await asyncio.gather(*(_push(t) for t in targets))
        reached = sum(1 for ok in results if ok)
        to_queue = [t for t in offline if t in queue_ok]
        if not to_queue:
            return reached
        blob = orjson.dumps(frame).decode()
        now = int(time.time())
        for target in to_queue:
            queued = await self._queue_repo.enqueue_relay(
                target,
                blob,
                created_at=now,
                expires_at=now + self._ttl,
                max_per_recipient=RELAY_QUEUE_MAX_PER_RECIPIENT,
                max_bytes_per_recipient=RELAY_QUEUE_MAX_BYTES_PER_RECIPIENT,
                max_total_bytes=RELAY_QUEUE_MAX_TOTAL_BYTES,
            )
            if queued:
                reached += 1
                self._hint_cluster(target)
            else:
                log.warning(
                    "gfs.envelope: a space item for %s exceeds the relay "
                    "queue caps on its own — not queued",
                    target,
                )
        return reached

    async def drain(self, to_instance: str) -> int:
        """Flush queued envelopes to a connected household.

        Called on hello and on a sibling node's ``NODE_DRAIN_HINT``. Delivers
        oldest first and deletes each row only after its frame went out, so a
        socket that dies mid-drain leaves the rest queued for the next hello
        rather than losing them. Serialised per household: two overlapping
        drains would both list the same rows and send each twice. Returns the
        number delivered.
        """
        async with self._drain_locks.hold(to_instance):
            return await self._drain_locked(to_instance)

    async def _drain_locked(self, to_instance: str) -> int:
        try:
            pending = await self._queue_repo.list_for(to_instance, now=int(time.time()))
        except Exception as exc:  # fail-soft: a drain must never kill the socket
            log.warning(
                "gfs.envelope: drain lookup failed for %s: %s", to_instance, exc
            )
            return 0
        delivered = 0
        for envelope in pending:
            if envelope.frame_type == QUEUE_KIND_RELAY:
                frame = {"type": RELAY_FRAME_TYPE, **envelope.sealed}
            else:
                frame = {"type": ENVELOPE_FRAME_TYPE, "sealed": envelope.sealed}
            sent = await self._ws_registry.send(to_instance, frame)
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
    return build_window_limiter(
        resolver,
        ENVELOPE_MAX_PER_MINUTE,
        lambda path: path == ENVELOPE_ROUTE_PATH,
    )
