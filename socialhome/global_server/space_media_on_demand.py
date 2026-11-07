"""Space media on demand (v_83) — a blob that left the cache comes back.

The space media cache (:mod:`.space_media`) keeps a blob only for its TTL and
evicts the least recently fetched first; after that every reader used to
miss it forever. Now a reader that misses asks
(``POST /gfs/media/{blob_id}/wanted`` — the scope it knows and a locator
sealed for space key holders, :mod:`socialhome.space_media_hint`) and this
server asks ONE household that holds the file to send the same
deterministic ciphertext (:mod:`socialhome.space_media_seal`) to
``PUT /gfs/media/serve/{ticket}``. The bytes are piped to every reader
waiting for that blob on this node; they are kept in the cache only when this
server decided to (:data:`MODE_CACHE`) and the holder attached its normal
upload authorization.

**Cache or stream** — :meth:`GfsSpaceMediaOnDemand.decide`, thresholds in
``[space_media]`` (:class:`~.config.SpaceMediaOnDemandConfig`):

* asked ``cache_after_requests`` times within ``request_window_seconds`` →
  cache (popular content earns its room even in a full cache: the LRU
  then evicts something fetched less recently);
* stored here at most ``fresh_seconds`` ago, while the cache is below
  ``full_percent`` → cache;
* anything else — old or never-seen content, a one-off view, a cache that
  full — streams, and nothing is stored.

**Who is asked.** For a listed space, its host — the owning household this
server already knows (its socket must have listed ``space_media_serve``).
For a private space's channel, the seated member households (key holders
this server already knows as seats), those connected to this node first.
At most :data:`MAX_HOLDERS_ASKED` per request, one after another, each
given :data:`HOLDER_START_TIMEOUT_S` to start (a decline moves on at once),
all within ``stream_wait_seconds``. Other member households of a listed
space are NOT asked: this server does not know them, and asking would tell
it who they are (the GFS-must-not-see-data rule).

**Coalescing.** One live session per (blob, scope) per node: every reader
of that blob joins it, and a session that delivered stays joinable in memory
for ``coalesce_seconds`` (a reader arriving then gets the bytes without a new
holder request). After a session nobody served, a repeat on this node within
``coalesce_seconds`` asks no holder (a clean miss). A re-upload is claimed
cluster-wide in the shared database (one per blob per window); a node that
loses the claim waits for the blob to land in the shared cache. A stream is
per node, so a blob read on every node at once costs at most one stream per
node per window.

**Bounds.** Per node: ``max_streams`` sessions, ``scope_max_streams`` per
scope; each holds at most one blob in memory (≤ 8 MiB) and never on disk.
Cluster-wide (shared DB, the keyed hourly budget of the cache): a scope
streams at most ``scope_stream_bytes_per_hour``; one reader network prefix
triggers at most ``prefix_wanted_per_hour`` holder requests. A reader waits
at most ``stream_wait_seconds`` (then 404, so its own miss cache applies).
Waiting readers per client / prefix / node are bounded by
:attr:`GfsSpaceMediaOnDemand.wait_gate` (the cache's :class:`BodyReadGate`).

**What anyone learns.** The holder's frame names the blob, its scope, the
reader's sealed hint and a ticket — no reader. Readers never learn which
household answered. The bytes are ciphertext; the last chunk is released
only once the whole blob sits at the address the reader asked for.
"""

from __future__ import annotations

import asyncio
import logging
import random
import time
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from aiohttp import web

from ..domain.gfs_space_media import SCOPE_CHANNEL, SCOPE_SPACE, space_media_address
from ..domain.gfs_space_media_on_demand import (
    MODE_CACHE,
    MODE_STREAM,
    SPACE_MEDIA_REQUEST_FRAME,
    MediaRequestFrame,
    WantedRequest,
)
from ..moment_public_media import is_blob_id
from ..space_media_hint import SpaceMediaHint
from .node_route import scoped_id
from .public import ClientIpResolver, SlidingWindowCounter, node_share
from .space_media import BodyReadGate

if TYPE_CHECKING:
    from .channels import GfsChannelService
    from .cluster_relay import ClusterRelayFanOut
    from .config import SpaceMediaOnDemandConfig
    from .federation import GfsFederationService
    from .repositories import AbstractGfsSpaceMediaDemandRepo
    from .space_media import GfsSpaceMediaStore
    from .ws_registry import GfsWebSocketRegistry

