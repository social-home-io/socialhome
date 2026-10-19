"""Tests for :class:`AutoPairCoordinator` — three-party transitive
pairing. Covers helper blobs, error paths, and a full happy path
through request_via → B forward → C queue → finalize_pending → ack
at A.

The crypto is real (Ed25519 + X25519 + HKDF); the federation transport
is mocked with :class:`AsyncMock`.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import pytest

from socialhome.crypto import (
    derive_instance_id,
    generate_identity_keypair,
    generate_x25519_keypair,
    sign_ed25519,
)
from socialhome.domain.events import PairingConfirmed
from socialhome.domain.federation import (
    AutoPairRequestExpiredError,
    FederationEvent,
    FederationEventType,
    InstanceSource,
    PairingAlreadyConfirmedError,
    PairingStatus,
    RemoteInstance,
)
from socialhome.domain.preferences import HouseholdPreferences
from socialhome.federation.auto_pair_coordinator import (
    AutoPairCoordinator,
    _ack_blob,
    _derive_session_keys,
    _vouch_blob,
)
from socialhome.infrastructure.event_bus import EventBus
from socialhome.infrastructure.key_manager import KeyManager
from socialhome.services.auto_pair_inbox import (
    AUTO_PAIR_REQUEST_TTL_SECONDS,
    AutoPairInbox,
)


class _InMemoryFederationRepo:
    """Minimal fake for :class:`AbstractFederationRepo`."""

    def __init__(self) -> None:
        self.instances: dict[str, RemoteInstance] = {}

    async def get_instance(self, iid: str) -> RemoteInstance | None:
        return self.instances.get(iid)

    async def get_instance_by_local_inbox_id(
        self, local_inbox_id: str
    ) -> RemoteInstance | None:
        for inst in self.instances.values():
            if inst.local_inbox_id == local_inbox_id:
                return inst
        return None

    async def save_instance(self, inst: RemoteInstance) -> RemoteInstance:
        self.instances[inst.id] = inst
        return inst

    async def promote_pending_instance(self, inst, *, token=None):
        """Mirrors the SQLite repo: only over a still-pending row."""
        row = self.instances.get(inst.id)
        if row is None or row.status is PairingStatus.CONFIRMED:
            return False
        self.instances[inst.id] = inst
        return True

    async def list_social_instances(self):
        return await self.list_instances(status="confirmed")

    async def list_instances(self, *, source=None, status=None):
        out = list(self.instances.values())
        if status is not None:
            out = [i for i in out if i.status.value == status]
        return out

    async def list_instances_in_space(self, space_id):
        return []


def _make_peer_remote_instance(
    pk_hex: str,
    status=PairingStatus.CONFIRMED,
    source=InstanceSource.MANUAL,
):
    return RemoteInstance(
        id=derive_instance_id(bytes.fromhex(pk_hex)),
        display_name="Peer",
        remote_identity_pk=pk_hex,
        key_self_to_remote="enc",
        key_remote_to_self="enc",
        remote_inbox_url="https://peer.example/inbox",
        local_inbox_id="loc-" + os.urandom(4).hex(),
        status=status,
        source=source,
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
    svc.own_inbox_url = "https://self.example/inbox"
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
        a_inbox_url="http://x",
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
    k1, k2 = _derive_session_keys(a.private_key, b.public_key.hex(), is_initiator=True)
    assert len(k1) == 32
    assert len(k2) == 32
    assert k1 != k2


def test_derive_session_keys_role_mirrors_between_sides():
    """A's outbound key must equal D's inbound key, and vice versa.

    Both sides derive the same shared ECDH secret, but the
    ``self``/``remote`` labels they apply to the derived keys are
    *mirror images*: the initiator's ``self-to-remote`` is the
    acceptor's ``remote-to-self``. Without role-anchored info strings
    in :func:`_derive_session_keys`, both sides label the keys
    identically — every envelope across the relay-paired link
    decrypts with the wrong key and the federation inbox surfaces it
    as ``400 Failed to decrypt payload``."""
    from socialhome.crypto import generate_x25519_keypair

    a = generate_x25519_keypair()
    c = generate_x25519_keypair()

    a_self, a_remote = _derive_session_keys(
        a.private_key, c.public_key.hex(), is_initiator=True
    )
    c_self, c_remote = _derive_session_keys(
        c.private_key, a.public_key.hex(), is_initiator=False
    )

    # A→C: encrypted with a_self, decrypted with c_remote.
    assert a_self == c_remote
    # C→A: encrypted with c_self, decrypted with a_remote.
    assert c_self == a_remote
    # Each side's two directional keys are distinct.
    assert a_self != a_remote


# ── request_via error paths ────────────────────────────────────────


_OWN_INBOX_BASE = "https://a.example/federation/inbox"


async def test_request_via_rejects_unknown_peer(coord):
    with pytest.raises(ValueError, match="confirmed paired"):
        await coord.request_via(
            via_instance_id="no-such-peer",
            target_instance_id="target",
            target_display_name="T",
            own_inbox_base_url=_OWN_INBOX_BASE,
        )


async def test_request_via_rejects_space_session_voucher(coord, fed_repo):
    """§D2b — an invite-link household never vouches for a third party.

    A ``space_session`` row is CONFIRMED but carries no QR handshake and
    no out-of-band SAS, so there is no trust to transit: letting it
    introduce us would turn "I joined their space" into a pairing with
    somebody we have never met.
    """
    b_kp = generate_identity_keypair()
    b = _make_peer_remote_instance(
        b_kp.public_key.hex(),
        source=InstanceSource.SPACE_SESSION,
    )
    fed_repo.instances[b.id] = b
    with pytest.raises(ValueError, match="confirmed paired"):
        await coord.request_via(
            via_instance_id=b.id,
            target_instance_id="target",
            target_display_name="T",
            own_inbox_base_url=_OWN_INBOX_BASE,
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
            own_inbox_base_url=_OWN_INBOX_BASE,
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
            own_inbox_base_url=_OWN_INBOX_BASE,
        )


async def test_request_via_rejects_missing_base_url(coord, fed_repo):
    """Regression guard: empty ``own_inbox_base_url`` would mean A sends
    an empty ``a_inbox_url``, which C would silently reject — the only
    feedback being a stuck spinner. Surface it as a 422 instead."""
    b_kp = generate_identity_keypair()
    b = _make_peer_remote_instance(b_kp.public_key.hex())
    fed_repo.instances[b.id] = b
    c_kp = generate_identity_keypair()
    c_id = derive_instance_id(c_kp.public_key)
    with pytest.raises(ValueError, match="own_inbox_base_url"):
        await coord.request_via(
            via_instance_id=b.id,
            target_instance_id=c_id,
            target_display_name="C",
            own_inbox_base_url="",
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
        own_inbox_base_url=_OWN_INBOX_BASE,
    )
    assert r["status"] == "sent"
    assert r["token"]
    fed_service.send_event.assert_awaited_once()
    kwargs = fed_service.send_event.await_args.kwargs
    assert kwargs["to_instance_id"] == b.id
    assert kwargs["event_type"] == FederationEventType.PAIRING_INTRO_AUTO
    # Provisional row exists.
    assert fed_repo.instances[c_id].status is PairingStatus.PENDING_SENT
    # The wire payload must carry a non-empty ``a_inbox_url`` — that's
    # how C's ``on_intro_at_target`` validates the introduction. The
    # silent-empty bug had bitten the harness before this regression
    # guard landed.
    payload = kwargs["payload"]
    assert payload["a_inbox_url"].startswith(_OWN_INBOX_BASE)
    assert payload["a_inbox_url"] != _OWN_INBOX_BASE  # has the per-peer suffix


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
    from datetime import datetime

    ts = datetime.now(UTC).isoformat()
    await coord.on_intro_from_peer(
        _evt(
            "no-sender",
            {
                "target_id": "t",
                "a_dh_pk": "dd",
                "ts": ts,
                "nonce": "n",
                "token": "tk",
                "a_inbox_url": "",
            },
        )
    )
    fed_service.send_event.assert_not_awaited()


async def test_on_intro_from_peer_unknown_target(coord, fed_repo, fed_service):
    from datetime import datetime

    a_kp = generate_identity_keypair()
    a = _make_peer_remote_instance(a_kp.public_key.hex())
    fed_repo.instances[a.id] = a
    ts = datetime.now(UTC).isoformat()
    await coord.on_intro_from_peer(
        _evt(
            a.id,
            {
                "target_id": "unknown-target",
                "a_dh_pk": "dd",
                "ts": ts,
                "nonce": "n",
                "token": "tk",
                "a_inbox_url": "",
            },
        )
    )
    fed_service.send_event.assert_not_awaited()


async def test_on_intro_from_peer_forwards_to_c(coord, fed_repo, fed_service):
    from datetime import datetime

    a_kp = generate_identity_keypair()
    a = _make_peer_remote_instance(a_kp.public_key.hex())
    fed_repo.instances[a.id] = a
    c_kp = generate_identity_keypair()
    c = _make_peer_remote_instance(c_kp.public_key.hex())
    fed_repo.instances[c.id] = c
    ts = datetime.now(UTC).isoformat()
    await coord.on_intro_from_peer(
        _evt(
            a.id,
            {
                "target_id": c.id,
                "a_dh_pk": "dd" * 16,
                "ts": ts,
                "nonce": "n" * 16,
                "token": "tk",
                "a_inbox_url": "http://a",
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
                "from_a_inbox_url": "w",
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
                "from_a_inbox_url": "w",
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
        await coord.finalize_pending(
            "nope",
            own_inbox_base_url=_OWN_INBOX_BASE,
        )


async def test_decline_pending_unknown_raises(coord):
    with pytest.raises(KeyError):
        await coord.decline_pending("nope")


# ── Full 3-party happy path ────────────────────────────────────────


async def test_full_auto_pair_handshake(
    kek_manager, fed_repo, bus, fed_service, identity, inbox, coord
):
    """Exercise the complete A → B → C → A flow with real crypto."""
    from datetime import datetime

    from socialhome.crypto import (
        derive_instance_id as _diid,
    )
    from socialhome.crypto import (
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
    ts = datetime.now(UTC).isoformat()
    await coord.on_intro_from_peer(
        _evt(
            a_id,
            {
                "target_id": c_id,
                "a_dh_pk": a_dh.public_key.hex(),
                "a_inbox_url": "https://a.example/inbox",
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
    confirmed = await c_coord.finalize_pending(
        req.request_id,
        own_inbox_base_url="https://c.example/federation/inbox",
    )
    # Not confirmed until A, having confirmed on its side, talks to us.
    assert confirmed.status is PairingStatus.PENDING_SENT
    # Ack sent to B (the trust relay), not directly to A — A has no
    # confirmed pairing with C yet, so the envelope-level signature
    # check on A would reject a direct C→A ack. B re-enwraps and
    # forwards to A using the established A↔B trust.
    assert fed_service.send_event.await_count == 1
    ack_kwargs = fed_service.send_event.await_args.kwargs
    assert ack_kwargs["event_type"] == FederationEventType.PAIRING_INTRO_AUTO_ACK_VIA
    assert ack_kwargs["to_instance_id"] == _diid(identity.public_key)
    assert ack_kwargs["payload"]["to_a_id"] == a_id


async def test_decline_pending_sends_abort(coord, inbox, fed_service):
    # Enqueue a fake pending request directly via the inbox.
    req = inbox.enqueue(
        from_a_id="a1",
        from_a_pk="aa",
        from_a_inbox_url="w",
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


# ── on_ack_via_at_relay (B side) ───────────────────────────────────


async def test_on_ack_via_unknown_a_noop(coord, fed_service):
    """B refuses to forward an ack-via for an unknown a_id — silent
    drop, but logs a warning (visible in operator logs)."""
    await coord.on_ack_via_at_relay(_evt("c", {"to_a_id": "unknown.a"}))
    fed_service.send_event.assert_not_awaited()


async def test_on_ack_via_forwards_to_a(coord, fed_repo, fed_service):
    a_kp = generate_identity_keypair()
    a_inst = _make_peer_remote_instance(a_kp.public_key.hex())
    fed_repo.instances[a_inst.id] = a_inst
    payload = {
        "to_a_id": a_inst.id,
        "a_id": a_inst.id,
        "c_id": "c.id",
        "c_pk": "cc" * 32,
        "c_inbox_url": "https://c.example/inbox/x",
        "c_dh_pk": "dd" * 32,
        "via_b_id": "b.id",
        "vouch_sig": "ab" * 32,
        "ack_sig": "cd" * 32,
        "ts": "2026-04-20T00:00:00+00:00",
        "nonce": "n" * 16,
        "token": "tk",
    }
    await coord.on_ack_via_at_relay(_evt("c.id", payload))
    fed_service.send_event.assert_awaited_once()
    kwargs = fed_service.send_event.await_args.kwargs
    assert kwargs["event_type"] == FederationEventType.PAIRING_INTRO_AUTO_ACK
    assert kwargs["to_instance_id"] == a_inst.id
    # ``to_a_id`` is a B-only routing field — strip before forwarding
    # so A's ``on_ack_at_originator`` doesn't see it.
    assert "to_a_id" not in kwargs["payload"]
    assert kwargs["payload"]["c_pk"] == "cc" * 32


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
        own_inbox_base_url=_OWN_INBOX_BASE,
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


# ── household address checks on the trust-relay flow ───────────────

_BAD_INBOX_URLS = [
    "file:///etc/passwd",
    "ftp://a.example/inbox",
    "javascript:alert(1)",
    "https:///inbox",
    "https://user:pw@a.example/inbox",
]


@pytest.mark.parametrize("bad_url", _BAD_INBOX_URLS)
async def test_on_intro_from_peer_refuses_to_vouch_for_invalid_inbox_url(
    coord, fed_repo, fed_service, bad_url
):
    a = _make_peer_remote_instance(generate_identity_keypair().public_key.hex())
    c = _make_peer_remote_instance(generate_identity_keypair().public_key.hex())
    fed_repo.instances[a.id] = a
    fed_repo.instances[c.id] = c
    await coord.on_intro_from_peer(
        _evt(
            a.id,
            {
                "target_id": c.id,
                "a_dh_pk": "dd" * 16,
                "ts": datetime.now(UTC).isoformat(),
                "nonce": "n" * 16,
                "token": "tk",
                "a_inbox_url": bad_url,
            },
        )
    )
    # B never signs a vouch over an address it would not itself call.
    fed_service.send_event.assert_not_awaited()


def _vouched_intro(b_kp, *, c_id: str, a_inbox_url: str) -> dict:
    a_kp = generate_identity_keypair()
    a_id = derive_instance_id(a_kp.public_key)
    ts = datetime.now(UTC).isoformat()
    a_dh = "dd" * 32
    sig = sign_ed25519(
        b_kp.private_key,
        _vouch_blob(
            a_id=a_id,
            a_pk_hex=a_kp.public_key.hex(),
            a_inbox_url=a_inbox_url,
            a_dh_pk_hex=a_dh,
            c_id=c_id,
            ts=ts,
            nonce="n" * 16,
        ),
    )
    return {
        "via_b_id": derive_instance_id(b_kp.public_key),
        "from_a_id": a_id,
        "from_a_pk": a_kp.public_key.hex(),
        "from_a_inbox_url": a_inbox_url,
        "from_a_dh_pk": a_dh,
        "vouch_sig": sig.hex(),
        "ts": ts,
        "nonce": "n" * 16,
        "token": "tk",
    }


@pytest.mark.parametrize("bad_url", _BAD_INBOX_URLS)
async def test_on_intro_at_target_rejects_invalid_inbox_url_even_if_vouched(
    coord, fed_repo, inbox, identity, bad_url
):
    b_kp = generate_identity_keypair()
    b = _make_peer_remote_instance(b_kp.public_key.hex())
    fed_repo.instances[b.id] = b
    c_id = derive_instance_id(identity.public_key)
    await coord.on_intro_at_target(
        _evt(b.id, _vouched_intro(b_kp, c_id=c_id, a_inbox_url=bad_url)),
    )
    assert inbox.list_pending() == []


async def test_on_intro_at_target_queues_valid_lan_inbox_url(
    coord, fed_repo, inbox, identity
):
    """Control for the test above — same vouch, a valid LAN address."""
    b_kp = generate_identity_keypair()
    b = _make_peer_remote_instance(b_kp.public_key.hex())
    fed_repo.instances[b.id] = b
    c_id = derive_instance_id(identity.public_key)
    await coord.on_intro_at_target(
        _evt(
            b.id,
            _vouched_intro(
                b_kp,
                c_id=c_id,
                a_inbox_url="http://192.168.1.20:8123/federation/inbox/x",
            ),
        ),
    )
    assert len(inbox.list_pending()) == 1


@pytest.mark.parametrize("bad_url", _BAD_INBOX_URLS)
async def test_on_ack_at_originator_rejects_invalid_c_inbox_url(
    coord, fed_repo, caplog, bad_url
):
    b = _make_peer_remote_instance(generate_identity_keypair().public_key.hex())
    fed_repo.instances[b.id] = b
    c_id = derive_instance_id(generate_identity_keypair().public_key)
    r = await coord.request_via(
        via_instance_id=b.id,
        target_instance_id=c_id,
        target_display_name="",
        own_inbox_base_url=_OWN_INBOX_BASE,
    )
    with caplog.at_level("WARNING"):
        await coord.on_ack_at_originator(
            _evt(
                b.id,
                {
                    "token": r["token"],
                    "c_id": c_id,
                    "via_b_id": b.id,
                    "c_inbox_url": bad_url,
                },
            )
        )
    assert fed_repo.instances[c_id].status is PairingStatus.PENDING_SENT
    assert "invalid c_inbox_url" in caplog.text


# ── cancelling an outstanding request (A side) ─────────────────────


async def _request_and_signed_ack(coord, fed_repo, identity) -> tuple[str, dict]:
    """A (``coord``) asks B to introduce it to C; return ``(c_id, ack)`` where
    ``ack`` is the valid ``PAIRING_INTRO_AUTO_ACK`` payload B would forward
    once C's admin approved (vouch signed by B, ack signed by C)."""
    b_kp = generate_identity_keypair()
    b = _make_peer_remote_instance(b_kp.public_key.hex())
    fed_repo.instances[b.id] = b
    c_kp = generate_identity_keypair()
    c_id = derive_instance_id(c_kp.public_key)
    r = await coord.request_via(
        via_instance_id=b.id,
        target_instance_id=c_id,
        target_display_name="C",
        own_inbox_base_url=_OWN_INBOX_BASE,
    )
    session = coord._pending[r["token"]]
    a_id = derive_instance_id(identity.public_key)
    c_dh = generate_x25519_keypair()
    vouch = sign_ed25519(
        b_kp.private_key,
        _vouch_blob(
            a_id=a_id,
            a_pk_hex=identity.public_key.hex(),
            a_inbox_url=session.a_inbox_url,
            a_dh_pk_hex=session.dh_pk_hex,
            c_id=c_id,
            ts=session.ts,
            nonce=session.nonce,
        ),
    )
    ack = sign_ed25519(
        c_kp.private_key,
        _ack_blob(
            a_id=a_id,
            a_dh_pk_hex=session.dh_pk_hex,
            c_id=c_id,
            c_dh_pk_hex=c_dh.public_key.hex(),
            ts=session.ts,
            nonce=session.nonce,
        ),
    )
    return c_id, {
        "token": r["token"],
        "a_id": a_id,
        "c_id": c_id,
        "c_pk": c_kp.public_key.hex(),
        "c_inbox_url": "https://c.example/federation/inbox/c1",
        "c_dh_pk": c_dh.public_key.hex(),
        "via_b_id": b.id,
        "vouch_sig": vouch.hex(),
        "ack_sig": ack.hex(),
        "ts": session.ts,
        "nonce": session.nonce,
    }


