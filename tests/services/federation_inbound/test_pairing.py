"""Tests for :class:`PairingInboundHandlers` (§11)."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone

import pytest

from socialhome.crypto import derive_instance_id
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
from socialhome.services.peer_unpair_service import PeerUnpairService


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
        self.capabilities_seen: list[str] = []

    async def save_instance(self, inst):
        self.instances[inst.id] = inst
        return inst

    async def get_instance(self, iid, *, include_unpairing=False):
        return self.instances.get(iid)

    async def delete_instance(self, iid):
        self.instances.pop(iid, None)

    async def get_pairing(self, token):
        return self.pairings.get(token)

    async def delete_pairing(self, token):
        self.pairings.pop(token, None)

    async def update_inbox(self, instance_id: str, new_url: str) -> None:
        inst = self.instances.get(instance_id)
        if inst is None:
            return
        self.instances[instance_id] = RemoteInstance(
            id=inst.id,
            display_name=inst.display_name,
            remote_identity_pk=inst.remote_identity_pk,
            key_self_to_remote=inst.key_self_to_remote,
            key_remote_to_self=inst.key_remote_to_self,
            remote_inbox_url=new_url,
            local_inbox_id=inst.local_inbox_id,
            status=inst.status,
            source=inst.source,
        )

    async def set_proto_version(self, instance_id: str, proto_version: int) -> None:
        inst = self.instances.get(instance_id)
        if inst is None:
            return
        # Targeted single-column update (mirrors the real repo's UPDATE) so
        # an earlier mark_capabilities_seen stamp isn't clobbered.
        self.instances[instance_id] = replace(inst, proto_version=proto_version)

    async def mark_capabilities_seen(self, instance_id: str) -> None:
        inst = self.instances.get(instance_id)
        if inst is None:
            return
        self.capabilities_seen.append(instance_id)
        self.instances[instance_id] = replace(
            inst,
            capabilities_seen_at=datetime.now(timezone.utc).isoformat(),
        )

    async def update_display_name(self, instance_id: str, name: str) -> None:
        inst = self.instances.get(instance_id)
        if inst is None:
            return
        # Targeted single-column update — only the advertised display_name,
        # never local_alias (mirrors the real repo's UPDATE).
        self.instances[instance_id] = replace(inst, display_name=name)


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


class _FakeOutboxRepo:
    def __init__(self) -> None:
        self.rows: dict[str, list[str]] = {}

    async def delete_for_instance(self, iid):
        self.rows.pop(iid, None)


class _FakeRoutingRepo:
    def __init__(self) -> None:
        self.discovered_via: dict[str, list[str]] = {}

    async def forget_discovered_via(self, iid):
        self.discovered_via.pop(iid, None)


class _NoSendFederation:
    async def send_event(self, **kw):
        raise AssertionError("an inbound UNPAIR must never be echoed back")


@pytest.fixture
def outbox():
    return _FakeOutboxRepo()


@pytest.fixture
def routing():
    return _FakeRoutingRepo()


@pytest.fixture
def peer_unpair(bus, repo, outbox, routing):
    return PeerUnpairService(
        bus=bus,
        federation=_NoSendFederation(),
        federation_repo=repo,
        outbox_repo=outbox,
        routing_repo=routing,
    )


@pytest.fixture
def handlers(bus, repo, peer_unpair):
    h = PairingInboundHandlers(bus=bus, federation_repo=repo, peer_unpair=peer_unpair)
    fed = _FakeFederationService()
    h.attach_to(fed)
    return h


async def test_attach_registers_pairing_event_types(bus, repo, peer_unpair):
    """attach_to wires the pairing-family events (six pairing-lifecycle
    events plus the proto_version capability announcement)."""
    h = PairingInboundHandlers(bus=bus, federation_repo=repo, peer_unpair=peer_unpair)
    fed = _FakeFederationService()
    h.attach_to(fed)
    types = {t for t, _ in fed._event_registry.registered}
    assert types == {
        FederationEventType.PAIRING_INTRO,
        FederationEventType.PAIRING_ACCEPT,
        FederationEventType.PAIRING_CONFIRM,
        FederationEventType.PAIRING_ABORT,
        FederationEventType.UNPAIR,
        FederationEventType.URL_UPDATED,
        FederationEventType.INSTANCE_CAPABILITIES_UPDATED,
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


async def test_confirm_never_flips_a_pending_pairing(bus, repo, handlers):
    """A pairing is confirmed only by this household's own verification
    step — an inbox PAIRING_CONFIRM from the pending peer changes nothing."""
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
    assert repo.instances["peer-a"].status is PairingStatus.PENDING_RECEIVED
    assert captured == []


async def test_confirm_from_unknown_instance_is_noop(bus, repo, handlers):
    captured: list[PairingConfirmed] = []
    bus.subscribe(PairingConfirmed, captured.append)
    await handlers._on_confirm(_event(FederationEventType.PAIRING_CONFIRM, {}))
    assert repo.instances == {}
    assert captured == []


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


def _pending_session(token: str, peer_identity_pk: str | None) -> PairingSession:
    return PairingSession(
        token=token,
        own_identity_pk="aa" * 32,
        own_dh_pk="bb" * 32,
        own_dh_sk="enc",
        inbox_url="https://peer/wh/own-id",
        own_local_inbox_id="own-id",
        peer_identity_pk=peer_identity_pk,
        issued_at="2026-04-18T00:00:00+00:00",
        expires_at="2026-04-18T01:00:00+00:00",
        status=PairingStatus.PENDING_RECEIVED,
    )


_PEER_PK = "cd" * 32
_PEER_ID = derive_instance_id(bytes.fromhex(_PEER_PK))


async def test_abort_drops_pending_and_publishes(bus, repo, handlers):
    repo.pairings["tok-1"] = _pending_session("tok-1", _PEER_PK)
    repo.instances[_PEER_ID] = _sample_instance(
        _PEER_ID,
        PairingStatus.PENDING_RECEIVED,
    )
    captured: list[PairingAborted] = []
    bus.subscribe(PairingAborted, captured.append)
    await handlers._on_abort(
        _event(
            FederationEventType.PAIRING_ABORT,
            {"token": "tok-1", "reason": "timeout"},
            from_instance=_PEER_ID,
        )
    )
    assert "tok-1" not in repo.pairings
    assert _PEER_ID not in repo.instances
    assert captured[0].reason == "timeout"


@pytest.mark.parametrize(
    "peer_identity_pk",
    [None, "ef" * 32, "not-hex"],
    ids=["peer-unknown", "another-peer", "malformed-pk"],
)
async def test_abort_keeps_session_not_bound_to_sender(
    repo, handlers, peer_identity_pk
):
    """The token only cancels the sender's own pending session."""
    repo.pairings["tok-1"] = _pending_session("tok-1", peer_identity_pk)
    await handlers._on_abort(
        _event(
            FederationEventType.PAIRING_ABORT,
            {"token": "tok-1"},
            from_instance=_PEER_ID,
        )
    )
    assert "tok-1" in repo.pairings


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