log = logging.getLogger(__name__)

#: Holders asked per request at most (one after another).
MAX_HOLDERS_ASKED: int = 3

#: How long a holder that got the request has to start sending.
HOLDER_START_TIMEOUT_S: float = 10.0

#: How often a node that lost the re-upload claim looks for the blob.
CLAIM_POLL_S: float = 0.5

#: Waiting readers per client address bucket, per network prefix, per node.
WAIT_PER_CLIENT: int = 4
WAIT_PER_PREFIX: int = 8
WAIT_PER_NODE: int = 512

#: ``wanted`` requests per client address per minute, shed before any work
#: (cluster-wide: each node allows its share).
WANTED_MAX_PER_MINUTE_PER_IP: int = 60

#: Distinct reader hints a session keeps to retry with when the holder
#: finds one unusable (a reader without the key can't make a blob dark).
MAX_HINTS_PER_SESSION: int = 4

#: One reader prefix may have this share of a scope's hourly stream bytes
#: (``scope_stream_bytes_per_hour // PREFIX_STREAM_SHARE``).
PREFIX_STREAM_SHARE: int = 8

#: A reader is cut off when its copy is not written within the time left
#: plus the blob at this rate (a reader that stops reading can't pin it).
MIN_READER_BYTES_PER_S: int = 64 * 1024

#: Why a holder declined (``POST …/decline`` body ``{"reason": …}``).
DECLINE_BAD_HINT = "bad_hint"
DECLINE_NOT_HELD = "not_held"

#: The keyed hourly budgets' labels (the cache's ``ip_bytes`` table).
_STREAM_BUDGET = "od-stream:"
_PREFIX_STREAM_BUDGET = "od-prefix-stream:"
_WANTED_BUDGET = "od-wanted:"

#: Outcomes of :meth:`GfsSpaceMediaOnDemand.want` that are not a session.
MISS = "miss"
BUSY = "busy"
LIMITED = "limited"


class StreamFailed(Exception):
    """The holder's bytes did not arrive whole and at their address."""


@dataclass(slots=True)
class MediaSession:
    """One holder request in flight on this node — a byte pipe every reader
    of the blob joins. In memory only: the bytes live as long as the
    session (≤ one blob), never on disk unless stored by the cache."""

    ticket: str
    blob_id: str
    scope_kind: str
    scope_id: str
    mode: str
    deadline: float
    #: The reader hints to try, the opener's first (others joined later).
    hints: list[SpaceMediaHint] = field(default_factory=list)
    #: The network prefix (keyed hash) of the reader that opened it.
    opener_prefix: str = ""
    #: Every refusal so far was an unusable hint (nobody said "not held"):
    #: the blob is not marked missing on this node.
    only_bad_hints: bool = True
    decline_reason: str = ""
    #: The household asked right now; only it may serve or decline.
    target: str = ""
    size: int = 0
    buf: bytearray = field(default_factory=bytearray)
    claimed: bool = False
    verified: bool = False
    failed: bool = False
    readers: int = 0
    started: asyncio.Event = field(default_factory=asyncio.Event)
    declined: asyncio.Event = field(default_factory=asyncio.Event)
    done: asyncio.Event = field(default_factory=asyncio.Event)
    _progress: asyncio.Event = field(default_factory=asyncio.Event)

    @property
    def key(self) -> tuple[str, str, str]:
        return (self.blob_id, self.scope_kind, self.scope_id)

    def visible(self) -> int:
        """Bytes readers may have now: all of them once verified, else all
        but the last — so no reader ever holds a whole blob that is not at
        its address."""
        if self.verified:
            return len(self.buf)
        return max(0, min(len(self.buf), self.size - 1))

    def feed(self, chunk: bytes) -> None:
        self.buf += chunk
        self._tick()

    def finish(self, *, ok: bool) -> None:
        if ok:
            self.verified = True
        else:
            self.failed = True
        self.started.set()
        self.done.set()
        self._tick()

    def _tick(self) -> None:
        self._progress.set()
        self._progress = asyncio.Event()

    async def chunks(self) -> AsyncIterator[bytes]:
        """The blob's bytes as they arrive; :class:`StreamFailed` when it
        ends short or at another address."""
        sent = 0
        while True:
            progress = self._progress
            visible = self.visible()
            if visible > sent:
                yield bytes(self.buf[sent:visible])
                sent = visible
                continue
            if self.verified and sent >= self.size:
                return
            if self.failed:
                raise StreamFailed
            await progress.wait()