async def test_valid_ack_confirms_the_request(coord, fed_repo, identity):
    """Control for the cancel test below: this ack is good."""
    c_id, ack = await _request_and_signed_ack(coord, fed_repo, identity)
    await coord.on_ack_at_originator(_evt("b", ack))
    assert fed_repo.instances[c_id].status is PairingStatus.CONFIRMED


async def test_forget_requests_to_refuses_a_late_ack(coord, fed_repo, identity, bus):
    """A cancelled request must never be completed by C's approval arriving
    afterwards — the in-memory session is what an ack is checked against."""
    c_id, ack = await _request_and_signed_ack(coord, fed_repo, identity)
    confirmed: list[PairingConfirmed] = []
    bus.subscribe(PairingConfirmed, confirmed.append)

    assert coord.forget_requests_to(c_id) == 1
    await coord.on_ack_at_originator(_evt("b", ack))

    assert fed_repo.instances[c_id].status is PairingStatus.PENDING_SENT
    assert confirmed == []


async def test_forget_requests_to_leaves_other_targets(coord, fed_repo, identity):
    c_id, _ack = await _request_and_signed_ack(coord, fed_repo, identity)
    other_id, other_ack = await _request_and_signed_ack(coord, fed_repo, identity)
    assert coord.forget_requests_to(c_id) == 1
    assert coord.forget_requests_to(c_id) == 0
    await coord.on_ack_at_originator(_evt("b", other_ack))
    assert fed_repo.instances[other_id].status is PairingStatus.CONFIRMED


