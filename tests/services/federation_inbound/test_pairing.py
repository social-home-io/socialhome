"""Tests for :class:`PairingInboundHandlers` (§11)."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from socialhome.domain.events import (
    PairingAborted,
    PairingAcceptReceived,
    PairingConfirmed,
    PairingIntroReceived,
    PeerUnpaired,
)
from socialhome.domain.federation import (
    FederationEvent,
    FederationEventType,
    InstanceSource,
    PairingSession,
    PairingStatus,
    RemoteInstance,
)
from socialhome.infrastructure.event_bus import EventBus
from socialhome.services.federation_inbound import PairingInboundHandlers


class _FakeRegistry:
    def __init__(self) -> None:
        self.registered: list[tuple] = []

    def register(self, event_type, handler):
        self.registered.append((event_type, handler))


class _FakeFederationService:
    def __init__(self) -> None:
        self._event_registry = _FakeRegistry()


class _FakeFederationRepo:
    def __init__(self) -> None:
        self.instances: dict[str, RemoteInstance] = {}
        self.pairings: dict[str, PairingSession] = {}

    async def save_instance(self, inst):
        self.instances[inst.id] = inst
        return inst

    async def get_instance(self, iid):
        return self.instances.get(iid)

    async def delete_instance(self, iid):
        self.instances.pop(iid, None)

    async def get_pairing(self, token):
        return self.pairings.get(token)

    async def delete_pairing(self, token):
        self.pairings.pop(token, None)


def _event(event_type, payload, *, from_instance="peer-a", space_id=None):
    return FederationEvent(
        msg_id="msg-" + event_type.value,
        event_type=event_type,
        from_instance=from_instance,
        to_instance="self",
        timestamp=datetime.now(timezone.utc).isoformat(),
        payload=payload,
        space_id=space_id,
    )


def _sample_instance(iid="peer-a", status=PairingStatus.PENDING_SENT) -> RemoteInstance:
    return RemoteInstance(
        id=iid,
        display_name=iid,
        remote_identity_pk="aa" * 32,
        key_self_to_remote="enc",
        key_remote_to_self="enc",
        remote_inbox_url="https://x/wh",
        local_inbox_id=f"wh-{iid}",
        status=status,
        source=InstanceSource.MANUAL,
    )


@pytest.fixture
def bus():
    return EventBus()


@pytest.fixture
def repo():
    return _FakeFederationRepo()


@pytest.fixture
def handlers(bus, repo):
    h = PairingInboundHandlers(bus=bus, federation_repo=repo)
    fed = _FakeFederationService()
    h.attach_to(fed)
    return h


async def test_attach_registers_five_event_types(bus, repo):
    """attach_to wires the expected five pairing events."""
    h = PairingInboundHandlers(bus=bus, federation_repo=repo)
    fed = _FakeFederationService()
    h.attach_to(fed)
    types = {t for t, _ in fed._event_registry.registered}
    assert types == {
        FederationEventType.PAIRING_INTRO,
        FederationEventType.PAIRING_ACCEPT,
        FederationEventType.PAIRING_CONFIRM,
        FederationEventType.PAIRING_ABORT,
        FederationEventType.UNPAIR,
    }


async def test_intro_publishes_event_and_stores_relay(bus, handlers):
    captured: list[PairingIntroReceived] = []
    bus.subscribe(PairingIntroReceived, captured.append)
    await handlers._on_intro(
        _event(
            FederationEventType.PAIRING_INTRO,
            {"via_instance_id": "peer-b", "message": "hi"},
        )
    )
    assert len(captured) == 1
    assert captured[0].via_instance_id == "peer-b"
    assert captured[0].from_instance == "peer-a"


async def test_intro_missing_via_is_noop(bus, handlers):
    captured: list[PairingIntroReceived] = []
    bus.subscribe(PairingIntroReceived, captured.append)
    await handlers._on_intro(
        _event(
            FederationEventType.PAIRING_INTRO,
            {},
        )
    )
    assert captured == []


async def test_accept_publishes_when_pending_session_exists(bus, repo, handlers):
    repo.pairings["tok-1"] = PairingSession(
        token="tok-1",
        own_identity_pk="aa" * 32,
        own_dh_pk="bb" * 32,
        own_dh_sk="enc",
        inbox_url="https://peer/wh/own-id",
        own_local_inbox_id="own-id",
        issued_at="2026-04-18T00:00:00+00:00",
        expires_at="2026-04-18T01:00:00+00:00",
        status=PairingStatus.PENDING_SENT,
    )
    captured: list[PairingAcceptReceived] = []
    bus.subscribe(PairingAcceptReceived, captured.append)
    await handlers._on_accept(
        _event(
            FederationEventType.PAIRING_ACCEPT,
            {"token": "tok-1", "verification_code": "123456"},
        )
    )
    assert len(captured) == 1
    assert captured[0].token == "tok-1"
    assert captured[0].verification_code == "123456"


async def test_accept_unknown_token_is_noop(bus, handlers):
    captured: list[PairingAcceptReceived] = []
    bus.subscribe(PairingAcceptReceived, captured.append)
    await handlers._on_accept(
        _event(
            FederationEventType.PAIRING_ACCEPT,
            {"token": "nonexistent"},
        )
    )
    assert captured == []


async def test_confirm_flips_status_to_confirmed(bus, repo, handlers):
    repo.instances["peer-a"] = _sample_instance(
        "peer-a", PairingStatus.PENDING_RECEIVED
    )
    captured: list[PairingConfirmed] = []
    bus.subscribe(PairingConfirmed, captured.append)
    await handlers._on_confirm(
        _event(
            FederationEventType.PAIRING_CONFIRM,
            {},
        )
    )
    assert repo.instances["peer-a"].status is PairingStatus.CONFIRMED
    assert len(captured) == 1
    assert captured[0].instance_id == "peer-a"


async def test_confirm_already_confirmed_is_noop(repo, handlers):
    repo.instances["peer-a"] = _sample_instance("peer-a", PairingStatus.CONFIRMED)
    # Should not raise or churn the row — just return.
    await handlers._on_confirm(
        _event(
            FederationEventType.PAIRING_CONFIRM,
            {},
        )
    )
    assert repo.instances["peer-a"].status is PairingStatus.CONFIRMED


async def test_abort_drops_pending_and_publishes(bus, repo, handlers):
    repo.pairings["tok-1"] = PairingSession(
        token="tok-1",
        own_identity_pk="aa" * 32,
        own_dh_pk="bb" * 32,
        own_dh_sk="enc",
        inbox_url="https://peer/wh/own-id",
        own_local_inbox_id="own-id",
        issued_at="2026-04-18T00:00:00+00:00",
        expires_at="2026-04-18T01:00:00+00:00",
        status=PairingStatus.PENDING_SENT,
    )
    repo.instances["peer-a"] = _sample_instance(
        "peer-a",
        PairingStatus.PENDING_RECEIVED,
    )
    captured: list[PairingAborted] = []
    bus.subscribe(PairingAborted, captured.append)
    await handlers._on_abort(
        _event(
            FederationEventType.PAIRING_ABORT,
            {"token": "tok-1", "reason": "timeout"},
        )
    )
    assert "tok-1" not in repo.pairings
    assert "peer-a" not in repo.instances
    assert captured[0].reason == "timeout"


async def test_abort_keeps_confirmed_instance(repo, handlers):
    """An abort arriving after confirmation shouldn't delete the pair."""
    repo.instances["peer-a"] = _sample_instance("peer-a", PairingStatus.CONFIRMED)
    await handlers._on_abort(
        _event(
            FederationEventType.PAIRING_ABORT,
            {},
        )
    )
    assert "peer-a" in repo.instances


async def test_unpair_deletes_instance_and_publishes(bus, repo, handlers):
    repo.instances["peer-a"] = _sample_instance("peer-a", PairingStatus.CONFIRMED)
    captured: list[PeerUnpaired] = []
    bus.subscribe(PeerUnpaired, captured.append)
    await handlers._on_unpair(
        _event(
            FederationEventType.UNPAIR,
            {},
        )
    )
    assert "peer-a" not in repo.instances
    assert captured[0].instance_id == "peer-a"


async def test_unpair_unknown_peer_is_noop(bus, handlers):
    captured: list[PeerUnpaired] = []
    bus.subscribe(PeerUnpaired, captured.append)
    await handlers._on_unpair(
        _event(
            FederationEventType.UNPAIR,
            {},
        )
    )
    assert captured == []
