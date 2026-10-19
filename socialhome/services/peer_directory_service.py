"""Outbound peer-public-space directory sync (§D1a).

Fans a ``SPACE_DIRECTORY_SYNC`` envelope to every CONFIRMED peer whenever
one of our ``type=public`` spaces changes (create / update / dissolve),
and sends a one-shot snapshot on pair confirmation so new peers don't
wait for the next change to populate their directory.

Payload carries a full snapshot — it's cheap (expected < ~50 public
spaces per household) and lets the receiver atomically replace its
cached rows without tracking deltas.

A local community's entry carries ``local_community: true`` and its
``radius_km`` — "within X km" — and never the centre or any verdict:
people's locations are not shared by design (owner decision 2026-10-19).
Whether a home is inside is decided when a join arrives (the join carries
the joiner's home rounded to ~1 km, see
:mod:`socialhome.services.local_community`). Only to a peer at v_59+
(:attr:`~FederationCapability.MIN_FOR_LOCAL_COMMUNITY_AREA`); an older peer
gets the entry without the fields, and is sent the snapshot again when it
first advertises v_59. Only paired (social) peers get a snapshot at all;
never a GFS, never a household met through an invite link.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from ..domain.events import (
    PairingConfirmed,
    PeerProtoVersionRaised,
    SpaceConfigChanged,
    SpaceLocalAreaChanged,
)
from ..domain.federation import FederationEventType
from ..domain.federation_capabilities import FederationCapability
from ..domain.local_community import local_area_of, local_community_wire
from ..domain.space import SpaceType, normalize_category
from ..infrastructure.event_bus import EventBus

if TYPE_CHECKING:
    from ..federation.federation_service import FederationService
    from ..repositories.federation_repo import AbstractFederationRepo
    from ..repositories.space_repo import AbstractSpaceRepo

log = logging.getLogger(__name__)

__all__ = ["PeerDirectoryService"]


class PeerDirectoryService:
    """Outbound fan-out for :data:`FederationEventType.SPACE_DIRECTORY_SYNC`."""

    __slots__ = (
        "_bus",
        "_federation",
        "_federation_repo",
        "_space_repo",
    )

    def __init__(
        self,
        *,
        bus: EventBus,
        federation_service: FederationService,
        federation_repo: AbstractFederationRepo,
        space_repo: AbstractSpaceRepo,
    ) -> None:
        self._bus = bus
        self._federation = federation_service
        self._federation_repo = federation_repo
        self._space_repo = space_repo

    def wire(self) -> None:
        self._bus.subscribe(SpaceConfigChanged, self._on_config_changed)
        self._bus.subscribe(SpaceLocalAreaChanged, self._on_local_area_changed)
        self._bus.subscribe(PairingConfirmed, self._on_pair_confirmed)
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

    async def _on_proto_raised(self, event: PeerProtoVersionRaised) -> None:
        """A peer just reached v_59: it has had its entries without the
        local-community fields until now."""
        floor = FederationCapability.MIN_FOR_LOCAL_COMMUNITY_AREA
        if event.old_version < floor <= event.new_version:
            await self.send_snapshot(event.instance_id)

    async def _build_snapshot(self, peer_id: str) -> list[dict]:
        """The snapshot as ``peer_id`` receives it: a local community's
        radius only to a v_59+ peer, never a centre."""
        spaces = await self._space_repo.list_by_type(SpaceType.PUBLIC)
        with_area = await self._federation.peer_supports(
            peer_id, min_version=FederationCapability.MIN_FOR_LOCAL_COMMUNITY_AREA
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
            area = local_area_of(s) if with_area else None
            if area is not None:
                entry.update(local_community_wire(area))
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
