"""PeerDirectoryService — fan ``SPACE_DIRECTORY_SYNC`` to confirmed peers.

Covers the three entry points: ``SpaceConfigChanged`` (broadcast),
``PairingConfirmed`` (single-peer push), and the direct
``send_snapshot`` helper. Filters non-confirmed peers; payload carries
the full public-space snapshot.
"""

from __future__ import annotations

import pytest

from socialhome.domain.events import (
    PairingConfirmed,
    SpaceConfigChanged,
)
from socialhome.domain.federation import FederationEventType, PairingStatus
from socialhome.domain.space import (
    JoinMode,
    Space,
    SpaceFeatures,
    SpaceType,
)
from socialhome.infrastructure.event_bus import EventBus
from socialhome.services.peer_directory_service import PeerDirectoryService


def _space(**over) -> Space:
    base = dict(
        id="sp-1",
        name="Garden",
        owner_instance_id="own-inst",
        owner_username="alice",
        identity_public_key="aa" * 32,
        config_sequence=1,
        features=SpaceFeatures(),
        space_type=SpaceType.PUBLIC,
        join_mode=JoinMode.OPEN,
        description="hi",
        emoji="🌱",
    )
    base.update(over)
    return Space(**base)


class _FakeSpaceRepo:
    def __init__(
        self, public_spaces: list[Space], members: dict[str, list[str]]
    ) -> None:
        self._spaces = public_spaces
        self._members = members

    async def list_by_type(self, space_type: SpaceType) -> list[Space]:
        assert space_type is SpaceType.PUBLIC
        return list(self._spaces)

    async def list_members(self, space_id: str) -> list[str]:
        return list(self._members.get(space_id, []))


class _PeerRow:
    def __init__(self, id: str, status: PairingStatus) -> None:
        self.id = id
        self.status = status


class _FakeFedRepo:
    def __init__(self, peers: list[_PeerRow]) -> None:
        self._peers = peers

    async def list_social_instances(self):
        return [
            p
            for p in self._peers
            if getattr(p.status, "value", p.status) == "confirmed"
        ]

    async def list_instances(self):
        return list(self._peers)


class _FakeFederationService:
    def __init__(self) -> None:
        self.sent: list[tuple[str, FederationEventType, dict]] = []

    async def send_event(self, *, to_instance_id, event_type, payload):
        self.sent.append((to_instance_id, event_type, payload))
        return None


@pytest.fixture
def env():
    sp1 = _space(id="sp-1", name="Garden")
    sp2 = _space(id="sp-2", name="Kitchen", emoji="🍳")
    space_repo = _FakeSpaceRepo(
        public_spaces=[sp1, sp2],
        members={"sp-1": ["u1", "u2"], "sp-2": ["u3"]},
    )
    fed_repo = _FakeFedRepo(
        [
            _PeerRow("peer-1", PairingStatus.CONFIRMED),
            _PeerRow("peer-2", PairingStatus.CONFIRMED),
            _PeerRow("peer-pending", PairingStatus.PENDING_SENT),
        ]
    )
    fed = _FakeFederationService()
    bus = EventBus()
    svc = PeerDirectoryService(
        bus=bus,
        federation_service=fed,
        federation_repo=fed_repo,
        space_repo=space_repo,
    )
    svc.wire()
    return bus, fed, svc


async def test_space_config_changed_broadcasts_to_confirmed_peers(env):
    bus, fed, _svc = env
    await bus.publish(
        SpaceConfigChanged(
            space_id="sp-1",
            event_type="SPACE_RENAMED",
            payload={},
            sequence=2,
        )
    )
    recipients = [r[0] for r in fed.sent]
    assert recipients == ["peer-1", "peer-2"]
    # Pending peer never receives the broadcast.
    assert "peer-pending" not in recipients
    for _to, event_type, payload in fed.sent:
        assert event_type is FederationEventType.SPACE_DIRECTORY_SYNC
        names = [s["name"] for s in payload["spaces"]]
        assert names == ["Garden", "Kitchen"]
        # Member counts come from list_members, join_mode is enum-stringified.
        garden = next(s for s in payload["spaces"] if s["space_id"] == "sp-1")
        assert garden["member_count"] == 2
        assert garden["join_mode"] == "open"


async def test_pairing_confirmed_sends_single_snapshot(env):
    bus, fed, _svc = env
    await bus.publish(PairingConfirmed(instance_id="peer-1"))
    assert len(fed.sent) == 1
    to, event_type, payload = fed.sent[0]
    assert to == "peer-1"
    assert event_type is FederationEventType.SPACE_DIRECTORY_SYNC
    assert {s["space_id"] for s in payload["spaces"]} == {"sp-1", "sp-2"}


async def test_send_snapshot_direct_call(env):
    _bus, fed, svc = env
    await svc.send_snapshot("peer-2")
    assert [r[0] for r in fed.sent] == ["peer-2"]


async def test_empty_public_spaces_still_broadcasts_empty_snapshot():
    space_repo = _FakeSpaceRepo(public_spaces=[], members={})
    fed_repo = _FakeFedRepo([_PeerRow("peer-1", PairingStatus.CONFIRMED)])
    fed = _FakeFederationService()
    bus = EventBus()
    svc = PeerDirectoryService(
        bus=bus,
        federation_service=fed,
        federation_repo=fed_repo,
        space_repo=space_repo,
    )
    svc.wire()
    await bus.publish(
        SpaceConfigChanged(
            space_id="sp-deleted",
            event_type="SPACE_DELETED",
            payload={},
            sequence=1,
        )
    )
    assert fed.sent == [
        ("peer-1", FederationEventType.SPACE_DIRECTORY_SYNC, {"spaces": []}),
    ]
