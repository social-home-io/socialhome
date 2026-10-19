"""Outbound peer-public-space directory sync (§D1a).

Fans a ``SPACE_DIRECTORY_SYNC`` envelope to every CONFIRMED peer whenever
one of our ``type=public`` spaces changes (create / update / dissolve),
and sends a one-shot snapshot on pair confirmation so new peers don't
wait for the next change to populate their directory.

Payload carries a full snapshot — it's cheap (expected < ~50 public
spaces per household) and lets the receiver atomically replace its
cached rows without tracking deltas.

**One snapshot per peer.** A local community's entry carries
``local_community: true``, its ``radius_km`` and ``in_area`` — this
household's verdict for THAT peer's home, the one it already chose to share
with us (``true`` / ``false``, ``null`` when it shares none) — and never the
centre: people's locations are not shared by design (owner decision
2026-10-19).

The verdict cannot be used to find the centre
(:func:`~socialhome.domain.local_community.directory_verdict`): near the rim
it is "unsure" (``in_area: null``, ``area_edge: true``), computed per grid
cell with a band shift only this household knows (:func:`~socialhome.
services.local_community.edge_shift_km`); and a peer whose shared home
keeps moving gets new answers for at most
:data:`MAX_VERDICT_HOMES_PER_DAY` homes a day — further moves are answered
for the last home taken, so a walk tells it nothing new. Withdrawing the
home always takes effect at once.

Each peer's payload is built for it alone, so no household ever receives
another household's verdict. Only to a peer at v_59+
(:attr:`~FederationCapability.MIN_FOR_LOCAL_COMMUNITY_AREA`); an older peer
gets the entry without the fields. The verdict is recomputed whenever the
snapshot is sent: on any public-space change, on a new pairing, when the
peer's shared home changes (:class:`PeerHomeChanged`, including it turning
sharing off) and when it first advertises v_59. Only paired (social) peers
get a snapshot at all; never a GFS, never a household met through an
invite link.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from ..domain.events import (
    PairingConfirmed,
    PeerHomeChanged,
    PeerProtoVersionRaised,
    SpaceConfigChanged,
    SpaceLocalAreaChanged,
)
from ..domain.federation import FederationEventType
from ..domain.federation_capabilities import FederationCapability
from ..domain.local_community import (
    Eligibility,
    LocalArea,
    directory_verdict,
    local_area_of,
    local_community_wire,
)
from ..domain.space import SpaceType, normalize_category
from ..infrastructure.event_bus import EventBus
from .local_community import (
    MAX_VERDICT_HOMES_PER_DAY,
    LocalAreaBudget,
    edge_shift_km,
    household_home,
)

if TYPE_CHECKING:
    from ..federation.federation_service import FederationService
    from ..repositories.federation_repo import AbstractFederationRepo
    from ..repositories.space_repo import AbstractSpaceRepo

log = logging.getLogger(__name__)

__all__ = ["MAX_VERDICT_HOMES_PER_DAY", "PeerDirectoryService"]


class PeerDirectoryService:
    """Outbound fan-out for :data:`FederationEventType.SPACE_DIRECTORY_SYNC`."""

    __slots__ = (
        "_budget",
        "_bus",
        "_edge_key",
        "_federation",
        "_federation_repo",
        "_space_repo",
        "_verdict_homes",
    )

    def __init__(
        self,
        *,
        bus: EventBus,
        federation_service: FederationService,
        federation_repo: AbstractFederationRepo,
        space_repo: AbstractSpaceRepo,
        edge_key: bytes,
        budget: LocalAreaBudget,
    ) -> None:
        self._bus = bus
        self._federation = federation_service
        self._federation_repo = federation_repo
        self._space_repo = space_repo
        #: The secret the "unsure" band's per-cell shift is drawn with
        #: (:func:`~socialhome.services.local_community.derive_edge_key`).
        self._edge_key = edge_key
        #: Counts each peer's newly taken homes per UTC day, in SQLite.
        self._budget = budget
        #: Per peer: the shared home its verdicts were last computed for.
        #: In memory: after a restart a peer already at its day's limit
        #: gets "unknown" verdicts (fail closed) until the next UTC day.
        self._verdict_homes: dict[str, tuple[float, float]] = {}

    def wire(self) -> None:
        self._bus.subscribe(SpaceConfigChanged, self._on_config_changed)
        self._bus.subscribe(SpaceLocalAreaChanged, self._on_local_area_changed)
        self._bus.subscribe(PairingConfirmed, self._on_pair_confirmed)
        self._bus.subscribe(PeerHomeChanged, self._on_peer_home_changed)
        self._bus.subscribe(PeerProtoVersionRaised, self._on_proto_raised)

    async def _on_config_changed(self, _event: SpaceConfigChanged) -> None:
        """Any space-config change can affect the public-spaces snapshot
        (name, description, emoji, join_mode, space_type). Broadcast the
        full current snapshot to every paired peer.
        """
        await self._broadcast_snapshot()

    async def _on_local_area_changed(self, _event: SpaceLocalAreaChanged) -> None:
        """A local community's area moved or went (host-local, so no
        ``SpaceConfigChanged``): re-send the snapshot."""
        await self._broadcast_snapshot()

    async def _on_pair_confirmed(self, event: PairingConfirmed) -> None:
        """A new pair just went live — push our current snapshot so the
        peer's browser doesn't wait for the next change."""
        await self.send_snapshot(event.instance_id)

    async def _on_peer_home_changed(self, event: PeerHomeChanged) -> None:
        """The home a peer shares with us moved, appeared or was withdrawn:
        its verdicts may have changed — re-send ITS snapshot only."""
        if await self._has_local_community():
            await self.send_snapshot(event.instance_id)

    async def _on_proto_raised(self, event: PeerProtoVersionRaised) -> None:
        """A peer just reached v_59: it has had its entries without the
        local-community fields until now."""
        floor = FederationCapability.MIN_FOR_LOCAL_COMMUNITY_AREA
        if event.old_version < floor <= event.new_version:
            await self.send_snapshot(event.instance_id)

    async def _has_local_community(self) -> bool:
        spaces = await self._space_repo.list_by_type(SpaceType.PUBLIC)
        return any(local_area_of(s) is not None for s in spaces)

    async def _verdict_home(
        self, peer_id: str, home: tuple[float, float] | None
    ) -> tuple[float, float] | None:
        """The home ``peer_id``'s verdicts are computed for: its shared
        ``home``, unless it already moved :data:`MAX_VERDICT_HOMES_PER_DAY`
        times today (UTC) — then the last home taken, or none (unknown)
        when it withdrew that one or this process has not seen it.
        ``None`` (sharing withdrawn) always takes effect but keeps the day's
        count, so turning sharing off and on again is no way around it."""
        if home is None:
            self._verdict_homes.pop(peer_id, None)
            return None
        last = self._verdict_homes.get(peer_id)
        if last == home:
            return home
        if not await self._budget.take_verdict_home(peer_id):
            log.info(
                "peer_directory: %s moved its shared home too often today; "
                "local-community verdicts stay on the last home taken",
                peer_id,
            )
            return last
        self._verdict_homes[peer_id] = home
        return home

    async def _build_snapshot(self, peer_id: str) -> list[dict]:
        """The snapshot as ``peer_id`` receives it — with ITS verdict on
        each local community (v_59+), never anyone else's, never a centre."""
        spaces = await self._space_repo.list_by_type(SpaceType.PUBLIC)
        with_area = await self._federation.peer_supports(
            peer_id, min_version=FederationCapability.MIN_FOR_LOCAL_COMMUNITY_AREA
        )
        home: tuple[float, float] | None = None
        if with_area and any(local_area_of(s) is not None for s in spaces):
            # The home THIS peer shares with us (``None`` when it shares
            # none) — read once per snapshot.
            home = await self._verdict_home(
                peer_id,
                await household_home(
                    self._federation_repo, peer_id, own_instance_id=None
                ),
            )
        out: list[dict] = []
        for s in spaces:
            entry: dict[str, object] = {
                "space_id": s.id,
                "name": s.name,
                "description": s.description,
                "emoji": s.emoji,
                # Members, never followers (``MEMBER_COUNT_SQL``).
                "member_count": await self._space_repo.count_members(s.id),
                "join_mode": s.join_mode.value,
                "category": normalize_category(s.category),
            }
            area: LocalArea | None = local_area_of(s) if with_area else None
            if area is not None:
                verdict = (
                    directory_verdict(
                        area,
                        home,
                        shift_km=edge_shift_km(self._edge_key, s.id, area, home),
                    )
                    if home is not None
                    else Eligibility.HOME_UNKNOWN
                )
                entry.update(local_community_wire(area, verdict))
            out.append(entry)
        return out

    async def _send(self, peer_id: str) -> None:
        try:
            await self._federation.send_event(
                to_instance_id=peer_id,
                event_type=FederationEventType.SPACE_DIRECTORY_SYNC,
                payload={"spaces": await self._build_snapshot(peer_id)},
            )
        except Exception as exc:  # pragma: no cover — defensive
            log.debug(
                "peer_directory: send snapshot to %s failed: %s",
                peer_id,
                exc,
                exc_info=True,
            )

    async def _broadcast_snapshot(self) -> None:
        # §D2b: social surface — a ``space_session`` row (a household we
        # only share a space with, via an invite link) is not a social
        # peer, so read the social list, not every CONFIRMED row.
        for peer in await self._federation_repo.list_social_instances():
            await self._send(peer.id)

    async def send_snapshot(self, to_instance_id: str) -> None:
        """Send the current snapshot to exactly one peer."""
        await self._send(to_instance_id)
