"""Cluster-wide fan-out of broadcast relay items (spec §24.10).

Each GFS node knows only its OWN ``/gfs/ws`` sockets, and production runs
several nodes on one shared database behind a round-robin balancer. A
broadcast item — an authority-signed ``/gfs/publish`` (``space_post_public``
and the other broadcast space events), a member publish (identified or
anonymous) or a private-channel item — used to reach only the subscribers
connected to the node that took the request: on four nodes about three
followers in four missed every post.

**One forward per peer node, not per subscriber.** The node that accepted
the item pushes it to its own sockets as before, then hands it to
:meth:`ClusterRelayFanOut.forward`. The item rides the next coalesced
``NODE_DRAIN_HINT`` frame (key ``relays``, see
:meth:`~socialhome.global_server.cluster.ClusterService.forward_relay`) —
one frame per peer per flush, however many subscribers and items. The frame
names the AUDIENCE, not the recipients: ``("space", space_id)``,
``("channel", channel_id)``, ``("followers", author_user_id)`` (a public
moment or its tombstone) or ``("instance", instance_id)`` (a push for one
household the origin had no socket for — ``follow_changed`` /
``moment_key_request`` to an author). The last two are TYPED: their frame
names the socket frame type it is pushed as, from a fixed allow-list
(:data:`TYPED_FRAME_TYPES`). Every receiving node resolves that audience
from the shared database and pushes to the members whose socket it holds
(:meth:`ClusterRelayFanOut.apply`). A receiver never forwards again, so
there are no loops.

**Exactly once.** Every node remembers the items it pushed or forwarded —
by the BLAKE2b digest of the frame, which each node computes itself (an id
taken off the wire could name other bytes) — for
:data:`RELAY_FORWARD_SEEN_TTL_S`. A second copy (a retried frame, or the
same bytes published to two nodes) is never pushed twice by one node; each
socket lives on one node, so each subscriber gets the item once. The same
memory makes a replay of a broadcast to ANOTHER node a no-op there
(``GfsFederationService.publish_event``).

An ``instance`` item is the exception (:data:`DEDUPED_KINDS`): it is
forwarded only after the origin found no socket, and the same request a
minute later must reach the household again.

**Offline copies.** Member, channel and moment items are queued for a
recently-seen subscriber with no socket (``queue_ok``, see
:mod:`.member_publish`). Only
the node that holds a socket knows it does, so for those items each
receiver answers the ``NODE_DRAIN_HINT`` POST with ``held``: per audience,
the instance ids it holds a socket for. The origin then queues
(:meth:`~socialhome.global_server.envelope_relay.GfsEnvelopeRelay.queue_relay_many`)
only for the recently-seen subscribers that NO node holds — instead of one
queue row per subscriber on another node. A receiver whose push to a held
socket fails queues that one itself.

**Rolling upgrade.** An older node ignores ``relays`` — and any item of an
audience kind it does not know (``followers`` / ``instance`` before the
moment fan-out) — and answers without ``held`` for it. The origin
therefore treats every subscriber it did not hear about as unreached and
queues it as before, and the older node drains those rows on the drain
hint — exactly the behaviour before this change. An older origin never
forwards, so a newer receiver keeps draining its rows.

**What the GFS learns.** Nothing new: the forwarded frame is the same
frame the node already pushes to sockets (an identity-free space item, a
moment ciphertext, an author notice built from the shared database), the
audience is an id the shared database already holds, ``skip`` is
the publisher the accepting node already authenticated, and ``held`` lists
instance ids of the same cluster's own sockets.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from functools import partial
from typing import TYPE_CHECKING, Any

from ..moment_public_seal import MOMENT_FRAME_TYPES
from .envelope_relay import (
    ENVELOPE_INSTANCE_ID_RE,
    RELAY_FAN_OUT_CONCURRENCY,
    RELAY_FRAME_TYPE,
)
from .federation import SeenPayloadCache

if TYPE_CHECKING:
    from .cluster import ClusterService
    from .envelope_relay import GfsEnvelopeRelay
    from .ws_registry import GfsWebSocketRegistry

log = logging.getLogger(__name__)


#: Audience kinds a forwarded item may name. ``space``: the space's active
#: subscribers (``/gfs/publish`` broadcasts and member publishes). ``channel``:
#: a private channel's seats for its open epochs (v_51). ``followers``: the
#: follower households of a public-Momentum author that can open its moments
#: (key: the author's user id). ``instance``: one household (key: its
#: instance id) — a single-recipient push the origin had no socket for.
#: ``all``: every household connected to the receiving node (key:
#: :data:`AUDIENCE_ALL_KEY`) — a GFS-wide notice such as a rename.
AUDIENCE_SPACE: str = "space"
AUDIENCE_CHANNEL: str = "channel"
AUDIENCE_FOLLOWERS: str = "followers"
AUDIENCE_INSTANCE: str = "instance"
AUDIENCE_ALL: str = "all"
AUDIENCE_KINDS: frozenset[str] = frozenset(
    {
        AUDIENCE_SPACE,
        AUDIENCE_CHANNEL,
        AUDIENCE_FOLLOWERS,
        AUDIENCE_INSTANCE,
        AUDIENCE_ALL,
    }
)

#: The one key of the ``all`` audience.
AUDIENCE_ALL_KEY: str = "*"

#: Socket frame types an item of a TYPED audience kind may be pushed as.
#: Its frame is ``{"event_type": <socket frame type>, …fields}`` and goes out
#: as ``{"type": <event_type>, …fields}``; the receiver refuses any other
#: type, so a forwarded item can never become, say, an ``envelope`` frame.
#: The other kinds are space items, pushed as ``{"type": "relay", …frame}``.
TYPED_FRAME_TYPES: dict[str, frozenset[str]] = {
    AUDIENCE_FOLLOWERS: MOMENT_FRAME_TYPES,
    AUDIENCE_INSTANCE: frozenset({"follow_changed", "moment_key_request"}),
    AUDIENCE_ALL: frozenset({"server_info_updated"}),
}

#: Kinds whose items are remembered by digest (:data:`RELAY_FORWARD_SEEN_TTL_S`)
#: so one node never pushes the same bytes twice. A single-recipient push is
#: forwarded only after the origin found no socket for it, so no node pushes
#: it twice — and a legitimate repeat (the same ``moment_key_request`` a
#: minute later) must not be muted. A GFS-wide notice is forwarded once by
#: the node that made it, and the same notice again (renamed back within ten
#: minutes) must arrive too.
DEDUPED_KINDS: frozenset[str] = AUDIENCE_KINDS - {AUDIENCE_INSTANCE, AUDIENCE_ALL}

#: Longest audience key (a space or channel id) a forwarded item may carry —
#: the relay routes' own bound on identifier-shaped fields.
AUDIENCE_KEY_MAX_LEN: int = 128

#: Max forwarded items in one ``NODE_DRAIN_HINT`` frame, and the most a
#: receiver reads from one.
RELAY_FORWARD_MAX_PER_FRAME: int = 64

#: Max bytes of forwarded items (their canonical JSON) in one frame. The
#: receiver reads ``/cluster/sync`` under aiohttp's 1 MiB ``client_max_size``;
#: the ids and notices that share the frame take at most ~75 KiB, so 768 KiB
#: of items always fits. One item is at most a 256 KiB publish body.
RELAY_FORWARD_MAX_FRAME_BYTES: int = 768 * 1024

#: Max items, and their bytes, waiting for the next flush. Past either the new
#: item is not forwarded (WARNING): its subscribers on other nodes miss it
#: live, a member item is queued for them by the origin as before, and space
#: sync catches up the rest.
RELAY_FORWARD_MAX_PENDING: int = 2048
RELAY_FORWARD_MAX_PENDING_BYTES: int = 32 * 1024 * 1024

#: How long a node remembers an item id it pushed or forwarded (seconds), and
#: how many. Ten minutes covers a frame retry and the ±300 s window of every
#: replay guard; past the cap the oldest ids go first.
RELAY_FORWARD_SEEN_TTL_S: float = 600.0
RELAY_FORWARD_SEEN_MAX: int = 20_000

#: Max background push tasks for inbound forwarded items. Past it a frame's
#: items are refused whole (nothing is reported held, so the origin queues
#: what it would queue anyway) — a stalled node must not pile up tasks.
RELAY_FORWARD_MAX_INFLIGHT: int = 32

#: Longest instance-id list read from one ``held`` audience in a reply.
RELAY_HELD_MAX_IDS: int = 100_000

#: Max offline candidates (``queue_for`` ids) held by forwarded items whose
#: frame has not gone out yet. Past it a member item is not forwarded and its
#: candidates are queued at once, as on a single node — so a burst of items
#: for a huge space cannot pin unbounded id lists in memory (~80 bytes each).
RELAY_FORWARD_MAX_PENDING_TARGETS: int = 200_000

#: Minimum seconds between two "forward buffer full" / "inbound saturated"
#: WARNINGs.
RELAY_FORWARD_WARN_INTERVAL_S: float = 60.0

#: How long :meth:`ClusterRelayFanOut.close` waits for background work.
RELAY_FORWARD_CLOSE_TIMEOUT_S: float = 5.0

#: Resolves an audience key to the instance ids it reaches (shared DB).
AudienceResolver = Callable[[str], Awaitable[list[str]]]

#: Called by the cluster once a forwarded item's frame went out, with the
#: union of the ids the answering peers hold a socket for.
SettleCallback = Callable[[frozenset[str]], None]


def wire_size(value: object) -> int:
    """Bytes *value* takes in a signed ``/cluster/sync`` frame (the same
    canonical encoding :meth:`ClusterService._signed_frame` uses)."""
    return len(json.dumps(value, separators=(",", ":"), sort_keys=True))


def _valid_key(raw: object) -> bool:
    return (
        isinstance(raw, str)
        and 0 < len(raw) <= AUDIENCE_KEY_MAX_LEN
        and raw.isprintable()
    )


def _valid_frame(kind: str, frame: dict) -> bool:
    """A forwarded frame never names its own ``type``; a typed kind's
    ``event_type`` must be one it may be pushed as."""
    if "type" in frame:
        return False
    typed = TYPED_FRAME_TYPES.get(kind)
    if typed is None:
        return _valid_key(frame.get("event_type"))
    return frame.get("event_type") in typed


def typed_frame(frame_type: str, fields: dict) -> dict:
    """The forwarded frame of a typed-kind push ``{"type": frame_type,
    **fields}`` (*fields* without ``type``)."""
    return {**fields, "event_type": frame_type}


def typed_push(kind: str, frame: dict) -> dict:
    """The socket frame a forwarded *frame* of audience *kind* goes out as."""
    if kind not in TYPED_FRAME_TYPES:
        return {"type": RELAY_FRAME_TYPE, **frame}
    fields = {k: v for k, v in frame.items() if k != "event_type"}
    return {"type": frame["event_type"], **fields}


async def _one_household(instance_id: str) -> list[str]:
    """The ``instance`` audience: the one household the key names."""
    return [instance_id] if ENVELOPE_INSTANCE_ID_RE.fullmatch(instance_id) else []


@dataclass(slots=True, frozen=True)
class ForwardedRelay:
    """One broadcast item forwarded to the other cluster nodes.

    ``frame`` is what a socket receives, minus its ``"type": "relay"``.
    ``skip`` is a household not to push to (the publisher; ``""`` for none).
    ``queue`` asks the receiver to report the audience members it holds and
    to queue an offline copy when its push to one of them fails."""

    kind: str
    key: str
    skip: str
    queue: bool
    frame: dict

    @property
    def audience(self) -> str:
        """The ``held`` key of this item's audience."""
        return f"{self.kind}:{self.key}"

    def digest(self) -> bytes:
        """The dedupe key: BLAKE2b-256 of the frame's canonical JSON."""
        return SeenPayloadCache.digest(self.frame)

    @property
    def deduped(self) -> bool:
        """Whether this item is pushed at most once per node (by digest)."""
        return self.kind in DEDUPED_KINDS

    def push_frame(self) -> dict:
        """What a socket receives: a space item as ``{"type": "relay", …}``,
        an item of a typed kind (:data:`TYPED_FRAME_TYPES`) as its own type."""
        return typed_push(self.kind, self.frame)

    def queued_frame(self) -> dict:
        """What an offline copy is queued as (``GfsEnvelopeRelay.drain``
        turns it back into :meth:`push_frame`)."""
        return self.push_frame() if self.kind in TYPED_FRAME_TYPES else self.frame

    def to_wire(self) -> dict:
        return {
            "aud": [self.kind, self.key],
            "skip": self.skip,
            "q": self.queue,
            "frame": self.frame,
        }

    @classmethod
    def from_wire(cls, raw: object) -> ForwardedRelay | None:
        """A forwarded item off the wire, or ``None`` if malformed."""
        if not isinstance(raw, dict):
            return None
        aud, skip = raw.get("aud"), raw.get("skip", "")
        queue, frame = raw.get("q", False), raw.get("frame")
        if not (
            isinstance(aud, list)
            and len(aud) == 2
            and aud[0] in AUDIENCE_KINDS
            and _valid_key(aud[1])
            and isinstance(skip, str)
            and (skip == "" or ENVELOPE_INSTANCE_ID_RE.fullmatch(skip))
            and isinstance(queue, bool)
            and isinstance(frame, dict)
            and _valid_frame(aud[0], frame)
        ):
            return None
        return cls(
            kind=aud[0],
            key=aud[1],
            skip=skip,
            queue=queue,
            frame=frame,
        )


