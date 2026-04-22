"""Tests for :class:`AutoPairCoordinator` — three-party transitive
pairing. Covers helper blobs, error paths, and a full happy path
through request_via → B forward → C queue → finalize_pending → ack
at A.

The crypto is real (Ed25519 + X25519 + HKDF); the federation transport
is mocked with :class:`AsyncMock`.
"""

from __future__ import annotations

import os
from unittest.mock import AsyncMock

import pytest

from socialhome.crypto import (
    derive_instance_id,
    generate_identity_keypair,
)
from socialhome.domain.federation import (
    FederationEvent,
    FederationEventType,
    InstanceSource,
    PairingStatus,
    RemoteInstance,
)
from socialhome.federation.auto_pair_coordinator import (
    AutoPairCoordinator,
    _ack_blob,
    _derive_session_keys,
    _vouch_blob,
)
from socialhome.infrastructure.event_bus import EventBus
from socialhome.infrastructure.key_manager import KeyManager
from socialhome.services.auto_pair_inbox import AutoPairInbox


class _InMemoryFederationRepo:
    """Minimal fake for :class:`AbstractFederationRepo`."""

    def __init__(self) -> None:
        self.instances: dict[str, RemoteInstance] = {}

    async def get_instance(self, iid: str) -> RemoteInstance | None:
        return self.instances.get(iid)

    async def save_instance(self, inst: RemoteInstance) -> RemoteInstance:
        self.instances[inst.id] = inst
        return inst

    async def list_instances(self, *, source=None, status=None):
        out = list(self.instances.values())
        if status is not None:
            out = [i for i in out if i.status.value == status]
        return out


def _make_peer_remote_instance(pk_hex: str, status=PairingStatus.CONFIRMED):
    return RemoteInstance(
        id=derive_instance_id(bytes.fromhex(pk_hex)),
        display_name="Peer",
        remote_identity_pk=pk_hex,
        key_self_to_remote="enc",
        key_remote_to_self="enc",
        remote_webhook_url="https://peer.example/webhook",
        local_webhook_id="loc-" + os.urandom(4).hex(),
        status=status,
        source=InstanceSource.MANUAL,
    )


@pytest.fixture
def kek_manager():
    return KeyManager(os.urandom(32))


@pytest.fixture
def bus():
    return EventBus()


@pytest.fixture
def fed_repo():
    return _InMemoryFederationRepo()


@pytest.fixture
def inbox(bus):
    return AutoPairInbox(bus=bus)


@pytest.fixture
def fed_service():
    svc = AsyncMock()
    svc.send_event = AsyncMock()
    svc.own_webhook_url = "https://self.example/webhook"
    return svc


@pytest.fixture
def identity():
    return generate_identity_keypair()


@pytest.fixture
def coord(fed_repo, kek_manager, bus, fed_service, identity, inbox):
    return AutoPairCoordinator(
        federation_repo=fed_repo,
        key_manager=kek_manager,
        bus=bus,
        federation_service=fed_service,
        own_identity_seed=identity.private_key,
        own_identity_pk=identity.public_key,
        inbox=inbox,
    )


# ── Helpers: pure-function coverage ────────────────────────────────


def test_vouch_blob_shape():
    b = _vouch_blob(
        a_id="a",
        a_pk_hex="aa",
        a_webhook="http://x",
        a_dh_pk_hex="bb",
        c_id="c",
        ts="2026-04-20T00:00:00Z",
        nonce="n",
    )
    assert b.startswith(b"vouch/v1|")
    assert b.endswith(b"|n")


def test_ack_blob_shape():
    b = _ack_blob(
        a_id="a",
        a_dh_pk_hex="aa",
        c_id="c",
        c_dh_pk_hex="bb",
        ts="t",
        nonce="n",
    )
    assert b.startswith(b"ack/v1|")
    assert b.endswith(b"|n")


def test_derive_session_keys_produces_32_bytes():
    from socialhome.crypto import generate_x25519_keypair

    a = generate_x25519_keypair()
    b = generate_x25519_keypair()
    k1, k2 = _derive_session_keys(a.private_key, b.public_key.hex())
    assert len(k1) == 32
    assert len(k2) == 32
    assert k1 != k2


# ── request_via error paths ────────────────────────────────────────


async def test_request_via_rejects_unknown_peer(coord):
    with pytest.raises(ValueError, match="confirmed paired"):
        await coord.request_via(
            via_instance_id="no-such-peer",
            target_instance_id="target",
            target_display_name="T",
        )


