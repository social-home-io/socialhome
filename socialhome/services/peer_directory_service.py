"""Outbound peer-public-space directory sync (§D1a).

Fans a ``SPACE_DIRECTORY_SYNC`` envelope to every CONFIRMED peer whenever
one of our ``type=public`` spaces changes (create / update / dissolve),
and sends a one-shot snapshot on pair confirmation so new peers don't
wait for the next change to populate their directory.

Payload carries a full snapshot — it's cheap (expected < ~50 public
spaces per household) and lets the receiver atomically replace its
cached rows without tracking deltas.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from ..domain.events import (
    PairingConfirmed,
    SpaceConfigChanged,
)
from ..domain.federation import FederationEventType, PairingStatus
from ..domain.space import SpaceType
from ..infrastructure.event_bus import EventBus

if TYPE_CHECKING:
    from ..federation.federation_service import FederationService
    from ..repositories.federation_repo import AbstractFederationRepo
    from ..repositories.space_repo import AbstractSpaceRepo

log = logging.getLogger(__name__)


class PeerDirectoryService:
    """Outbound fan-out for :data:`FederationEventType.SPACE_DIRECTORY_SYNC`."""

    __slots__ = ("_bus", "_federation", "_federation_repo", "_space_repo")

    def __init__(
        self,
        *,
        bus: EventBus,
        federation_service: "FederationService",
        federation_repo: "AbstractFederationRepo",
        space_repo: "AbstractSpaceRepo",
    ) -> None:
        self._bus = bus
        self._federation = federation_service
        self._federation_repo = federation_repo
        self._space_repo = space_repo

    def wire(self) -> None:
        self._bus.subscribe(SpaceConfigChanged, self._on_config_changed)
        self._bus.subscribe(PairingConfirmed, self._on_pair_confirmed)

    async def _on_config_changed(self, _event: SpaceConfigChanged) -> None:
        """Any space-config change can affect the public-spaces snapshot
        (name, description, emoji, join_mode, space_type). Broadcast the
        full current snapshot to every paired peer.
        """
        await self._broadcast_snapshot()

    async def _on_pair_confirmed(self, event: PairingConfirmed) -> None:
        """A new pair just went live — push our current snapshot so the
        peer's browser doesn't wait for the next change."""
        await self.send_snapshot(event.instance_id)

    async def _build_snapshot(self) -> list[dict]:
        spaces = await self._space_repo.list_by_type(SpaceType.PUBLIC)
        out: list[dict] = []
        for s in spaces:
            members = await self._space_repo.list_members(s.id)
            out.append(
                {
                    "space_id": s.id,
                    "name": s.name,
                    "description": s.description,
                    "emoji": s.emoji,
                    "member_count": len(members),
                    "join_mode": s.join_mode.value,
                }
            )
        return out

    async def _broadcast_snapshot(self) -> None:
        snapshot = await self._build_snapshot()
        peers = await self._federation_repo.list_instances()
        for peer in peers:
            if peer.status is not PairingStatus.CONFIRMED:
                continue
            try:
                await self._federation.send_event(
                    to_instance_id=peer.id,
                    event_type=FederationEventType.SPACE_DIRECTORY_SYNC,
                    payload={"spaces": snapshot},
                )
            except Exception as exc:  # pragma: no cover — defensive
                log.debug(
                    "peer_directory: send to %s failed: %s",
                    peer.id,
                    exc,
                )

    async def send_snapshot(self, to_instance_id: str) -> None:
        """Send the current snapshot to exactly one peer."""
        snapshot = await self._build_snapshot()
        try:
            await self._federation.send_event(
                to_instance_id=to_instance_id,
                event_type=FederationEventType.SPACE_DIRECTORY_SYNC,
                payload={"spaces": snapshot},
            )
        except Exception as exc:  # pragma: no cover — defensive
            log.debug(
                "peer_directory: send snapshot to %s failed: %s",
                to_instance_id,
                exc,
            )
