"""Outbound federation for the Momentum pillar (§Momentum).

Subscribes to :class:`MomentCreated`, :class:`MomentDeleted`,
:class:`MomentReactionChanged` on the bus and translates each into
the matching ``MOMENT_*`` envelope. Two distinct fan-out paths share
the code in this file:

1. **Origin fan-out.** The author's instance fans the moment to every
   confirmed peer with ``hop_count = 1`` and
   ``origin_instance_id = self``.

2. **Relay fan-out** (up to ``MOMENT_MAX_HOPS``, bounded by the signed
   reach). When an inbound ``MOMENT_*`` envelope lands on an instance and
   ``hop < min(reach, MOMENT_MAX_HOPS)``, :meth:`relay_inbound` re-fans the same payload to the local paired
   peers, excluding the origin instance and the immediate sender — once
   per moment: a later copy (one arrives per relaying neighbour) is
   relayed only when it comes from closer to the origin. Receivers dedupe
   by ``moment.id`` (the row's PRIMARY KEY makes the second save a no-op,
   and the inbound handler stores and announces only the first copy).

**Echo-loop guard.** Bus events fired by inbound handlers are
indistinguishable from local writes by event-class identity alone, so
the subscriber gates each event on "is the *actor* (author for create
/ delete, reactor for reactions) local on this instance?" — only the
local actor's instance fans on the bus path. The relay path is
explicit (not bus-driven) and runs from inside the inbound handler
before it republishes anything.

**Reaction totals (v_69).** Only the author's household holds every
reaction, so it sends a moment's totals (``MOMENT_REACTIONS_COUNTED``,
:mod:`socialhome.moment_counts`) at fixed checkpoints
(:class:`~socialhome.services.moment_reaction_counts.MomentReactionCounts`
decides when; :meth:`fan_counts` sends). Relays pass them on through
:meth:`relay_inbound` like a create — once per (moment, ``rev``), only when
they hold or relayed the moment, only to v_69 peers.

**Reach (v_96).** A moment goes as many hops as its author's household
signed (:func:`~socialhome.moment_origin.moment_reach_of`): reach 1 is the
v_38 ``no_relay`` mark, 2 / 4 / 5 a signed ``reach`` field, 3 neither. The
origin sends reach 1 only to v_38 households, reach 2 only to v_96 ones;
a delete carries the same mark under the same gate. A relay forwards only
while ``hop < min(reach, MOMENT_MAX_HOPS)``, counts a copy not sent by its
origin as at least hop 2 (the relay clamp — ``hop_count`` is outside the
signature), sends a ``reach``-carrying copy only to v_96 households,
relays a closer copy at most once more, and queues relayed creates and
totals under a relayed outbox key the per-peer cap evicts first.

**Per-author relay cap** (owner 2026-11-24, local policy — no protocol
bump). A relay holds a signed author (origin + author, never the
neighbour, so a relay can't launder a flood) to the posting rate limit: at
most :data:`MOMENT_RELAY_PER_RATE_WINDOW` new top-level moments per
:data:`~socialhome.domain.moment.MOMENT_RATE_WINDOW` of their signed time;
replies at most :data:`MOMENT_RELAY_AUTHOR_CAP` an hour. Every first forward
of a create counts; deletes of moments neither relayed nor stored here (or whose
create was capped) count on a budget of their own; a delete of a moment
relayed or stored here, a closer copy and totals don't. Over the cap only the forward is dropped — the inbound
handler has already stored the moment for anyone here who looks.

**Reactions further away (v_67).** A reaction is unicast to the author's
household when it is this one or a paired one. Otherwise the
:class:`~socialhome.services.moment_reaction_router.MomentReactionRouter`
carries it back along the moment's return tokens; this service adds the
origin's key-wrap key to every create it authors and each relay's return
token to every create it relays (per peer).
"""

from __future__ import annotations

import enum
import hashlib
import logging
import time
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass
from time import monotonic
from typing import TYPE_CHECKING

from ..domain.events import (
    MomentCreated,
    MomentDeleted,
    MomentReactionChanged,
)
from ..domain.federation import FederationEventType, InstanceSource
from ..domain.federation_capabilities import FederationCapability
from ..domain.federation_retention import relayed_moment_key
from ..domain.moment import (
    MOMENT_DEFAULT_REACH,
    MOMENT_MAX_HOPS,
    MOMENT_RATE_WINDOW,
    MOMENT_RELAY_MAX_AGE_HOURS,
    MOMENT_UNGATED_HOPS,
    moment_rate_slot,
    moment_too_old_to_relay,
)
from ..infrastructure.event_bus import EventBus
from ..moment_counts import MAX_COUNTS_REV_AGE_MS
from ..moment_origin import (
    NO_RELAY_FIELD,
    REACH_FIELD,
    carries_no_relay,
    moment_hop_of,
    moment_reach_of,
    reach_marks_of,
    sign_moment_origin,
)
from ..rate_limiter import RateLimiter
from .peer_outbound import ConfirmedPeerBroadcaster, SingleTargetSender
from .protection_gate import ProtectionGateMixin
from .visibility import VisibilityMixin

