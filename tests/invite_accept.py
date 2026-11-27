"""Test helper: the invitee household's real §D1b accept.

Migration 0093 — a private invite seats nothing on receipt; the stub is
seated by ``SpaceService.accept_remote_invite`` from the invitation's own
stored snapshot. Tests that need a seated stub drive that real accept with
the federation send stubbed (the host side is not under test there).
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

from socialhome.crypto import generate_identity_keypair
from socialhome.domain.federation import DeliveryResult
from socialhome.infrastructure.event_bus import EventBus
from socialhome.repositories import SqliteUserRepo
from socialhome.repositories.space_post_repo import SqliteSpacePostRepo
from socialhome.services.space_service import SpaceService


class _RingConnectedGfs:
    """The invitee household holds a GFS connection that can carry a GFS
    space's channel (v_97: accepting an invitation to one needs it — the
    consent check is tested in ``tests/services/test_space_service_gfs_consent.py``)."""

    async def ring_connected(self, only=None) -> bool:
        return True

    async def ring_connected_at(self, gfs_url: str) -> bool:
        return True

    async def server_targets(self, space_id: str):
        return None


def accepting_service(
    db,
    space_repo,
    remote_member_repo,
    *,
    own_instance_id: str,
    bus: EventBus | None = None,
    space_crypto_service=None,
) -> SpaceService:
    """A :class:`SpaceService` whose ``accept_remote_invite`` runs for real,
    over a federation that reports every send delivered."""
    svc = SpaceService(
        space_repo,
        SqliteSpacePostRepo(db),
        SqliteUserRepo(db),
        bus or EventBus(),
        own_instance_id=own_instance_id,
    )
    fed = MagicMock()
    fed.send_with_mesh_fallback = AsyncMock(
        return_value=DeliveryResult(instance_id="peer", ok=True)
    )
    fed.begin_mesh_catchup_sync = AsyncMock()
    fed.own_identity_pk = generate_identity_keypair().public_key
    fed_repo = MagicMock()
    fed_repo.get_instance = AsyncMock(return_value=object())
    svc.attach_federation(
        federation_service=fed,
        federation_repo=fed_repo,
        remote_member_repo=remote_member_repo,
    )
    if space_crypto_service is not None:
        svc.attach_space_crypto_service(space_crypto_service)
    svc.attach_gfs_connection_service(_RingConnectedGfs())
    return svc


async def accept_invite(svc: SpaceService, token: str, user_id: str) -> None:
    await svc.accept_remote_invite(token=token, user_id=user_id)
