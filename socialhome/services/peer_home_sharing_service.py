"""Per-peer ``share_home`` toggle orchestrator.

The toggle lives in ``remote_instances.share_home`` (default ``True``) and is
mutated through :meth:`PeerHomeSharingService.set_share_home`. Flipping it
also fires a one-shot ``LOCAL_HOME_LOCATION_CHANGED`` to the affected peer so
its map updates immediately:

* OFF: empty coords ``{latitude: None, longitude: None}`` — the peer's
  inbound handler treats this as a revoke and clears its row.
* ON: the current home from ``instance_identity`` as the ~3 km cell
  centre (``coarse_home_for_peer``), never the 4 dp home (via
  :meth:`AbstractFederationRepo.get_local_identity`); if local coords are
  unset, no envelope is sent (the peer re-enters the fan-out as soon as HA
  Core pushes a location).

Idempotent: setting the toggle to the value it already has is a no-op
(no DB write, no envelope).
"""

from __future__ import annotations

import logging

from ..domain.federation import FederationEventType
from ..domain.home_location import coarse_home_for_peer_or_none
from ..federation.federation_service import FederationService
from ..repositories.federation_repo import AbstractFederationRepo

log = logging.getLogger(__name__)


class UnknownInstanceError(ValueError):
    """Raised when set_share_home targets an instance that does not exist."""


class PeerHomeSharingService:
    """Toggle the per-peer home-coords share gate, plus one-off revoke/re-share envelope."""

    __slots__ = ("_federation_repo", "_federation_service")

    def __init__(
        self,
        *,
        federation_repo: AbstractFederationRepo,
        federation_service: FederationService,
    ) -> None:
        self._federation_repo = federation_repo
        self._federation_service = federation_service

    async def set_share_home(
        self,
        instance_id: str,
        *,
        value: bool,
        set_by: str | None,
    ) -> None:
        """Set the per-peer share_home toggle; fire a one-off envelope on flip."""
        peer = await self._federation_repo.get_instance(instance_id)
        if peer is None:
            raise UnknownInstanceError(instance_id)
        if peer.share_home == value:
            return  # idempotent — nothing to do

        await self._federation_repo.set_share_home(instance_id, value=value)
        log.info(
            "share_home for %s set to %s by %s",
            instance_id,
            value,
            set_by or "<unknown>",
        )

        if not value:
            # OFF: tell the peer to forget our home location.
            await self._federation_service.send_event(
                to_instance_id=instance_id,
                event_type=FederationEventType.LOCAL_HOME_LOCATION_CHANGED,
                payload={"latitude": None, "longitude": None},
            )
            return

        # ON: send current local coords if we have any.
        identity = await self._federation_repo.get_local_identity() or {}
        # Only the ~3 km cell centre leaves the household, never the 4 dp
        # home.
        coarse = coarse_home_for_peer_or_none(
            identity.get("home_lat"), identity.get("home_lon")
        )
        if coarse is None:
            return  # no coords yet — peer will receive them on next HA location push

        await self._federation_service.send_event(
            to_instance_id=instance_id,
            event_type=FederationEventType.LOCAL_HOME_LOCATION_CHANGED,
            payload={"latitude": coarse[0], "longitude": coarse[1]},
        )