if TYPE_CHECKING:
    from ..federation.federation_service import FederationService
    from ..repositories.federation_repo import AbstractFederationRepo
    from ..repositories.peer_user_visibility_repo import AbstractPeerUserVisibilityRepo
    from ..repositories.user_repo import AbstractUserRepo
    from .moment_reaction_router import MomentReactionRouter
    from .relay_policy import RelayPolicy

log = logging.getLogger(__name__)

#: How long a relayed moment is remembered — as long as a create may be
#: relayed at all (:data:`MOMENT_RELAY_MAX_AGE_HOURS`), so a copy
#: redelivered by a peer's outbox inside that window is never relayed twice
#: (until a restart or the size cap drops the entry: bounded and harmless).
RELAY_SEEN_TTL_S: float = MOMENT_RELAY_MAX_AGE_HOURS * 3600.0

#: Most entries remembered at once (oldest dropped first) — ~100 bytes
#: each, so a few MB at worst. A relayed create takes two (its seen key and,
#: v_69, its origin), a relayed totals update one per rev.
#:
#: Sized for v_96 (cap 5) with decision C: every household still takes (and
#: so remembers) the moments of authors within 3 hops — hop-4–5 copies reach
#: only households that look that far. With ≈ 4 moments per household a day
#: at d = 10 peers (tree bound): 10 + 90 + 810 = 910 authors → ≈ 3 640
#: creates/day × 2 entries × 2 days (the 48 h window) ≈ 14 600, plus at most
#: one delete each (≈ 7 300) and a few totals revs on the reacted ones —
#: ≈ 30 000 entries, inside 50 000. A household that itself looks 5 hops at
#: d = 10 can overflow it: the oldest entries go first, and a replayed copy
#: younger than 48 h is then relayed once more — bounded (relays still stop
#: at the reach) and the cost lands on whoever asked to look that far.
RELAY_SEEN_MAX: int = 50_000

#: v_69 — a household relays a moment's totals only while they are this
#: fresh (by their signed ``rev``): a day of outbox retries plus clock skew.
#: Older totals are a replay — neither kept nor passed on (the receiver
#: refuses them before any budget, :data:`~socialhome.moment_counts
#: .MAX_COUNTS_REV_AGE_MS`).
COUNTS_RELAY_MAX_AGE_MS: int = MAX_COUNTS_REV_AGE_MS

#: Per-author relay cap (owner 2026-11-24), top-level moments: the posting
#: rate limit (:data:`MOMENT_RATE_WINDOW`, one per person per 15 min) applied
#: to the signed author — at most this many new top-level moments of one
#: (origin, author) per window slot of their signed ``occurred_at``
#: (:func:`moment_rate_slot`): 1, plus 1 of slack (an origin clock stepped
#: back, a re-send after our restart). More comes only from a household
#: ignoring the rule, so dropping the forward is safe.
MOMENT_RELAY_PER_RATE_WINDOW: int = 2

#: How long a slot's count is kept: as long as a create can be relayed at
#: all (48 h) plus one window, so an old slot can't be refilled.
MOMENT_RELAY_SLOT_TTL_S: int = int(
    MOMENT_RELAY_MAX_AGE_HOURS * 3600 + MOMENT_RATE_WINDOW.total_seconds()
)

#: Most slot counts remembered (least recently touched dropped first — a
#: dropped slot only lets one old slot take its 2 again). Sized like the
#: seen-map: every top-level moment relayed in 48 h, ~0.3 KB each.
RELAY_SLOT_KEYS_MAX: int = 50_000

#: Replies (the origin doesn't rate-limit them) and deletes of moments this
#: household neither relayed nor stored: at most this many forwards per
#: (origin, author) per :data:`MOMENT_RELAY_AUTHOR_WINDOW_S` of arrival — an
#: hour of lively replies; each kind its own budget.
MOMENT_RELAY_AUTHOR_CAP: int = 30
MOMENT_RELAY_AUTHOR_WINDOW_S: int = 3600

#: Most (origin, author) reply / delete budgets remembered (least recently
#: touched dropped first — its budget starts afresh). Author ids are free to
#: mint, so the map must be bounded; keys are fixed-size digests, so a busy
#: key holds ≤ 30 stamps (~1 KB) whatever the ids are: ≈ 10–15 MB at worst.
#: The once-a-window log throttle has the same bound.
RELAY_AUTHOR_KEYS_MAX: int = 10_000


def _budget_key(*parts: str) -> str:
    """A fixed-size relay-cap key whatever the (peer-chosen) ids are."""
    return hashlib.blake2b("\0".join(parts).encode(), digest_size=16).hexdigest()


def counts_outbox_key(moment_id: str) -> str:
    """The latest-only outbox key of a moment's totals: a newer update
    replaces one still queued for an offline peer (local, never on the
    wire)."""
    return f"moment-counts:{moment_id}"