async def test_inbound_unpair_cleans_up_like_a_local_unpair(
    repo, outbox, routing, handlers
):
    """Symmetric with ``DELETE /api/pairing/connections/{id}``: the same
    :meth:`PeerUnpairService.forget` drops the queued envelopes and the mesh
    hints the peer announced — and never sends an UNPAIR back."""
    repo.instances["peer-a"] = _sample_instance("peer-a", PairingStatus.CONFIRMED)
    outbox.rows["peer-a"] = ["m1"]
    routing.discovered_via["peer-a"] = ["peer-x"]
    await handlers._on_unpair(_event(FederationEventType.UNPAIR, {}))
    assert outbox.rows == {}
    assert routing.discovered_via == {}


async def test_inbound_unpair_ignores_an_instance_named_in_the_payload(repo, handlers):
    """Only the signer (``from_instance``) is unpaired — a payload naming
    another household is not an authority to drop that pairing."""
    repo.instances["peer-a"] = _sample_instance("peer-a", PairingStatus.CONFIRMED)
    repo.instances["peer-b"] = _sample_instance("peer-b", PairingStatus.CONFIRMED)
    await handlers._on_unpair(
        _event(FederationEventType.UNPAIR, {"instance_id": "peer-b"}),
    )
    assert "peer-a" not in repo.instances
    assert "peer-b" in repo.instances


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


# ── URL_UPDATED ──────────────────────────────────────────────────────────


async def test_url_updated_rewrites_remote_inbox_url(repo, handlers):
    repo.instances["peer-a"] = _sample_instance("peer-a", PairingStatus.CONFIRMED)
    await handlers._on_url_updated(
        _event(
            FederationEventType.URL_UPDATED,
            {"inbox_url": "https://new.example.com/federation/inbox/wh-peer-a"},
        )
    )
    assert (
        repo.instances["peer-a"].remote_inbox_url
        == "https://new.example.com/federation/inbox/wh-peer-a"
    )


