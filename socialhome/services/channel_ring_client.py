"""The household side of a GFS space's channel ring (v_97).

A GFS proving ``channel_ring`` keeps one ring of recent items per channel
(``docs/protocol/space-items.md``). This household:

* **watches** a space's channel on every GFS it reads the ring on while a
  person has the space open (the SPA sends ``space.viewing`` about once a
  minute) and :data:`WATCH_LINGER_S` after the last view; the **host always
  watches** the channels of the spaces it owns (owner F5). The GFS forgets a
  socket's watches when it drops, so they are sent again on every connect
  (the first heads ping after a hello);
* keeps a **cursor** ``(ring_id, seq)`` per ``(gfs, channel)``, persisted
  (coalesced) in ``instance_config`` — no table of its own. A new seat
  starts at the head (owner F8: history comes from the join catch-up);
* takes a **heads ping**: a head above the cursor raises the space's badge
  (:class:`~socialhome.domain.events.SpaceChannelActivity`, contentless —
  owner F6), or — the host, or a space being viewed — fetches at once;
* **fetches** since the cursor on open; every item arrives as an ordinary
  relay frame (handled by the channel inbound first) and moves the cursor;
* on a **gap** (the ring was reset, the cursor fell off it) or when more
  than :data:`MAX_BEHIND_ITEMS` behind: moves the cursor near the head,
  fetches the recent window (current pins, the last chat) and runs the
  §25.6 catch-up from the host;
* **fails over**: a GFS where it has no cursor yet while another GFS of the
  same channel has one starts :data:`MAX_BEHIND_ITEMS` back; the item ids
  dedupe what both delivered.

Scheduler: the CLAUDE.md ``asyncio.Event`` pattern (one loop: linger expiry
and the cursor flush).
"""

from __future__ import annotations

import asyncio
import logging
import secrets
import time
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Protocol

import orjson

from ..domain.events import SpaceChannelActivity
from ..domain.gfs_channel import (
    CHANNEL_FETCH_DONE_FRAME,
    CHANNEL_GAP_FRAME,
    CHANNEL_HEADS_FRAME,
    ChannelFetch,
    ChannelHead,
    ChannelWatch,
    InvalidChannelWire,
    ring_position,
    valid_channel_id,
    valid_ring_id,
)
from .gfs_ws_client import RING_CONNECTED_FRAME

if TYPE_CHECKING:
    from ..domain.federation import GfsConnection
    from ..infrastructure.event_bus import EventBus
    from ..repositories.instance_config_repo import AbstractInstanceConfigRepo
    from ..repositories.space_repo import AbstractSpaceRepo

log = logging.getLogger(__name__)

#: How long a channel stays watched after the last ``space.viewing``.
WATCH_LINGER_S: float = 120.0
#: A cursor this far behind the head is not fetched item by item: the
#: household skips to the head and runs the §25.6 catch-up (also the
#: failover window on a second GFS).
MAX_BEHIND_ITEMS: int = 2000
#: After a gap: the recent window fetched so current pins and the last
#: chat show at once (the catch-up brings the rest).
RECENT_AFTER_GAP: int = 200
#: A fetch with no answer is forgotten after this long (another may go).
FETCH_TIMEOUT_S: float = 30.0
#: The loop's tick (linger expiry, cursor flush).
TICK_S: float = 10.0
#: A gap runs the §25.6 catch-up for a space at most once per this long.
GAP_CATCH_UP_INTERVAL_S: float = 600.0
#: ``instance_config`` key prefix of a GFS's cursors.
CURSOR_KEY_PREFIX: str = "channel_ring_cursors:"

Sender = Callable[[str, dict], Awaitable[bool]]


class _Channels(Protocol):
    async def ring_connections(
        self, space_id: str
    ) -> tuple[str, list[GfsConnection]] | None: ...


class _SyncScheduler(Protocol):
    async def enqueue_sync_for_space(
        self, *, space_id: str, peer_instance_id: str
    ) -> None: ...


