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
    PeerHomeChanged,
    PeerProtoVersionRaised,
    SpaceConfigChanged,
    SpaceLocalAreaChanged,
)
from socialhome.domain.federation import FederationEventType, PairingStatus
from socialhome.domain.federation_capabilities import OURS, FederationCapability
from socialhome.domain.space import (
    JoinMode,
    Space,
    SpaceFeatures,
    SpaceType,
)
from socialhome.infrastructure.event_bus import EventBus
from socialhome.services.peer_directory_service import PeerDirectoryService


def _space(**over) -> Space:
    base = {
        "id": "sp-1",
        "name": "Garden",
        "owner_instance_id": "own-inst",
        "owner_username": "alice",
        "identity_public_key": "aa" * 32,
        "config_sequence": 1,
        "features": SpaceFeatures(),
        "space_type": SpaceType.PUBLIC,
        "join_mode": JoinMode.OPEN,
        "description": "hi",
        "emoji": "🌱",
    }
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

    async def count_members(self, space_id: str) -> int:
        # The repo's member count (followers excluded) — the snapshot
        # publishes it as is.
        return len(self._members.get(space_id, []))


class _PeerRow:
    def __init__(self, id: str, status: PairingStatus, home=None) -> None:
        self.id = id
        self.status = status
        #: A home this peer shares through pairing — never read for the
        #: directory any more.
        self.home_lat = home[0] if home else None
        self.home_lon = home[1] if home else None


class _FakeFedRepo:
    def __init__(self, peers: list[_PeerRow]) -> None:
        self._peers = peers

    async def get_instance(self, instance_id):
        return next((p for p in self._peers if p.id == instance_id), None)

    async def list_social_instances(self):
        return [
            p
            for p in self._peers
            if getattr(p.status, "value", p.status) == "confirmed"
        ]

    async def list_instances(self):
        return list(self._peers)


class _FakeFederationService:
    def __init__(self, versions: dict[str, int] | None = None) -> None:
        self.sent: list[tuple[str, FederationEventType, dict]] = []
        #: Advertised ``proto_version`` per peer; unknown peers are current.
        self.versions = versions or {}

    async def send_event(self, *, to_instance_id, event_type, payload):
        self.sent.append((to_instance_id, event_type, payload))

    async def peer_supports(self, instance_id, *, min_version):
        return self.versions.get(instance_id, OURS) >= min_version


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


# ── v_59: a local community — the radius only ───────────────────────────

#: The community's centre (Zürich) — must never appear on the wire.
CENTRE = (47.3769, 8.5417)
#: Homes the peers share with us through pairing — never used for the
#: directory: ~1 km away (inside 5 km), Bern (outside).
NEAR = (47.3850, 8.5500)
FAR = (46.9481, 7.4474)


def _local_env(versions: dict[str, int] | None = None):
    community = _space(
        id="sp-local", name="Quartier", lat=CENTRE[0], lon=CENTRE[1], radius_km=5.0
    )
    pinned = _space(id="sp-pin", name="Old pin", lat=1.0, lon=2.0)
    space_repo = _FakeSpaceRepo(public_spaces=[community, pinned], members={})
    homes = {"peer-near": NEAR, "peer-far": FAR, "peer-none": None}
    fed_repo = _FakeFedRepo(
        [_PeerRow(p, PairingStatus.CONFIRMED, home=h) for p, h in homes.items()]
    )
    fed = _FakeFederationService(versions)
    bus = EventBus()
    svc = PeerDirectoryService(
        bus=bus,
        federation_service=fed,
        federation_repo=fed_repo,
        space_repo=space_repo,
    )
    svc.wire()
    return bus, fed, svc


def _entries(fed, to):
    payloads = [p for (peer, _t, p) in fed.sent if peer == to]
    return {s["space_id"]: s for s in payloads[0]["spaces"]}


def _numbers(obj) -> list[float]:
    """Every number anywhere in ``obj`` (a wire payload)."""
    if isinstance(obj, bool):
        return []
    if isinstance(obj, int | float):
        return [float(obj)]
    if isinstance(obj, dict):
        return [n for v in obj.values() for n in _numbers(v)]
    if isinstance(obj, list):
        return [n for v in obj for n in _numbers(v)]
    return []


_COORD_KEYS = {"lat", "lon", "latitude", "longitude", "local_area", "centre", "center"}


def _keys(obj) -> set[str]:
    if isinstance(obj, dict):
        return set(obj) | {k for v in obj.values() for k in _keys(v)}
    if isinstance(obj, list):
        return {k for v in obj for k in _keys(v)}
    return set()


async def test_every_peer_gets_the_radius_and_nothing_else():
    """No verdict any more: whatever home a peer shares with us, its entry
    says "a local community, within 5 km" — the same for every peer."""
    bus, fed, _svc = _local_env()
    await bus.publish(SpaceLocalAreaChanged(space_id="sp-local"))
    entries = [
        _entries(fed, p)["sp-local"] for p in ("peer-near", "peer-far", "peer-none")
    ]
    for entry in entries:
        assert entry["local_community"] is True
        assert entry["radius_km"] == 5.0
        assert not {"in_area", "area_edge"} & set(entry)
    assert entries[0] == entries[1] == entries[2]
    # A pin without a radius is no community — nothing about it is sent.
    assert not {"local_community", "radius_km"} & set(
        _entries(fed, "peer-near")["sp-pin"]
    )


async def test_no_payload_carries_any_coordinate():
    """The adversarial check: no coordinate key anywhere in any payload,
    and none of the centre's or a peer's home's numbers either."""
    bus, fed, svc = _local_env()
    await bus.publish(SpaceLocalAreaChanged(space_id="sp-local"))
    await svc.send_snapshot("peer-near")
    secrets = {*CENTRE, *NEAR, *FAR, 1.0, 2.0}
    assert fed.sent
    for _to, _t, payload in fed.sent:
        assert not _COORD_KEYS & _keys(payload)
        assert not secrets & set(_numbers(payload)) - {0.0, 5.0}
        assert "47.37" not in repr(payload) and "8.54" not in repr(payload)


async def test_below_v59_the_entry_has_no_local_community_fields():
    old = FederationCapability.MIN_FOR_LOCAL_COMMUNITY_AREA - 1
    _bus, fed, svc = _local_env({"peer-far": old})
    await svc.send_snapshot("peer-far")
    await svc.send_snapshot("peer-near")
    assert set(_entries(fed, "peer-far")) == {"sp-local", "sp-pin"}
    assert all(
        not {"local_community", "radius_km"} & set(e)
        for e in _entries(fed, "peer-far").values()
    )
    assert _entries(fed, "peer-near")["sp-local"]["radius_km"] == 5.0


async def test_a_peers_home_change_sends_nothing():
    """The directory no longer depends on any household's home."""
    bus, fed, _svc = _local_env()
    await bus.publish(
        PeerHomeChanged(instance_id="peer-none", latitude=NEAR[0], longitude=NEAR[1])
    )
    assert fed.sent == []


async def test_reaching_v59_sends_the_community_fields():
    floor = FederationCapability.MIN_FOR_LOCAL_COMMUNITY_AREA
    bus, fed, _svc = _local_env()
    await bus.publish(
        PeerProtoVersionRaised(
            instance_id="peer-near", old_version=floor - 1, new_version=floor
        )
    )
    assert _entries(fed, "peer-near")["sp-local"]["radius_km"] == 5.0
    # A later raise (already past v_59) sends nothing new.
    await bus.publish(
        PeerProtoVersionRaised(
            instance_id="peer-near", old_version=floor, new_version=floor + 1
        )
    )
    assert len(fed.sent) == 1