async def test_url_updated_rejects_empty(repo, handlers):
    repo.instances["peer-a"] = _sample_instance("peer-a", PairingStatus.CONFIRMED)
    original = repo.instances["peer-a"].remote_inbox_url
    await handlers._on_url_updated(
        _event(FederationEventType.URL_UPDATED, {"inbox_url": ""}),
    )
    assert repo.instances["peer-a"].remote_inbox_url == original


async def test_url_updated_rejects_bad_scheme(repo, handlers):
    repo.instances["peer-a"] = _sample_instance("peer-a", PairingStatus.CONFIRMED)
    original = repo.instances["peer-a"].remote_inbox_url
    await handlers._on_url_updated(
        _event(
            FederationEventType.URL_UPDATED,
            {"inbox_url": "ftp://nope.example/x"},
        )
    )
    assert repo.instances["peer-a"].remote_inbox_url == original


@pytest.mark.parametrize(
    "bad_url",
    [
        "https://user:pw@new.example.com/federation/inbox/wh",
        "https:///federation/inbox/wh",
        "http://",
        "https://new.example.com/inbox\r\nX: y",
    ],
)
async def test_url_updated_rejects_malformed_household_url(repo, handlers, bad_url):
    """Same household-address rules as pairing: the URL is where every
    later envelope is POSTed, so credentials / no host / control chars
    never land in ``remote_inbox_url``."""
    repo.instances["peer-a"] = _sample_instance("peer-a", PairingStatus.CONFIRMED)
    original = repo.instances["peer-a"].remote_inbox_url
    await handlers._on_url_updated(
        _event(FederationEventType.URL_UPDATED, {"inbox_url": bad_url}),
    )
    assert repo.instances["peer-a"].remote_inbox_url == original


async def test_url_updated_accepts_lan_http(repo, handlers):
    repo.instances["peer-a"] = _sample_instance("peer-a", PairingStatus.CONFIRMED)
    url = "http://192.168.1.20:8123/api/socialhome/inbox/wh"
    await handlers._on_url_updated(
        _event(FederationEventType.URL_UPDATED, {"inbox_url": url}),
    )
    assert repo.instances["peer-a"].remote_inbox_url == url


async def test_url_updated_unknown_peer_is_noop(repo, handlers):
    # No side-effects even if the instance is not known locally.
    await handlers._on_url_updated(
        _event(
            FederationEventType.URL_UPDATED,
            {"inbox_url": "https://x/y"},
            from_instance="unknown-peer",
        )
    )
    assert "unknown-peer" not in repo.instances


# ─── INSTANCE_CAPABILITIES_UPDATED ────────────────────────────────────────


async def test_capabilities_updated_persists_proto_version(repo, handlers):
    """Peer announces ``proto_version=2`` → repo row is updated so
    later ``peer_supports`` calls see the new version."""
    repo.instances["peer-a"] = _sample_instance("peer-a", PairingStatus.CONFIRMED)
    await handlers._on_capabilities_updated(
        _event(
            FederationEventType.INSTANCE_CAPABILITIES_UPDATED,
            {"proto_version": 2},
        )
    )
    assert repo.instances["peer-a"].proto_version == 2
    assert repo.capabilities_seen == ["peer-a"]
    assert repo.instances["peer-a"].capabilities_seen_at is not None


async def test_capabilities_updated_same_version_still_stamps_seen(repo, handlers):
    """A re-advertisement of the SAME version still stamps
    ``capabilities_seen_at`` — so a paired-but-never-advertised v1 peer is
    distinguishable from a genuine v1 peer that just re-announced. The
    ``proto_version`` value short-circuits, but the seen-stamp must not."""
    repo.instances["peer-a"] = _sample_instance("peer-a", PairingStatus.CONFIRMED)
    # _sample_instance defaults to proto_version=1; re-advertise the same.
    assert repo.instances["peer-a"].proto_version == 1
    await handlers._on_capabilities_updated(
        _event(
            FederationEventType.INSTANCE_CAPABILITIES_UPDATED,
            {"proto_version": 1},
        )
    )
    assert repo.capabilities_seen == ["peer-a"]
    assert repo.instances["peer-a"].capabilities_seen_at is not None


async def test_capabilities_updated_unknown_instance_is_noop(repo, handlers):
    """Announcement from a peer we don't have a row for is dropped —
    we have no row to attach the version to and don't want to
    fabricate one (identity / keys are unknown)."""
    await handlers._on_capabilities_updated(
        _event(
            FederationEventType.INSTANCE_CAPABILITIES_UPDATED,
            {"proto_version": 2},
            from_instance="ghost-peer",
        )
    )
    assert "ghost-peer" not in repo.instances