class ChannelRingClient:
    """Watch, cursors, fetch, gaps — see the module docstring."""

    __slots__ = (
        "_bus",
        "_caught_up_at",
        "_channels",
        "_clock",
        "_config",
        "_cursors",
        "_dirty",
        "_fetching",
        "_loaded",
        "_own_instance_id",
        "_send",
        "_spaces",
        "_stop",
        "_sync",
        "_task",
        "_unread",
        "_viewed",
        "_watched",
    )

    def __init__(
        self,
        *,
        spaces: AbstractSpaceRepo,
        channels: _Channels,
        config: AbstractInstanceConfigRepo,
        bus: EventBus,
        own_instance_id: str,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._spaces = spaces
        self._channels = channels
        self._config = config
        self._bus = bus
        self._own_instance_id = own_instance_id
        self._clock = clock
        self._send: Sender | None = None
        self._sync: _SyncScheduler | None = None
        #: ``(gfs_id, channel_id)`` → ``(ring_id, seq)``.
        self._cursors: dict[tuple[str, str], tuple[str, int]] = {}
        self._loaded: set[str] = set()
        self._dirty: set[str] = set()
        #: ``(gfs_id, channel_id)`` → when the fetch went out.
        self._fetching: dict[tuple[str, str], float] = {}
        #: space id → last ``space.viewing``.
        self._viewed: dict[str, float] = {}
        #: gfs id → the channels we told that GFS we watch.
        self._watched: dict[str, set[str]] = {}
        #: space id → when a gap last ran its catch-up.
        self._caught_up_at: dict[str, float] = {}
        #: space id → the unread count last reported.
        self._unread: dict[str, int] = {}
        self._stop = asyncio.Event()
        self._task: asyncio.Task[None] | None = None

    def attach_sender(self, send: Sender) -> None:
        """The socket send (``GfsWebSocketSupervisor.send``)."""
        self._send = send

    def attach_sync_scheduler(self, scheduler: _SyncScheduler) -> None:
        """The §25.6 catch-up a gap falls back to."""
        self._sync = scheduler

    # ── Cursors ──────────────────────────────────────────────────────────

    async def _load(self, gfs_id: str) -> None:
        if gfs_id in self._loaded:
            return
        self._loaded.add(gfs_id)
        raw = await self._config.get(CURSOR_KEY_PREFIX + gfs_id)
        if not raw:
            return
        try:
            stored = orjson.loads(raw)
            for channel_id, (ring_id, seq) in stored.items():
                key = (gfs_id, valid_channel_id(channel_id))
                if key not in self._cursors:
                    self._cursors[key] = (valid_ring_id(ring_id), int(seq))
        except ValueError, TypeError, InvalidChannelWire:
            log.warning("channel ring: unreadable cursors for a GFS — reset")

    def _set_cursor(self, gfs_id: str, channel_id: str, ring_id: str, seq: int) -> None:
        self._cursors[(gfs_id, channel_id)] = (ring_id, seq)
        self._dirty.add(gfs_id)

    async def flush(self) -> None:
        """Persist the cursors of every GFS that moved since the last flush."""
        for gfs_id in list(self._dirty):
            self._dirty.discard(gfs_id)
            body = {
                ch: [rid, seq]
                for (g, ch), (rid, seq) in self._cursors.items()
                if g == gfs_id
            }
            await self._config.set(
                CURSOR_KEY_PREFIX + gfs_id, orjson.dumps(body).decode()
            )

    def cursor(self, gfs_id: str, channel_id: str) -> tuple[str, int] | None:
        return self._cursors.get((gfs_id, channel_id))

    # ── Frames from a GFS ────────────────────────────────────────────────

    async def on_frame(self, frame: dict, *, gfs_id: str) -> None:
        """A ring answer, or a relay item's place (after the item itself was
        handled by the channel inbound)."""
        await self._load(gfs_id)
        kind = frame.get("type")
        try:
            if kind == RING_CONNECTED_FRAME:
                await self.disconnected(gfs_id)
                await self._send_watch(gfs_id, initial=True)
            elif kind == "relay":
                await self._on_item(gfs_id, frame)
            elif kind == CHANNEL_HEADS_FRAME:
                await self._on_heads(gfs_id, frame)
            elif kind == CHANNEL_FETCH_DONE_FRAME:
                await self._on_done(gfs_id, frame)
            elif kind == CHANNEL_GAP_FRAME:
                await self._on_gap(gfs_id, frame)
        except (InvalidChannelWire, KeyError, TypeError, ValueError) as exc:
            log.info("channel ring: malformed %s frame: %s", kind, exc)

    async def _on_item(self, gfs_id: str, frame: dict) -> None:
        pos = ring_position(frame)
        if pos is None:
            return
        seq, ring_id = pos
        channel_id = valid_channel_id(frame.get("channel_id"))
        cur = self.cursor(gfs_id, channel_id)
        if cur is None:
            # A new seat starts here (owner F8).
            self._set_cursor(gfs_id, channel_id, ring_id, seq)
        elif cur[0] != ring_id:
            # The ring was reset under us: what we missed is unknown.
            await self._gap(gfs_id, channel_id, ring_id, head=seq, oldest=seq)
        elif seq == cur[1] + 1:
            self._set_cursor(gfs_id, channel_id, ring_id, seq)
        elif seq > cur[1] + 1:
            # A hole before a live item (another node's push raced it, or
            # we were not watching): pull it; the copies dedupe by item id.
            await self._fetch(gfs_id, channel_id)

    async def _on_heads(self, gfs_id: str, frame: dict) -> None:
        heads = frame.get("heads")
        if not isinstance(heads, list):
            return
        for raw in heads:
            head = ChannelHead.from_wire(raw)
            await self._on_head(gfs_id, head)

    async def _on_head(self, gfs_id: str, head: ChannelHead) -> None:
        cur = self.cursor(gfs_id, head.channel_id)
        if cur is None:
            elsewhere = any(
                ch == head.channel_id and g != gfs_id for (g, ch) in self._cursors
            )
            if elsewhere:
                # Failover: we read this channel on another GFS; take the
                # recent window here too (item ids dedupe).
                start = max(0, head.head_seq - MAX_BEHIND_ITEMS)
                self._set_cursor(gfs_id, head.channel_id, head.ring_id, start)
                await self._fetch(gfs_id, head.channel_id)
            else:
                self._set_cursor(gfs_id, head.channel_id, head.ring_id, head.head_seq)
            return
        if cur[0] != head.ring_id or head.head_seq < cur[1]:
            # A heads ping names no oldest item: take the recent window.
            await self._gap(gfs_id, head.channel_id, head.ring_id, head.head_seq, 0)
            return
        behind = head.head_seq - cur[1]
        if behind <= 0:
            return
        if behind > MAX_BEHIND_ITEMS:
            # A heads ping names no oldest item: take the recent window.
            await self._gap(gfs_id, head.channel_id, head.ring_id, head.head_seq, 0)
            return
        for space_id in await self._spaces.spaces_for_gfs_channel(head.channel_id):
            if await self._eager(space_id):
                await self._fetch(gfs_id, head.channel_id)
            else:
                await self._report(space_id, behind)

    async def _on_done(self, gfs_id: str, frame: dict) -> None:
        channel_id = valid_channel_id(frame.get("channel_id"))
        self._fetching.pop((gfs_id, channel_id), None)
        if frame.get("more") is True:
            await self._fetch(gfs_id, channel_id)
            return
        # Caught up: whatever is left above the cursor expired (an ephemeral
        # item), so the cursor moves to the head.
        ring_id = frame.get("ring_id")
        cur = self.cursor(gfs_id, channel_id)
        head = frame.get("head_seq")
        if (
            cur is not None
            and isinstance(head, int)
            and ring_id == cur[0]
            and head > cur[1]
        ):
            self._set_cursor(gfs_id, channel_id, cur[0], head)
        for space_id in await self._spaces.spaces_for_gfs_channel(channel_id):
            await self._report(space_id, 0)

    async def _on_gap(self, gfs_id: str, frame: dict) -> None:
        channel_id = valid_channel_id(frame.get("channel_id"))
        self._fetching.pop((gfs_id, channel_id), None)
        head = int(frame["head_seq"])
        oldest = int(frame.get("oldest_seq") or head)
        await self._gap(
            gfs_id, channel_id, valid_ring_id(frame.get("ring_id")), head, oldest
        )

    async def _gap(
        self, gfs_id: str, channel_id: str, ring_id: str, head: int, oldest: int
    ) -> None:
        """Skip to near the head, pull the recent window, and let the §25.6
        catch-up bring the rest."""
        start = max(oldest - 1, head - RECENT_AFTER_GAP, 0)
        self._set_cursor(gfs_id, channel_id, ring_id, start)
        if start < head:
            await self._fetch(gfs_id, channel_id)
        for space_id in await self._spaces.spaces_for_gfs_channel(channel_id):
            space = await self._spaces.get(space_id)
            if (
                self._sync is None
                or space is None
                or space.owner_instance_id == self._own_instance_id
            ):
                continue
            last = self._caught_up_at.get(space_id)
            if last is not None and self._clock() - last < GAP_CATCH_UP_INTERVAL_S:
                continue
            self._caught_up_at[space_id] = self._clock()
            log.info(
                "channel ring: gap on space %s's channel — §25.6 catch-up", space_id
            )
            await self._sync.enqueue_sync_for_space(
                space_id=space_id, peer_instance_id=space.owner_instance_id
            )

    async def _fetch(self, gfs_id: str, channel_id: str) -> None:
        key = (gfs_id, channel_id)
        sent = self._fetching.get(key)
        if sent is not None and self._clock() - sent < FETCH_TIMEOUT_S:
            return
        cur = self.cursor(gfs_id, channel_id)
        if cur is None or self._send is None:
            return
        req = ChannelFetch(
            channel_id=channel_id,
            since_seq=cur[1],
            req=secrets.token_hex(8),
            ring_id=cur[0],
        )
        if await self._send(gfs_id, req.to_wire()):
            self._fetching[key] = self._clock()

    async def _report(self, space_id: str, unread: int) -> None:
        if self._unread.get(space_id, 0) == unread:
            return
        self._unread[space_id] = unread
        await self._bus.publish(SpaceChannelActivity(space_id=space_id, unread=unread))

    # ── Watching ─────────────────────────────────────────────────────────

    async def _hosts(self, space_id: str) -> bool:
        space = await self._spaces.get(space_id)
        return space is not None and space.owner_instance_id == self._own_instance_id

    def _viewing(self, space_id: str) -> bool:
        at = self._viewed.get(space_id)
        return at is not None and self._clock() - at <= WATCH_LINGER_S

    async def _eager(self, space_id: str) -> bool:
        """Fetch at a heads ping (instead of only badging): the host, or a
        space a person has open."""
        return self._viewing(space_id) or await self._hosts(space_id)

    async def _desired(self) -> dict[str, set[str]]:
        """gfs id → the channels we should be watching there: the spaces a
        person has open (plus the linger) and the spaces we host."""
        out: dict[str, set[str]] = {}
        space_ids = {s for s in self._viewed if self._viewing(s)}
        space_ids |= set(
            await self._spaces.hosted_channel_spaces(self._own_instance_id)
        )
        for space_id in space_ids:
            found = await self._channels.ring_connections(space_id)
            if found is None:
                continue
            channel_id, conns = found
            for conn in conns:
                out.setdefault(conn.id, set()).add(channel_id)
        return out

    async def _send_watch(self, gfs_id: str, *, initial: bool = False) -> None:
        if self._send is None:
            return
        want = (await self._desired()).get(gfs_id, set())
        have = set() if initial else self._watched.get(gfs_id, set())
        add, drop = want - have, have - want
        if not initial and not add and not drop:
            return
        frame = ChannelWatch(watch=tuple(sorted(add)), unwatch=tuple(sorted(drop)))
        if await self._send(gfs_id, frame.to_wire()):
            self._watched[gfs_id] = want

    async def viewing(self, space_id: str) -> None:
        """A person has ``space_id`` open (the SPA says so about once a
        minute): watch its channel and pull what we missed."""
        fresh = not self._viewing(space_id)
        self._viewed[space_id] = self._clock()
        found = await self._channels.ring_connections(space_id)
        if found is None:
            return
        channel_id, conns = found
        for conn in conns:
            await self._load(conn.id)
            await self._send_watch(conn.id)
            if fresh:
                await self._fetch(conn.id, channel_id)

    async def disconnected(self, gfs_id: str) -> None:
        """A socket dropped: its watches and fetches went with it."""
        self._watched.pop(gfs_id, None)
        for key in [k for k in self._fetching if k[0] == gfs_id]:
            self._fetching.pop(key, None)

    async def tick(self) -> None:
        """Expire views past the linger (unwatch) and persist cursors."""
        expired = [s for s in self._viewed if not self._viewing(s)]
        for space_id in expired:
            self._viewed.pop(space_id, None)
        if expired:
            for gfs_id in list(self._watched):
                await self._send_watch(gfs_id)
        await self.flush()

    # ── Lifecycle ────────────────────────────────────────────────────────

    async def start(self) -> None:
        self._stop.clear()
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._loop(), name="channel-ring-client")

    async def stop(self) -> None:
        self._stop.set()
        if self._task is not None:
            await self._task
            self._task = None
        await self.flush()

    async def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=TICK_S)
            except TimeoutError:
                pass
            if self._stop.is_set():
                return
            try:
                await self.tick()
            except Exception:
                log.warning("channel ring: tick failed", exc_info=True)