# ── The target's approval is not a pairing until the requester confirms ──


def _queued_request(inbox, *, ts: str | None = None, a_id: str = "a1"):
    a_kp = generate_identity_keypair()
    return inbox.enqueue(
        from_a_id=derive_instance_id(a_kp.public_key) if a_id == "a1" else a_id,
        from_a_pk=a_kp.public_key.hex(),
        from_a_inbox_url="https://a.example/federation/inbox/x",
        from_a_dh_pk=generate_x25519_keypair().public_key.hex(),
        via_b_id="b1",
        vouch_sig="vv",
        ts=ts or datetime.now(UTC).isoformat(),
        nonce="n",
        token="tk",
        from_a_display="A",
        via_b_display="B",
    )


async def test_approval_stores_a_pending_row_and_confirms_nothing_yet(
    coord, inbox, fed_repo, fed_service, bus
):
    """A may have cancelled its request meanwhile: approving must not leave
    us with a one-sided confirmed pairing. The row waits (keyed,
    ``pending_sent``) for A's first authenticated envelope."""
    confirmed: list[PairingConfirmed] = []
    bus.subscribe(PairingConfirmed, confirmed.append)
    req = _queued_request(inbox)

    inst = await coord.finalize_pending(
        req.request_id, own_inbox_base_url="https://c.example/federation/inbox"
    )

    assert inst.status is PairingStatus.PENDING_SENT
    assert inst.remote_identity_pk and inst.key_self_to_remote
    assert fed_repo.instances[req.from_a_id].status is PairingStatus.PENDING_SENT
    assert confirmed == []
    # The ack still goes back through the introducer.
    kwargs = fed_service.send_event.await_args.kwargs
    assert kwargs["event_type"] == FederationEventType.PAIRING_INTRO_AUTO_ACK_VIA