async def test_request_via_rejects_self_target(coord, fed_repo, identity):
    # Set up a CONFIRMED "B" peer so the first check passes.
    b_kp = generate_identity_keypair()
    b = _make_peer_remote_instance(b_kp.public_key.hex())
    fed_repo.instances[b.id] = b
    own_id = derive_instance_id(identity.public_key)
    with pytest.raises(ValueError, match="yourself"):
        await coord.request_via(
            via_instance_id=b.id,
            target_instance_id=own_id,
            target_display_name="self",
        )


async def test_request_via_rejects_already_paired(coord, fed_repo):
    b_kp = generate_identity_keypair()
    b = _make_peer_remote_instance(b_kp.public_key.hex())
    fed_repo.instances[b.id] = b
    c_kp = generate_identity_keypair()
    c_existing = _make_peer_remote_instance(c_kp.public_key.hex())
    fed_repo.instances[c_existing.id] = c_existing
    with pytest.raises(ValueError, match="already paired"):
        await coord.request_via(
            via_instance_id=b.id,
            target_instance_id=c_existing.id,
            target_display_name="C",
        )


async def test_request_via_happy_path_sends_intro(coord, fed_repo, fed_service):
    b_kp = generate_identity_keypair()
    b = _make_peer_remote_instance(b_kp.public_key.hex())
    fed_repo.instances[b.id] = b
    c_kp = generate_identity_keypair()
    c_id = derive_instance_id(c_kp.public_key)
    r = await coord.request_via(
        via_instance_id=b.id,
        target_instance_id=c_id,
        target_display_name="Chess Club",
    )
    assert r["status"] == "sent"
    assert r["token"]
    fed_service.send_event.assert_awaited_once()
    kwargs = fed_service.send_event.await_args.kwargs
    assert kwargs["to_instance_id"] == b.id
    assert kwargs["event_type"] == FederationEventType.PAIRING_INTRO_AUTO
    # Provisional row exists.
    assert fed_repo.instances[c_id].status is PairingStatus.PENDING_SENT


# ── on_intro_from_peer (B side) ────────────────────────────────────


def _evt(from_instance: str, payload: dict) -> FederationEvent:
    return FederationEvent(
        msg_id="m",
        event_type=FederationEventType.PAIRING_INTRO_AUTO,
        from_instance=from_instance,
        to_instance="me",
        timestamp="2026-04-20T00:00:00+00:00",
        payload=payload,
    )


async def test_on_intro_from_peer_missing_fields_noop(coord, fed_service):
    await coord.on_intro_from_peer(_evt("any", {}))
    fed_service.send_event.assert_not_awaited()


async def test_on_intro_from_peer_bad_ts(coord, fed_service):
    await coord.on_intro_from_peer(
        _evt(
            "any",
            {
                "target_id": "t",
                "a_dh_pk": "dd",
                "ts": "not-a-ts",
                "nonce": "n",
                "token": "tk",
            },
        )
    )
    fed_service.send_event.assert_not_awaited()


async def test_on_intro_from_peer_stale_ts(coord, fed_service):
    # Timestamp from year 2000 → blown past TTL.
    await coord.on_intro_from_peer(
        _evt(
            "any",
            {
                "target_id": "t",
                "a_dh_pk": "dd",
                "ts": "2000-01-01T00:00:00+00:00",
                "nonce": "n",
                "token": "tk",
            },
        )
    )
    fed_service.send_event.assert_not_awaited()


async def test_on_intro_from_peer_unknown_sender(coord, fed_service):
    from datetime import datetime, timezone

    ts = datetime.now(timezone.utc).isoformat()
    await coord.on_intro_from_peer(
        _evt(
            "no-sender",
            {
                "target_id": "t",
                "a_dh_pk": "dd",
                "ts": ts,
                "nonce": "n",
                "token": "tk",
                "a_webhook": "",
            },
        )
    )
    fed_service.send_event.assert_not_awaited()


async def test_on_intro_from_peer_unknown_target(coord, fed_repo, fed_service):
    from datetime import datetime, timezone

    a_kp = generate_identity_keypair()
    a = _make_peer_remote_instance(a_kp.public_key.hex())
    fed_repo.instances[a.id] = a
    ts = datetime.now(timezone.utc).isoformat()
    await coord.on_intro_from_peer(
        _evt(
            a.id,
            {
                "target_id": "unknown-target",
                "a_dh_pk": "dd",
                "ts": ts,
                "nonce": "n",
                "token": "tk",
                "a_webhook": "",
            },
        )
    )
    fed_service.send_event.assert_not_awaited()


