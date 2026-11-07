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
space are never asked by name: this server does not know them, and asking
would tell it who they are (the GFS-must-not-see-data rule).

**Member fallback** (listed spaces, ``member_fallback``). When the space has
a writer group key pinned here for an open epoch, the owner gets
:data:`MEMBER_FALLBACK_OWNER_WINDOW_S` to start; if it is connected nowhere,
does not start in time or declines :data:`DECLINE_BUSY`, the want is posted
to the shared ``gfs_space_media_member_wants`` table (a session's
:data:`MEMBER_TARGET` phase). The space's members long-poll
``POST /gfs/media/member-pull`` signed by the writer GROUP key — anonymous,
the same proof as ``/gfs/member-publish-anon`` — and one of them claims the
want atomically (:data:`MEMBER_START_TIMEOUT_S`, then re-offered), serving
or declining the ticket with a writer-key signature; at most
:data:`MAX_MEMBER_ATTEMPTS` declines. An owner ``not_held`` is
authoritative: members are not asked. Nothing about a puller is stored.
Pulls wait on a per-scope event, woken at once by a want posted on this node
and, for wants another node posted, by ONE ticker per node that looks at the
open scopes once a second while anyone waits. Bounded per scope per node
(``member_pullers_per_scope``), per node (``member_pullers_max``) and per
network prefix (:data:`MAX_PULLS_PER_PREFIX`); every refusal is one uniform
answer that costs the verifications a success does.

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
import math
import random
import time
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

import orjson
from aiohttp import web

from ..crypto import counting_ed25519_verifies
from ..domain.gfs_space_media import SCOPE_CHANNEL, SCOPE_SPACE, space_media_address
from ..domain.gfs_space_media_on_demand import (
    DECLINE_BAD_HINT,
    DECLINE_BUSY,
    MAX_MEMBER_WANT_HINTS,
    MEMBER_PULL_CAPABILITY_KEY,
    MEMBER_PULL_ROUTE,
    MODE_CACHE,
    MODE_STREAM,
    SPACE_MEDIA_REQUEST_FRAME,
    InvalidOnDemandWire,
    MediaRequestFrame,
    MemberPullRequest,
    MemberWant,
    WantedRequest,
    member_serve_signing_bytes,
)
from ..moment_public_media import is_blob_id
from ..space_media_hint import SpaceMediaHint
from .domain import GfsSpaceMediaMemberWant
from .federation import SeenPayloadCache
from .node_route import scoped_id
from .public import ClientIpResolver, SlidingWindowCounter, node_share
from .space_media import BodyReadGate
from .space_media_auth import burn_verifies

if TYPE_CHECKING:
    from .addressee import GfsAddressee
    from .channels import GfsChannelService
    from .cluster_relay import ClusterRelayFanOut
    from .config import SpaceMediaOnDemandConfig
    from .federation import GfsFederationService
    from .member_publish import GfsMemberPublishService
    from .repositories import (
        AbstractGfsSpaceMediaDemandRepo,
        AbstractGfsSpaceMediaMemberWantRepo,
    )
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

#: The longest a holder may take to send a whole blob once it started.
SERVE_BODY_DEADLINE_S: float = 120.0

#: Sessions one reader network prefix may hold open by reading slowly
#: (each pinned until its copy is written).
MAX_PINNED_PER_PREFIX: int = 2

#: A session's own note that the asked holder did not answer (connected
#: nowhere, or silent past its window) — never on the wire.
_UNANSWERED = "unanswered"

#: With the member fallback, how long the owner has to start before the
#: want goes to the members.
MEMBER_FALLBACK_OWNER_WINDOW_S: float = 5.0

#: How long a member's claim on a want lasts before it is offered again
#: (it must have started sending by then).
MEMBER_START_TIMEOUT_S: int = 10

#: Member declines a session takes before it gives up.
MAX_MEMBER_ATTEMPTS: int = 3

#: Pulls one network prefix may hold open at once on a node.
MAX_PULLS_PER_PREFIX: int = 4

#: Member pulls per client address per minute, shed before any work
#: (cluster-wide: each node allows its share).
MEMBER_PULL_MAX_PER_MINUTE_PER_IP: int = 12

#: How often the node's ticker looks for wants other nodes posted.
MEMBER_TICK_S: float = 1.0