async def test_approving_a_request_from_a_paired_household_changes_nothing(
    coord, inbox, fed_repo, fed_service
):
    req = _queued_request(inbox)
    live = _make_peer_remote_instance(req.from_a_pk)
    fed_repo.instances[live.id] = live
    fed_service.send_event.reset_mock()

    with pytest.raises(PairingAlreadyConfirmedError, match="(?i)already paired"):
        await coord.finalize_pending(
            req.request_id, own_inbox_base_url="https://c.example/federation/inbox"
        )

    assert fed_repo.instances[live.id].status is PairingStatus.CONFIRMED
    assert fed_repo.instances[live.id].key_self_to_remote == "enc"
    fed_service.send_event.assert_not_awaited()


async def test_a_stale_request_cannot_be_approved(coord, inbox, fed_repo, fed_service):
    old = (
        datetime.now(UTC) - timedelta(seconds=AUTO_PAIR_REQUEST_TTL_SECONDS + 60)
    ).isoformat()
    req = _queued_request(inbox, ts=old)
    fed_service.send_event.reset_mock()

    with pytest.raises(AutoPairRequestExpiredError):
        await coord.finalize_pending(
            req.request_id, own_inbox_base_url="https://c.example/federation/inbox"
        )

    assert req.from_a_id not in fed_repo.instances
    assert inbox.get(req.request_id) is None
    fed_service.send_event.assert_not_awaited()