async def test_on_intro_from_peer_forwards_to_c(coord, fed_repo, fed_service):
    from datetime import datetime, timezone

    a_kp = generate_identity_keypair()
    a = _make_peer_remote_instance(a_kp.public_key.hex())
    fed_repo.instances[a.id] = a
    c_kp = generate_identity_keypair()
    c = _make_peer_remote_instance(c_kp.public_key.hex())
    fed_repo.instances[c.id] = c
    ts = datetime.now(timezone.utc).isoformat()
    await coord.on_intro_from_peer(
        _evt(
            a.id,
            {
                "target_id": c.id,
                "a_dh_pk": "dd" * 16,
                "ts": ts,
                "nonce": "n" * 16,
                "token": "tk",
                "a_webhook": "http://a",
            },
        )
    )
    fed_service.send_event.assert_awaited_once()
    fwd = fed_service.send_event.await_args.kwargs
    assert fwd["to_instance_id"] == c.id
    assert fwd["event_type"] == FederationEventType.PAIRING_INTRO_AUTO
    assert fwd["payload"]["from_a_id"] == a.id
    assert fwd["payload"]["via_b_id"]
    assert fwd["payload"]["vouch_sig"]


# ── on_intro_at_target (C side) ────────────────────────────────────


async def test_on_intro_at_target_routes_to_peer_handler_when_no_via(
    coord, fed_service
):
    # When payload lacks via_b_id, it's really an A→B message that
    # reached C (shouldn't happen in prod but the code handles it).
    await coord.on_intro_at_target(_evt("sender", {}))
    fed_service.send_event.assert_not_awaited()


async def test_on_intro_at_target_missing_fields_noop(coord, inbox):
    await coord.on_intro_at_target(_evt("sender", {"via_b_id": "b"}))
    assert inbox.list_pending() == []


async def test_on_intro_at_target_unknown_via(coord, inbox):
    await coord.on_intro_at_target(
        _evt(
            "sender",
            {
                "via_b_id": "no-such-b",
                "from_a_id": "a",
                "from_a_pk": "aa",
                "from_a_webhook": "w",
                "from_a_dh_pk": "dd",
                "vouch_sig": "ab",
                "ts": "2026-04-20T00:00:00+00:00",
                "nonce": "n",
                "token": "tk",
            },
        )
    )
    assert inbox.list_pending() == []


async def test_on_intro_at_target_bad_vouch_sig(coord, fed_repo, inbox):
    # B is a confirmed peer — we'll mess up the vouch signature so
    # verify fails.
    b_kp = generate_identity_keypair()
    b = _make_peer_remote_instance(b_kp.public_key.hex())
    fed_repo.instances[b.id] = b
    await coord.on_intro_at_target(
        _evt(
            b.id,
            {
                "via_b_id": b.id,
                "from_a_id": "a",
                "from_a_pk": "aa" * 16,
                "from_a_webhook": "w",
                "from_a_dh_pk": "dd" * 16,
                "vouch_sig": "ab" * 32,
                "ts": "2026-04-20T00:00:00+00:00",
                "nonce": "n",
                "token": "tk",
            },
        )
    )
    assert inbox.list_pending() == []


# ── finalize_pending / decline_pending ─────────────────────────────


async def test_finalize_pending_unknown_raises(coord):
    with pytest.raises(KeyError):
        await coord.finalize_pending("nope")


async def test_decline_pending_unknown_raises(coord):
    with pytest.raises(KeyError):
        await coord.decline_pending("nope")


# ── Full 3-party happy path ────────────────────────────────────────


