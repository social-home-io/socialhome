"""The channel ring (v_97, capability ``channel_ring``) — the GFS side.

Every opaque channel keeps ONE ring of recent items (GFS migration 0034),
not a copy per seat. A household socket that listed ``channel_ring`` in
its hello:

* gets an item live only while it **watches** that channel
  (``channel_watch``; the filter is in :class:`~.ws_registry.GfsWebSocketRegistry`)
  — or for every item published with ``push_all`` (a host statement);
* gets ``channel_heads`` right after its hello and then every
  :data:`HEADS_PING_INTERVAL_S` (owner F2: 15 min), naming only the channels
  whose head moved since the last ping to that socket;
* pulls what it missed with ``channel_fetch`` — answered with ``relay``
  frames carrying ``seq`` / ``ring_id`` and a ``channel_fetch_done``, or a
  ``channel_gap`` when its cursor is no longer in the ring.

Every watch and fetch needs a LIVE seat at an open epoch — the same seat a
push goes to — so the ring tells a household nothing it would not be pushed.
Nothing here learns a space: the ring is keyed by the random channel id.

Bounds (owner F1): :data:`RING_MAX_ITEMS` / :data:`RING_MAX_BYTES` per
channel at insert, :data:`~socialhome.domain.gfs_channel.RING_DURABLE_TTL_S`
/ :data:`~socialhome.domain.gfs_channel.RING_EPHEMERAL_TTL_S` by age, and
:data:`SERVER_MAX_BYTES` for the whole server (fair eviction: the biggest
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
    RING_DURABLE_TTL_S,
    RING_EPHEMERAL_TTL_S,
    ChannelFetch,
    ChannelHead,
    ChannelWatch,
    InvalidChannelWire,
    channel_frame,
    ring_frame,
)
from .envelope_relay import RELAY_FRAME_TYPE
from .public import SlidingWindowCounter

if TYPE_CHECKING:
    from ..domain.gfs_channel import ChannelPublishAnonRequest
    from .domain import GfsChannel
    from .repositories import AbstractGfsChannelRepo, AbstractGfsChannelRingRepo
    from .ws_registry import GfsWebSocketRegistry

log = logging.getLogger(__name__)

#: Per-channel bounds at insert (owner F1, 2026-11-27).
RING_MAX_ITEMS: int = 32_768
RING_MAX_BYTES: int = 64 * 1024 * 1024
#: The whole server's rings (owner F1).
SERVER_MAX_BYTES: int = 8 * 1024 * 1024 * 1024

#: How often a ring socket hears which of its channels moved (owner F2).
HEADS_PING_INTERVAL_S: float = 15 * 60.0
#: How often the age and server-cap sweep runs.
SWEEP_INTERVAL_S: float = 60.0
#: Fetches one socket may make per minute.
FETCH_MAX_PER_MINUTE: int = 60

#: Frame types a ring socket may send.
RING_CLIENT_FRAMES: frozenset[str] = frozenset(
    {CHANNEL_WATCH_FRAME, CHANNEL_FETCH_FRAME}
)


def _open_epochs(ch: GfsChannel, *, now: int) -> set[int]:
    """The epochs a seat may hold to be served now (same rule as a push)."""
    return ch.open_epochs(now=now)


class GfsChannelRing:
    """Append, watch, fetch, ping and sweep the channel rings."""

    __slots__ = (
        "_channels",
        "_clock",
        "_fetch_limiter",
        "_last_heads",
        "_ring",
        "_stop",
        "_task",
        "_ws",
    )

    def __init__(
        self,
        *,
        ring_repo: AbstractGfsChannelRingRepo,
        channel_repo: AbstractGfsChannelRepo,
        ws_registry: GfsWebSocketRegistry,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._ring = ring_repo
        self._channels = channel_repo
        self._ws = ws_registry
        self._clock = clock
        self._fetch_limiter = SlidingWindowCounter(FETCH_MAX_PER_MINUTE)
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
            max_items=RING_MAX_ITEMS,
            max_bytes=RING_MAX_BYTES,
        )
        if pos is None:
            return frame
        seq, ring_id = pos
        return ring_frame(frame, seq=seq, ring_id=ring_id, push_all=req.push_all)

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
        return pass_epoch in _open_epochs(ch, now=self._now())

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
        allowed = [c for c in req.watch if await self.seated(c, instance_id)]
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
        if (
            req.ring_id != state.ring_id
            or req.since_seq > state.head_seq
            or req.since_seq < oldest - 1
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
        for item in items:
            frame = ring_frame(
                channel_frame(item.channel_id, item.epoch, item.payload),
                seq=item.seq,
                ring_id=state.ring_id,
                push_all=False,
            )
            # Straight to the socket: a fetch answer is never watch-filtered.
            if not await self._ws.send_direct(
                instance_id, {"type": RELAY_FRAME_TYPE, **frame}
            ):
                return
        last = items[-1].seq if items else req.since_seq
        await self._send(
            instance_id,
            {
                "type": CHANNEL_FETCH_DONE_FRAME,
                "channel_id": req.channel_id,
                "req": req.req,
                "head_seq": state.head_seq,
                "ring_id": state.ring_id,
                "more": last < state.head_seq,
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
            if ch is None or pass_epoch not in _open_epochs(ch, now=now):
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
        server is over :data:`SERVER_MAX_BYTES`. Returns items expired."""
        now = self._now()
        expired = await self._ring.expire(
            durable_before=now - RING_DURABLE_TTL_S,
            ephemeral_before=now - RING_EPHEMERAL_TTL_S,
        )
        for _ in range(64):  # bounded per sweep
            excess = await self._ring.total_bytes() - SERVER_MAX_BYTES
            if excess <= 0:
                break
            if await self._ring.trim_largest(excess=excess) <= 0:
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