async def test_ack_after_the_request_was_cancelled_never_confirms(
    coord, fed_repo, identity, bus
):
    """The cancel committed after the ack had already taken the in-memory
    request: the provisional row is gone, so nothing is written back."""
    c_id, ack = await _request_and_signed_ack(coord, fed_repo, identity)
    confirmed: list[PairingConfirmed] = []
    bus.subscribe(PairingConfirmed, confirmed.append)
    del fed_repo.instances[c_id]  # the cancel's transaction

    await coord.on_ack_at_originator(_evt("b", ack))

    assert c_id not in fed_repo.instances
    assert confirmed == []


async def test_an_ack_for_a_request_past_its_ttl_is_ignored(coord, fed_repo, identity):
    c_id, ack = await _request_and_signed_ack(coord, fed_repo, identity)
    session = coord._pending[ack["token"]]
    session.ts = (
        datetime.now(UTC) - timedelta(seconds=AUTO_PAIR_REQUEST_TTL_SECONDS + 60)
    ).isoformat()

    await coord.on_ack_at_originator(_evt("b", ack))

    assert fed_repo.instances[c_id].status is PairingStatus.PENDING_SENT


@pytest.mark.parametrize(
    "target",
    ["../connections/" + "a" * 32, "a" * 31, "A" * 32, "a" * 31 + "/"],
)
async def test_request_via_refuses_a_target_that_is_not_a_household_id(
    coord, fed_repo, fed_service, target
):
    """The target id becomes our provisional row's id — and from there a
    URL path segment in the SPA. Only a derived household id is a target."""
    b = _make_peer_remote_instance(generate_identity_keypair().public_key.hex())
    fed_repo.instances[b.id] = b
    with pytest.raises(ValueError, match="household id"):
        await coord.request_via(
            via_instance_id=b.id,
            target_instance_id=target,
            target_display_name="C",
            own_inbox_base_url=_OWN_INBOX_BASE,
        )
    assert target not in fed_repo.instances
    fed_service.send_event.assert_not_awaited()