async def test_full_auto_pair_handshake(
    kek_manager, fed_repo, bus, fed_service, identity, inbox, coord
):
    """Exercise the complete A → B → C → A flow with real crypto."""
    from datetime import datetime, timezone

    from socialhome.crypto import (
        derive_instance_id as _diid,
        generate_identity_keypair as _gik,
    )

    # Set up: B = `coord`'s identity (we simulate the B node).
    # We need a separate A and a separate C.
    a_kp = _gik()
    c_kp = _gik()
    a_id = _diid(a_kp.public_key)
    c_id = _diid(c_kp.public_key)

    # As B, we already have A and C as confirmed peers.
    a_inst = _make_peer_remote_instance(a_kp.public_key.hex())
    c_inst = _make_peer_remote_instance(c_kp.public_key.hex())
    assert a_inst.id == a_id
    assert c_inst.id == c_id
    fed_repo.instances[a_id] = a_inst
    fed_repo.instances[c_id] = c_inst

    # Step: A's intro arrives at B (coord).
    from socialhome.crypto import generate_x25519_keypair

    a_dh = generate_x25519_keypair()
    ts = datetime.now(timezone.utc).isoformat()
    await coord.on_intro_from_peer(
        _evt(
            a_id,
            {
                "target_id": c_id,
                "a_dh_pk": a_dh.public_key.hex(),
                "a_webhook": "https://a.example/webhook",
                "ts": ts,
                "nonce": "n" * 16,
                "token": "tk",
            },
        )
    )
    # B forwarded to C.
    assert fed_service.send_event.await_count == 1
    forward = fed_service.send_event.await_args.kwargs["payload"]
    assert forward["from_a_id"] == a_id
    assert forward["via_b_id"] == _diid(identity.public_key)
    assert forward["vouch_sig"]

    # Simulate C receiving that forward. We reuse ``coord`` on the
    # assumption we're now acting as C, but C's own_identity is
    # different from B's. So spin up a separate coordinator for C.
    c_coord = AutoPairCoordinator(
        federation_repo=_InMemoryFederationRepo(),
        key_manager=kek_manager,
        bus=bus,
        federation_service=fed_service,
        own_identity_seed=c_kp.private_key,
        own_identity_pk=c_kp.public_key,
        inbox=inbox,
    )
    # C knows B (same identity as coord's own identity).
    b_pk_hex = identity.public_key.hex()
    b_as_peer = _make_peer_remote_instance(b_pk_hex)
    c_coord._repo.instances[b_as_peer.id] = b_as_peer

    # Feed forwarded event into C.
    await c_coord.on_intro_at_target(
        FederationEvent(
            msg_id="m2",
            event_type=FederationEventType.PAIRING_INTRO_AUTO,
            from_instance=b_as_peer.id,
            to_instance=c_id,
            timestamp=ts,
            payload=forward,
        ),
    )
    pending = inbox.list_pending()
    assert len(pending) == 1
    req = pending[0]
    assert req.from_a_id == a_id

    # C admin approves.
    fed_service.send_event.reset_mock()
    confirmed = await c_coord.finalize_pending(req.request_id)
    assert confirmed.status is PairingStatus.CONFIRMED
    # Ack sent to A.
    assert fed_service.send_event.await_count == 1
    ack_kwargs = fed_service.send_event.await_args.kwargs
    assert ack_kwargs["event_type"] == FederationEventType.PAIRING_INTRO_AUTO_ACK
    assert ack_kwargs["to_instance_id"] == a_id


async def test_decline_pending_sends_abort(coord, inbox, fed_service):
    # Enqueue a fake pending request directly via the inbox.
    req = inbox.enqueue(
        from_a_id="a1",
        from_a_pk="aa",
        from_a_webhook="w",
        from_a_dh_pk="dd",
        via_b_id="b1",
        vouch_sig="vv",
        ts="2026-04-20T00:00:00+00:00",
        nonce="n",
        token="tk",
        from_a_display="A",
        via_b_display="B",
    )
    fed_service.send_event.reset_mock()
    await coord.decline_pending(req.request_id, reason="spam")
    fed_service.send_event.assert_awaited_once()
    kwargs = fed_service.send_event.await_args.kwargs
    assert kwargs["event_type"] == FederationEventType.PAIRING_ABORT
    assert kwargs["payload"]["reason"] == "spam"


# ── on_ack_at_originator ───────────────────────────────────────────


async def test_on_ack_unknown_token_noop(coord):
    await coord.on_ack_at_originator(_evt("c", {"token": "nope"}))


async def test_on_ack_target_mismatch(coord, fed_repo):
    # Get a session into _pending via request_via.
    b_kp = generate_identity_keypair()
    b = _make_peer_remote_instance(b_kp.public_key.hex())
    fed_repo.instances[b.id] = b
    c_kp = generate_identity_keypair()
    c_id = derive_instance_id(c_kp.public_key)
    r = await coord.request_via(
        via_instance_id=b.id,
        target_instance_id=c_id,
        target_display_name="",
    )
    # Reply with a mismatched c_id → early return.
    await coord.on_ack_at_originator(
        _evt(
            "wrong",
            {
                "token": r["token"],
                "c_id": "something-else",
                "via_b_id": b.id,
            },
        )
    )
    # instance still PENDING_SENT
    assert fed_repo.instances[c_id].status is PairingStatus.PENDING_SENT