def held_from_replies(replies: list[dict | None], audience: str) -> frozenset[str]:
    """Union of the ids the *replies* report held for *audience*. A reply
    without ``held`` (an older node, or a failed send) contributes nothing,
    so the origin queues for every subscriber it did not hear about."""
    held: set[str] = set()
    for reply in replies:
        if not isinstance(reply, dict) or not isinstance(reply.get("held"), dict):
            continue
        ids = reply["held"].get(audience)
        if not isinstance(ids, list):
            continue
        held.update(
            i
            for i in ids[:RELAY_HELD_MAX_IDS]
            if isinstance(i, str) and ENVELOPE_INSTANCE_ID_RE.fullmatch(i)
        )
    return frozenset(held)


class ClusterRelayFanOut:
    """Forward broadcast items to the other nodes, and push the ones they
    forward to the sockets held here (see the module docstring)."""

    __slots__ = (
        "_audiences",
        "_clock",
        "_cluster",
        "_dropped",
        "_pending_targets",
        "_relay",
        "_seen",
        "_tasks",
        "_warned_at",
        "_ws_registry",
    )

    def __init__(
        self,
        *,
        ws_registry: GfsWebSocketRegistry,
        relay: GfsEnvelopeRelay,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._ws_registry = ws_registry
        self._relay = relay
        self._clock = clock
        self._cluster: ClusterService | None = None
        self._audiences: dict[str, AudienceResolver] = {
            AUDIENCE_INSTANCE: _one_household,
            AUDIENCE_ALL: self._connected_here,
        }
        self._seen = SeenPayloadCache(
            ttl_s=RELAY_FORWARD_SEEN_TTL_S, cap=RELAY_FORWARD_SEEN_MAX
        )
        #: Inbound pushes and origin-side offline queueing, awaited in close.
        self._tasks: set[asyncio.Task[None]] = set()
        #: Offline candidates held by forwarded items not yet settled.
        self._pending_targets = 0
        #: Inbound frames refused at saturation since the last WARNING.
        self._dropped = 0
        self._warned_at = -math.inf

    async def _connected_here(self, key: str) -> list[str]:
        """The ``all`` audience: every household with a socket on this node."""
        if key != AUDIENCE_ALL_KEY:
            return []
        return sorted(self._ws_registry.connected_instances())

    def attach_cluster(self, cluster: ClusterService) -> None:
        """Forward through *cluster* (a setter: the cluster calls back into
        :meth:`apply`, so neither can be built first)."""
        self._cluster = cluster

    def register_audience(self, kind: str, resolve: AudienceResolver) -> None:
        """Resolve audiences of *kind* with *resolve* on the receiving side."""
        if kind not in AUDIENCE_KINDS:
            raise ValueError(f"unknown audience kind: {kind!r}")
        self._audiences[kind] = resolve

    def seen_frame(self, frame: dict) -> bool:
        """Whether this node already pushed or forwarded *frame* (within
        :data:`RELAY_FORWARD_SEEN_TTL_S`)."""
        return self._seen.seen(SeenPayloadCache.digest(frame))

    # ── Origin side ─────────────────────────────────────────────────────

    def forward(
        self,
        *,
        kind: str,
        key: str,
        frame: dict,
        skip: str = "",
        queue_for: list[str] | None = None,
    ) -> bool:
        """Forward *frame* for audience ``(kind, key)`` to every other node.

        Called by the node that accepted the item, AFTER its own push.
        *queue_for* are the recently-seen audience members that push did not
        reach: once the frame went out, the ones no peer holds get an offline
        copy (:meth:`GfsEnvelopeRelay.queue_relay_many`). ``False`` when the
        item could not be forwarded (no cluster, or the buffer is full) — the
        caller then queues *queue_for* itself, as on a single node."""
        if self._cluster is None:
            return False
        targets = list(queue_for or ())
        if self._pending_targets + len(targets) > RELAY_FORWARD_MAX_PENDING_TARGETS:
            return False
        item = ForwardedRelay(
            kind=kind, key=key, skip=skip, queue=bool(targets), frame=frame
        )
        on_settled = (
            partial(self._queue_unheld, targets, item.queued_frame())
            if targets
            else None
        )
        if not self._cluster.forward_relay(item, on_settled=on_settled):
            return False
        self._pending_targets += len(targets)
        if item.deduped:
            self._seen.record(item.digest())
        return True

    def forward_to_space(self, space_id: str, frame: dict) -> bool:
        """:meth:`forward` a broadcast ``/gfs/publish`` relay to the space's
        subscribers on the other nodes (nothing is queued for a broadcast)."""
        return self.forward(kind=AUDIENCE_SPACE, key=space_id, frame=frame)

    def forward_to_household(self, instance_id: str, push: dict) -> bool:
        """:meth:`forward` a single-recipient socket frame *push* (one of
        :data:`TYPED_FRAME_TYPES` for ``instance``) that found no socket on
        this node, to whichever node holds *instance_id*'s socket. Nothing is
        queued: a household connected nowhere misses it, as on one node."""
        fields = {k: v for k, v in push.items() if k != "type"}
        return self.forward(
            kind=AUDIENCE_INSTANCE,
            key=instance_id,
            frame=typed_frame(str(push.get("type")), fields),
        )

    def forward_to_all(self, push: dict) -> bool:
        """:meth:`forward` a GFS-wide socket frame *push* (one of
        :data:`TYPED_FRAME_TYPES` for ``all``) that this node already pushed
        to its own sockets, so every other node pushes it to theirs. Nothing
        is queued: a household connected nowhere refreshes on its next
        hello, as on one node."""
        fields = {k: v for k, v in push.items() if k != "type"}
        return self.forward(
            kind=AUDIENCE_ALL,
            key=AUDIENCE_ALL_KEY,
            frame=typed_frame(str(push.get("type")), fields),
        )

    def _queue_unheld(
        self, targets: list[str], frame: dict, held: frozenset[str]
    ) -> None:
        self._pending_targets -= len(targets)
        rest = [t for t in targets if t not in held]
        if rest:
            self._spawn(self._queue(rest, frame), name="gfs-cluster-relay-queue")

    async def _queue(self, targets: list[str], frame: dict) -> None:
        try:
            await self._relay.queue_relay_many(targets, frame)
        except Exception as exc:
            log.warning(
                "cluster relay: offline copies for %d household(s) not queued: %r",
                len(targets),
                exc,
                exc_info=True,
            )

    # ── Receiving side ──────────────────────────────────────────────────

    async def apply(self, items: list[ForwardedRelay]) -> dict[str, list[str]]:
        """Inbound forwarded *items*: push each to the audience members whose
        socket is on THIS node, in the background, and return ``held`` — per
        audience of a ``queue`` item, the members connected here.

        Audiences are resolved once per frame from the shared database. An
        item already seen here is not pushed again (but its held members
        are still reported: they got it from this node). Never forwards."""
        if len(self._tasks) >= RELAY_FORWARD_MAX_INFLIGHT:
            self._note_saturated(len(items))
            return {}
        held: dict[str, set[str]] = {}
        resolved: dict[str, list[str] | None] = {}
        jobs: list[tuple[list[str], ForwardedRelay]] = []
        for item in items:
            audience = item.audience
            if audience not in resolved:
                resolved[audience] = await self._resolve(item.kind, item.key)
            targets = resolved[audience]
            if targets is None:
                continue
            local = [t for t in targets if self._ws_registry.is_connected(t)]
            if item.queue:
                held.setdefault(audience, set()).update(local)
            if item.deduped:
                digest = item.digest()
                if self._seen.seen(digest):
                    continue
                self._seen.record(digest)
            push = [t for t in local if t != item.skip]
            if push:
                jobs.append((push, item))
        if jobs:
            self._spawn(self._push(jobs), name="gfs-cluster-relay-push")
        return {aud: sorted(ids) for aud, ids in held.items() if ids}

    async def _resolve(self, kind: str, key: str) -> list[str] | None:
        resolve = self._audiences.get(kind)
        if resolve is None:
            return None
        try:
            return await resolve(key)
        except Exception as exc:
            log.warning(
                "cluster relay: audience %s lookup failed: %r", kind, exc, exc_info=True
            )
            return None

    async def _push(self, jobs: list[tuple[list[str], ForwardedRelay]]) -> None:
        limit = asyncio.Semaphore(RELAY_FAN_OUT_CONCURRENCY)
        for targets, item in jobs:
            push = item.push_frame()
            failed: list[str] = []

            async def _one(target: str, push: dict = push, failed=failed) -> None:
                async with limit:
                    if not await self._ws_registry.send(target, push):
                        failed.append(target)

            await asyncio.gather(*(_one(t) for t in targets))
            if item.queue and failed:
                # Reported held, so the origin will not queue these: the
                # socket dropped between the report and the push.
                await self._queue(failed, item.queued_frame())

    def _spawn(self, coro: Any, *, name: str) -> None:
        task = asyncio.get_running_loop().create_task(coro, name=name)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    def _note_saturated(self, count: int) -> None:
        self._dropped += count
        now = self._clock()
        if now - self._warned_at < RELAY_FORWARD_WARN_INTERVAL_S:
            return
        log.warning(
            "cluster relay: %d inbound push task(s) running — refused %d "
            "forwarded item(s) since the last warning (their offline copies "
            "are queued by the origin; live subscribers here miss them)",
            len(self._tasks),
            self._dropped,
        )
        self._dropped = 0
        self._warned_at = now

    @property
    def in_flight(self) -> int:
        """Background push / queue tasks currently running."""
        return len(self._tasks)

    async def close(self, timeout: float | None = None) -> None:
        """Let background pushes and queue writes finish, cancelling any
        still running after *timeout*. Called at shutdown AFTER the cluster
        stopped (which settles its unsent items) and BEFORE the database
        shuts down."""
        pending = set(self._tasks)
        if not pending:
            return
        _done, still = await asyncio.wait(
            pending,
            timeout=RELAY_FORWARD_CLOSE_TIMEOUT_S if timeout is None else timeout,
        )
        for task in still:
            task.cancel()
        if still:
            await asyncio.gather(*still, return_exceptions=True)
