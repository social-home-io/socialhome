"""Tests for socialhome.services.resync_on_upgrade.

The capability-resync upgrade trigger fires at most once per OURS bump,
asks only confirmed peers that support v_19+ to re-advertise capabilities,
and persists OURS so an unchanged restart never re-fires (storm guard).
"""

from __future__ import annotations

from socialhome.domain.federation import FederationEventType, PairingStatus
from socialhome.domain.federation_capabilities import (
    OURS,
    FederationCapability,
)
from socialhome.services.resync_on_upgrade import (
    request_capability_resync_if_upgraded,
)

MIN_FOR_INSTANCE_RESYNC = FederationCapability.MIN_FOR_INSTANCE_RESYNC


class FakeIdentityRepo:
    def __init__(self, last: int | None) -> None:
        self.last = last
        self.set_calls: list[int] = []

    async def get_last_proto_version(self) -> int | None:
        return self.last

    async def set_last_proto_version(self, version: int) -> None:
        self.last = version
        self.set_calls.append(version)


class FakePeer:
    def __init__(self, peer_id: str) -> None:
        self.id = peer_id


class FakeFederationRepo:
    def __init__(self, peers: list[FakePeer]) -> None:
        self._peers = peers
        self.list_kwargs: dict | None = None

    async def list_social_instances(self):
        return await self.list_instances(status="confirmed")

    async def list_instances(self, **kwargs) -> list[FakePeer]:
        self.list_kwargs = kwargs
        return list(self._peers)


class FakeFederation:
    def __init__(self, supports: dict[str, bool]) -> None:
        self._supports = supports
        self.sends: list[dict] = []

    async def peer_supports(self, instance_id: str, *, min_version: int) -> bool:
        assert min_version == MIN_FOR_INSTANCE_RESYNC
        return self._supports.get(instance_id, False)

    async def send_event(self, *, to_instance_id, event_type, payload):
        self.sends.append(
            {
                "to": to_instance_id,
                "event_type": event_type,
                "payload": payload,
            }
        )


async def test_no_upgrade_when_last_equals_ours_sends_nothing():
    """last == OURS → returns 0, no sends, last unchanged (the storm guard)."""
    ident = FakeIdentityRepo(last=OURS)
    fed_repo = FakeFederationRepo([FakePeer("p1")])
    fed = FakeFederation({"p1": True})

    sent = await request_capability_resync_if_upgraded(
        federation=fed, federation_repo=fed_repo, identity_repo=ident
    )

    assert sent == 0
    assert fed.sends == []
    assert ident.set_calls == []  # last untouched
    assert fed_repo.list_kwargs is None  # never even enumerated peers


async def test_last_greater_than_ours_sends_nothing():
    """A downgrade (last > OURS) is also not an upgrade — no sends."""
    ident = FakeIdentityRepo(last=OURS + 5)
    fed_repo = FakeFederationRepo([FakePeer("p1")])
    fed = FakeFederation({"p1": True})

    sent = await request_capability_resync_if_upgraded(
        federation=fed, federation_repo=fed_repo, identity_repo=ident
    )

    assert sent == 0
    assert fed.sends == []
    assert ident.set_calls == []


async def test_first_boot_none_sends_and_persists():
    """last is None (first boot post-migration) → sends, persists OURS."""
    ident = FakeIdentityRepo(last=None)
    fed_repo = FakeFederationRepo([FakePeer("p1"), FakePeer("p2")])
    fed = FakeFederation({"p1": True, "p2": True})

    sent = await request_capability_resync_if_upgraded(
        federation=fed, federation_repo=fed_repo, identity_repo=ident
    )

    assert sent == 2
    assert {s["to"] for s in fed.sends} == {"p1", "p2"}
    assert ident.last == OURS
    assert ident.set_calls == [OURS]
    # Confirmed-only enumeration.
    assert fed_repo.list_kwargs == {"status": PairingStatus.CONFIRMED.value}


async def test_upgrade_sends_and_persists():
    """last < OURS (upgrade) → sends to supporting peers, persists OURS."""
    ident = FakeIdentityRepo(last=OURS - 1)
    fed_repo = FakeFederationRepo([FakePeer("p1")])
    fed = FakeFederation({"p1": True})

    sent = await request_capability_resync_if_upgraded(
        federation=fed, federation_repo=fed_repo, identity_repo=ident
    )

    assert sent == 1
    assert ident.last == OURS


async def test_old_peer_skipped_but_ours_still_persisted():
    """A peer below v_19 is skipped (peer_supports False); OURS still persists."""
    ident = FakeIdentityRepo(last=None)
    fed_repo = FakeFederationRepo([FakePeer("old"), FakePeer("new")])
    fed = FakeFederation({"old": False, "new": True})

    sent = await request_capability_resync_if_upgraded(
        federation=fed, federation_repo=fed_repo, identity_repo=ident
    )

    assert sent == 1
    assert [s["to"] for s in fed.sends] == ["new"]
    assert ident.last == OURS  # persisted regardless of skips


async def test_payload_is_capabilities_scope_only():
    """payload is exactly {'scope': 'capabilities'}; event INSTANCE_RESYNC_REQUEST."""
    ident = FakeIdentityRepo(last=None)
    fed_repo = FakeFederationRepo([FakePeer("p1")])
    fed = FakeFederation({"p1": True})

    await request_capability_resync_if_upgraded(
        federation=fed, federation_repo=fed_repo, identity_repo=ident
    )

    assert len(fed.sends) == 1
    sent = fed.sends[0]
    assert sent["event_type"] is FederationEventType.INSTANCE_RESYNC_REQUEST
    assert sent["payload"] == {"scope": "capabilities"}


async def test_no_peers_persists_ours_and_returns_zero():
    """No confirmed peers → 0 sends but still records OURS (don't re-fire)."""
    ident = FakeIdentityRepo(last=None)
    fed_repo = FakeFederationRepo([])
    fed = FakeFederation({})

    sent = await request_capability_resync_if_upgraded(
        federation=fed, federation_repo=fed_repo, identity_repo=ident
    )

    assert sent == 0
    assert ident.last == OURS
