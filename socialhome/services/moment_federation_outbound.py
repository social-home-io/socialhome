"""Outbound federation for the Momentum pillar (§Momentum).

Subscribes to :class:`MomentCreated`, :class:`MomentDeleted`,
:class:`MomentReactionChanged` on the bus and translates each into
the matching ``MOMENT_*`` envelope. Two distinct fan-out paths share
the code in this file:

1. **Origin fan-out.** The author's instance fans the moment to every
   confirmed peer with ``hop_count = 1`` and
   ``origin_instance_id = self``.

2. **Relay fan-out** (up to 3 hops total). When an inbound
   ``MOMENT_*`` envelope lands on an instance and ``hop_count < 3``,
   :meth:`relay_inbound` re-fans the same payload to the local paired
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

**Reactions further away (v_67).** A reaction is unicast to the author's
household when it is this one or a paired one. Otherwise the
:class:`~socialhome.services.moment_reaction_router.MomentReactionRouter`
carries it back along the moment's return tokens; this service adds the
origin's key-wrap key to every create it authors and each relay's return
token to every create it relays (per peer).
"""

from __future__ import annotations

import logging
import time
from collections import OrderedDict
from collections.abc import Callable
from time import monotonic
from typing import TYPE_CHECKING

from ..domain.events import (
    MomentCreated,
    MomentDeleted,
    MomentReactionChanged,
)
from ..domain.federation import FederationEventType
from ..domain.federation_capabilities import FederationCapability
from ..domain.federation_retention import relayed_moment_key
from ..domain.moment import (
    MOMENT_DEFAULT_REACH,
    MOMENT_MAX_HOPS,
    MOMENT_MAX_REACH,
    MOMENT_RELAY_MAX_AGE_HOURS,
    moment_too_old_to_relay,
)
from ..infrastructure.event_bus import EventBus
from ..moment_counts import MAX_COUNTS_REV_AGE_MS
from ..moment_origin import (
    NO_RELAY_FIELD,
    REACH_FIELD,
    carries_no_relay,
    moment_reach_of,
    sign_moment_origin,
)
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
RELAY_SEEN_MAX: int = 50_000

#: v_69 — a household relays a moment's totals only while they are this
#: fresh (by their signed ``rev``): a day of outbox retries plus clock skew.
#: Older totals are a replay — neither kept nor passed on (the receiver
#: refuses them before any budget, :data:`~socialhome.moment_counts
#: .MAX_COUNTS_REV_AGE_MS`).
COUNTS_RELAY_MAX_AGE_MS: int = MAX_COUNTS_REV_AGE_MS


def counts_outbox_key(moment_id: str) -> str:
    """The latest-only outbox key of a moment's totals: a newer update
    replaces one still queued for an offline peer (local, never on the
    wire)."""
    return f"moment-counts:{moment_id}"


def reach_marks(reach: int) -> tuple[dict[str, object], int | None]:
    """v_96 — what the origin puts under its signature for a moment (or
    delete) of *reach* hops, and the lowest peer version it may go to:
    reach 1 → ``no_relay`` (v_38), 2 → ``reach: 2`` (v_96), 3 → nothing
    (every peer), 4–5 → ``reach: N`` (every peer — a direct delivery needs
    no origin check, an older relay passes it on up to hop 3)."""
    reach = max(1, min(int(reach), MOMENT_MAX_REACH))
    if reach == 1:
        return {NO_RELAY_FIELD: True}, FederationCapability.MIN_FOR_MOMENT_NO_RELAY
    if reach == MOMENT_DEFAULT_REACH:
        return {}, None
    if reach == 2:
        return {REACH_FIELD: 2}, FederationCapability.MIN_FOR_MOMENT_REACH
    return {REACH_FIELD: reach}, None


def _max_version(*versions: int | None) -> int | None:
    """The strictest of several version gates (``None`` = no gate)."""
    present = [v for v in versions if v is not None]
    return max(present) if present else None


