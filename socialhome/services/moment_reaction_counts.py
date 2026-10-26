"""Everyone sees the full reaction count (v_69, owner decision 10).

Since v_67 / v_68 a reaction reaches the author's household from paired,
2–3-hop and GFS followers alike, so only that household holds a moment's
full count; every other household holds just its own people's reactions.
The author's household therefore re-broadcasts the **totals** — per-emoji
counts (the 8 most used) and the total, never who — and this service plays
both sides:

* **Author side — when.** A tick (every :data:`TICK_S`) looks at this
  household's own live moments and sends their totals at fixed
  *checkpoints* after each was made, **only when they changed** since the
  last send on that path:

  - mesh (``MOMENT_REACTIONS_COUNTED``): +10 min, +6 h and — only while
    followers still keep the moment — +3 days (:data:`MESH_CHECKPOINTS_S`);
  - GFS (public moments, :meth:`CountsPublisher.send_counts`): +10 min,
    +3 h and — only while still in its window — +24 h
    (:data:`GFS_CHECKPOINTS_S`).

  What was last sent lives in memory only. After a restart each own live
  moment of the last :data:`RESTART_CATCH_UP_S` that has reactions gets one
  checkpoint (its latest due one) — older moments wait for their next
  checkpoint, so a restart costs at most one update per recent moment.

  **Paced** under every receiver's budget: at most
  :data:`MESH_SENDS_PER_TICK` mesh updates and :data:`GFS_SENDS_PER_TICK`
  GFS posts a tick; the rest — a restart's catch-up last — stay due for the
  next ticks. A checkpoint counts as sent only once handed to the outbox
  (mesh) or taken (``2xx``) by every GFS; a GFS that refused is posted again
  after :data:`GFS_RETRY_S`, at most :data:`GFS_MAX_TRIES` times.
* **Receiver side.** :meth:`_on_counted` takes the mesh event: shape, origin
  signature (:mod:`socialhome.moment_counts`, against the pinned or derived
  origin key), a ``rev`` at most :data:`MAX_REV_SKEW_MS` ahead and
  :data:`~socialhome.moment_counts.MAX_COUNTS_REV_AGE_MS` old, a moment held
  here, live, by that author and origin (or whose create it relayed), and a
  ``rev`` newer than the one kept (``moments.reaction_summary``) or last
  relayed. It relays every verified copy through
  :meth:`~.moment_federation_outbound.MomentFederationOutbound.relay_inbound`
  (once per (moment, rev), only if held or relayed, only to v_69 peers). The
  GFS path (:class:`~.moment_public_inbound.MomentPublicInbound`) ends in
  :meth:`apply_totals` too.

**Budgets.** A receiver takes (stores and relays) at most
:data:`NEW_REVS_PER_ORIGIN_PER_MIN` new totals a minute per origin household
and :data:`NEW_REVS_PER_MOMENT_PER_DAY` per moment a day — counted once per
(moment, rev), not per copy — so an origin minting fresh revs can't flood the
mesh. Three checkpoints plus a restart's catch-up fit. Charged last — after
every check that can refuse — and only for totals actually kept or relayed,
so a neighbour replaying genuine old, older-than-kept or unheld totals
spends nobody's budget (adversarial review of #1184).

**Privacy residual** (``docs/principles.md``): a household holding the moment
learns its totals including reactions from audiences it can't see (the mesh
sees GFS followers' reactions and the other way round); a total of 1 plus the
timing of a change can hint at who reacted; a GFS learns that a public
moment's totals changed before checkpoint *k* (it reads no values, and the
sealed body is padded to one size).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Protocol

from ..domain.events import MomentReactionTotalsUpdated
from ..domain.federation import FederationEvent, FederationEventType
from ..domain.moment import (
    Moment,
    MomentReactionSummary,
    reaction_counts_valid,
    summarize_reactions,
)
from ..moment_counts import (
    MAX_COUNTS_REV_AGE_MS,
    NO_RELAY_FIELD,
    counts_origin_refusal,
    counts_shape_refusal,
    sign_counts,
)
from ..rate_limiter import RateLimiter
from .protection_gate import ProtectionGateMixin

if TYPE_CHECKING:
    from ..federation.federation_service import FederationService
    from ..infrastructure.event_bus import EventBus
    from ..repositories.moment_repo import AbstractMomentRepo
    from ..repositories.user_repo import AbstractUserRepo
    from .moment_federation_outbound import MomentFederationOutbound

log = logging.getLogger(__name__)

FET = FederationEventType

#: Mesh checkpoints, seconds after the moment was made: +10 min, +6 h, +3 d.
#: The last only while followers still keep the moment (``expires_at`` past
#: it).
MESH_CHECKPOINTS_S: tuple[int, ...] = (600, 6 * 3600, 3 * 86_400)

#: GFS checkpoints: +10 min, +3 h, +24 h — the last only while the moment is
#: still in its window then.
GFS_CHECKPOINTS_S: tuple[int, ...] = (600, 3 * 3600, 86_400)

#: How often the author side looks for due checkpoints.
TICK_S: float = 60.0

#: After a restart, own live moments younger than this that have reactions
#: get one checkpoint (what was sent before is not remembered).
RESTART_CATCH_UP_S: int = 86_400

#: How far ahead of this household's clock a totals ``rev`` may be.
MAX_REV_SKEW_MS: int = 300 * 1000

#: Receiver budgets, charged once per (moment, rev) actually kept or
#: relayed: per origin household a minute, and per moment a day (3
#: checkpoints + 1 restart catch-up).
NEW_REVS_PER_ORIGIN_PER_MIN: int = 10
NEW_REVS_PER_MOMENT_PER_DAY: int = 4

#: Author pacing: at most this many mesh updates a tick (ticks are at least
#: :data:`TICK_S` apart) — under every receiver's
#: :data:`NEW_REVS_PER_ORIGIN_PER_MIN`, with room for delivery jitter. More
#: due at once (many moments at one checkpoint, a restart's catch-up) wait
#: for the next ticks.
MESH_SENDS_PER_TICK: int = 8

#: The same for GFS posts (each GFS holds an author household to
#: ``COUNTS_PER_HOUSEHOLD_PER_MINUTE`` = 10 a minute, cluster-wide).
GFS_SENDS_PER_TICK: int = 8

#: A GFS that refused a checkpoint (``429``, unreachable) is tried again
#: after this long, at most :data:`GFS_MAX_TRIES` posts a checkpoint.
GFS_RETRY_S: int = 300
GFS_MAX_TRIES: int = 3

#: (moment, rev) pairs remembered as taken — a copy of one arriving through
#: another neighbour is not charged again.
SEEN_REVS_MAX: int = 20_000

#: A moment the author side looks at is at most this old (the last mesh
#: checkpoint, with an hour of slack for a late tick).
_LOOKBACK_S: int = MESH_CHECKPOINTS_S[-1] + 3600

_MESH = "mesh"
_GFS = "gfs"

#: What a moment's totals were before any send: nothing to say.
_NOTHING: tuple[tuple[tuple[str, int], ...], int] = ((), 0)


class CountsPublisher(Protocol):
    """The GFS path (:class:`~.moment_public_outbound.MomentPublicOutbound`):
    send a public moment's totals to every GFS that proves ``moment_counts``
    but those in *skip*; returns ``(accepted, refused)`` GFS ids."""

    async def send_counts(
        self,
        moment: Moment,
        *,
        rev: int,
        counts: dict[str, int],
        total: int,
        skip: frozenset[str] = frozenset(),
    ) -> tuple[frozenset[str], frozenset[str]]: ...


@dataclass(slots=True, frozen=True)
class _GfsRetry:
    """A public moment's GFS checkpoint some GFS refused: which checkpoint,
    the GFSs that took it, posts so far, and when to try again (ms)."""

    idx: int
    took: frozenset[str]
    tries: int
    next_ms: int


def _now_ms() -> int:
    return int(time.time() * 1000)


def _epoch_s(iso: str) -> float | None:
    try:
        when = datetime.fromisoformat(iso)
    except TypeError, ValueError:
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    return when.timestamp()


def due_checkpoint(
    checkpoints: tuple[int, ...],
    *,
    created_s: float,
    expires_s: float,
    now_s: float,
) -> int | None:
    """The index of the latest checkpoint of *checkpoints* that is due at
    *now_s* for a moment made at *created_s* — ``None`` before the first.
    The last checkpoint counts only when the moment is still kept then
    (``expires_s`` after it)."""
    due: int | None = None
    for i, offset in enumerate(checkpoints):
        at = created_s + offset
        if i == len(checkpoints) - 1 and expires_s <= at:
            break
        if at <= now_s:
            due = i
    return due


class MomentReactionCounts(ProtectionGateMixin):
    """Send this household's moments' reaction totals at checkpoints, and
    keep the totals other households send for theirs."""

    __slots__ = (
        "_bus",
        "_child_protection",
        "_clock_ms",
        "_done",
        "_federation",
        "_gfs",
        "_gfs_retry",
        "_last_rev",
        "_limiter",
        "_moments",
        "_newest_revs",
        "_outbound",
        "_seen_revs",
        "_sent",
        "_started_ms",
        "_stop",
        "_task",
        "_tick_s",
        "_users",
    )

    def __init__(
        self,
        *,
        bus: EventBus,
        federation_service: FederationService,
        moment_repo: AbstractMomentRepo,
        user_repo: AbstractUserRepo,
        outbound: MomentFederationOutbound,
        tick_s: float = TICK_S,
        clock_ms: Callable[[], int] = _now_ms,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._bus = bus
        self._federation = federation_service
        self._moments = moment_repo
        self._users = user_repo
        self._outbound = outbound
        self._child_protection = None
        #: The GFS path. Unwired: totals go over the mesh only.
        self._gfs: CountsPublisher | None = None
        self._tick_s = tick_s
        self._clock_ms = clock_ms
        self._limiter = RateLimiter(monotonic=monotonic)
        self._started_ms = clock_ms()
        #: (moment id, path) → index of the last checkpoint handled.
        self._done: dict[tuple[str, str], int] = {}
        #: (moment id, path) → the totals last sent on that path.
        self._sent: dict[tuple[str, str], tuple[tuple[tuple[str, int], ...], int]] = {}
        #: The last ``rev`` minted — revs only ever grow, even within a ms.
        self._last_rev = 0
        #: (moment id, "gfs") → a checkpoint a GFS refused, to try again.
        self._gfs_retry: dict[tuple[str, str], _GfsRetry] = {}
        #: (origin, moment id, rev) taken as a receiver → when (ms).
        self._seen_revs: OrderedDict[tuple[str, str, int], int] = OrderedDict()
        #: (origin, moment id) → the newest rev taken — what a relay-only
        #: household (it relayed the create, holds no row) compares against.
        self._newest_revs: OrderedDict[tuple[str, str], int] = OrderedDict()
        self._stop = asyncio.Event()
        self._task: asyncio.Task[None] | None = None

    # ── Wiring ─────────────────────────────────────────────────────────

    def attach_to(self, federation_service: FederationService) -> None:
        """Register the ``MOMENT_REACTIONS_COUNTED`` inbound handler."""
        federation_service._event_registry.register(
            FET.MOMENT_REACTIONS_COUNTED, self._on_counted
        )

    def attach_gfs(self, publisher: CountsPublisher) -> None:
        """Send public moments' totals through their GFSs too."""
        self._gfs = publisher

    async def start(self) -> None:
        """Start the checkpoint loop. Idempotent."""
        if self._task is not None and not self._task.done():
            return
        self._stop.clear()
        self._started_ms = self._clock_ms()
        self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        """Stop the loop and wait for it to exit."""
        self._stop.set()
        if self._task is not None:
            try:
                await asyncio.wait_for(self._task, timeout=5.0)
            except TimeoutError, asyncio.CancelledError:
                self._task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await self._task
            self._task = None

    async def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self._tick_s)
            except TimeoutError:
                pass
            if self._stop.is_set():
                return
            try:
                await self.tick()
            except Exception as exc:  # pragma: no cover — never kill the loop
                log.warning("moment totals: tick failed: %s", exc, exc_info=True)

    # ── Author side ────────────────────────────────────────────────────

    async def tick(self) -> int:
        """Send due, changed checkpoints — at most :data:`MESH_SENDS_PER_TICK`
        over the mesh and :data:`GFS_SENDS_PER_TICK` to the GFSs a tick; the
        rest (a restart's catch-up last) stay due for the next ticks. A
        checkpoint counts as sent only once handed to the outbox (mesh) or
        taken by every GFS (``2xx``). Returns how many sends (one per path
        per moment) went out. Exposed for tests."""
        now_ms = self._clock_ms()
        now_s = now_ms / 1000
        since = datetime.fromtimestamp(now_s - _LOOKBACK_S, UTC)
        moments = await self._moments.list_own_live_since(
            since.strftime("%Y-%m-%d %H:%M:%S")
        )
        live = {m.id for m in moments}
        for key in [k for k in (*self._done, *self._gfs_retry) if k[0] not in live]:
            self._done.pop(key, None)
            self._sent.pop(key, None)
            self._gfs_retry.pop(key, None)
        # (catch-up last, oldest first, moment, path, checkpoint index)
        due: list[tuple[bool, float, Moment, str, int]] = []
        for m in moments:
            created, expires = _epoch_s(m.created_at), _epoch_s(m.expires_at)
            if created is None or expires is None:
                continue
            for path, checkpoints in (
                (_MESH, MESH_CHECKPOINTS_S),
                (_GFS, GFS_CHECKPOINTS_S),
            ):
                if path == _GFS and (self._gfs is None or not m.is_public):
                    continue
                idx = due_checkpoint(
                    checkpoints, created_s=created, expires_s=expires, now_s=now_s
                )
                key = (m.id, path)
                if idx is None or self._done.get(key, -1) >= idx:
                    continue
                first_seen = key not in self._done
                if first_seen and (
                    created * 1000 < self._started_ms
                    and self._started_ms - created * 1000 > RESTART_CATCH_UP_S * 1000
                ):
                    # Made long before this process started: what was sent
                    # then is not known, and a restart must not re-send every
                    # old moment — it waits for its next checkpoint.
                    self._done[key] = idx
                    continue
                retry = self._gfs_retry.get(key)
                if retry is not None and retry.idx == idx and now_ms < retry.next_ms:
                    continue  # a GFS refused it lately: not before the retry
                catch_up = first_seen and created * 1000 < self._started_ms
                due.append((catch_up, created, m, path, idx))
        if not due:
            return 0
        due.sort(key=lambda d: (d[0], d[1]))
        tallies = await self._moments.reaction_tallies(
            list(dict.fromkeys(d[2].id for d in due))
        )
        room = {_MESH: MESH_SENDS_PER_TICK, _GFS: GFS_SENDS_PER_TICK}
        sent = 0
        for _catch_up, _created, m, path, idx in due:
            counts, total = summarize_reactions(tallies.get(m.id, {}))
            state = (tuple(counts.items()), total)
            key = (m.id, path)
            if state == self._sent.get(key, _NOTHING):
                # Unchanged since the last send on this path: nothing to say.
                self._done[key] = idx
                self._gfs_retry.pop(key, None)
                continue
            if room[path] <= 0:
                continue  # paced: still due, a later tick takes it
            room[path] -= 1
            if path == _MESH:
                if not await self._send_mesh(m, counts, total):
                    continue  # not handed on: still due, tried next tick
                went = done = complete = True
            else:
                went, done, complete = await self._send_gfs(
                    m, idx, counts, total, now_ms
                )
            if went:
                sent += 1
                log.info(
                    "moment totals: %s checkpoint %d for %s sent (total=%d)",
                    path,
                    idx,
                    m.id,
                    total,
                )
            if done:
                self._done[key] = idx
                if complete:
                    self._sent[key] = state
        return sent

    def _next_rev(self) -> int:
        self._last_rev = max(self._clock_ms(), self._last_rev + 1)
        return self._last_rev

    async def _send_mesh(
        self, moment: Moment, counts: dict[str, int], total: int
    ) -> bool:
        """Sign the totals and hand them to every v_69 peer (a peer offline
        gets them from the outbox). ``False`` when that failed — the
        checkpoint stays due and is tried again next tick."""
        try:
            payload = sign_counts(
                seed=self._federation.own_identity_seed,
                identity_pk=self._federation.own_identity_pk,
                moment_id=moment.id,
                author_user_id=moment.author_user_id,
                origin_instance_id=self._federation.own_instance_id,
                rev=self._next_rev(),
                counts=counts,
                total=total,
                # §CP.R: a protected account's moment went to paired
                # households only; its totals do too, never relayed.
                no_relay=await self._is_protected(moment.author_user_id),
            )
            await self._outbound.fan_counts(
                payload, author_user_id=moment.author_user_id
            )
        except Exception as exc:  # noqa: BLE001 — retried, never kills the tick
            log.warning(
                "moment totals: mesh send for %s failed, tried again next tick: %s",
                moment.id,
                exc,
            )
            return False
        return True

    async def _send_gfs(
        self,
        moment: Moment,
        idx: int,
        counts: dict[str, int],
        total: int,
        now_ms: int,
    ) -> tuple[bool, bool, bool]:
        """Post the totals to every GFS that has not taken this checkpoint
        yet. Returns ``(went, done, complete)``: whether a GFS took them
        now; whether this checkpoint is finished; whether every GFS has it.
        A GFS that refused (``429``, unreachable) is tried again after
        :data:`GFS_RETRY_S`, at most :data:`GFS_MAX_TRIES` times a checkpoint
        — then the next checkpoint sends again, even if unchanged."""
        assert self._gfs is not None
        key = (moment.id, _GFS)
        retry = self._gfs_retry.get(key)
        if retry is not None and retry.idx != idx:
            retry = None  # a newer checkpoint: every GFS gets it
        took = retry.took if retry is not None else frozenset()
        try:
            accepted, refused = await self._gfs.send_counts(
                moment,
                rev=self._next_rev(),
                counts=counts,
                total=total,
                skip=took,
            )
        except Exception as exc:  # noqa: BLE001 — retried, never kills the tick
            log.warning("moment totals: GFS send for %s failed: %s", moment.id, exc)
            accepted, refused = frozenset(), frozenset({"*"})
        if not refused:
            self._gfs_retry.pop(key, None)
            return bool(accepted), True, True
        tries = (retry.tries if retry is not None else 0) + 1
        if tries >= GFS_MAX_TRIES:
            self._gfs_retry.pop(key, None)
            log.info(
                "moment totals: %d GFS(s) kept refusing %s — next checkpoint",
                len(refused),
                moment.id,
            )
            return bool(accepted), True, False
        self._gfs_retry[key] = _GfsRetry(
            idx=idx,
            took=took | accepted,
            tries=tries,
            next_ms=now_ms + GFS_RETRY_S * 1000,
        )
        return bool(accepted), False, False

    # ── Receiver side ──────────────────────────────────────────────────

    async def _on_counted(self, event: FederationEvent) -> None:
        p = event.payload
        reason = counts_shape_refusal(p)
        if reason is None:
            reason = await self._origin_refusal(event)
        if reason is not None:
            self._refuse(event, reason)
            return
        moment_id, rev = str(p["moment_id"]), int(p["rev"])
        origin = str(p["origin_instance_id"])
        now = self._clock_ms()
        # Every check that can refuse comes BEFORE a budget is touched: a
        # relay replaying genuine old totals spends nobody's budget.
        if rev > now + MAX_REV_SKEW_MS:
            self._refuse(event, "totals from the future")
            return
        if rev < now - MAX_COUNTS_REV_AGE_MS:
            self._refuse(event, "totals older than a day", quiet=True)
            return
        moment = await self._moments.get(moment_id)
        if moment is not None and moment.origin_instance_id != origin:
            # Someone else's moment id: a household can sign totals only for
            # its own moments — neither kept nor passed on.
            self._refuse(event, "the moment held here has another origin")
            return
        held = (
            moment is not None
            and moment.author_user_id == p["author_user_id"]
            and moment.received_via in ("household", "gfs")
            and (_epoch_s(moment.expires_at) or 0) * 1000 > now
        )
        if not held and not self._outbound.relayed_create(moment_id, origin):
            # Neither kept nor passed on here. Quiet: an honest origin sends
            # to every v_69 peer, most of which never held this moment.
            self._refuse(event, "moment neither held nor relayed here", quiet=True)
            return
        key = (origin, moment_id, rev)
        taken = key in self._seen_revs
        newest = await self._newest_rev(moment_id, origin, held=held)
        if newest is not None and (rev < newest or (rev == newest and not taken)):
            # Not newer than the totals kept (or, relay-only, last passed
            # on): a replay. A copy of a rev taken here still goes to the
            # relay — free — as it may come from closer to the origin.
            self._refuse(event, "totals not newer than the ones kept", quiet=True)
            return
        if not taken and not self._has_budget(origin, moment_id):
            self._refuse(event, "over the totals budget")
            return
        kept = held and await self.apply_totals(
            moment_id=moment_id,
            author_user_id=str(p["author_user_id"]),
            origin_instance_id=origin,
            rev=rev,
            counts=dict(p["counts"]),
            total=int(p["total"]),
        )
        relayed = 0
        try:
            relayed = await self._outbound.relay_inbound(
                event_type=FET.MOMENT_REACTIONS_COUNTED,
                payload=p,
                from_instance=event.from_instance,
                holds_moment=held
                and moment is not None
                and moment.received_via == "household",
                now_ms=now,
            )
        except Exception as exc:  # pragma: no cover — defensive
            log.debug("moment totals relay failed: %s", exc, exc_info=True)
        if not taken and (kept or relayed):
            # Charged only for totals actually kept or passed on.
            self._charge(key, now)

    async def _newest_rev(
        self, moment_id: str, origin: str, *, held: bool
    ) -> int | None:
        """The newest ``rev`` this household has for the moment: the kept
        totals' (database — survives a restart) when held, else the last one
        it passed on (memory — as is the relayed create that allows it)."""
        if held:
            kept = await self._moments.get_reaction_summaries([moment_id])
            summary = kept.get(moment_id)
            return summary.rev if summary is not None else None
        return self._newest_revs.get((origin, moment_id))

    async def _origin_refusal(self, event: FederationEvent) -> str | None:
        p = event.payload
        origin = str(p["origin_instance_id"])
        if origin == self._federation.own_instance_id:
            return "names this household as the origin"
        if await self._users.get_by_user_id(str(p["author_user_id"])) is not None:
            return "author is one of this household's people"
        if NO_RELAY_FIELD in p and event.from_instance != origin:
            return "marked no_relay, but relayed"
        return counts_origin_refusal(
            p, pinned_pk=await self._federation.peer_identity_public_key(origin)
        )

    def _has_budget(self, origin: str, moment_id: str) -> bool:
        """Whether one more new (moment, rev) of *origin* fits both budgets.
        Keyed by the signing origin, so a household naming another origin's
        moment id spends only its own budget, never that moment's."""
        self._limiter.prune(window_s=86_400)
        return self._limiter.has_room(
            f"o:{origin}", limit=NEW_REVS_PER_ORIGIN_PER_MIN, window_s=60
        ) and self._limiter.has_room(
            f"m:{origin}|{moment_id}",
            limit=NEW_REVS_PER_MOMENT_PER_DAY,
            window_s=86_400,
        )

    def _charge(self, key: tuple[str, str, int], now_ms: int) -> None:
        """Spend both budgets once for a (moment, rev) kept or relayed — a
        copy of it arriving later is free — and remember it as the newest."""
        origin, moment_id, rev = key
        self._limiter.is_allowed(
            f"o:{origin}", limit=NEW_REVS_PER_ORIGIN_PER_MIN, window_s=60
        )
        self._limiter.is_allowed(
            f"m:{origin}|{moment_id}",
            limit=NEW_REVS_PER_MOMENT_PER_DAY,
            window_s=86_400,
        )
        self._seen_revs[key] = now_ms
        while len(self._seen_revs) > SEEN_REVS_MAX:
            self._seen_revs.popitem(last=False)
        pair = (origin, moment_id)
        if rev > self._newest_revs.get(pair, 0):
            self._newest_revs[pair] = rev
            self._newest_revs.move_to_end(pair)
            while len(self._newest_revs) > SEEN_REVS_MAX:
                self._newest_revs.popitem(last=False)

    async def apply_totals(
        self,
        *,
        moment_id: str,
        author_user_id: str,
        origin_instance_id: str,
        rev: int,
        counts: dict[str, int],
        total: int,
    ) -> bool:
        """Keep the author household's totals on a moment held here — only
        valid ones, only for that author and origin's live moment, only when
        newer than the kept ones — and tell the household's open views.
        Returns whether they were kept."""
        if not reaction_counts_valid(counts, total):
            return False
        kept = await self._moments.set_reaction_summary(
            moment_id,
            MomentReactionSummary(
                rev=rev,
                counts=tuple(sorted(counts.items(), key=lambda p: (-p[1], p[0]))),
                total=total,
            ),
            author_user_id=author_user_id,
            origin_instance_id=origin_instance_id,
        )
        if kept:
            moment = await self._moments.get(moment_id)
            await self._bus.publish(
                MomentReactionTotalsUpdated(
                    moment_id=moment_id,
                    author_user_id=author_user_id,
                    received_via=moment.received_via if moment else "household",
                    hop_count=moment.hop_count if moment else 1,
                )
            )
        return kept

    @staticmethod
    def _refuse(event: FederationEvent, reason: str, *, quiet: bool = False) -> None:
        """Log a refused update — a WARNING, or DEBUG (*quiet*) for what
        honest traffic produces too (old, unheld, already kept)."""
        log.log(
            logging.DEBUG if quiet else logging.WARNING,
            "%s from %s: %s — refusing (moment=%s)",
            event.event_type,
            event.from_instance,
            reason,
            event.payload.get("moment_id") if isinstance(event.payload, dict) else None,
        )


__all__ = [
    "GFS_CHECKPOINTS_S",
    "GFS_MAX_TRIES",
    "GFS_RETRY_S",
    "GFS_SENDS_PER_TICK",
    "MESH_CHECKPOINTS_S",
    "MESH_SENDS_PER_TICK",
    "NEW_REVS_PER_MOMENT_PER_DAY",
    "NEW_REVS_PER_ORIGIN_PER_MIN",
    "RESTART_CATCH_UP_S",
    "TICK_S",
    "CountsPublisher",
    "MomentReactionCounts",
    "due_checkpoint",
]