@dataclass(slots=True, frozen=True)
class Cached:
    """The blob was (or just got) in the cache."""

    data: bytes


class _Counters:
    __slots__ = (
        "declined",
        "holder_requests",
        "misses",
        "reuploads",
        "streamed_bytes",
        "streams",
    )

    def __init__(self) -> None:
        self.holder_requests = 0
        self.streams = 0
        self.streamed_bytes = 0
        self.reuploads = 0
        self.declined = 0
        self.misses = 0


class GfsSpaceMediaOnDemand:
    """Answer ``wanted`` requests: serve from the cache, join or open a
    holder session, or miss cleanly."""

    __slots__ = (
        "_by_key",
        "_cfg",
        "_channels",
        "_clock",
        "_counters",
        "_demand",
        "_drivers",
        "_fan_out",
        "_federation",
        "_pending",
        "_pinned",
        "_recent",
        "_route_tag",
        "_sessions",
        "_store",
        "_ws",
        "wait_gate",
    )

    def __init__(
        self,
        *,
        store: GfsSpaceMediaStore,
        demand: AbstractGfsSpaceMediaDemandRepo,
        federation: GfsFederationService,
        channels: GfsChannelService,
        config: SpaceMediaOnDemandConfig,
        fan_out: ClusterRelayFanOut | None = None,
        ws_registry: GfsWebSocketRegistry | None = None,
        route_tag: str = "",
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._store = store
        self._demand = demand
        self._federation = federation
        self._channels = channels
        self._cfg = config
        self._fan_out = fan_out
        #: Which holders hold a capable socket on THIS node (asked first).
        self._ws = ws_registry
        self._route_tag = route_tag
        self._clock = clock
        self._counters = _Counters()
        self._sessions: dict[str, MediaSession] = {}
        self._by_key: dict[tuple[str, str, str], str] = {}
        #: (blob, scope) → monotonic time until which a repeat does nothing.
        self._recent: dict[tuple[str, str, str], float] = {}
        self._drivers: set[asyncio.Task] = set()
        #: (blob, scope) → the decision being made for its first reader;
        #: readers arriving meanwhile wait on it instead of deciding again.
        self._pending: dict[tuple[str, str, str], asyncio.Future] = {}
        #: Sessions a reader is still being written from (they hold memory
        #: after the session closed): counted in the room check.
        self._pinned: set[str] = set()
        #: Readers waiting at once (per client, per prefix, per node).
        self.wait_gate = BodyReadGate(
            per_client=WAIT_PER_CLIENT,
            per_prefix=WAIT_PER_PREFIX,
            max_bodies=WAIT_PER_NODE,
            max_bytes=1,
        )

    def attach_fan_out(self, fan_out: ClusterRelayFanOut) -> None:
        self._fan_out = fan_out

    @property
    def enabled(self) -> bool:
        return self._cfg.enabled and self._store.limits.enabled

    def capability(self) -> dict[str, int] | None:
        """The ``on_demand`` entry inside the signed ``space_media``
        capability, or ``None`` when off."""
        if not self.enabled:
            return None
        return {"wait_s": self._cfg.stream_wait_seconds}

    # ── Readers ──────────────────────────────────────────────────────────

    async def want(
        self, blob_id: str, req: WantedRequest, *, prefix_key: str
    ) -> Cached | MediaSession | str:
        """One reader's request for *blob_id*: :class:`Cached` bytes, a
        :class:`MediaSession` to read from, or :data:`MISS` /
        :data:`BUSY` / :data:`LIMITED`."""
        if not self.enabled or not is_blob_id(blob_id):
            return MISS
        held = await self._store.read(blob_id)
        if held is not None:
            return Cached(held)
        key = (blob_id, req.scope, req.scope_id)
        live = self._live(key)
        if live is not None:
            self._add_hint(live, req.hint)
            return live
        pending = self._pending.get(key)
        if pending is not None:
            # Another reader of this blob is being decided right now: share
            # its outcome (never its own prefix's limit).
            shared = await asyncio.shield(pending)
            if isinstance(shared, MediaSession):
                self._add_hint(shared, req.hint)
                return shared
            return shared if isinstance(shared, Cached) else MISS
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending[key] = fut
        outcome: Cached | MediaSession | str = MISS
        try:
            outcome = await self._decide_and_open(blob_id, key, req, prefix_key)
        finally:
            self._pending.pop(key, None)
            fut.set_result(outcome)
        return outcome

    async def _decide_and_open(
        self,
        blob_id: str,
        key: tuple[str, str, str],
        req: WantedRequest,
        prefix_key: str,
    ) -> Cached | MediaSession | str:
        """The first reader's path: count it, decide, claim, open."""
        if not await self._scope_active(req.scope, req.scope_id):
            return MISS
        if not await self._store.reserve_hourly(
            _WANTED_BUDGET + prefix_key, 1, self._cfg.prefix_wanted_per_hour
        ):
            return LIMITED
        now = int(time.time())
        demand = await self._demand.note_request(
            blob_id,
            req.scope,
            req.scope_id,
            now=now,
            window_s=self._cfg.request_window_seconds,
        )
        now_m = self._clock()
        self._gc_recent(now_m)
        if self._recent.get(key, 0.0) > now_m:
            # Nobody served it a moment ago: counted, nothing asked.
            self._counters.misses += 1
            return MISS
        mode = await self.decide(demand.requests, demand.stored_at, now=now)
        if not self._room_for(req.scope, req.scope_id):
            return BUSY
        if mode == MODE_CACHE and not await self._demand.claim(
            blob_id,
            req.scope,
            req.scope_id,
            now=now,
            hold_s=self._cfg.coalesce_seconds,
        ):
            # Another node (or an earlier request) holds the one re-upload:
            # wait for it to land in the shared cache.
            return await self._await_stored(blob_id)
        session = self._open(blob_id, req, mode)
        session.opener_prefix = prefix_key
        task = asyncio.create_task(self._drive(session))
        self._drivers.add(task)
        task.add_done_callback(self._drivers.discard)
        return session

    async def decide(self, requests: int, stored_at: int | None, *, now: int) -> str:
        """Cache or stream (module docstring)."""
        cfg = self._cfg
        if requests >= cfg.cache_after_requests:
            return MODE_CACHE
        if stored_at is not None and now - stored_at <= cfg.fresh_seconds:
            _, total = await self._store.totals()
            if total * 100 < self._store.limits.max_bytes * cfg.full_percent:
                return MODE_CACHE
        return MODE_STREAM

    def session(self, ticket: str) -> MediaSession | None:
        return self._sessions.get(ticket)

    def remaining(self, session: MediaSession) -> float:
        return max(0.0, session.deadline - self._clock())

    # ── Holders ──────────────────────────────────────────────────────────

    async def charge_stream(self, session: MediaSession, size: int) -> bool:
        """Charge *size* bytes to the scope's hourly stream budget and to the
        opening reader prefix's share of it (one prefix can't use up a
        scope's budget for everyone)."""
        share = max(1, self._cfg.scope_stream_bytes_per_hour // PREFIX_STREAM_SHARE)
        if not await self._store.reserve_hourly(
            f"{_PREFIX_STREAM_BUDGET}{session.opener_prefix}", size, share
        ):
            return False
        return await self._store.reserve_hourly(
            f"{_STREAM_BUDGET}{session.scope_kind}:{session.scope_id}",
            size,
            self._cfg.scope_stream_bytes_per_hour,
        )

    def decline(self, session: MediaSession, reason: str) -> None:
        """The asked holder can't serve: :data:`DECLINE_BAD_HINT` (the hint
        did not open, or named nothing at this address) or anything else
        (it doesn't hold the file, its budget)."""
        session.decline_reason = reason
        session.declined.set()

    def reader_deadline(self, session: MediaSession) -> float:
        """Seconds a reader may take to receive *session*'s blob."""
        return self.remaining(session) + 10.0 + session.size / MIN_READER_BYTES_PER_S

    def pin(self, session: MediaSession) -> None:
        self._pinned.add(session.ticket)

    def unpin(self, session: MediaSession) -> None:
        if session.readers <= 0:
            self._pinned.discard(session.ticket)

    async def verify(self, session: MediaSession) -> bool:
        """Whether the whole body sits at the address the readers asked for."""
        if len(session.buf) != session.size:
            return False
        try:
            address = await asyncio.to_thread(
                space_media_address,
                session.scope_kind,
                session.scope_id,
                bytes(session.buf),
            )
        except ValueError:
            return False
        return address == session.blob_id

    def note_served(self, session: MediaSession, *, stored: bool) -> None:
        self._counters.streams += 1
        self._counters.streamed_bytes += session.size
        if stored:
            self._counters.reuploads += 1

    async def stats(self) -> dict[str, int | bool]:
        c = self._counters
        return {
            "on_demand": self.enabled,
            "holder_requests": c.holder_requests,
            "streams_served": c.streams,
            "streamed_bytes": c.streamed_bytes,
            "reuploads": c.reuploads,
            "declined": c.declined,
            "on_demand_misses": c.misses,
            "live_sessions": len(self._sessions),
        }

    async def close(self) -> None:
        for session in list(self._sessions.values()):
            session.finish(ok=False)
        for task in list(self._drivers):
            task.cancel()
        if self._drivers:
            await asyncio.gather(*self._drivers, return_exceptions=True)

    # ── Internals ────────────────────────────────────────────────────────

    def _live(self, key: tuple[str, str, str]) -> MediaSession | None:
        ticket = self._by_key.get(key)
        session = self._sessions.get(ticket) if ticket else None
        if session is None or session.failed:
            return None
        return session

    def _gc_recent(self, now: float) -> None:
        if len(self._recent) > 4096:
            self._recent = {k: t for k, t in self._recent.items() if t > now}

    def _room_for(self, scope_kind: str, scope_id: str) -> bool:
        """Room for one more session: live ones and closed ones a reader is
        still being written from count alike — a delivered session merely
        lingering (nobody reading it) gives way first."""
        if not self._has_room(scope_kind, scope_id):
            for session in list(self._sessions.values()):
                if session.verified and session.readers <= 0:
                    self._close(session)
        return self._has_room(scope_kind, scope_id)

    def _has_room(self, scope_kind: str, scope_id: str) -> bool:
        if len(self._sessions.keys() | self._pinned) >= self._cfg.max_streams:
            return False
        same = sum(
            1
            for s in self._sessions.values()
            if (s.scope_kind, s.scope_id) == (scope_kind, scope_id)
        )
        return same < self._cfg.scope_max_streams

    @staticmethod
    def _add_hint(session: MediaSession, hint: SpaceMediaHint) -> None:
        if len(session.hints) < MAX_HINTS_PER_SESSION and all(
            h.ct != hint.ct for h in session.hints
        ):
            session.hints.append(hint)

    def _open(self, blob_id: str, req: WantedRequest, mode: str) -> MediaSession:
        session = MediaSession(
            ticket=scoped_id(self._route_tag),
            blob_id=blob_id,
            scope_kind=req.scope,
            scope_id=req.scope_id,
            mode=mode,
            deadline=self._clock() + self._cfg.stream_wait_seconds,
            hints=[req.hint],
        )
        self._sessions[session.ticket] = session
        self._by_key[session.key] = session.ticket
        return session

    def _close(self, session: MediaSession) -> None:
        self._sessions.pop(session.ticket, None)
        if self._by_key.get(session.key) == session.ticket:
            self._by_key.pop(session.key, None)
        if not session.verified and not session.only_bad_hints:
            # A holder answered "not held" (or nobody answered): this node
            # asks nobody for it for a while.
            self._recent[session.key] = self._clock() + self._cfg.coalesce_seconds

    async def _scope_active(self, scope_kind: str, scope_id: str) -> bool:
        try:
            if scope_kind == SCOPE_SPACE:
                space = await self._federation.listed_space(scope_id)
                return space.status == "active"
            if scope_kind == SCOPE_CHANNEL:
                await self._channels.require_channel_in_use(scope_id)
                return True
        except PermissionError:
            return False
        return False

    async def _holders(self, scope_kind: str, scope_id: str) -> list[str]:
        """Who may be asked, in order (module docstring)."""
        if scope_kind == SCOPE_SPACE:
            try:
                space = await self._federation.listed_space(scope_id)
            except PermissionError:
                return []
            return [space.owning_instance] if space.owning_instance else []
        seats = list(await self._channels.audience_targets(scope_id))
        random.shuffle(seats)
        ws = self._ws
        if ws is not None:
            seats.sort(key=lambda iid: not ws.takes(iid, SPACE_MEDIA_REQUEST_FRAME))
        return seats[:MAX_HOLDERS_ASKED]

    async def _drive(self, session: MediaSession) -> None:
        """Ask the holders one after another until one serves, within the
        session's deadline; a session nobody serves fails (a clean miss)."""
        try:
            await self._ask_holders(session)
            if not session.claimed:
                self._counters.misses += 1
                session.finish(ok=False)
                if session.mode == MODE_CACHE and session.only_bad_hints:
                    # Only junk hints: the one re-upload is still to come.
                    await self._demand.release_claim(*session.key)
            await session.done.wait()
            if session.verified:
                # Joinable a little longer: a reader arriving now gets the
                # bytes without asking a holder again.
                await asyncio.sleep(self._cfg.coalesce_seconds)
        except asyncio.CancelledError:
            if not session.done.is_set():
                session.finish(ok=False)
            raise
        except Exception:  # pragma: no cover - defensive
            log.warning("space media on demand: session failed", exc_info=True)
            session.finish(ok=False)
        finally:
            self._close(session)

    async def _ask_holders(self, session: MediaSession) -> None:
        """Each holder in turn, each with the session's hints in turn (a
        hint the holder can't use is no reason to give up on the blob)."""
        for target in await self._holders(session.scope_kind, session.scope_id):
            tried = 0
            while tried < len(session.hints):
                if session.claimed or self.remaining(session) <= 0:
                    return
                hint = session.hints[tried]
                tried += 1
                if await self._ask(session, hint, target):
                    return
                if session.decline_reason != DECLINE_BAD_HINT:
                    session.only_bad_hints = False
                    break

    async def _ask(
        self, session: MediaSession, hint: SpaceMediaHint, target: str
    ) -> bool:
        """Ask *target* with *hint*; ``True`` once it started sending."""
        if self._fan_out is None:
            return False
        session.target = target
        session.declined.clear()
        session.decline_reason = ""
        frame = MediaRequestFrame(
            ticket=session.ticket,
            scope=session.scope_kind,
            scope_id=session.scope_id,
            blob_id=session.blob_id,
            mode=session.mode,
            hint=hint,
        )
        self._counters.holder_requests += 1
        delivered = await self._fan_out.deliver_to_household(
            target, frame.to_push(), timeout=min(8.0, self.remaining(session))
        )
        if not delivered:
            session.decline_reason = DECLINE_NOT_HELD
            return False
        waits = [
            asyncio.ensure_future(session.started.wait()),
            asyncio.ensure_future(session.declined.wait()),
        ]
        try:
            await asyncio.wait(
                waits,
                timeout=min(HOLDER_START_TIMEOUT_S, self.remaining(session)),
                return_when=asyncio.FIRST_COMPLETED,
            )
        finally:
            for w in waits:
                w.cancel()
        if session.claimed:
            return True
        if session.declined.is_set():
            self._counters.declined += 1
        else:
            session.decline_reason = DECLINE_NOT_HELD  # no answer in time
        # Late bytes from this household are refused from now on.
        session.target = ""
        return False

    async def _await_stored(self, blob_id: str) -> Cached | str:
        deadline = self._clock() + self._cfg.stream_wait_seconds
        while self._clock() < deadline:
            await asyncio.sleep(CLAIM_POLL_S)
            data = await self._store.read(blob_id)
            if data is not None:
                return Cached(data)
        self._counters.misses += 1
        return MISS


def build_space_media_wanted_rate_limit(
    resolver: ClientIpResolver, *, nodes: Callable[[], int] | None = None
):
    """Per-IP limiter for ``POST /gfs/media/{blob_id}/wanted`` (matched on
    the route it resolved to), shed before any work; cluster-wide with
    *nodes* (each node allows its share)."""
    counter = SlidingWindowCounter(WANTED_MAX_PER_MINUTE_PER_IP)

    @web.middleware
    async def _rate_limit(request: web.Request, handler):
        route = request.match_info.route
        resource = route.resource if route is not None else None
        canonical = resource.canonical if resource is not None else None
        if request.method != "POST" or canonical != SPACE_MEDIA_WANTED_ROUTE_CANONICAL:
            return await handler(request)
        limit = WANTED_MAX_PER_MINUTE_PER_IP
        share = limit if nodes is None else node_share(limit, nodes())
        if not counter.allow(resolver.bucket(request), limit=share):
            resp = web.json_response({"error": "rate_limited"}, status=429)
            resp.headers["Retry-After"] = "60"
            resp.headers["Cache-Control"] = "no-store"
            return resp
        return await handler(request)

    return _rate_limit


#: The reader route as aiohttp names it (for the limiter).
SPACE_MEDIA_WANTED_ROUTE_CANONICAL: str = "/gfs/media/{blob_id}/wanted"