class MomentFederationOutbound(
    VisibilityMixin,
    ConfirmedPeerBroadcaster,
    SingleTargetSender,
    ProtectionGateMixin,
):
    """Publish moment mutations to the 3-hop peer mesh."""

    __slots__ = (
        "_bus",
        "_child_protection",
        "_federation",
        "_federation_repo",
        "_reaction_router",
        "_relay_policy",
        "_relayed",
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
        #: (event type, moment id) → (lowest hop we relayed it at,
        #: monotonic time, extra). In a mesh a household gets one copy of a
        #: moment per relaying neighbour; only the first — or, once (v_96,
        #: F8), one from closer to the origin, which reaches further — is
        #: relayed on; ``extra`` counts that closer re-relay. Bounded by
        #: :data:`RELAY_SEEN_MAX` and :data:`RELAY_SEEN_TTL_S`; lost on
        #: restart (a copy arriving after one is relayed once more). v_69
        #: totals are remembered per (type, moment id, rev); a relayed
        #: create's ``("origin", moment id, origin)`` entry keeps the
        #: moment's reach (v_96) as ``extra``.
        self._relayed: OrderedDict[tuple[str, ...], tuple[int, float, int]] = (
            OrderedDict()
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
        # too old to honour it (it would relay it onward). §CP.R (v_38): a
        # protected account's moments reach our directly paired households
        # only, whatever the event says (reach 1, ``no_relay``).
        reach = 1 if await self._is_protected(event.author_user_id) else event.reach
        marks, min_version = reach_marks(reach)
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
            min_version=min_version,
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
        # v_96: the delete goes exactly as far as the moment did — the same
        # signed mark (reach 1: ``no_relay``, so a protected account's delete
        # is never relayed) under the same version gate. A protected
        # account's row written before v_96 reads as reach 3, but its create
        # went ``no_relay``: protection still means one hop.
        protected = await self._is_protected(event.author_user_id)
        marks, min_version = reach_marks(1 if protected else event.reach)
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
        reach: int = MOMENT_DEFAULT_REACH,
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
            min_version=_max_version(
                FederationCapability.MIN_FOR_MOMENT_REACTION_COUNTS,
                reach_marks(reach)[1],
            ),
            outbox_key=counts_outbox_key(moment_id),
        )

    # ── Relay (called by the inbound handler when hop_count < 3) ───────

    async def relay_inbound(
        self,
        *,
        event_type: FederationEventType,
        payload: dict,
        from_instance: str,
        holds_moment: bool = False,
        held_reach: int | None = None,
        now_ms: int | None = None,
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
        household holds the moment from the mesh within its reach
        (*holds_moment*, the row's *held_reach*) or relayed its create, only
        while its ``rev`` is younger than :data:`COUNTS_RELAY_MAX_AGE_MS`
        (*now_ms*), and only to v_69 peers (v_96 for a reach-2 moment).

        v_96 — forwards only while ``hop < min(reach, MOMENT_MAX_HOPS)``; a
        copy not sent by its origin counts as at least hop 2 (the relay
        clamp); an invalid reach is never relayed; a ``reach``-carrying copy
        goes only to v_96 peers.

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
        counts = event_type == FederationEventType.MOMENT_REACTIONS_COUNTED
        # v_96: the signed reach of a create / delete; ``None`` is refused.
        reach = None if counts else moment_reach_of(payload)
        if reach is None and not counts:
            return 0
        try:
            hop = int(payload.get("hop_count") or 0)
        except TypeError, ValueError:
            hop = 0
        if hop <= 0:
            return 0
        origin = str(payload.get("origin_instance_id") or "")
        if from_instance != origin:
            # v_96 relay clamp (F1): ``hop_count`` is outside the signature —
            # a copy not sent by its origin is at least 2 hops away.
            hop = max(hop, 2)
        if hop >= MOMENT_MAX_HOPS or (reach is not None and hop >= reach):
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
        moment_id = str(payload.get("moment_id") or "")
        seen_key: tuple[str, ...] = (event_type.value, moment_id)
        # v_96: a copy carrying a signed ``reach`` only to v_96 households —
        # an older one verifies the v1 bytes only.
        min_version: int | None = (
            FederationCapability.MIN_FOR_MOMENT_REACH
            if REACH_FIELD in payload
            else None
        )
        outbox_key: str | None = None
        if event_type == FederationEventType.MOMENT_CREATED:
            outbox_key = relayed_moment_key("create", origin, moment_id)
        if counts:
            reach = self._counts_reach(
                payload, moment_id, holds_moment, held_reach, now_ms
            )
            if reach is None or hop >= reach:
                return 0
            seen_key = (event_type.value, moment_id, str(payload.get("rev")))
            min_version = _max_version(
                FederationCapability.MIN_FOR_MOMENT_REACTION_COUNTS,
                reach_marks(reach)[1],
            )
            outbox_key = relayed_moment_key("counts", origin, moment_id)
        extra = self._relay_extra(seen_key, hop)
        if extra is None:
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
        self._remember_relay(seen_key, hop, extra)
        if event_type == FederationEventType.MOMENT_CREATED and origin:
            # v_69: which origin's create this was — totals for the moment
            # are passed on only when signed by that same origin; v_96: and
            # how far the moment goes, which bounds them.
            self._remember_relay(
                ("origin", moment_id, origin), hop, reach or MOMENT_DEFAULT_REACH
            )
        next_payload = dict(payload)
        next_payload["hop_count"] = hop + 1
        router = self._reaction_router
        payload_for = None
        if router is not None and event_type == FederationEventType.MOMENT_CREATED:
            # v_67: each peer's copy carries our return token for it.
            def payload_for(peer_id: str) -> dict:
                return router.stamp_relayed_create(
                    next_payload,
                    received_hop=hop,  # clamped: tokens must match the hop
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
        )

    def _counts_reach(
        self,
        payload: dict,
        moment_id: str,
        holds_moment: bool,
        held_reach: int | None,
        now_ms: int | None,
    ) -> int | None:
        """v_69: totals go on only from a household that holds the moment
        from the mesh (within its reach, v_96 — *holds_moment*) or relayed
        its create (a household that never passed the moment on has nobody
        downstream who holds it), and only while fresh. Returns the moment's
        reach (the held row's, else the relayed create's), or ``None``: not
        relayed."""
        origin = str(payload.get("origin_instance_id") or "")
        reach = (
            (held_reach or MOMENT_DEFAULT_REACH)
            if holds_moment
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

    def relayed_create(self, moment_id: str, origin: str) -> bool:
        """Whether this household relayed *moment_id*'s create, as *origin*'s
        moment, lately (the seen-map entry still lives)."""
        return self.relayed_create_reach(moment_id, origin) is not None

    def relayed_create_reach(self, moment_id: str, origin: str) -> int | None:
        """The reach of *moment_id* when this household relayed its create,
        as *origin*'s moment, lately — else ``None``."""
        held = self._relayed.get(("origin", moment_id, origin))
        if held is None or monotonic() - held[1] > RELAY_SEEN_TTL_S:
            return None
        return held[2]

    # ── Relay seen-map ─────────────────────────────────────────────────

    def _relay_extra(self, key: tuple[str, ...], hop: int) -> int | None:
        """Whether a copy arriving at ``hop`` is worth relaying: never
        relayed (or the entry aged out) → ``0``; relayed only from further
        away and not re-relayed yet — a closer copy, relayed on, reaches
        more hops — → ``1`` (v_96, F8: at most one extra wave, so a relay
        claiming hops 4, 3, 2 … can't make us send r − 1); else ``None``."""
        held = self._relayed.get(key)
        if held is None:
            return 0
        relayed_hop, at, closer = held
        if monotonic() - at > RELAY_SEEN_TTL_S:
            del self._relayed[key]
            return 0
        return 1 if hop < relayed_hop and closer == 0 else None

    def _remember_relay(self, key: tuple[str, ...], hop: int, extra: int = 0) -> None:
        self._relayed[key] = (hop, monotonic(), extra)
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
        3-hop mesh can check the moment against its origin rather than
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
    ) -> int:
        """Send to every confirmed peer but *exclude_instances* and the
        origin; returns how many it was handed to."""
        # ``confirmed_peers`` already drops our own instance + null ids;
        # the relay/origin skip set adds the origin + the immediate sender.
        skip = exclude_instances | {origin_instance_id}
        sent = 0
        hidden_per_peer: dict[str, frozenset[str]] = {}
        for peer in await self.confirmed_peers():
            instance_id = peer.id
            if instance_id in skip:
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