#: How far a member's ``ts`` may be from this server's clock.
MEMBER_TS_SKEW_S: int = 300

#: A session's target while the want is with the members — never an
#: instance id (ids carry no ``*``), so no household-signed serve matches.
MEMBER_TARGET: str = "*member"

#: Outcomes of :meth:`GfsSpaceMediaOnDemand.member_pull` that are not a want.
PULL_NONE = "none"
PULL_FULL = "full"
PULL_FORBIDDEN = "forbidden"

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
    #: The reader hints to try, the opener's first (others joined later) —
    #: one per reader prefix (``hint_prefixes``), so one prefix's junk
    #: can't crowd out another reader's hint.
    hints: list[SpaceMediaHint] = field(default_factory=list)
    hint_prefixes: list[str] = field(default_factory=list)
    #: Until when this node holds the blob's re-upload claim (0: none).
    claimed_until: int = 0
    #: The cache kept the holder's bytes (a re-upload that landed).
    stored: bool = False
    #: The network prefix (keyed hash) of the reader that opened it.
    opener_prefix: str = ""
    #: Every refusal so far was an unusable hint (nobody said "not held"):
    #: the blob is not marked missing on this node.
    only_bad_hints: bool = True
    decline_reason: str = ""
    #: The household asked right now; only it may serve or decline
    #: (:data:`MEMBER_TARGET` while the want is with the members).
    target: str = ""
    #: The want went to the members (the member fallback).
    member: bool = False
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
        "member_serves",
        "member_wants",
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
        self.member_wants = 0
        self.member_serves = 0