# ── A malicious introducer: ids bound to keys, timestamps bounded ───────


async def _forged_intro(
    coord,
    fed_repo,
    identity,
    *,
    a_id: str | None = None,
    a_pk_hex: str | None = None,
    ts: str | None = None,
) -> str:
    """B (a confirmed peer of ours) vouches for an introduction it built
    itself — a valid vouch signature over whatever ids and keys B chose.
    Returns the ``a_id`` it claimed."""
    b_kp = generate_identity_keypair()
    b = _make_peer_remote_instance(b_kp.public_key.hex())
    fed_repo.instances[b.id] = b
    attacker = generate_identity_keypair()  # B's own key, not the victim's
    a_pk_hex = a_pk_hex if a_pk_hex is not None else attacker.public_key.hex()
    a_id = a_id or derive_instance_id(attacker.public_key)
    a_dh = generate_x25519_keypair()
    c_id = derive_instance_id(identity.public_key)
    ts = ts or datetime.now(UTC).isoformat()
    url = "https://evil.example/federation/inbox/x"
    vouch = sign_ed25519(
        b_kp.private_key,
        _vouch_blob(
            a_id=a_id,
            a_pk_hex=a_pk_hex,
            a_inbox_url=url,
            a_dh_pk_hex=a_dh.public_key.hex(),
            c_id=c_id,
            ts=ts,
            nonce="n1",
        ),
    )
    await coord.on_intro_at_target(
        _evt(
            b.id,
            {
                "via_b_id": b.id,
                "from_a_id": a_id,
                "from_a_pk": a_pk_hex,
                "from_a_inbox_url": url,
                "from_a_dh_pk": a_dh.public_key.hex(),
                "from_a_display": "Grandma",
                "vouch_sig": vouch.hex(),
                "ts": ts,
                "nonce": "n1",
                "token": "tk",
            },
        )
    )
    return a_id


