"""Tests for PeerHomeSharingService."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from socialhome.domain.federation import (
    FederationEventType,
    InstanceSource,
    PairingStatus,
    RemoteInstance,
)
from socialhome.services.peer_home_sharing_service import (
    PeerHomeSharingService,
    UnknownInstanceError,
)

pytestmark = pytest.mark.asyncio


# ─── Helpers ─────────────────────────────────────────────────────────────────


def _peer(iid: str, *, share_home: bool = True) -> RemoteInstance:
    return RemoteInstance(
        id=iid,
        display_name=iid,
        remote_identity_pk="aa" * 32,
        key_self_to_remote="enc",
        key_remote_to_self="enc",
        remote_inbox_url=f"https://example.com/inbox/{iid}",
        local_inbox_id=f"wh-{iid}",
        status=PairingStatus.CONFIRMED,
        source=InstanceSource.MANUAL,
        share_home=share_home,
    )


class _FakeFederationRepo:
    """Minimal in-memory federation repo stub for PeerHomeSharingService tests."""

    def __init__(
        self,
        instances: list[RemoteInstance],
        *,
        local_lat: float | None = None,
        local_lon: float | None = None,
    ) -> None:
        self._instances: dict[str, RemoteInstance] = {i.id: i for i in instances}
        self._local_lat = local_lat
        self._local_lon = local_lon
        self.share_home_calls: list[tuple[str, bool]] = []

    async def get_instance(self, instance_id: str) -> RemoteInstance | None:
        return self._instances.get(instance_id)

    async def set_share_home(self, instance_id: str, *, value: bool) -> None:
        self.share_home_calls.append((instance_id, value))
        old = self._instances[instance_id]
        # Rebuild the frozen dataclass with the new value
        self._instances[instance_id] = RemoteInstance(
            id=old.id,
            display_name=old.display_name,
            remote_identity_pk=old.remote_identity_pk,
            key_self_to_remote=old.key_self_to_remote,
            key_remote_to_self=old.key_remote_to_self,
            remote_inbox_url=old.remote_inbox_url,
            local_inbox_id=old.local_inbox_id,
            status=old.status,
            source=old.source,
            share_home=value,
        )

    async def get_local_identity(self) -> dict | None:
        if self._local_lat is None or self._local_lon is None:
            return {
                "instance_id": "self",
                "display_name": "Home",
                "home_lat": None,
                "home_lon": None,
            }
        return {
            "instance_id": "self",
            "display_name": "Home",
            "home_lat": self._local_lat,
            "home_lon": self._local_lon,
        }


# ─── Tests ────────────────────────────────────────────────────────────────────


async def test_set_share_home_off_sends_null_coords_envelope():
    """Turning share_home OFF sends null coords to the peer immediately."""
    peer = _peer("peer-1", share_home=True)
    repo = _FakeFederationRepo([peer], local_lat=52.52, local_lon=13.405)
    fed = AsyncMock()
    svc = PeerHomeSharingService(
        federation_repo=repo,
        federation_service=fed,
    )

    await svc.set_share_home("peer-1", value=False, set_by="admin")

    # Repo must have recorded the flip
    assert repo.share_home_calls == [("peer-1", False)]
    # One send_event call with null coords
    fed.send_event.assert_awaited_once_with(
        to_instance_id="peer-1",
        event_type=FederationEventType.LOCAL_HOME_LOCATION_CHANGED,
        payload={"latitude": None, "longitude": None},
    )


async def test_set_share_home_on_sends_current_coords():
    """Turning share_home ON sends the current local coords to the peer."""
    peer = _peer("peer-1", share_home=False)
    repo = _FakeFederationRepo([peer], local_lat=52.52, local_lon=13.405)
    fed = AsyncMock()
    svc = PeerHomeSharingService(
        federation_repo=repo,
        federation_service=fed,
    )

    await svc.set_share_home("peer-1", value=True, set_by="admin")

    assert repo.share_home_calls == [("peer-1", True)]
    fed.send_event.assert_awaited_once_with(
        to_instance_id="peer-1",
        event_type=FederationEventType.LOCAL_HOME_LOCATION_CHANGED,
        payload={"latitude": 52.52, "longitude": 13.405},
    )


async def test_set_share_home_idempotent_no_envelope():
    """Setting share_home to the value it already has is a no-op."""
    peer = _peer("peer-1", share_home=True)
    repo = _FakeFederationRepo([peer], local_lat=52.52, local_lon=13.405)
    fed = AsyncMock()
    svc = PeerHomeSharingService(
        federation_repo=repo,
        federation_service=fed,
    )

    await svc.set_share_home("peer-1", value=True, set_by="admin")

    assert repo.share_home_calls == []
    fed.send_event.assert_not_awaited()


async def test_set_share_home_unknown_peer_raises():
    """set_share_home raises UnknownInstanceError for an unrecognised peer."""
    repo = _FakeFederationRepo([])
    fed = AsyncMock()
    svc = PeerHomeSharingService(
        federation_repo=repo,
        federation_service=fed,
    )

    with pytest.raises(UnknownInstanceError):
        await svc.set_share_home("peer-x", value=False, set_by="admin")

    fed.send_event.assert_not_awaited()


async def test_set_share_home_on_with_no_local_coords_does_not_send():
    """Turning ON when local coords are unset skips the envelope (no-send)."""
    peer = _peer("peer-1", share_home=False)
    # No local lat/lon
    repo = _FakeFederationRepo([peer], local_lat=None, local_lon=None)
    fed = AsyncMock()
    svc = PeerHomeSharingService(
        federation_repo=repo,
        federation_service=fed,
    )

    await svc.set_share_home("peer-1", value=True, set_by="admin")

    # Flip still persists
    assert repo.share_home_calls == [("peer-1", True)]
    # But no envelope — nothing to send
    fed.send_event.assert_not_awaited()