class GfsSpaceMediaOnDemand:
    """Answer ``wanted`` requests: serve from the cache, join or open a
    holder session, or miss cleanly."""

    __slots__ = (
        "_addressee",
        "_by_key",
        "_cfg",
        "_channels",
        "_clock",
        "_counters",
        "_demand",
        "_drivers",
        "_fan_out",
        "_federation",
        "_member_publish",
        "_member_wants",
        "_pending",
        "_pinned",
        "_polls",
        "_prefix_pulls",
        "_pull_events",
        "_pull_seen",
        "_pullers",
        "_pullers_total",
        "_recent",
        "_route_tag",
        "_sessions",
        "_stop",
        "_store",
        "_ticker",
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
        member_wants: AbstractGfsSpaceMediaMemberWantRepo | None = None,
        member_publish: GfsMemberPublishService | None = None,
        addressee: GfsAddressee | None = None,
    ) -> None:
        #: The member fallback (all three, or it is off): the shared wants,
        #: the writer-key checks, and this server's addressee id.
        self._member_wants = member_wants
        self._member_publish = member_publish
        self._addressee = addressee
        #: Pulls waiting per scope / per network prefix / in all (this node).
        self._pullers: dict[tuple[str, str], int] = {}
        self._prefix_pulls: dict[str, int] = {}
        self._pullers_total = 0
        #: scope → the event its waiting pulls wake on (replaced per wake).
        self._pull_events: dict[tuple[str, str], asyncio.Event] = {}
        #: Pulls already taken (their signed body's digest), for the skew
        #: window on both sides.
        self._pull_seen = SeenPayloadCache(ttl_s=float(2 * MEMBER_TS_SKEW_S))
        self._stop = asyncio.Event()
        self._ticker: asyncio.Task | None = None
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
        self._pinned: dict[tuple[str, str], int] = {}
        #: blob → the one poll of the shared cache readers of a re-upload
        #: another node claimed are waiting on.
        self._polls: dict[str, asyncio.Future] = {}
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

    @property
    def member_fallback(self) -> bool:
        """Whether wants of a listed space may go to its members."""
        return (
            self.enabled
            and self._cfg.member_fallback
            and self._member_wants is not None
            and self._member_publish is not None
            and self._addressee is not None
        )

    def capability(self) -> dict[str, object] | None:
        """The ``on_demand`` entry inside the signed ``space_media``
        capability, or ``None`` when off. ``member_pull`` is present iff
        the member fallback is on."""
        if not self.enabled:
            return None
        entry: dict[str, object] = {"wait_s": self._cfg.stream_wait_seconds}
        if self.member_fallback:
            entry[MEMBER_PULL_CAPABILITY_KEY] = {
                "hold_s": self._cfg.member_pull_hold_seconds
            }
        return entry

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
            self._add_hint(live, req.hint, prefix_key)
            return live
        pending = self._pending.get(key)
        if pending is not None:
            # Another reader of this blob is being decided right now: share
            # its outcome (never its own prefix's limit).
            shared = await asyncio.shield(pending)
            if isinstance(shared, MediaSession):
                self._add_hint(shared, req.hint, prefix_key)
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
        if not await self._stream_budget_left(req.scope, req.scope_id, prefix_key):
            # Checked BEFORE any holder is asked: a reader past its share
            # (or a scope past its budget) never makes a holder seal and send
            # bytes that would be refused.
            return LIMITED
        claimed_until = now + self._cfg.coalesce_seconds
        if mode == MODE_CACHE and not await self._demand.claim(
            blob_id,
            req.scope,
            req.scope_id,
            now=now,
            hold_s=self._cfg.coalesce_seconds,
        ):
            # Another node (or an earlier request) holds the one re-upload:
            # wait for it to land in the shared cache.
            return await self._await_stored(key)
        session = self._open(blob_id, req, mode)
        session.opener_prefix = prefix_key
        session.hint_prefixes = [prefix_key]
        if mode == MODE_CACHE:
            session.claimed_until = claimed_until
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
        did not open, or named nothing at this address),
        :data:`DECLINE_BUSY` (over its budget — the members may be asked)
        or anything else (it doesn't hold the file)."""
        session.decline_reason = reason
        session.declined.set()

    async def member_decline(self, session: MediaSession, reason: str) -> None:
        """The member that claimed *session*'s want can't serve: the want is
        offered again (one attempt more) and the session counts it."""
        if self._member_wants is not None:
            await self._member_wants.release(session.ticket)
        self.decline(session, reason)

    async def authorize_member(
        self,
        session: MediaSession,
        *,
        op: str,
        epoch: str,
        ts: str,
        sig: str,
        suite: str,
    ) -> bool:
        """Whether a writer-key holder may serve or decline *session* now:
        the session is with the members, the signature (writer group key of
        *epoch*, pinned here, epoch open) binds the ticket, *op*, the scope
        and a fresh *ts*, and the want's claim is live in the shared table
        (only a puller that claimed it can feed it). Every refusal costs the
        one verification a success makes."""
        with counting_ed25519_verifies() as done:
            try:
                await self._check_member(
                    session, op=op, epoch=epoch, ts=ts, sig=sig, suite=suite
                )
                return True
            except PermissionError as exc:
                log.debug("space media member serve refused: %s", exc)
                burn_verifies(1 - done[0])
                return False

    async def _check_member(
        self,
        session: MediaSession,
        *,
        op: str,
        epoch: str,
        ts: str,
        sig: str,
        suite: str,
    ) -> None:
        if (
            self._member_publish is None
            or self._member_wants is None
            or not session.member
            or session.target != MEMBER_TARGET
        ):
            raise PermissionError("not a member-phase session")
        try:
            epoch_int = int(epoch)
            ts_int = int(ts)
        except ValueError as exc:
            raise PermissionError("malformed epoch or ts") from exc
        if abs(ts_int - time.time()) > MEMBER_TS_SKEW_S:
            raise PermissionError("stale or future ts")
        await self._member_publish.verify_writer_key_sig(
            session.scope_id,
            epoch=epoch_int,
            message=member_serve_signing_bytes(
                session.ticket,
                op,
                session.scope_kind,
                session.scope_id,
                epoch_int,
                ts_int,
            ),
            writer_sig=sig,
            suite=suite,
        )
        await self._member_publish.require_writer_key_epoch(session.scope_id, epoch_int)
        if not await self._member_wants.claimed(session.ticket, now=int(time.time())):
            raise PermissionError("no live claim on this want")

    def cached_deadline(self, size: int) -> float:
        """Seconds a reader may take to receive a cached blob of *size*."""
        return 10.0 + size / MIN_READER_BYTES_PER_S

    def reader_deadline(self, session: MediaSession) -> float:
        """Seconds a reader may take to receive *session*'s blob."""
        return self.remaining(session) + 10.0 + session.size / MIN_READER_BYTES_PER_S

    def pin(self, session: MediaSession, prefix: str) -> bool:
        """A reader of *prefix* starts being written *session*'s blob —
        ``False`` (refused) when that prefix already holds
        :data:`MAX_PINNED_PER_PREFIX` other sessions this way."""
        key = (prefix, session.ticket)
        if key not in self._pinned:
            held = sum(1 for p, _ in self._pinned if p == prefix)
            if held >= MAX_PINNED_PER_PREFIX:
                return False
        self._pinned[key] = self._pinned.get(key, 0) + 1
        return True

    def unpin(self, session: MediaSession, prefix: str) -> None:
        key = (prefix, session.ticket)
        left = self._pinned.get(key, 0) - 1
        if left > 0:
            self._pinned[key] = left
        else:
            self._pinned.pop(key, None)

    def quota_refused(self, session: MediaSession) -> None:
        """The holder's bytes were refused by a budget after all (a race
        past the check before asking): the session ends — nothing marks
        the blob missing, the re-upload claim is given back."""
        session.finish(ok=False)

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
        session.stored = stored
        self._counters.streams += 1
        self._counters.streamed_bytes += session.size
        if stored:
            self._counters.reuploads += 1
        if session.member:
            self._counters.member_serves += 1

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
            "member_fallback": self.member_fallback,
            "member_wants": c.member_wants,
            "member_serves": c.member_serves,
            "member_pullers": self._pullers_total,
        }

    async def close(self) -> None:
        self._stop.set()
        for event in list(self._pull_events.values()):
            event.set()
        self._pull_events.clear()
        for session in list(self._sessions.values()):
            session.finish(ok=False)
        for task in list(self._drivers) + list(self._polls.values()):
            task.cancel()
        if self._drivers:
            await asyncio.gather(*self._drivers, return_exceptions=True)
        ticker = self._ticker
        if ticker is not None:
            await asyncio.gather(ticker, return_exceptions=True)

    # ── Members (the member fallback) ────────────────────────────────────

    async def member_pull(
        self, req: MemberPullRequest, *, prefix_key: str
    ) -> MemberWant | str:
        """One writer-key holder's pull: a :class:`MemberWant` it claimed,
        or :data:`PULL_NONE` (nothing within the hold), :data:`PULL_FULL`
        (no slot) or :data:`PULL_FORBIDDEN` (any refusal, uniform)."""
        scope = (req.scope, req.scope_id)
        if not self._pull_room(scope, prefix_key):
            # A cheap shed before any signature work.
            return PULL_FULL
        if not await self._authorize_pull(req):
            return PULL_FORBIDDEN
        if not self._pull_room(scope, prefix_key):
            return PULL_FULL
        self._pullers[scope] = self._pullers.get(scope, 0) + 1
        self._prefix_pulls[prefix_key] = self._prefix_pulls.get(prefix_key, 0) + 1
        self._pullers_total += 1
        try:
            self._ensure_ticker()
            deadline = self._clock() + self._cfg.member_pull_hold_seconds
            while not self._stop.is_set():
                # The event first: a want posted while the claim below runs
                # wakes it.
                event = self._pull_events.setdefault(scope, asyncio.Event())
                want = await self._claim(scope)
                if want is not None:
                    return want
                left = deadline - self._clock()
                if left <= 0:
                    break
                try:
                    await asyncio.wait_for(event.wait(), left)
                except TimeoutError:
                    break
            return PULL_NONE
        finally:
            self._leave_pull(scope, prefix_key)

    def _pull_room(self, scope: tuple[str, str], prefix_key: str) -> bool:
        cfg = self._cfg
        return (
            self._pullers_total < cfg.member_pullers_max
            and self._pullers.get(scope, 0) < cfg.member_pullers_per_scope
            and self._prefix_pulls.get(prefix_key, 0) < MAX_PULLS_PER_PREFIX
        )

    def _leave_pull(self, scope: tuple[str, str], prefix_key: str) -> None:
        self._pullers_total -= 1
        left = self._pullers.get(scope, 0) - 1
        if left > 0:
            self._pullers[scope] = left
        else:
            self._pullers.pop(scope, None)
            self._pull_events.pop(scope, None)
        left = self._prefix_pulls.get(prefix_key, 0) - 1
        if left > 0:
            self._prefix_pulls[prefix_key] = left
        else:
            self._prefix_pulls.pop(prefix_key, None)

    async def _authorize_pull(self, req: MemberPullRequest) -> bool:
        """Every check of a pull; a refusal costs the one verification a
        success makes, whatever failed."""
        with counting_ed25519_verifies() as done:
            try:
                await self._check_pull(req)
                return True
            except PermissionError as exc:
                log.debug("space media member pull refused: %s", exc)
                burn_verifies(1 - done[0])
                return False

    async def _check_pull(self, req: MemberPullRequest) -> None:
        if self._addressee is None or self._member_publish is None:
            raise PermissionError("member fallback off")
        if not self._addressee.accepts(req.gfs_instance_id, req.gfs_key):
            raise PermissionError("pull is addressed to another server")
        if abs(req.ts - time.time()) > MEMBER_TS_SKEW_S:
            raise PermissionError("stale or future ts")
        space = await self._federation.listed_space(req.scope_id)
        if space.status != "active":
            raise PermissionError("space is not active")
        await self._member_publish.verify_writer_key_sig(
            req.scope_id,
            epoch=req.epoch,
            message=req.signing_bytes(),
            writer_sig=req.sig,
            suite=req.sig_suite,
        )
        await self._member_publish.require_writer_key_epoch(req.scope_id, req.epoch)
        # After the signature, so a replay costs what a success does and an
        # unverified body can't fill the cache. Keyed on the signed body.
        digest = SeenPayloadCache.digest(req.signing_body())
        if self._pull_seen.seen(digest):
            raise PermissionError("replayed pull")
        self._pull_seen.record(digest)

    async def _claim(self, scope: tuple[str, str]) -> MemberWant | None:
        if self._member_wants is None:  # pragma: no cover - checked by callers
            return None
        row = await self._member_wants.claim_next(
            scope[0],
            scope[1],
            now=int(time.time()),
            hold_s=MEMBER_START_TIMEOUT_S,
            max_attempts=MAX_MEMBER_ATTEMPTS,
        )
        if row is None:
            return None
        try:
            return MemberWant.from_wire(
                {
                    "ticket": row.ticket,
                    "blob_id": row.blob_id,
                    "mode": row.mode,
                    "hints": orjson.loads(row.hints),
                }
            )
        except InvalidOnDemandWire, orjson.JSONDecodeError:  # pragma: no cover
            log.warning("space media member want unreadable: %s", row.ticket)
            return None

    def _wake(self, scope: tuple[str, str]) -> None:
        event = self._pull_events.pop(scope, None)
        if event is not None:
            event.set()

    def _ensure_ticker(self) -> None:
        if self._ticker is None or self._ticker.done():
            self._ticker = asyncio.create_task(self._tick())

    async def _tick(self) -> None:
        """While anyone pulls on this node: once a second, wake the pulls of
        every scope with a want open in the shared table (posted on any
        node) — one indexed query per node, however many pulls wait."""
        while not self._stop.is_set() and self._pullers_total > 0:
            try:
                await asyncio.wait_for(self._stop.wait(), MEMBER_TICK_S)
            except TimeoutError:
                pass
            if self._stop.is_set() or not self._pull_events:
                continue
            if self._member_wants is None:  # pragma: no cover - never started
                return
            try:
                scopes = await self._member_wants.open_scopes(now=int(time.time()))
            except Exception:  # pragma: no cover - defensive
                log.warning("space media member wants: lookup failed", exc_info=True)
                continue
            for scope in scopes:
                self._wake(scope)

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
        pinned = {ticket for _, ticket in self._pinned}
        if len(self._sessions.keys() | pinned) >= self._cfg.max_streams:
            return False
        same = sum(
            1
            for s in self._sessions.values()
            if (s.scope_kind, s.scope_id) == (scope_kind, scope_id)
        )
        return same < self._cfg.scope_max_streams

    @staticmethod
    def _add_hint(session: MediaSession, hint: SpaceMediaHint, prefix: str) -> None:
        """Keep a joining reader's hint to retry with — one per reader
        prefix (only the first counts), at most :data:`MAX_HINTS_PER_SESSION`."""
        if (
            len(session.hints) < MAX_HINTS_PER_SESSION
            and prefix not in session.hint_prefixes
            and all(h.ct != hint.ct for h in session.hints)
        ):
            session.hints.append(hint)
            session.hint_prefixes.append(prefix)

    async def _stream_budget_left(
        self, scope_kind: str, scope_id: str, prefix_key: str
    ) -> bool:
        """Whether the opener's prefix share and the scope still have stream
        bytes this hour (a peek; the holder's actual bytes are charged when
        it sends them)."""
        share = max(1, self._cfg.scope_stream_bytes_per_hour // PREFIX_STREAM_SHARE)
        if (
            await self._store.hourly_left(f"{_PREFIX_STREAM_BUDGET}{prefix_key}", share)
            <= 0
        ):
            return False
        return (
            await self._store.hourly_left(
                f"{_STREAM_BUDGET}{scope_kind}:{scope_id}",
                self._cfg.scope_stream_bytes_per_hour,
            )
            > 0
        )

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
            try:
                # Bounded: a holder that started has its own body deadline;
                # nothing may keep a session (and its slot) forever.
                await asyncio.wait_for(
                    session.done.wait(),
                    self.remaining(session) + SERVE_BODY_DEADLINE_S + 30.0,
                )
            except TimeoutError:
                session.finish(ok=False)
            if session.claimed_until and not session.stored:
                # Nothing was re-uploaded (streamed only, nobody served, junk
                # hints, a quota): give this node's claim back, so the readers
                # another node keeps waiting stop now (they poll the claim).
                await self._demand.release_claim(
                    *session.key, until=session.claimed_until
                )
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
        hint the holder can't use is no reason to give up on the blob);
        then, for a listed space whose owner did not answer or is busy, its
        members (module docstring)."""
        members = await self._member_applies(session)
        window = MEMBER_FALLBACK_OWNER_WINDOW_S if members else HOLDER_START_TIMEOUT_S
        for target in await self._holders(session.scope_kind, session.scope_id):
            tried = 0
            while tried < len(session.hints):
                if session.claimed or self.remaining(session) <= 0:
                    return
                hint = session.hints[tried]
                tried += 1
                if await self._ask(session, hint, target, window=window):
                    return
                if session.decline_reason != DECLINE_BAD_HINT:
                    session.only_bad_hints = False
                    break
        if (
            members
            and not session.claimed
            and self.remaining(session) > 0
            # An owner's "not held" is authoritative (it deleted the item,
            # or it is not this space's); unusable hints are unusable for
            # the members too.
            and session.decline_reason in (DECLINE_BUSY, _UNANSWERED, "")
        ):
            await self._member_phase(session)

    async def _member_applies(self, session: MediaSession) -> bool:
        """Whether the member fallback may take over this session: a listed
        space whose members can prove themselves anonymously here."""
        if session.scope_kind != SCOPE_SPACE or not self.member_fallback:
            return False
        if self._member_publish is None:  # pragma: no cover - member_fallback
            return False
        try:
            return await self._member_publish.writer_key_open(session.scope_id)
        except Exception:  # pragma: no cover - defensive
            log.warning("space media member fallback: lookup failed", exc_info=True)
            return False

    async def _member_phase(self, session: MediaSession) -> None:
        """Offer the want to the space's members (the shared table) and
        wait, within the session's deadline, for one to start — at most
        :data:`MAX_MEMBER_ATTEMPTS` declines. The row is always removed when
        this ends."""
        wants = self._member_wants
        if wants is None:  # pragma: no cover - member_fallback checked it
            return
        now = int(time.time())
        hints = [h.to_wire() for h in session.hints[:MAX_MEMBER_WANT_HINTS]]
        try:
            await wants.post(
                GfsSpaceMediaMemberWant(
                    ticket=session.ticket,
                    scope_kind=session.scope_kind,
                    scope_id=session.scope_id,
                    blob_id=session.blob_id,
                    mode=session.mode,
                    hints=orjson.dumps(hints).decode(),
                    expires_at=now + max(1, math.ceil(self.remaining(session))),
                    created_at=now,
                )
            )
        except Exception:  # pragma: no cover - defensive
            log.warning("space media member fallback: post failed", exc_info=True)
            return
        scope = (session.scope_kind, session.scope_id)
        session.member = True
        session.target = MEMBER_TARGET
        self._counters.member_wants += 1
        self._wake(scope)
        declines = 0
        try:
            while not session.claimed and self.remaining(session) > 0:
                session.declined.clear()
                session.decline_reason = ""
                await self._started_or_declined(session, self.remaining(session))
                if session.claimed:
                    return
                if not session.declined.is_set():
                    session.decline_reason = _UNANSWERED
                    return
                declines += 1
                self._counters.declined += 1
                if declines >= MAX_MEMBER_ATTEMPTS:
                    return
                # Released by the decline: the next pull may take it.
                self._wake(scope)
        finally:
            if not session.claimed:
                # Late bytes from any member are refused from now on.
                session.target = ""
            try:
                await wants.remove(session.ticket)
            except Exception:  # pragma: no cover - defensive
                log.warning("space media member fallback: remove failed", exc_info=True)

    @staticmethod
    async def _started_or_declined(session: MediaSession, timeout: float) -> None:
        waits = [
            asyncio.ensure_future(session.started.wait()),
            asyncio.ensure_future(session.declined.wait()),
        ]
        try:
            await asyncio.wait(
                waits, timeout=timeout, return_when=asyncio.FIRST_COMPLETED
            )
        finally:
            for w in waits:
                w.cancel()

    async def _ask(
        self,
        session: MediaSession,
        hint: SpaceMediaHint,
        target: str,
        *,
        window: float = HOLDER_START_TIMEOUT_S,
    ) -> bool:
        """Ask *target* with *hint*; ``True`` once it started sending."""
        if self._fan_out is None:
            session.decline_reason = _UNANSWERED
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
            # Connected nowhere (or an older node that can't reach it).
            session.decline_reason = _UNANSWERED
            return False
        await self._started_or_declined(
            session,
            min(window, HOLDER_START_TIMEOUT_S, self.remaining(session)),
        )
        if session.claimed:
            return True
        if session.declined.is_set():
            self._counters.declined += 1
        else:
            session.decline_reason = _UNANSWERED  # no answer in time
        # Late bytes from this household are refused from now on.
        session.target = ""
        return False

    async def _await_stored(self, key: tuple[str, str, str]) -> Cached | str:
        """Wait for the re-upload another node claimed to land in the shared
        cache — ONE poll per blob on this node, however many readers wait,
        ending as soon as that claim ends (nothing came) or the wait does."""
        blob_id = key[0]
        poll = self._polls.get(blob_id)
        if poll is None:
            poll = asyncio.ensure_future(self._poll_stored(key))
            self._polls[blob_id] = poll

            def _done(_f: asyncio.Future, b: str = blob_id) -> None:
                self._polls.pop(b, None)

            poll.add_done_callback(_done)
        outcome = await asyncio.shield(poll)
        if outcome == MISS:
            self._counters.misses += 1
        return outcome

    async def _poll_stored(self, key: tuple[str, str, str]) -> Cached | str:
        deadline = self._clock() + self._cfg.stream_wait_seconds
        while self._clock() < deadline:
            await asyncio.sleep(CLAIM_POLL_S)
            data = await self._store.read(key[0])
            if data is not None:
                return Cached(data)
            if not await self._demand.claim_live(*key, now=int(time.time())):
                return MISS
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


def build_space_media_member_pull_rate_limit(
    resolver: ClientIpResolver, *, nodes: Callable[[], int] | None = None
):
    """Per-IP limiter for ``POST /gfs/media/member-pull`` (matched on the
    route it resolved to), shed before any signature work; cluster-wide
    with *nodes* (each node allows its share)."""
    counter = SlidingWindowCounter(MEMBER_PULL_MAX_PER_MINUTE_PER_IP)

    @web.middleware
    async def _rate_limit(request: web.Request, handler):
        route = request.match_info.route
        resource = route.resource if route is not None else None
        canonical = resource.canonical if resource is not None else None
        if request.method != "POST" or canonical != MEMBER_PULL_ROUTE:
            return await handler(request)
        limit = MEMBER_PULL_MAX_PER_MINUTE_PER_IP
        share = limit if nodes is None else node_share(limit, nodes())
        if not counter.allow(resolver.bucket(request), limit=share):
            resp = web.json_response({"error": "rate_limited"}, status=429)
            resp.headers["Retry-After"] = "60"
            resp.headers["Cache-Control"] = "no-store"
            return resp
        return await handler(request)

    return _rate_limit