async def test_a_well_formed_vouched_intro_is_queued(coord, fed_repo, inbox, identity):
    """Control for the forgeries below."""
    a_id = await _forged_intro(coord, fed_repo, identity)
    assert [r.from_a_id for r in inbox.list_pending()] == [a_id]


async def test_an_introduction_naming_another_households_id_is_refused(
    coord, fed_repo, inbox, identity
):
    """B vouches a victim's id with its own key: the id must derive from the
    key, else the approval would seat the attacker's key under the victim's
    name (and the row id would not be the key's)."""
    victim_id = derive_instance_id(generate_identity_keypair().public_key)
    await _forged_intro(coord, fed_repo, identity, a_id=victim_id)
    assert inbox.list_pending() == []


@pytest.mark.parametrize("a_id", ["../connections/" + "a" * 32, "A" * 32, "a" * 31, ""])
async def test_an_introduction_with_a_malformed_id_is_refused(
    coord, fed_repo, inbox, identity, a_id
):
    await _forged_intro(coord, fed_repo, identity, a_id=a_id or "x")
    assert inbox.list_pending() == []


@pytest.mark.parametrize("a_pk_hex", ["zz" * 32, "aa" * 31, "abc"])
async def test_an_introduction_with_a_malformed_key_is_refused(
    coord, fed_repo, inbox, identity, a_pk_hex
):
    await _forged_intro(coord, fed_repo, identity, a_id="a" * 32, a_pk_hex=a_pk_hex)
    assert inbox.list_pending() == []


async def test_a_future_dated_introduction_is_refused_at_the_target(
    coord, fed_repo, inbox, identity
):
    """A stamp far in the future would never expire."""
    far = (datetime.now(UTC) + timedelta(days=3650)).isoformat()
    await _forged_intro(coord, fed_repo, identity, ts=far)
    assert inbox.list_pending() == []