def reach_min_version(reach: int | None) -> int | None:
    """v_96 — the lowest peer version the origin sends a moment (its delete,
    its totals' reach part) of *reach* hops to: reach 1 (``no_relay``) →
    v_38, reach 2 → v_96 (an older household would relay it to hop 3),
    anything else — 3, 4–5 (a direct delivery needs no origin check), or
    ``None`` for a row written before v_96 — every peer."""
    if reach == 1:
        return FederationCapability.MIN_FOR_MOMENT_NO_RELAY
    if reach == 2:
        return FederationCapability.MIN_FOR_MOMENT_REACH
    return None


@dataclass(slots=True, frozen=True)
class _Relayed:
    """One relay seen-map entry: the lowest ``hop`` it was relayed at, when
    (monotonic ``at``), whether a closer copy was relayed again already
    (``re_relayed``, v_96 F8), and — on a relayed create's ``origin`` entry
    only — the moment's ``reach``."""

    hop: int
    at: float
    re_relayed: bool = False
    reach: int | None = None


class _Copy(enum.Enum):
    """What a copy arriving at the relay is, against the seen-map."""

    #: Never relayed (or the entry aged out): relay it.
    FIRST = "first"
    #: Closer to the origin than the one relayed: relay it once more.
    CLOSER = "closer"
    #: Nothing new: drop it.
    SEEN = "seen"


