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
   peers, excluding the origin instance and the immediate sender.
   Receivers dedupe by ``moment.id`` (the row's PRIMARY KEY makes the
   second save a no-op).

**Echo-loop guard.** Bus events fired by inbound handlers are
indistinguishable from local writes by event-class identity alone, so
the subscriber gates each event on "is the *actor* (author for create
/ delete, reactor for reactions) local on this instance?" — only the
local actor's instance fans on the bus path. The relay path is
explicit (not bus-driven) and runs from inside the inbound handler
before it republishes anything.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from ..domain.events import (
    MomentCreated,
    MomentDeleted,
    MomentReactionChanged,
)
from ..domain.federation import FederationEventType
from ..domain.moment import MOMENT_MAX_HOPS
from ..federation.moment_origin import sign_moment_origin
from ..infrastructure.event_bus import EventBus
from .peer_outbound import ConfirmedPeerBroadcaster, SingleTargetSender
from .protection_gate import ProtectionGateMixin
from .visibility import VisibilityMixin

if TYPE_CHECKING:
    from ..federation.federation_service import FederationService
    from ..repositories.federation_repo import AbstractFederationRepo
    from ..repositories.peer_user_visibility_repo import AbstractPeerUserVisibilityRepo
    from ..repositories.user_repo import AbstractUserRepo
    from .relay_policy import RelayPolicy

log = logging.getLogger(__name__)


class MomentFederationOutbound(
    VisibilityMixin,
    ConfirmedPeerBroadcaster,
    SingleTargetSender,
    ProtectionGateMixin,
):
    """Publish moment mutations to the 3-hop peer mesh."""

    __slots__ = (
        "_child_protection",
        "_bus",
        "_federation",
        "_federation_repo",
        "_user_repo",
        "_relay_policy",
    )

    # Narrow the mixins' optional ``_federation`` — this service requires it
    # at construction, so direct attribute access elsewhere is non-None.
    _federation: "FederationService"

    def __init__(
        self,
        *,
        bus: EventBus,
        federation_service: "FederationService",
        federation_repo: "AbstractFederationRepo",
        user_repo: "AbstractUserRepo",
        relay_policy: "RelayPolicy | None" = None,
        visibility_repo: "AbstractPeerUserVisibilityRepo | None" = None,
    ) -> None:
        self._bus = bus
        self._federation = federation_service
        self._federation_repo = federation_repo
        self._user_repo = user_repo
        self._relay_policy = relay_policy
        self._visibility_repo = visibility_repo
        self._child_protection = None

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
        await self._fan_to_peers(
            event_type=FederationEventType.MOMENT_CREATED,
            payload=self._origin_signed(
                FederationEventType.MOMENT_CREATED,
                {
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
                },
            ),
            origin_instance_id=event.origin_instance_id,
            exclude_instances=set(),
            author_user_id=event.author_user_id,
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
                },
            ),
            origin_instance_id=event.origin_instance_id,
            exclude_instances=set(),
            author_user_id=event.author_user_id,
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
        # the Highlights back-channel.)
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

    # ── Relay (called by the inbound handler when hop_count < 3) ───────

    async def relay_inbound(
        self,
        *,
        event_type: FederationEventType,
        payload: dict,
        from_instance: str,
    ) -> None:
        """Re-broadcast an inbound moment envelope to *our* paired peers,
        bumping ``hop_count`` and excluding both the original origin
        and the immediate sender. No-op when the payload already hit
        ``MOMENT_MAX_HOPS``, OR when the source row arrived via a GFS
        public-share fan-out (§Momentum-public no-redistribute rule).
        """
        # No-redistribute rule for §Momentum-public: a moment that
        # arrived through a GFS public fan-out must not bleed back into
        # the household federation mesh. The sender flags this on the
        # payload so we can skip the relay here without consulting
        # local DB state on the inbound hot path.
        if str(payload.get("received_via") or "") == "gfs":
            return
        try:
            hop = int(payload.get("hop_count") or 0)
        except TypeError, ValueError:
            hop = 0
        if hop <= 0 or hop >= MOMENT_MAX_HOPS:
            return
        origin = str(payload.get("origin_instance_id") or "")
        # §Momentum-relay-policy: don't relay a moment whose source
        # is on the household ban list, or whose author / moment is
        # under an open report. Layered on top of the no-redistribute
        # guard above.
        if not await self._policy_allows(
            source_instance_id=origin or from_instance,
            author_user_id=str(payload.get("author_user_id") or "") or None,
            target_id=str(payload.get("moment_id") or "") or None,
        ):
            return
        next_payload = dict(payload)
        next_payload["hop_count"] = hop + 1
        await self._fan_to_peers(
            event_type=event_type,
            payload=next_payload,
            origin_instance_id=origin,
            exclude_instances={from_instance},
            author_user_id=str(payload.get("author_user_id") or "") or None,
        )

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
    ) -> None:
        # ``confirmed_peers`` already drops our own instance + null ids;
        # the relay/origin skip set adds the origin + the immediate sender.
        skip = exclude_instances | {origin_instance_id}
        hidden_per_peer: dict[str, frozenset[str]] = {}
        for peer in await self.confirmed_peers():
            instance_id = peer.id
            if instance_id in skip:
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
            await self.send_to_instance(instance_id, event_type, payload)

    async def _is_local_user(self, user_id: str) -> bool:
        return await self._home_or_none(user_id) == self._federation.own_instance_id

    async def _home_or_none(self, user_id: str) -> str | None:
        try:
            return await self._user_repo.get_instance_for_user(user_id)
        except Exception as exc:  # pragma: no cover — defensive
            log.debug("moment-outbound: user lookup failed: %s", exc)
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
