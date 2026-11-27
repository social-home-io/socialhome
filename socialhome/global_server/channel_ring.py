"""The channel ring (v_97, capability ``channel_ring``) — the GFS side.

Every opaque channel keeps ONE ring of recent items (GFS migration 0034),
not a copy per seat. A household socket that listed ``channel_ring`` in
its hello:

* gets an item live only while it **watches** that channel
  (``channel_watch``; the filter is in :class:`~.ws_registry.GfsWebSocketRegistry`);
* gets ``channel_heads`` right after its hello and then every
  :data:`HEADS_PING_INTERVAL_S` (owner F2: 15 min), naming only the channels
  whose head moved since the last ping to that socket;
* pulls what it missed with ``channel_fetch`` — answered with ``relay``
  frames carrying ``seq`` / ``ring_id`` and a ``channel_fetch_done``, or a
  ``channel_gap`` when its cursor is no longer in the ring.

Every watch and fetch needs a LIVE seat at an open epoch — the same seat a
push goes to. A fresh seat may fetch what the ring still holds from before
it joined (at most 24 h): it holds the content key for those epochs' items
only if it was a member then, and the join catch-up gives it the history
anyway. Nothing here learns a space: the ring is keyed by the random
channel id. Heads name only channels whose ring has an item.

Bounds (owner F1; the operator's ``[channel_ring]``,
:class:`~.config.ChannelRingConfig`): ``max_items`` / ``max_bytes`` per
channel at insert, ``durable_ttl_seconds`` / ``ephemeral_ttl_seconds`` by
age, and ``server_max_bytes`` for the whole server (fair eviction: the biggest
ring loses its oldest first). The sequence number is allocated in one
transaction with its row on the shared database (gap-free across nodes).

Lifecycle: the CLAUDE.md ``asyncio.Event`` scheduler pattern (one loop:
the heads ping and the age / server-cap sweep).
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable
from typing import TYPE_CHECKING

from ..domain.gfs_channel import (
    CHANNEL_FETCH_DONE_FRAME,
    CHANNEL_FETCH_FRAME,
    CHANNEL_GAP_FRAME,
    CHANNEL_HEADS_FRAME,
    CHANNEL_WATCH_FRAME,
    FETCH_PAGE_MAX_BYTES,
    FETCH_PAGE_MAX_ITEMS,
    RING_CLASS_DURABLE,
    RING_CLASS_FIELD,
    ChannelFetch,
    ChannelHead,
    ChannelWatch,
    InvalidChannelWire,
    channel_frame,
    ring_frame,
)
from .config import ChannelRingConfig
from .envelope_relay import RELAY_FRAME_TYPE
from .public import SlidingWindowCounter

if TYPE_CHECKING:
    from ..domain.gfs_channel import ChannelPublishAnonRequest
    from .domain import GfsChannel
    from .repositories import AbstractGfsChannelRepo, AbstractGfsChannelRingRepo
    from .ws_registry import GfsWebSocketRegistry

log = logging.getLogger(__name__)

#: Per-channel bounds at insert (owner F1, 2026-11-27).

#: How often a ring socket hears which of its channels moved (owner F2).
HEADS_PING_INTERVAL_S: float = 15 * 60.0
#: How often the age and server-cap sweep runs.
SWEEP_INTERVAL_S: float = 60.0
#: Fetches one socket may make per minute.
FETCH_MAX_PER_MINUTE: int = 60
#: Watch changes one socket may make per minute (each may name up to
#: ``MAX_WATCHED_CHANNELS`` channels, one batched seat read).
WATCH_MAX_PER_MINUTE: int = 30

#: Frame types a ring socket may send.
RING_CLIENT_FRAMES: frozenset[str] = frozenset(
    {CHANNEL_WATCH_FRAME, CHANNEL_FETCH_FRAME}
)


class GfsChannelRing:
    """Append, watch, fetch, ping and sweep the channel rings."""

    __slots__ = (
        "_channels",
        "_clock",
        "_fetch_limiter",
        "_last_heads",
        "_limits",
        "_ring",
        "_stop",
        "_task",
        "_watch_limiter",
        "_ws",
    )

    def __init__(
        self,
        *,
        ring_repo: AbstractGfsChannelRingRepo,
        channel_repo: AbstractGfsChannelRepo,
        ws_registry: GfsWebSocketRegistry,
        limits: ChannelRingConfig | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        #: The operator's ``[channel_ring]`` bounds (sizes, ages, server cap).
        self._limits = limits or ChannelRingConfig()
        self._ring = ring_repo
        self._channels = channel_repo
        self._ws = ws_registry
        self._clock = clock
        self._fetch_limiter = SlidingWindowCounter(FETCH_MAX_PER_MINUTE)
        self._watch_limiter = SlidingWindowCounter(WATCH_MAX_PER_MINUTE)
        #: What each ring socket was last told, per channel: ``(head, ring_id)``.
        self._last_heads: dict[str, dict[str, tuple[int, str]]] = {}
        self._stop = asyncio.Event()
        self._task: asyncio.Task[None] | None = None

    def _now(self) -> int:
        return int(self._clock())

    # ── Publish ──────────────────────────────────────────────────────────

    async def append(self, req: ChannelPublishAnonRequest) -> dict:
        """Put an accepted publish into its channel's ring and return the
        frame to fan out (with ``seq`` / ``ring_id``; the plain v_96 frame
        when the channel vanished meanwhile)."""
        frame = channel_frame(req.channel_id, req.epoch, req.payload)
        pos = await self._ring.append(
            req.channel_id,
            epoch=req.epoch,
            ring_class=req.ring_class or RING_CLASS_DURABLE,
            payload=req.payload,
            now=self._now(),
            max_items=self._limits.max_items,
            max_bytes=self._limits.max_bytes,
        )
        if pos is None:
            return frame
        seq, ring_id = pos
        out = ring_frame(frame, seq=seq, ring_id=ring_id)
        if req.ring_class is not None:
            # A v_97 publisher (it signs ``ring_class``): a v_96 socket gets
            # the legacy event from it instead (``skips_live``).
            out[RING_CLASS_FIELD] = req.ring_class
        return out

    # ── Seats ────────────────────────────────────────────────────────────

    async def seated(self, channel_id: str, instance_id: str) -> bool:
        """A live seat of ``instance_id`` on ``channel_id`` at an open
        epoch — exactly what a push to it needs."""
        pass_epoch = await self._ring.seat_epoch(channel_id, instance_id)
        if pass_epoch is None:
            return False
        ch = await self._channels.get(channel_id)
        if ch is None:
            return False
        return pass_epoch in ch.open_epochs(now=self._now())

    # ── Socket frames ────────────────────────────────────────────────────

    async def on_frame(self, instance_id: str, raw: object) -> None:
        """A frame a ring socket sent. Malformed or unseated requests are
        dropped silently (DEBUG): the socket learns nothing about a channel
        it holds no seat on."""
        kind = raw.get("type") if isinstance(raw, dict) else None
        try:
            if kind == CHANNEL_WATCH_FRAME:
                await self._watch(instance_id, ChannelWatch.from_wire(raw))
            elif kind == CHANNEL_FETCH_FRAME:
                await self._fetch(instance_id, ChannelFetch.from_wire(raw))
        except InvalidChannelWire as exc:
            log.debug("gfs.ring: malformed %s frame dropped: %s", kind, exc)

    async def _watch(self, instance_id: str, req: ChannelWatch) -> None:
        if not self._watch_limiter.allow(instance_id):
            log.debug("gfs.ring: watch rate limit for a socket — dropped")
            return
        allowed: list[str] = []
        if req.watch:
            now = self._now()
            seats = await self._ring.seat_epochs(instance_id, req.watch)
            for channel_id, pass_epoch in seats.items():
                ch = await self._channels.get(channel_id)
                if ch is not None and pass_epoch in ch.open_epochs(now=now):
                    allowed.append(channel_id)
        self._ws.set_watching(instance_id, add=allowed, remove=req.unwatch)

    async def _fetch(self, instance_id: str, req: ChannelFetch) -> None:
        if not self._fetch_limiter.allow(instance_id):
            log.debug("gfs.ring: fetch rate limit for a socket — dropped")
            return
        if not await self.seated(req.channel_id, instance_id):
            return
        state = await self._ring.state(req.channel_id)
        if state is None or state.ring_id is None:
            # Nothing in this ring yet: done, at head 0.
            await self._send(
                instance_id,
                {
                    "type": CHANNEL_FETCH_DONE_FRAME,
                    "channel_id": req.channel_id,
                    "req": req.req,
                    "head_seq": state.head_seq if state else 0,
                    "ring_id": state.ring_id if state else None,
                    "more": False,
                },
            )
            return
        oldest = (
            state.oldest_seq if state.oldest_seq is not None else state.head_seq + 1
        )
        # A gap only when a DURABLE item after the cursor is gone: missing
        # an expired ephemeral item (a location pin) is no gap.
        if (
            req.ring_id != state.ring_id
            or req.since_seq > state.head_seq
            or req.since_seq < state.durable_floor
        ):
            await self._send(
                instance_id,
                {
                    "type": CHANNEL_GAP_FRAME,
                    "channel_id": req.channel_id,
                    "req": req.req,
                    "head_seq": state.head_seq,
                    "ring_id": state.ring_id,
                    "oldest_seq": oldest,
                },
            )
            return
        items = await self._ring.fetch(
            req.channel_id,
            after_seq=req.since_seq,
            max_items=FETCH_PAGE_MAX_ITEMS,
            max_bytes=FETCH_PAGE_MAX_BYTES,
        )
        # Read again AFTER the items: an eviction between the two reads may
        # have dropped durable items after the cursor — then this is a gap,
        # never a page that silently skips them.
        after = await self._ring.state(req.channel_id)
        if (
            after is None
            or after.ring_id != state.ring_id
            or req.since_seq < after.durable_floor
        ):
            await self._send(
                instance_id,
                {
                    "type": CHANNEL_GAP_FRAME,
                    "channel_id": req.channel_id,
                    "req": req.req,
                    "head_seq": after.head_seq if after else state.head_seq,
                    "ring_id": (after.ring_id if after else None) or state.ring_id,
                    "oldest_seq": (
                        after.oldest_seq
                        if after and after.oldest_seq is not None
                        else state.head_seq + 1
                    ),
                },
            )
            return
        for item in items:
            frame = ring_frame(
                channel_frame(item.channel_id, item.epoch, item.payload),
                seq=item.seq,
                ring_id=state.ring_id,
            )
            # Straight to the socket: a fetch answer is never watch-filtered.
            # ``fetch`` names the request it answers: the household moves its
            # cursor over a page's holes (expired items) only for those.
            if not await self._ws.send_direct(
                instance_id, {"type": RELAY_FRAME_TYPE, **frame, "fetch": req.req}
            ):
                return
        # ``more`` only when the page was cut: an empty page under a head
        # above the cursor means the tail expired (an ephemeral item) —
        # ``more: false`` tells the household to move its cursor to the head.
        last = items[-1].seq if items else req.since_seq
        await self._send(
            instance_id,
            {
                "type": CHANNEL_FETCH_DONE_FRAME,
                "channel_id": req.channel_id,
                "req": req.req,
                "head_seq": state.head_seq,
                "ring_id": state.ring_id,
                "more": bool(items) and last < state.head_seq,
            },
        )

    async def _send(self, instance_id: str, frame: dict) -> bool:
        return await self._ws.send_direct(instance_id, frame)

    # ── Heads ────────────────────────────────────────────────────────────

    async def send_heads(self, instance_id: str, *, initial: bool) -> int:
        """Tell a ring socket the heads of its seated channels — all of them
        (``initial``, right after the hello) or only those that moved since
        the last ping. Nothing is sent when nothing moved. Returns how many
        heads went out."""
        now = self._now()
        rows = await self._ring.heads_for_seats(instance_id)
        channels: dict[str, GfsChannel | None] = {}
        heads: list[ChannelHead] = []
        last = {} if initial else self._last_heads.get(instance_id, {})
        current: dict[str, tuple[int, str]] = {}
        for channel_id, head, ring_id, pass_epoch in rows:
            if channel_id not in channels:
                channels[channel_id] = await self._channels.get(channel_id)
            ch = channels[channel_id]
            if ch is None or pass_epoch not in ch.open_epochs(now=now):
                continue
            current[channel_id] = (head, ring_id)
            if last.get(channel_id) != (head, ring_id):
                heads.append(ChannelHead(channel_id, head, ring_id))
        if heads and not await self._send(
            instance_id,
            {"type": CHANNEL_HEADS_FRAME, "heads": [h.to_wire() for h in heads]},
        ):
            return 0
        self._last_heads[instance_id] = current
        return len(heads)

    def forget(self, instance_id: str) -> None:
        """A ring socket closed: its last-ping memory goes with it."""
        self._last_heads.pop(instance_id, None)

    # ── Sweep ────────────────────────────────────────────────────────────

    async def sweep(self) -> int:
        """Drop items past their age, then trim the biggest rings while the
        server is over its ``server_max_bytes``. Returns items expired."""
        now = self._now()
        expired = 0
        for _ in range(64):  # bounded batches, each its own transaction
            n = await self._ring.expire(
                durable_before=now - self._limits.durable_ttl_seconds,
                ephemeral_before=now - self._limits.ephemeral_ttl_seconds,
            )
            expired += n
            if n == 0:
                break
        # Until the server is under its cap (each pass cuts the biggest rings
        # to one level; bounded so a sweep always ends).
        for _ in range(64):
            if await self._ring.trim_to_cap(cap=self._limits.server_max_bytes) <= 0:
                break
        return expired

    # ── Lifecycle ────────────────────────────────────────────────────────

    async def start(self) -> None:
        self._stop.clear()
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._loop(), name="gfs-channel-ring")

    async def stop(self) -> None:
        self._stop.set()
        if self._task is not None:
            await self._task
            self._task = None

    async def _loop(self) -> None:
        next_heads = time.monotonic() + HEADS_PING_INTERVAL_S
        while not self._stop.is_set():
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=SWEEP_INTERVAL_S)
            except TimeoutError:
                pass
            if self._stop.is_set():
                return
            try:
                await self.sweep()
            except Exception:
                log.warning("gfs.ring: sweep failed", exc_info=True)
            if time.monotonic() < next_heads:
                continue
            next_heads = time.monotonic() + HEADS_PING_INTERVAL_S
            for instance_id in self._ws.ring_instances():
                if self._stop.is_set():
                    return
                try:
                    await self.send_heads(instance_id, initial=False)
                except Exception:
                    log.warning("gfs.ring: heads ping failed", exc_info=True)
