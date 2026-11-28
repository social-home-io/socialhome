"""The household side of a GFS space's channel ring (v_97).

A GFS proving ``channel_ring`` keeps one ring of recent items per channel
(``docs/protocol/space-items.md``). This household:

* **watches** a space's channel on every ring-capable GFS it reads it on
  while a person has the space open (the SPA sends ``space.viewing`` about
  once a minute) and :data:`WATCH_LINGER_S` after the last view; the **host
  always watches** the channels of the spaces it owns (owner F5 — effective
  once the owner holds a seat). The GFS forgets a socket's watches when it
  drops, so they go again on every connect;
* keeps a **cursor** ``(ring_id, seq)`` per ``(gfs, channel)``, persisted
  (coalesced) in ``instance_config`` — no table of its own. A new seat
  starts at the head (owner F8: history comes from the join catch-up);
* takes a **heads ping**: a head above the cursor raises the space's badge
  (:class:`~socialhome.domain.events.SpaceChannelActivity`, only on the
  change "nothing new" → "something new" — owner F6), or — the host, or a
  space being viewed — fetches at once;
* **fetches** since the cursor; the GFS tags its answers with the request
  id, and those move the cursor over the page's holes (expired location
  pins — a missing DURABLE item is a ``channel_gap``). A live item moves it
  only in order; a hole before one is a badge (a non-viewer) or a fetch;
  a live item never pulls a backlog by itself. An item the inbound
  path DROPPED (over its budget) keeps the cursor before it, and that fetch
  stops; pages follow each other at most every :data:`PAGE_INTERVAL_S`, so
  a backlog never outruns the per-channel inbound budget;
* on a **gap** (the ring was reset, the cursor fell off it) or when more
  than :data:`MAX_BEHIND_ITEMS` behind: moves the cursor near the head,
  fetches the recent window where someone looks, and runs the §25.6
  catch-up (:meth:`SpaceSyncScheduler.sync_now` — the acting hosts,
  incremental), at most once per :data:`GAP_CATCH_UP_INTERVAL_S` per space,
  a later one deferred, never dropped;
* takes ring frames only for channels it reads **on that GFS** (a space
  here whose channel lives on that connection), so no GFS can grow its
  state, raise badges or force catch-ups for a channel it doesn't serve.

Scheduler: the CLAUDE.md ``asyncio.Event`` pattern (one loop: pages, linger
expiry, watch retries, deferred catch-ups, the cursor flush).
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
    MAX_RING_SEQ,
    MAX_WATCHED_CHANNELS,
    ChannelFetch,
    ChannelHead,
    ChannelWatch,
    InvalidChannelWire,
    ring_position,
    valid_channel_id,
    valid_ring_id,
)
from ..federation.host_equivalence import serves_as_host
from .gfs_ws_client import RING_CONNECTED_FRAME, RING_TAKEN_FIELD

if TYPE_CHECKING:
    from ..domain.federation import GfsConnection
    from ..infrastructure.event_bus import EventBus
    from ..repositories.instance_config_repo import AbstractInstanceConfigRepo
    from ..repositories.space_repo import AbstractSpaceRepo

log = logging.getLogger(__name__)

#: How long a channel stays watched after the last ``space.viewing``.
WATCH_LINGER_S: float = 120.0
#: A cursor this far behind the head is not fetched item by item: the
#: household skips to the recent window and runs the §25.6 catch-up.
MAX_BEHIND_ITEMS: int = 2000
#: After a gap: the recent window fetched so current pins and the last
#: chat show at once (the catch-up brings the rest).
RECENT_AFTER_GAP: int = 200
#: A fetch with no answer is forgotten after this long (another may go).
FETCH_TIMEOUT_S: float = 30.0
#: Between two pages of one fetch (500 items each — the per-channel
#: inbound budget is 1 500 a minute: three pages).
PAGE_INTERVAL_S: float = 20.0
#: A gap runs the §25.6 catch-up for a space at most once per this long.
GAP_CATCH_UP_INTERVAL_S: float = 600.0
#: "New activity" is announced at most once per space per this long.
ACTIVITY_QUIET_S: float = 3600.0
#: How long the "which channels do we read on which GFS" answer is kept.
KNOWN_TTL_S: float = 300.0
#: The loop's tick.
TICK_S: float = 10.0
#: ``instance_config`` key prefix of a GFS's cursors, and the most cursors
#: one GFS's blob may hold when read back.
CURSOR_KEY_PREFIX: str = "channel_ring_cursors:"
MAX_CURSORS_PER_GFS: int = 4096

Sender = Callable[[str, dict], Awaitable[bool]]


class _Channels(Protocol):
    async def ring_connections(
        self, space_id: str
    ) -> tuple[str, list[GfsConnection]] | None: ...


class _SyncScheduler(Protocol):
    async def sync_now(self, space_id: str) -> list[str]: ...


def _seq(value: object) -> int:
    if (
        not isinstance(value, int)
        or isinstance(value, bool)
        or not 0 <= value <= MAX_RING_SEQ
    ):
        raise InvalidChannelWire("invalid seq")
    return value


class ChannelRingClient:
    """Watch, cursors, fetch, gaps — see the module docstring."""

    __slots__ = (
        "_active",
        "_announced_at",
        "_bus",
        "_catch_up_due",
        "_caught_up_at",
        "_channels",
        "_clock",
        "_config",
        "_cursors",
        "_dirty",
        "_fetching",
        "_heads",
        "_known",
        "_loaded",
        "_next_page",
        "_own_instance_id",
        "_send",
        "_spaces",
        "_stalled",
        "_stop",
        "_sync",
        "_task",
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
        #: ``(gfs_id, channel_id)`` → the last head seen ``(ring_id, seq)``.
        self._heads: dict[tuple[str, str], tuple[str, int]] = {}
        self._loaded: set[str] = set()
        self._dirty: set[str] = set()
        #: ``(gfs_id, channel_id)`` → ``(req, sent_at)`` of the fetch out.
        self._fetching: dict[tuple[str, str], tuple[str, float]] = {}
        #: Fetches whose page met a dropped item: no more cursor moves
        #: from that page.
        self._stalled: set[tuple[str, str]] = set()
        #: ``(gfs_id, channel_id)`` → when its next page may go.
        self._next_page: dict[tuple[str, str], float] = {}
        #: gfs id → (when read, the channel → space ids we read there).
        self._known: dict[str, tuple[float, dict[str, list[str]]]] = {}
        #: space id → last ``space.viewing``.
        self._viewed: dict[str, float] = {}
        #: gfs id → the channels we told that GFS we watch.
        self._watched: dict[str, set[str]] = {}
        #: Spaces whose badge is on, and when "New activity" last went out.
        self._active: set[str] = set()
        self._announced_at: dict[str, float] = {}
        #: space id → when a gap last ran its catch-up; spaces owed one.
        self._caught_up_at: dict[str, float] = {}
        self._catch_up_due: set[str] = set()
        self._stop = asyncio.Event()
        self._task: asyncio.Task[None] | None = None

    def attach_sender(self, send: Sender) -> None:
        """The socket send (``GfsWebSocketSupervisor.send``)."""
        self._send = send

    def attach_sync_scheduler(self, scheduler: _SyncScheduler) -> None:
        """The §25.6 catch-up a gap falls back to."""
        self._sync = scheduler

    # ── Which channels a GFS may talk about ──────────────────────────────

    async def _read_here(self, gfs_id: str) -> dict[str, list[str]]:
        """channel id → the spaces here we read on ``gfs_id`` (cached). A
        ring frame for any other channel is ignored."""
        cached = self._known.get(gfs_id)
        if cached is not None and self._clock() - cached[0] < KNOWN_TTL_S:
            return cached[1]
        out: dict[str, list[str]] = {}
        for space_id in await self._spaces.list_channel_spaces():
            found = await self._channels.ring_connections(space_id)
            if found is None:
                continue
            channel_id, conns = found
            if any(c.id == gfs_id for c in conns):
                out.setdefault(channel_id, []).append(space_id)
        self._known[gfs_id] = (self._clock(), out)
        return out

    async def _spaces_of(self, gfs_id: str, channel_id: str) -> list[str]:
        return (await self._read_here(gfs_id)).get(channel_id, [])

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
            if not isinstance(stored, dict) or len(stored) > MAX_CURSORS_PER_GFS:
                raise InvalidChannelWire("cursor blob")
            parsed = {
                (gfs_id, valid_channel_id(ch)): (valid_ring_id(rid), _seq(seq))
                for ch, (rid, seq) in stored.items()
            }
        except ValueError, TypeError, InvalidChannelWire:
            log.warning("channel ring: unreadable cursors for a GFS — reset")
            return
        for key, value in parsed.items():
            self._cursors.setdefault(key, value)

    def _set_cursor(self, gfs_id: str, channel_id: str, ring_id: str, seq: int) -> None:
        self._cursors[(gfs_id, channel_id)] = (ring_id, seq)
        self._dirty.add(gfs_id)

    async def flush(self) -> None:
        """Persist the cursors of every GFS that moved since the last flush
        (one GFS failing never keeps the others from being saved)."""
        for gfs_id in list(self._dirty):
            self._dirty.discard(gfs_id)
            body = {
                ch: [rid, seq]
                for (g, ch), (rid, seq) in self._cursors.items()
                if g == gfs_id
            }
            try:
                await self._config.set(
                    CURSOR_KEY_PREFIX + gfs_id, orjson.dumps(body).decode()
                )
            except Exception:
                self._dirty.add(gfs_id)
                log.warning("channel ring: saving cursors failed", exc_info=True)

    def cursor(self, gfs_id: str, channel_id: str) -> tuple[str, int] | None:
        return self._cursors.get((gfs_id, channel_id))

    # ── Frames from a GFS ────────────────────────────────────────────────

    async def on_frame(self, frame: dict, *, gfs_id: str) -> None:
        """A ring answer, or a relay item's place (after the item itself was
        handled by the channel inbound)."""
        kind = frame.get("type")
        if kind == RING_CONNECTED_FRAME:
            await self.disconnected(gfs_id)
            self._known.pop(gfs_id, None)
            await self._send_watch(gfs_id, initial=True)
            return
        await self._load(gfs_id)
        try:
            if kind == "relay":
                await self._on_item(gfs_id, frame)
            elif kind == CHANNEL_HEADS_FRAME:
                await self._on_heads(gfs_id, frame)
            elif kind == CHANNEL_FETCH_DONE_FRAME:
                await self._on_done(gfs_id, frame)
            elif kind == CHANNEL_GAP_FRAME:
                await self._on_gap(gfs_id, frame)
        except (InvalidChannelWire, KeyError, TypeError, ValueError) as exc:
            log.debug("channel ring: malformed %s frame: %s", kind, exc)

    def _answers(self, key: tuple[str, str], req: object) -> bool:
        out = self._fetching.get(key)
        return out is not None and out[0] == req

    async def _on_item(self, gfs_id: str, frame: dict) -> None:
        pos = ring_position(frame)
        if pos is None:
            return
        seq, ring_id = pos
        channel_id = valid_channel_id(frame.get("channel_id"))
        spaces = await self._spaces_of(gfs_id, channel_id)
        if not spaces:
            return
        key = (gfs_id, channel_id)
        taken = frame.get(RING_TAKEN_FIELD) is not False
        cur = self.cursor(gfs_id, channel_id)
        if self._answers(key, frame.get("fetch")):
            if key in self._stalled:
                return
            if not taken:
                # Dropped here (over the inbound budget): the cursor stays
                # before it, the rest of this page moves nothing.
                self._stalled.add(key)
                return
            if cur is not None and cur[0] == ring_id and seq > cur[1]:
                # A page in ring order: a hole is an expired item (a missing
                # durable one is a gap answer instead).
                self._set_cursor(gfs_id, channel_id, ring_id, seq)
            return
        if not taken:
            return
        if cur is None:
            # A new seat starts here (owner F8).
            self._set_cursor(gfs_id, channel_id, ring_id, seq)
        elif cur[0] != ring_id:
            # The ring was reset under us: what we missed is unknown.
            await self._gap(gfs_id, channel_id, ring_id, head=seq, oldest=seq)
        elif seq == cur[1] + 1:
            self._set_cursor(gfs_id, channel_id, ring_id, seq)
        elif seq > cur[1] + 1:
            # A hole before a live item (another node raced it): pull it
            # only where someone looks.
            known = self._heads.get(key)
            top = max(seq, known[1]) if known and known[0] == ring_id else seq
            self._heads[key] = (ring_id, top)
            await self._behind(gfs_id, channel_id, spaces)

    async def _on_heads(self, gfs_id: str, frame: dict) -> None:
        heads = frame.get("heads")
        if not isinstance(heads, list):
            return
        seen: set[str] = set()
        for raw in heads[:MAX_WATCHED_CHANNELS]:
            head = ChannelHead.from_wire(raw)
            if head.channel_id in seen:
                continue
            seen.add(head.channel_id)
            await self._on_head(gfs_id, head)

    async def _on_head(self, gfs_id: str, head: ChannelHead) -> None:
        spaces = await self._spaces_of(gfs_id, head.channel_id)
        if not spaces:
            return
        key = (gfs_id, head.channel_id)
        cur = self.cursor(gfs_id, head.channel_id)
        self._heads[key] = (head.ring_id, head.head_seq)
        if cur is None:
            elsewhere = any(
                g != gfs_id and ch == head.channel_id for (g, ch) in self._cursors
            )
            start = (
                max(0, head.head_seq - MAX_BEHIND_ITEMS) if elsewhere else head.head_seq
            )
            # A new seat starts at the head (F8); on failover from another
            # GFS of the channel, the window back (item ids dedupe).
            self._set_cursor(gfs_id, head.channel_id, head.ring_id, start)
            if start < head.head_seq:
                await self._behind(gfs_id, head.channel_id, spaces)
            return
        if cur[0] != head.ring_id:
            await self._gap(gfs_id, head.channel_id, head.ring_id, head.head_seq, 0)
            return
        if head.head_seq > cur[1]:
            await self._behind(gfs_id, head.channel_id, spaces)

    async def _behind(self, gfs_id: str, channel_id: str, spaces: list[str]) -> None:
        """The ring holds items past our cursor: fetch where someone looks
        (too far behind → a gap), else the badge."""
        cur = self.cursor(gfs_id, channel_id)
        head = self._heads.get((gfs_id, channel_id))
        if cur is None or head is None:
            return
        if head[1] - cur[1] > MAX_BEHIND_ITEMS:
            await self._gap(gfs_id, channel_id, head[0], head[1], 0)
            return
        for space_id in spaces:
            if await self._eager(space_id):
                await self._fetch(gfs_id, channel_id)
                return
        for space_id in spaces:
            await self.report_activity(space_id, active=True)

    async def _on_done(self, gfs_id: str, frame: dict) -> None:
        channel_id = valid_channel_id(frame.get("channel_id"))
        key = (gfs_id, channel_id)
        if not self._answers(key, frame.get("req")):
            return
        self._fetching.pop(key, None)
        stalled = key in self._stalled
        self._stalled.discard(key)
        head = _seq(frame.get("head_seq"))
        ring_id = valid_ring_id(frame.get("ring_id"))
        cur = self.cursor(gfs_id, channel_id)
        if stalled or cur is None or cur[0] != ring_id:
            return
        self._heads[key] = (ring_id, head)
        if frame.get("more") is True:
            # The next page waits, so a backlog never outruns the inbound
            # budget; the loop sends it.
            self._next_page[key] = self._clock() + PAGE_INTERVAL_S
            return
        # Caught up: whatever is left above the cursor expired (an ephemeral
        # item), so the cursor moves to the head.
        if head > cur[1]:
            self._set_cursor(gfs_id, channel_id, ring_id, head)
        for space_id in await self._spaces_of(gfs_id, channel_id):
            await self.report_activity(space_id, active=False)

    async def _on_gap(self, gfs_id: str, frame: dict) -> None:
        channel_id = valid_channel_id(frame.get("channel_id"))
        key = (gfs_id, channel_id)
        if not self._answers(key, frame.get("req")):
            return
        self._fetching.pop(key, None)
        self._stalled.discard(key)
        head = _seq(frame.get("head_seq"))
        oldest = _seq(frame.get("oldest_seq", head))
        await self._gap(
            gfs_id, channel_id, valid_ring_id(frame.get("ring_id")), head, oldest
        )

    async def _gap(
        self, gfs_id: str, channel_id: str, ring_id: str, head: int, oldest: int
    ) -> None:
        """Skip to near the head, pull the recent window where someone
        looks, and let the §25.6 catch-up bring the rest."""
        start = max(oldest - 1, head - RECENT_AFTER_GAP, 0)
        self._set_cursor(gfs_id, channel_id, ring_id, start)
        self._heads[(gfs_id, channel_id)] = (ring_id, head)
        spaces = await self._spaces_of(gfs_id, channel_id)
        for space_id in spaces:
            if start < head and await self._eager(space_id):
                await self._fetch(gfs_id, channel_id)
                break
        for space_id in spaces:
            if not await self._owns(space_id):
                self._catch_up_due.add(space_id)
        await self._run_catch_ups()

    async def _run_catch_ups(self) -> None:
        """The deferred §25.6 catch-ups whose window is open."""
        if self._sync is None:
            return
        now = self._clock()
        for space_id in list(self._catch_up_due):
            last = self._caught_up_at.get(space_id)
            if last is not None and now - last < GAP_CATCH_UP_INTERVAL_S:
                continue
            self._catch_up_due.discard(space_id)
            self._caught_up_at[space_id] = now
            log.info("channel ring: gap on a space's channel — §25.6 catch-up")
            try:
                await self._sync.sync_now(space_id)
            except Exception:
                log.warning("channel ring: catch-up failed", exc_info=True)

    async def _fetch(self, gfs_id: str, channel_id: str) -> None:
        key = (gfs_id, channel_id)
        out = self._fetching.get(key)
        if out is not None and self._clock() - out[1] < FETCH_TIMEOUT_S:
            return
        cur = self.cursor(gfs_id, channel_id)
        if cur is None or self._send is None:
            return
        head = self._heads.get(key)
        if (
            head is not None
            and head[0] == cur[0]
            and head[1] - cur[1] > MAX_BEHIND_ITEMS
        ):
            await self._gap(gfs_id, channel_id, head[0], head[1], 0)
            return
        self._stalled.discard(key)
        self._next_page.pop(key, None)
        req = ChannelFetch(
            channel_id=channel_id,
            since_seq=cur[1],
            req=secrets.token_hex(8),
            ring_id=cur[0],
        )
        if await self._send(gfs_id, req.to_wire()):
            self._fetching[key] = (req.req, self._clock())

    def has_activity(self, space_id: str) -> bool:
        """Whether *space_id* wears the "New activity" badge right now —
        what ``GET /api/spaces`` carries as ``new_activity``, so a reloaded
        page shows it again (in memory: a restart badges again at the next
        heads ping above the persisted cursor)."""
        return space_id in self._active

    async def report_activity(self, space_id: str, *, active: bool) -> None:
        """The badge: on when something new waits, off once caught up —
        published only on a change, and "New activity" at most once per
        :data:`ACTIVITY_QUIET_S` per space."""
        if (space_id in self._active) == active:
            return
        if active:
            self._active.add(space_id)
            last = self._announced_at.get(space_id)
            if last is not None and self._clock() - last < ACTIVITY_QUIET_S:
                return
            self._announced_at[space_id] = self._clock()
        else:
            self._active.discard(space_id)
        await self._bus.publish(
            SpaceChannelActivity(space_id=space_id, unread=1 if active else 0)
        )

    # ── Watching ─────────────────────────────────────────────────────────

    async def _hosts(self, space_id: str) -> bool:
        """Whether this household acts as the space's host — its owner or a
        delegated admin household (:func:`serves_as_host`): the host always
        watches (owner F5) and fetches at each ping."""
        space = await self._spaces.get(space_id)
        return await serves_as_host(
            self._spaces, space, own_instance_id=self._own_instance_id
        )

    async def _owns(self, space_id: str) -> bool:
        """Whether this household OWNS the space: only the owner has no
        host above it to run a catch-up from."""
        space = await self._spaces.get(space_id)
        return space is not None and space.owner_instance_id == self._own_instance_id

    def _viewing(self, space_id: str) -> bool:
        at = self._viewed.get(space_id)
        return at is not None and self._clock() - at <= WATCH_LINGER_S

    async def _eager(self, space_id: str) -> bool:
        """Fetch at a ping (instead of only badging): the host, or a space a
        person has open."""
        return self._viewing(space_id) or await self._hosts(space_id)

    async def _desired(self) -> dict[str, set[str]]:
        """gfs id → the channels we should be watching there: the spaces a
        person has open (plus the linger) and the spaces we host."""
        out: dict[str, set[str]] = {}
        space_ids = {s for s in self._viewed if self._viewing(s)}
        space_ids |= {
            s for s in await self._spaces.list_channel_spaces() if await self._hosts(s)
        }
        for space_id in sorted(space_ids):
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
        wanted = sorted((await self._desired()).get(gfs_id, set()))
        want = set(wanted[:MAX_WATCHED_CHANNELS])
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
        self._viewed[space_id] = self._clock()
        found = await self._channels.ring_connections(space_id)
        if found is None:
            return
        channel_id, conns = found
        for conn in conns:
            await self._load(conn.id)
            await self._send_watch(conn.id)
            await self._fetch(conn.id, channel_id)

    async def disconnected(self, gfs_id: str) -> None:
        """A socket dropped: its watches and fetches went with it."""
        self._watched.pop(gfs_id, None)
        for key in [k for k in self._fetching if k[0] == gfs_id]:
            self._fetching.pop(key, None)
            self._stalled.discard(key)

    async def tick(self) -> None:
        """Next pages, linger expiry, watch retries, deferred catch-ups,
        the cursor flush."""
        now = self._clock()
        for key, at in list(self._next_page.items()):
            if now >= at:
                self._next_page.pop(key, None)
                await self._fetch(*key)
        for space_id in [s for s in self._viewed if not self._viewing(s)]:
            self._viewed.pop(space_id, None)
        for gfs_id in set(self._watched) | set(self._loaded):
            await self._send_watch(gfs_id)
        await self._run_catch_ups()
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
        try:
            await self.flush()
        except Exception:
            log.warning("channel ring: final cursor flush failed", exc_info=True)

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