class MomentFederationOutbound(
    VisibilityMixin,
    ConfirmedPeerBroadcaster,
    SingleTargetSender,
    ProtectionGateMixin,
):
    """Publish moment mutations to the peer mesh — up to
    ``MOMENT_MAX_HOPS``, bounded by the signed reach."""

    __slots__ = (
        "_author_budget",
        "_bus",
        "_cap_logged",
        "_child_protection",
        "_federation",
        "_federation_repo",
        "_reaction_router",
        "_relay_policy",
        "_relayed",
        "_slots",
        "_user_repo",
    )

    # Narrow the mixins' optional ``_federation`` — this service requires it
    # at construction, so direct attribute access elsewhere is non-None.
    _federation: FederationService

    def __init__(
        self,
        *,
        bus: EventBus,
        federation_service: FederationService,
        federation_repo: AbstractFederationRepo,
        user_repo: AbstractUserRepo,
        relay_policy: RelayPolicy | None = None,
        visibility_repo: AbstractPeerUserVisibilityRepo | None = None,
    ) -> None:
        self._bus = bus
        self._federation = federation_service
        self._federation_repo = federation_repo
        self._user_repo = user_repo
        self._relay_policy = relay_policy
        self._visibility_repo = visibility_repo
        self._child_protection = None
        self._reaction_router: MomentReactionRouter | None = None
        #: (event type, moment id) → :class:`_Relayed`. In a mesh a
        #: household gets one copy of a moment per relaying neighbour; only
        #: the first — or, once (v_96, F8; always when the origin itself
        #: sent it), one from closer to the origin, which reaches further —
        #: is relayed on. Bounded by :data:`RELAY_SEEN_MAX` and
        #: :data:`RELAY_SEEN_TTL_S`; lost on restart (a copy arriving after
        #: one is relayed once more). v_69 totals are remembered per (type,
        #: moment id, rev); a relayed create's ``("origin", moment id,
        #: origin)`` entry keeps the moment's reach (v_96).
        self._relayed: OrderedDict[tuple[str, ...], _Relayed] = OrderedDict()
        #: New top-level moments forwarded per (origin, author, rate slot).
        #: Reads the module clock at each call, like the seen-map.
        self._slots = RateLimiter(
            monotonic=lambda: monotonic(), max_keys=RELAY_SLOT_KEYS_MAX
        )
        #: Replies / unknown deletes forwarded per (origin, author) an hour.
        self._author_budget = RateLimiter(
            monotonic=lambda: monotonic(), max_keys=RELAY_AUTHOR_KEYS_MAX
        )
        #: Which capped authors were logged this window (at most once each).
        self._cap_logged = RateLimiter(
            monotonic=lambda: monotonic(), max_keys=RELAY_AUTHOR_KEYS_MAX
        )

    def attach_reaction_router(self, router: MomentReactionRouter) -> None:
        """Wire the v_67 reaction router (key-wrap key on creates, return
        tokens on relays, routed reactions to authors further away)."""
        self._reaction_router = router

    def wire(self) -> None:
        self._bus.subscribe(MomentCreated, self._on_created)
        self._bus.subscribe(MomentDeleted, self._on_deleted)
        self._bus.subscribe(MomentReactionChanged, self._on_reaction_changed)

    # ── Bus subscribers (origin-side fan-out) ──────────────────────────

    async def _on_created(self, event: MomentCreated) -> None:
        if not await self._is_local_user(event.author_user_id):
            return
        # §Momentum-relay-policy: skip outbound fan-out while the
        # author or this moment has an open report. Local writes still
        # fire on the bus before we get here, so the author sees their
        # own row; peers don't until moderation resolves.
        if not await self._policy_allows(
            source_instance_id=event.origin_instance_id,
            author_user_id=event.author_user_id,
            target_id=event.moment_id,
        ):
            return
        payload: dict[str, object] = {
            "moment_id": event.moment_id,
            "author_user_id": event.author_user_id,
            "content": event.content,
            "media_url": event.media_url,
            "media_type": event.media_type,
            "duration_ms": event.duration_ms,
            "parent_moment_id": event.parent_moment_id,
            "origin_instance_id": event.origin_instance_id,
            "expires_at": event.expires_at,
            "occurred_at": event.occurred_at.isoformat(),
            "hop_count": 1,
        }
        # v_96: as far as the moment goes, signed — and never to a household
        # too old to honour it (it would relay it onward). §CP.R (v_38): the
        # service already writes a protected account's moment at reach 1;
        # forcing it here too is defence in depth.
        reach = 1 if await self._is_protected(event.author_user_id) else event.reach
        marks = reach_marks_of(reach)
        payload.update(marks)
        no_relay = NO_RELAY_FIELD in marks
        signed = self._origin_signed(FederationEventType.MOMENT_CREATED, payload)
        if not no_relay and self._reaction_router is not None:
            # v_67: where a reaction from further away gets sealed to. Outside
            # the origin signature (older verifiers keep the v1 bytes) and
            # bound to our identity key instead; older receivers ignore it.
            signed.update(self._reaction_router.origin_keywrap_fields())
        await self._fan_to_peers(
            event_type=FederationEventType.MOMENT_CREATED,
            payload=signed,
            origin_instance_id=event.origin_instance_id,
            exclude_instances=set(),
            author_user_id=event.author_user_id,
            min_version=reach_min_version(reach),
        )

    async def _on_deleted(self, event: MomentDeleted) -> None:
        if not await self._is_local_user(event.author_user_id):
            return
        if not await self._policy_allows(
            source_instance_id=event.origin_instance_id,
            author_user_id=event.author_user_id,
            target_id=event.moment_id,
        ):
            return
        # v_96: the delete goes exactly as far as the moment did — its row's
        # reach, under the same signed mark (reach 1: ``no_relay``, so a
        # protected account's delete is never relayed) and version gate. A
        # row written before v_96 (``None``) went as it did then: one hop
        # (``no_relay``) for a protected author, else 3 hops, unmarked.
        # Accepted edge: an author protected only after posting a legacy
        # moment gets a one-hop delete for it.
        reach = event.reach
        if reach is None and await self._is_protected(event.author_user_id):
            reach = 1
        marks = {} if reach is None else reach_marks_of(reach)
        min_version = reach_min_version(reach)
        await self._fan_to_peers(
            event_type=FederationEventType.MOMENT_DELETED,
            payload=self._origin_signed(
                FederationEventType.MOMENT_DELETED,
                {
                    "moment_id": event.moment_id,
                    "author_user_id": event.author_user_id,
                    "origin_instance_id": event.origin_instance_id,
                    "occurred_at": event.occurred_at.isoformat(),
                    "hop_count": 1,
                    **marks,
                },
            ),
            origin_instance_id=event.origin_instance_id,
            exclude_instances=set(),
            author_user_id=event.author_user_id,
            min_version=min_version,
        )

    async def _on_reaction_changed(self, event: MomentReactionChanged) -> None:
        if not await self._is_local_user(event.reactor_user_id):
            return
        ev_type = (
            FederationEventType.MOMENT_REACTION_REMOVED
            if event.emoji is None
            else FederationEventType.MOMENT_REACTED
        )
        # Reactions are unicast to the author's home instance —
        # everyone else's view of the reaction is hydrated from the
        # author's instance via the next list refresh. (Same shape as
        # the Highlights back-channel.) Only a household we are paired with
        # gets it directly; an author further away is reached along the
        # moment's return tokens (v_67), or the reaction stays here.
        router = self._reaction_router
        if router is not None:
            target = await router.direct_target(
                event.author_user_id, moment_id=event.moment_id
            )
            if target is None:
                await router.queue_reaction(
                    moment_id=event.moment_id,
                    reactor_user_id=event.reactor_user_id,
                    emoji=event.emoji,
                    occurred_at=event.occurred_at,
                )
                return
        else:
            target = await self._home_or_none(event.author_user_id)
        if target is None or target == self._federation.own_instance_id:
            return
        hidden = await self.hidden_for_peer(target)
        if event.reactor_user_id in hidden:
            return
        await self.send_to_instance(
            target,
            ev_type,
            {
                "moment_id": event.moment_id,
                "reactor_user_id": event.reactor_user_id,
                "author_user_id": event.author_user_id,
                "emoji": event.emoji,
                "occurred_at": event.occurred_at.isoformat(),
            },
        )

    # ── Reaction totals (v_69) ─────────────────────────────────────────

    async def fan_counts(
        self,
        payload: dict,
        *,
        author_user_id: str,
        reach: int | None = None,
    ) -> int:
        """Send a signed ``MOMENT_REACTIONS_COUNTED`` *payload* (hop 1) for a
        moment *author_user_id* wrote here to every confirmed v_69 peer —
        under the same hide list, guardian blocks and relay policy as the
        moment itself, and (v_96) the moment's *reach* gate: a reach-2
        moment's totals only to v_96 peers, as the moment went. Latest-only
        per (peer, moment) in the outbox. Returns how many peers it was
        handed to (0 when the policy holds it back)."""
        moment_id = str(payload.get("moment_id") or "")
        if not await self._policy_allows(
            source_instance_id=self._federation.own_instance_id,
            author_user_id=author_user_id,
            target_id=moment_id,
        ):
            return 0
        return await self._fan_to_peers(
            event_type=FederationEventType.MOMENT_REACTIONS_COUNTED,
            payload=payload,
            origin_instance_id=self._federation.own_instance_id,
            exclude_instances=set(),
            author_user_id=author_user_id,
            min_version=max(
                FederationCapability.MIN_FOR_MOMENT_REACTION_COUNTS,
                reach_min_version(reach) or 0,
            ),
            outbox_key=counts_outbox_key(moment_id),
        )

    # ── Relay (called by the inbound handler for every taken copy) ─────

    async def relay_inbound(
        self,
        *,
        event_type: FederationEventType,
        payload: dict,
        from_instance: str,
        held_reach: int | None = None,
        now_ms: int | None = None,
        removed_here: bool = False,
    ) -> int:
        """Re-broadcast an inbound moment envelope to *our* paired peers,
        bumping ``hop_count`` and excluding both the original origin
        and the immediate sender. No-op when the payload already hit
        ``MOMENT_MAX_HOPS``, OR when the source row arrived via a GFS
        public-share fan-out (§Momentum-public no-redistribute rule), OR
        (v_65) when it is a create signed more than
        :data:`MOMENT_RELAY_MAX_AGE_HOURS` ago. The inbound handler only
        calls this for a delete that changed something here.

        v_69 — a ``MOMENT_REACTIONS_COUNTED`` (totals) is relayed once per
        (moment, ``rev``) — the seen key carries the rev — only when this
        household holds the moment from the mesh within its reach (*held_reach*
        set: the held row's reach) or relayed its create, only while its
        ``rev`` is younger than :data:`COUNTS_RELAY_MAX_AGE_MS` (*now_ms*),
        and only to v_69 peers (v_96 when the moment's reach isn't 3 — an
        older household can't hold it through this relay).

        v_96 — forwards only while ``hop < min(reach, MOMENT_MAX_HOPS)``; a
        copy not sent by its origin counts as at least hop 2 (the relay
        clamp); an invalid reach is never relayed; a ``reach``-carrying copy
        goes only to v_96 peers.

        Per-author relay cap (owner 2026-11-24): see
        :meth:`_author_budget_allows`; *removed_here* — this delete wiped a
        live moment stored here (the inbound handler's tombstone outcome).

        Returns how many peers it was handed to (0: not relayed).
        """
        # No-redistribute rule for §Momentum-public: a moment that
        # arrived through a GFS public fan-out must not bleed back into
        # the household federation mesh. The sender flags this on the
        # payload so we can skip the relay here without consulting
        # local DB state on the inbound hot path.
        if str(payload.get("received_via") or "") == "gfs":
            return 0
        # v_38: a ``no_relay`` moment (a protected account's, or reach 1)
        # goes no further.
        if carries_no_relay(payload):
            return 0
        origin = str(payload.get("origin_instance_id") or "")
        # v_96 relay clamp (F1): a copy not sent by its origin is at least 2
        # hops away, whatever its unsigned ``hop_count`` says.
        hop = moment_hop_of(payload, from_instance=from_instance, origin=origin)
        if hop is None:
            return 0
        moment_id = str(payload.get("moment_id") or "")
        counts = event_type == FederationEventType.MOMENT_REACTIONS_COUNTED
        # v_96: the signed reach of a create / delete (``None``: refused);
        # for totals, the reach of the moment held or relayed here.
        reach = (
            self._counts_reach(payload, moment_id, held_reach, now_ms)
            if counts
            else moment_reach_of(payload)
        )
        if reach is None or hop >= min(reach, MOMENT_MAX_HOPS):
            return 0
        # v_65: a moment lives up to 30 days, so an old signed create is a
        # replay worth refusing to spread — kept here, not passed on past
        # 48 h after its signed ``occurred_at``.
        if event_type == FederationEventType.MOMENT_CREATED and moment_too_old_to_relay(
            payload.get("occurred_at")
        ):
            log.debug(
                "moment relay: create %s older than %d h — not relayed",
                payload.get("moment_id"),
                MOMENT_RELAY_MAX_AGE_HOURS,
            )
            return 0
        seen_key: tuple[str, ...] = (event_type.value, moment_id)
        min_version: int | None
        outbox_key: str | None = None
        if counts:
            seen_key = (event_type.value, moment_id, str(payload.get("rev")))
            # v_96 (S1): a moment whose reach isn't 3 went to v_96 households
            # only past us — an older one can't hold it through this relay.
            min_version = (
                FederationCapability.MIN_FOR_MOMENT_REACTION_COUNTS
                if reach == MOMENT_DEFAULT_REACH
                else FederationCapability.MIN_FOR_MOMENT_REACH
            )
            outbox_key = relayed_moment_key("counts", origin, moment_id)
        else:
            # v_96: a copy carrying a signed ``reach`` only to v_96
            # households — an older one verifies the v1 bytes only.
            min_version = (
                FederationCapability.MIN_FOR_MOMENT_REACH
                if REACH_FIELD in payload
                else None
            )
            if event_type == FederationEventType.MOMENT_CREATED:
                outbox_key = relayed_moment_key("create", origin, moment_id)
        copy = self._classify_copy(seen_key, hop, from_origin=from_instance == origin)
        if copy is _Copy.SEEN:
            return 0
        # §Momentum-relay-policy: don't relay a moment whose source
        # is on the household ban list, or whose author / moment is
        # under an open report. Layered on top of the no-redistribute
        # guard above.
        if not await self._policy_allows(
            source_instance_id=origin or from_instance,
            author_user_id=str(payload.get("author_user_id") or "") or None,
            target_id=str(payload.get("moment_id") or "") or None,
        ):
            return 0
        if copy is _Copy.FIRST and not self._author_budget_allows(
            event_type,
            payload,
            origin=origin,
            moment_id=moment_id,
            removed_here=removed_here,
        ):
            if event_type == FederationEventType.MOMENT_CREATED:
                # Its delete is charged too, even where the moment is stored.
                self._remember_relay(("capped", moment_id, origin), hop)
            return 0
        self._remember_relay(seen_key, hop, re_relayed=copy is _Copy.CLOSER)
        if event_type == FederationEventType.MOMENT_CREATED and origin:
            # v_69: which origin's create this was — totals for the moment
            # are passed on only when signed by that same origin; v_96: and
            # how far the moment goes, which bounds them.
            self._remember_relay(("origin", moment_id, origin), hop, reach=reach)
        next_hop = hop + 1
        look_hop: int | None = None
        for_delete = False
        if next_hop > MOMENT_UNGATED_HOPS:
            # v_96 decision C: a hop-4 / hop-5 copy (create, delete or
            # totals) goes only to a household that looks that far — and is
            # v_96 (an older one refuses a hop above 3), whatever its marks.
            look_hop = next_hop
            # A delete follows the farthest look a peer said since our start
            # (review F1: after its look fell, a follower there still reads
            # the moment) and also goes to a directly paired household whose
            # look we don't know yet (after our restart, before its
            # capabilities answer lands): the tombstone held here stops every
            # later copy, so skipping it would leave that household holding
            # the deleted moment for up to 30 days. Deletes are small and
            # content-free, and this only widens those windows.
            for_delete = event_type == FederationEventType.MOMENT_DELETED
            min_version = max(
                min_version or 0, FederationCapability.MIN_FOR_MOMENT_REACH
            )
        next_payload = dict(payload)
        next_payload["hop_count"] = next_hop
        router = self._reaction_router
        payload_for = None
        if router is not None and event_type == FederationEventType.MOMENT_CREATED:
            # v_67: each peer's copy carries our return token for it.
            def payload_for(peer_id: str) -> dict:
                return router.stamp_relayed_create(
                    next_payload,
                    received_hop=hop,  # clamped: tokens must match the hop
                    reach=reach,
                    from_instance=from_instance,
                    to_instance=peer_id,
                )

        return await self._fan_to_peers(
            event_type=event_type,
            payload=next_payload,
            origin_instance_id=origin,
            exclude_instances={from_instance},
            author_user_id=str(payload.get("author_user_id") or "") or None,
            payload_for=payload_for,
            min_version=min_version,
            outbox_key=outbox_key,
            look_hop=look_hop,
            for_delete=for_delete,
        )

    def _counts_reach(
        self,
        payload: dict,
        moment_id: str,
        held_reach: int | None,
        now_ms: int | None,
    ) -> int | None:
        """v_69: totals go on only from a household that holds the moment
        from the mesh within its reach (*held_reach* set, v_96) or relayed
        its create (a household that never passed the moment on has nobody
        downstream who holds it), and only while fresh. Returns the moment's
        reach (the held row's, else the relayed create's), or ``None``: not
        relayed."""
        origin = str(payload.get("origin_instance_id") or "")
        reach = (
            held_reach
            if held_reach is not None
            else self.relayed_create_reach(moment_id, origin)
        )
        if reach is None:
            log.debug(
                "moment relay: totals for %s — moment neither held nor relayed",
                moment_id,
            )
            return None
        rev = payload.get("rev")
        now = int(time.time() * 1000) if now_ms is None else now_ms
        if not isinstance(rev, int) or rev < now - COUNTS_RELAY_MAX_AGE_MS:
            log.debug("moment relay: totals for %s too old — not relayed", moment_id)
            return None
        return reach

    def _author_budget_allows(
        self,
        event_type: FederationEventType,
        payload: dict,
        *,
        origin: str,
        moment_id: str,
        removed_here: bool,
    ) -> bool:
        """The per-author relay cap: whether a moment's first forward from
        here still fits its signed (origin, author)'s budget — charged when
        it does (even if no peer turns out to take it).

        A top-level create follows the posting rate limit, signed: at most
        :data:`MOMENT_RELAY_PER_RATE_WINDOW` per :data:`MOMENT_RATE_WINDOW`
        slot of its ``occurred_at`` (one signed further ahead than the
        envelope skew is never relayed). Replies (not rate-limited at the
        origin) get :data:`MOMENT_RELAY_AUTHOR_CAP` an hour of arrival.

        Charged: every first forward of a create (held here or not — a
        stored copy is no licence to forward), and — on a budget of its own,
        so posting never starves deletes — a delete of a moment this
        household neither relayed nor stored (one for a made-up id is new
        everywhere, so it would otherwise travel uncapped) or whose create it
        capped. Free: a delete of a moment whose create it relayed lately or
        that wiped a moment stored here (*removed_here*; a delete must
        follow its moment — a dropped one is never resent), and totals
        (budgeted per origin by every receiver, relayed only for a moment
        held or relayed here). Over the cap: dropped, logged once per
        window, nothing said to the sender."""
        if event_type == FederationEventType.MOMENT_REACTIONS_COUNTED:
            return True
        if event_type == FederationEventType.MOMENT_DELETED and (
            self.relayed_create(moment_id, origin)
            or (removed_here and not self._capped_here(moment_id, origin))
        ):
            return True
        author = str(payload.get("author_user_id") or "")
        if event_type == FederationEventType.MOMENT_DELETED:
            kind = "deletes"
        elif payload.get("parent_moment_id"):
            kind = "replies"
        else:
            kind = "top-level moments"
            slot = moment_rate_slot(payload.get("occurred_at"))
            if slot is None:
                log.debug("moment relay: %s signed in the future", moment_id[:64])
                return False
            if self._slots.is_allowed(
                _budget_key("t", origin, author, str(slot)),
                limit=MOMENT_RELAY_PER_RATE_WINDOW,
                window_s=MOMENT_RELAY_SLOT_TTL_S,
            ):
                return True
            return self._log_capped(kind, origin, author)
        if self._author_budget.is_allowed(
            _budget_key(kind[0], origin, author),
            limit=MOMENT_RELAY_AUTHOR_CAP,
            window_s=MOMENT_RELAY_AUTHOR_WINDOW_S,
        ):
            return True
        return self._log_capped(kind, origin, author)

    def _log_capped(self, kind: str, origin: str, author: str) -> bool:
        """Log a capped author at most once a window per kind; ``False``."""
        if self._cap_logged.is_allowed(
            _budget_key(kind, origin, author),
            limit=1,
            window_s=MOMENT_RELAY_AUTHOR_WINDOW_S,
        ):
            log.info(
                "moment relay cap: author %r (origin %r) over the %s cap"
                " — not relaying more of theirs for now",
                author[:64],
                origin[:64],
                kind,
            )
        return False

    def _capped_here(self, moment_id: str, origin: str) -> bool:
        """Whether this household refused to forward *moment_id*'s create
        (the author cap) lately."""
        held = self._relayed.get(("capped", moment_id, origin))
        return held is not None and monotonic() - held.at <= RELAY_SEEN_TTL_S

    def relayed_create(self, moment_id: str, origin: str) -> bool:
        """Whether this household relayed *moment_id*'s create, as *origin*'s
        moment, lately (the seen-map entry still lives)."""
        return self.relayed_create_reach(moment_id, origin) is not None

    def relayed_create_reach(self, moment_id: str, origin: str) -> int | None:
        """The reach of *moment_id* when this household relayed its create,
        as *origin*'s moment, lately — else ``None``."""
        held = self._relayed.get(("origin", moment_id, origin))
        if held is None or monotonic() - held.at > RELAY_SEEN_TTL_S:
            return None
        return held.reach if held.reach is not None else MOMENT_DEFAULT_REACH

    # ── Relay seen-map ─────────────────────────────────────────────────

    def _classify_copy(
        self, key: tuple[str, ...], hop: int, *, from_origin: bool
    ) -> _Copy:
        """What a copy arriving at ``hop`` is: :attr:`_Copy.FIRST` (never
        relayed, or the entry aged out), :attr:`_Copy.CLOSER` (relayed only
        from further away — a closer copy, relayed on, reaches more hops),
        or :attr:`_Copy.SEEN`. v_96 (F8): a closer copy is relayed again at
        most once — a relay claiming hops 4, 3, 2 … gets one extra wave, not
        r − 1 — except one the origin itself sent (envelope-authenticated
        hop 1), which can't be faked and so can't burn the slot, and the
        first copy that reaches every household (forwarded at a hop up to
        :data:`MOMENT_UNGATED_HOPS`) after only gated ones (forwarded past
        it, to who looks that far): a gated wave never uses up the ungated
        one. So a relay sends at most two gated and two ungated waves."""
        held = self._relayed.get(key)
        if held is None:
            return _Copy.FIRST
        if monotonic() - held.at > RELAY_SEEN_TTL_S:
            del self._relayed[key]
            return _Copy.FIRST
        if hop >= held.hop:
            return _Copy.SEEN
        if from_origin or not held.re_relayed:
            return _Copy.CLOSER
        if hop + 1 <= MOMENT_UNGATED_HOPS < held.hop + 1:
            return _Copy.CLOSER
        return _Copy.SEEN

    def _remember_relay(
        self,
        key: tuple[str, ...],
        hop: int,
        *,
        re_relayed: bool = False,
        reach: int | None = None,
    ) -> None:
        self._relayed[key] = _Relayed(
            hop=hop, at=monotonic(), re_relayed=re_relayed, reach=reach
        )
        self._relayed.move_to_end(key)
        while len(self._relayed) > RELAY_SEEN_MAX:
            self._relayed.popitem(last=False)

    # ── Helpers ────────────────────────────────────────────────────────

    def _origin_signed(
        self,
        event_type: FederationEventType,
        payload: dict,
    ) -> dict:
        """Attach this household's origin signature (v_35).

        Relays forward the payload verbatim, so every household down the
        mesh (up to ``MOMENT_MAX_HOPS``, bounded by the signed reach) can check the moment against its origin rather than
        taking the relay's ``origin_instance_id`` on trust. Always signed,
        never gated on the peer's version: the fields are additive (older
        receivers ignore them) and the relay targets are households we
        often hold no row for. One Ed25519 signature per moment — cheap
        enough to run inline.
        """
        return sign_moment_origin(
            seed=self._federation.own_identity_seed,
            identity_pk=self._federation.own_identity_pk,
            event_type=event_type,
            payload=payload,
        )

    async def _fan_to_peers(
        self,
        *,
        event_type: FederationEventType,
        payload: dict,
        origin_instance_id: str,
        exclude_instances: set[str],
        author_user_id: str | None = None,
        min_version: int | None = None,
        payload_for: Callable[[str], dict] | None = None,
        outbox_key: str | None = None,
        look_hop: int | None = None,
        for_delete: bool = False,
    ) -> int:
        """Send to every confirmed peer but *exclude_instances* and the
        origin; returns how many it was handed to. With *look_hop* (a relayed
        copy at hop 4 or 5, v_96 decision C) only to a peer whose people look
        at least that far (``FederationService.peer_moment_look_hops``; a peer
        not heard from since our start gets none — unless *for_delete*: a
        delete goes by the farthest look said since our start
        (``peer_moment_look_high``), and to a directly paired one whose look
        is still unknown, the households a look is learned from)."""
        # ``confirmed_peers`` already drops our own instance + null ids;
        # the relay/origin skip set adds the origin + the immediate sender.
        skip = exclude_instances | {origin_instance_id}
        sent = 0
        hidden_per_peer: dict[str, frozenset[str]] = {}
        for peer in await self.confirmed_peers():
            instance_id = peer.id
            if instance_id in skip:
                continue
            if look_hop is not None:
                look = (
                    self._federation.peer_moment_look_high(instance_id)
                    if for_delete
                    else self._federation.peer_moment_look_hops(instance_id)
                )
                if look is None:
                    if not (for_delete and peer.source is InstanceSource.MANUAL):
                        continue
                elif look < look_hop:
                    continue
            if min_version is not None and not await self._federation.peer_supports(
                instance_id, min_version=min_version
            ):
                continue
            if author_user_id is not None and await self._guardian_blocks_household(
                author_user_id, instance_id
            ):
                # §CP.F2: a guardian blocked someone homed there.
                continue
            if author_user_id is not None:
                if instance_id not in hidden_per_peer:
                    hidden_per_peer[instance_id] = await self.hidden_for_peer(
                        instance_id
                    )
                if author_user_id in hidden_per_peer[instance_id]:
                    continue
            await self.send_to_instance(
                instance_id,
                event_type,
                payload if payload_for is None else payload_for(instance_id),
                outbox_key=outbox_key,
            )
            sent += 1
        return sent

    async def _is_local_user(self, user_id: str) -> bool:
        return await self._home_or_none(user_id) == self._federation.own_instance_id

    async def _home_or_none(self, user_id: str) -> str | None:
        try:
            return await self._user_repo.get_instance_for_user(user_id)
        except Exception as exc:  # pragma: no cover — defensive
            log.debug("moment-outbound: user lookup failed: %s", exc, exc_info=True)
            return None

    async def _policy_allows(
        self,
        *,
        source_instance_id: str | None,
        author_user_id: str | None,
        target_id: str | None,
    ) -> bool:
        """Defer to :class:`RelayPolicy` when one is wired; default
        allow when no policy is attached (legacy callers / tests)."""
        if self._relay_policy is None:
            return True
        return await self._relay_policy.allow_relay(
            source_instance_id=source_instance_id or "",
            author_user_id=author_user_id,
            target_id=target_id,
        )