async def test_a_future_dated_intro_is_not_vouched_for(coord, fed_repo, fed_service):
    """B side: A's request is forwarded only within the clock-skew window."""
    a = _make_peer_remote_instance(generate_identity_keypair().public_key.hex())
    c = _make_peer_remote_instance(generate_identity_keypair().public_key.hex())
    fed_repo.instances[a.id] = a
    fed_repo.instances[c.id] = c
    far = (datetime.now(UTC) + timedelta(hours=1)).isoformat()
    await coord.on_intro_from_peer(
        _evt(
            a.id,
            {
                "target_id": c.id,
                "a_dh_pk": generate_x25519_keypair().public_key.hex(),
                "a_inbox_url": "https://a.example/federation/inbox/x",
                "ts": far,
                "nonce": "n" * 16,
                "token": "tk",
            },
        )
    )
    fed_service.send_event.assert_not_awaited()


async def test_an_ack_whose_target_id_is_not_its_key_is_ignored(
    coord, fed_repo, identity
):
    """The ack's c_id must derive from its c_pk — the ack signature alone is
    over a c_id the signer chose."""
    c_id, ack = await _request_and_signed_ack(coord, fed_repo, identity)
    # A malicious introducer answers with ITS key for the target's id, and
    # signs the ack blob with that key — every signature checks out.
    attacker = generate_identity_keypair()
    session = coord._pending[ack["token"]]
    forged_sig = sign_ed25519(
        attacker.private_key,
        _ack_blob(
            a_id=ack["a_id"],
            a_dh_pk_hex=session.dh_pk_hex,
            c_id=c_id,
            c_dh_pk_hex=ack["c_dh_pk"],
            ts=ack["ts"],
            nonce=ack["nonce"],
        ),
    )
    ack = dict(ack, c_pk=attacker.public_key.hex(), ack_sig=forged_sig.hex())

    await coord.on_ack_at_originator(_evt("b", ack))

    assert fed_repo.instances[c_id].status is PairingStatus.PENDING_SENT


# ── The household's home-sharing default (owner decision 2026-10-19) ──


class _Prefs:
    def __init__(self, share_home_default: bool) -> None:
        self._value = share_home_default

    async def get_household(self) -> HouseholdPreferences:
        return HouseholdPreferences(share_home_default=self._value)


def _coord_with_default(
    value: bool | None, fed_repo, kek_manager, bus, fed_service, identity, inbox
) -> AutoPairCoordinator:
    return AutoPairCoordinator(
        federation_repo=fed_repo,
        key_manager=kek_manager,
        bus=bus,
        federation_service=fed_service,
        own_identity_seed=identity.private_key,
        own_identity_pk=identity.public_key,
        inbox=inbox,
        preferences_repo=None if value is None else _Prefs(value),
    )


@pytest.mark.parametrize("value", [True, False])
async def test_requested_pair_takes_the_households_sharing_default(
    value, fed_repo, kek_manager, bus, fed_service, identity, inbox
):
    """A pair made through an introducer has no dialog switch: its
    ``share_home`` is the household's default."""
    coord = _coord_with_default(
        value, fed_repo, kek_manager, bus, fed_service, identity, inbox
    )
    b = _make_peer_remote_instance(generate_identity_keypair().public_key.hex())
    fed_repo.instances[b.id] = b
    c_id = derive_instance_id(generate_identity_keypair().public_key)
    await coord.request_via(
        via_instance_id=b.id,
        target_instance_id=c_id,
        target_display_name="C",
        own_inbox_base_url=_OWN_INBOX_BASE,
    )
    assert fed_repo.instances[c_id].share_home is value


@pytest.mark.parametrize("value", [True, False])
async def test_approved_pair_takes_the_households_sharing_default(
    value, fed_repo, kek_manager, bus, fed_service, identity, inbox
):
    coord = _coord_with_default(
        value, fed_repo, kek_manager, bus, fed_service, identity, inbox
    )
    req = _queued_request(inbox)
    inst = await coord.finalize_pending(
        req.request_id, own_inbox_base_url="https://c.example/federation/inbox"
    )
    assert inst.share_home is value
    assert fed_repo.instances[req.from_a_id].share_home is value


async def test_without_preferences_the_documented_default_applies(
    fed_repo, kek_manager, bus, fed_service, identity, inbox
):
    coord = _coord_with_default(
        None, fed_repo, kek_manager, bus, fed_service, identity, inbox
    )
    req = _queued_request(inbox)
    inst = await coord.finalize_pending(
        req.request_id, own_inbox_base_url="https://c.example/federation/inbox"
    )
    assert inst.share_home is True