async def test_capabilities_updated_invalid_payload_keeps_existing(repo, handlers):
    """Malformed announcement (non-int proto_version) is dropped —
    we never overwrite the existing version with a worse one."""
    repo.instances["peer-a"] = _sample_instance("peer-a", PairingStatus.CONFIRMED)
    # Seed an existing high version so the test catches an accidental
    # clobber.
    await repo.set_proto_version("peer-a", 5)
    await handlers._on_capabilities_updated(
        _event(
            FederationEventType.INSTANCE_CAPABILITIES_UPDATED,
            {"proto_version": "garbage"},
        )
    )
    assert repo.instances["peer-a"].proto_version == 5


async def test_capabilities_updated_applies_display_name(repo, handlers):
    """A capabilities envelope carrying a ``display_name`` updates the
    peer's stored advertised name — so a peer rename reaches us."""
    repo.instances["peer-a"] = _sample_instance("peer-a", PairingStatus.CONFIRMED)
    await handlers._on_capabilities_updated(
        _event(
            FederationEventType.INSTANCE_CAPABILITIES_UPDATED,
            {"proto_version": 2, "display_name": "Casa Vizeli"},
        )
    )
    assert repo.instances["peer-a"].display_name == "Casa Vizeli"


async def test_capabilities_updated_sanitizes_and_caps_display_name(repo, handlers):
    """A peer-controlled name is stripped of control chars and capped to 80
    chars before it's persisted — so a hostile peer can't store a multi-KB /
    multi-line / control-char name for layout-DoS or impersonation."""
    repo.instances["peer-a"] = _sample_instance("peer-a", PairingStatus.CONFIRMED)
    hostile = "Casa\nVizeli\t\x00" + ("A" * 200)
    await handlers._on_capabilities_updated(
        _event(
            FederationEventType.INSTANCE_CAPABILITIES_UPDATED,
            {"proto_version": 2, "display_name": hostile},
        )
    )
    stored = repo.instances["peer-a"].display_name
    assert len(stored) <= 80
    assert "\n" not in stored and "\t" not in stored and "\x00" not in stored
    assert stored.startswith("CasaVizeli")


async def test_capabilities_updated_applies_display_name_at_same_version(
    repo, handlers
):
    """A RENAME re-broadcast carries the SAME proto_version but a NEW name.
    The name update MUST land even though the proto_version short-circuits —
    otherwise an existing pairing stays stuck at the QR-time name."""
    repo.instances["peer-a"] = _sample_instance("peer-a", PairingStatus.CONFIRMED)
    # _sample_instance defaults to proto_version=1 and display_name="peer-a".
    assert repo.instances["peer-a"].proto_version == 1
    await handlers._on_capabilities_updated(
        _event(
            FederationEventType.INSTANCE_CAPABILITIES_UPDATED,
            {"proto_version": 1, "display_name": "Casa Vizeli"},
        )
    )
    # proto_version unchanged, but the name updated.
    assert repo.instances["peer-a"].proto_version == 1
    assert repo.instances["peer-a"].display_name == "Casa Vizeli"


async def test_capabilities_updated_blank_or_missing_name_is_noop(repo, handlers):
    """A missing or blank ``display_name`` leaves the stored name intact —
    older peers omit the field entirely and must not clear our name."""
    repo.instances["peer-a"] = _sample_instance("peer-a", PairingStatus.CONFIRMED)
    # Missing field (older peer).
    await handlers._on_capabilities_updated(
        _event(
            FederationEventType.INSTANCE_CAPABILITIES_UPDATED,
            {"proto_version": 1},
        )
    )
    assert repo.instances["peer-a"].display_name == "peer-a"
    # Blank field.
    await handlers._on_capabilities_updated(
        _event(
            FederationEventType.INSTANCE_CAPABILITIES_UPDATED,
            {"proto_version": 1, "display_name": "   "},
        )
    )
    assert repo.instances["peer-a"].display_name == "peer-a"


async def test_a_raised_proto_version_is_announced_on_the_bus(bus, repo, handlers):
    """Senders catch an upgraded household up (e.g. the v_32 roster
    snapshot) — only when the version really went up."""
    from socialhome.domain.events import PeerProtoVersionRaised

    seen: list = []
    bus.subscribe(PeerProtoVersionRaised, seen.append)
    repo.instances["peer-a"] = _sample_instance("peer-a", PairingStatus.CONFIRMED)
    for version in (32, 32, 5):
        await handlers._on_capabilities_updated(
            _event(
                FederationEventType.INSTANCE_CAPABILITIES_UPDATED,
                {"proto_version": version},
            )
        )
    assert [(e.instance_id, e.old_version, e.new_version) for e in seen] == [
        ("peer-a", 1, 32)
    ]
