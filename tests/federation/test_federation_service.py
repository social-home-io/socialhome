"""Tests for socialhome.federation.FederationService.

All tests use in-memory stubs — no network, no real disk.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from socialhome.crypto import (
    b64url_decode,
    b64url_encode,
    derive_instance_id,
    generate_identity_keypair,
    generate_x25519_keypair,
)
from socialhome.domain.federation import (
    FederationEventType,
    PairingSession,
    PairingStatus,
    RemoteInstance,
)
from socialhome.federation import FederationService
from socialhome.infrastructure.event_bus import EventBus
from socialhome.infrastructure.key_manager import KeyManager


# ─── Shared fixtures & stubs ──────────────────────────────────────────────

try:
    import orjson as _json_lib

    def _dumps(obj):
        return _json_lib.dumps(obj).decode("utf-8")

    def _loads(s):
        return _json_lib.loads(s)
except ImportError:
    import json as _json_lib

    def _dumps(obj):
        return _json_lib.dumps(obj, separators=(",", ":"))

    def _loads(s):
        return _json_lib.loads(s)


def _make_kek_manager() -> KeyManager:
    import os

    return KeyManager(os.urandom(32))


def _make_remote_instance(
    key_manager: KeyManager,
    *,
    own_kp=None,
    peer_kp=None,
    session_key: bytes | None = None,
) -> tuple[RemoteInstance, bytes]:
    """Return (RemoteInstance, raw_session_key) for use in tests."""
    import os

    if peer_kp is None:
        peer_kp = generate_identity_keypair()
    peer_id = derive_instance_id(peer_kp.public_key)
    if session_key is None:
        session_key = os.urandom(32)

    key_self_enc = key_manager.encrypt(session_key)
    key_remote_enc = key_manager.encrypt(session_key)

    inst = RemoteInstance(
        id=peer_id,
        display_name="peer-instance",
        remote_identity_pk=peer_kp.public_key.hex(),
        key_self_to_remote=key_self_enc,
        key_remote_to_self=key_remote_enc,
        remote_webhook_url="http://peer.example.com/fed/webhook",
        local_webhook_id="local-wh-id-abc",
        status=PairingStatus.CONFIRMED,
    )
    return inst, session_key


class InMemoryFederationRepo:
    """Minimal in-memory federation repo for tests."""

    def __init__(self):
        self._instances: dict[str, RemoteInstance] = {}
        self._pairings: dict[str, PairingSession] = {}
        self._replay: dict[str, str] = {}
        self._bans: set[tuple[str, str]] = set()
        self.reachable_calls: list[str] = []
        self.unreachable_calls: list[str] = []

    async def get_instance(self, instance_id: str) -> RemoteInstance | None:
        return self._instances.get(instance_id)

    async def save_instance(self, inst: RemoteInstance) -> RemoteInstance:
        self._instances[inst.id] = inst
        return inst

    async def list_instances(
        self,
        *,
        source: str | None = None,
        status: str | None = None,
    ) -> list[RemoteInstance]:
        result = list(self._instances.values())
        if status is not None:
            result = [i for i in result if i.status.value == status]
        return result

    async def delete_instance(self, instance_id: str) -> None:
        self._instances.pop(instance_id, None)

    async def mark_reachable(self, instance_id: str) -> None:
        self.reachable_calls.append(instance_id)

    async def mark_unreachable(self, instance_id: str) -> None:
        self.unreachable_calls.append(instance_id)

    async def update_webhook(self, instance_id: str, new_url: str) -> None:
        pass

    async def load_replay_cache(self, within_hours: int = 1) -> list[tuple[str, str]]:
        return list(self._replay.items())

    async def insert_replay_id(self, msg_id: str) -> None:
        self._replay[msg_id] = datetime.now(timezone.utc).isoformat()

    async def prune_replay_cache(self, cutoff_iso: str) -> int:
        return 0

    async def create_pairing(self, session: PairingSession) -> None:
        self._pairings[session.token] = session

    async def get_pairing(self, token: str) -> PairingSession | None:
        return self._pairings.get(token)

    async def update_pairing(self, session: PairingSession) -> None:
        self._pairings[session.token] = session

    async def delete_pairing(self, token: str) -> None:
        self._pairings.pop(token, None)

    async def ban_instance_from_space(
        self,
        space_id: str,
        instance_id: str,
        *,
        reason: str | None = None,
    ) -> None:
        self._bans.add((space_id, instance_id))

    async def is_instance_banned_from_space(
        self,
        space_id: str,
        instance_id: str,
    ) -> bool:
        return (space_id, instance_id) in self._bans


class InMemoryOutboxRepo:
    """Minimal in-memory outbox repo for tests."""

    def __init__(self):
        self.enqueued: list[dict] = []

    async def enqueue(
        self,
        *,
        instance_id: str,
        event_type: FederationEventType,
        payload_json: str,
        msg_id: str | None = None,
        authority_json: str | None = None,
        expires_at: str | None = None,
    ) -> str:
        entry_id = msg_id or str(uuid.uuid4())
        self.enqueued.append(
            {
                "id": entry_id,
                "instance_id": instance_id,
                "event_type": event_type,
                "payload_json": payload_json,
            }
        )
        return entry_id

    async def list_due(self, limit: int = 50):
        return []

    async def mark_delivered(self, entry_id: str) -> None:
        pass

    async def mark_failed(self, entry_id: str) -> None:
        pass

    async def reschedule(
        self, entry_id: str, next_attempt_at: str, attempts: int
    ) -> None:
        pass

    async def expire_past_retention(self, now_iso: str) -> int:
        return 0

    async def count_pending_for(self, instance_id: str) -> int:
        return 0


def _make_service(
    *,
    federation_repo: InMemoryFederationRepo | None = None,
    outbox_repo: InMemoryOutboxRepo | None = None,
    key_manager: KeyManager | None = None,
    bus: EventBus | None = None,
    http_client=None,
) -> "tuple[FederationService, object]":
    own_kp = generate_identity_keypair()
    own_id = derive_instance_id(own_kp.public_key)
    km = key_manager or _make_kek_manager()
    svc = FederationService(
        db=MagicMock(),
        federation_repo=federation_repo or InMemoryFederationRepo(),
        outbox_repo=outbox_repo or InMemoryOutboxRepo(),
        key_manager=km,
        bus=bus or EventBus(),
        own_instance_id=own_id,
        own_identity_seed=own_kp.private_key,
        own_identity_pk=own_kp.public_key,
        http_client=http_client,
    )
    return svc, own_kp


# ─── Crypto helpers ───────────────────────────────────────────────────────


def test_encrypt_decrypt_roundtrip():
    """Encrypt then decrypt returns the original plaintext."""
    import os

    svc, _ = _make_service()
    session_key = os.urandom(32)
    original = '{"hello": "world", "num": 42}'
    encrypted = svc._encrypt_payload(original, session_key)
    # Should contain nonce:ciphertext format
    assert ":" in encrypted
    decrypted = svc._decrypt_payload(encrypted, session_key)
    assert decrypted == original


def test_sign_verify_roundtrip():
    """Sign then verify with the same key succeeds."""
    svc, own_kp = _make_service()
    message = b"test envelope bytes"
    sig = svc._sign_envelope(message)
    assert svc._verify_signature(message, sig, own_kp.public_key)


def test_tampered_signature_rejected():
    """A modified signature fails verification."""
    svc, own_kp = _make_service()
    message = b"test envelope bytes"
    sig = svc._sign_envelope(message)
    # Corrupt the signature
    sig_bytes = b64url_decode(sig)
    corrupted = bytearray(sig_bytes)
    corrupted[0] ^= 0xFF
    bad_sig = b64url_encode(bytes(corrupted))
    assert not svc._verify_signature(message, bad_sig, own_kp.public_key)


def test_wrong_key_signature_rejected():
    """Verifying with a different public key fails."""
    svc, _ = _make_service()
    message = b"test envelope bytes"
    sig = svc._sign_envelope(message)
    other_kp = generate_identity_keypair()
    assert not svc._verify_signature(message, sig, other_kp.public_key)


# ─── send_event ───────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_send_event_to_peer():
    """send_event: mock HTTP POST, verify envelope structure."""
    km = _make_kek_manager()
    fed_repo = InMemoryFederationRepo()
    outbox_repo = InMemoryOutboxRepo()
    peer_kp = generate_identity_keypair()

    inst, session_key = _make_remote_instance(km, peer_kp=peer_kp)
    await fed_repo.save_instance(inst)

    # Mock HTTP client.
    mock_resp = AsyncMock()
    mock_resp.status = 200
    mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
    mock_resp.__aexit__ = AsyncMock(return_value=False)

    mock_post = MagicMock(return_value=mock_resp)
    mock_http = MagicMock()
    mock_http.post = mock_post

    svc, own_kp = _make_service(
        federation_repo=fed_repo,
        outbox_repo=outbox_repo,
        key_manager=km,
        http_client=mock_http,
    )

    result = await svc.send_event(
        to_instance_id=inst.id,
        event_type=FederationEventType.USER_UPDATED,
        payload={"user_id": "abc123", "display_name": "Alice"},
    )

    assert result.ok is True
    assert result.instance_id == inst.id
    assert inst.id in fed_repo.reachable_calls
    assert len(outbox_repo.enqueued) == 0

    # Verify envelope structure passed to HTTP.
    call_kwargs = mock_post.call_args
    assert call_kwargs is not None
    posted_json = (
        call_kwargs.kwargs.get("json") or call_kwargs.args[1]
        if len(call_kwargs.args) > 1
        else call_kwargs.kwargs["json"]
    )
    assert "msg_id" in posted_json
    assert "sig_suite" in posted_json
    assert "signatures" in posted_json
    assert "ed25519" in posted_json["signatures"]
    assert "encrypted_payload" in posted_json
    assert posted_json["from_instance"] == svc._own_instance_id
    assert posted_json["to_instance"] == inst.id
    # event_type is stored as a string on the wire
    assert posted_json["event_type"] == FederationEventType.USER_UPDATED.value


@pytest.mark.asyncio
async def test_send_event_failure_enqueues_outbox():
    """On HTTP failure, send_event marks unreachable and enqueues to outbox."""
    km = _make_kek_manager()
    fed_repo = InMemoryFederationRepo()
    outbox_repo = InMemoryOutboxRepo()
    peer_kp = generate_identity_keypair()

    inst, session_key = _make_remote_instance(km, peer_kp=peer_kp)
    await fed_repo.save_instance(inst)

    # Simulate HTTP error.
    mock_http = MagicMock()
    mock_http.post = MagicMock(side_effect=Exception("connection refused"))

    svc, _ = _make_service(
        federation_repo=fed_repo,
        outbox_repo=outbox_repo,
        key_manager=km,
        http_client=mock_http,
    )

    result = await svc.send_event(
        to_instance_id=inst.id,
        event_type=FederationEventType.USER_UPDATED,
        payload={"user_id": "abc123"},
    )

    assert result.ok is False
    assert inst.id in fed_repo.unreachable_calls
    assert len(outbox_repo.enqueued) == 1
    assert outbox_repo.enqueued[0]["instance_id"] == inst.id


@pytest.mark.asyncio
async def test_send_event_prefers_attached_transport():
    """When a FederationTransport facade is attached, send_event delegates to it.

    The webhook HTTP client is wired to raise — if send_event is
    routing through the legacy inline path it would surface an
    exception or enqueue to outbox. Instead, the facade's fake
    returns ok=True and ok bubbles up.
    """
    from socialhome.federation.transport import _TransportSendResult

    km = _make_kek_manager()
    fed_repo = InMemoryFederationRepo()
    outbox_repo = InMemoryOutboxRepo()
    peer_kp = generate_identity_keypair()
    inst, _ = _make_remote_instance(km, peer_kp=peer_kp)
    await fed_repo.save_instance(inst)

    # Legacy HTTP path would raise if consulted.
    failing_http = MagicMock()
    failing_http.post = MagicMock(side_effect=Exception("must-not-be-called"))

    svc, _ = _make_service(
        federation_repo=fed_repo,
        outbox_repo=outbox_repo,
        key_manager=km,
        http_client=failing_http,
    )

    class _FakeTransport:
        def __init__(self):
            self.calls = []

        async def send(self, *, instance, envelope_dict):
            self.calls.append((instance.id, envelope_dict["event_type"]))
            return _TransportSendResult(ok=True, via="rtc", status_code=None)

    fake = _FakeTransport()
    svc.attach_transport(fake)

    result = await svc.send_event(
        to_instance_id=inst.id,
        event_type=FederationEventType.USER_UPDATED,
        payload={"user_id": "abc"},
    )

    assert result.ok is True
    assert fake.calls and fake.calls[0][0] == inst.id
    # Outbox was not touched; legacy HTTP client was not invoked.
    assert outbox_repo.enqueued == []
    failing_http.post.assert_not_called()


# ─── Inbound webhook ──────────────────────────────────────────────────────


def _make_valid_envelope(
    *,
    svc: FederationService,
    peer_kp,
    session_key: bytes,
    km: KeyManager,
    peer_inst: RemoteInstance,
    space_id: str | None = None,
    timestamp: str | None = None,
    msg_id: str | None = None,
    payload: dict | None = None,
) -> bytes:
    """Produce a valid raw JSON envelope bytes that the inbound pipeline will accept."""
    if timestamp is None:
        timestamp = datetime.now(timezone.utc).isoformat()
    if msg_id is None:
        msg_id = str(uuid.uuid4())
    if payload is None:
        payload = {"data": "test"}

    # Encrypt payload with session key.
    from socialhome.federation.federation_service import _dumps

    payload_json = _dumps(payload)
    import os
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    nonce = os.urandom(12)
    ct = AESGCM(session_key).encrypt(nonce, payload_json.encode(), None)
    encrypted_payload = b64url_encode(nonce) + ":" + b64url_encode(ct)

    from_instance = derive_instance_id(peer_kp.public_key)
    to_instance = svc._own_instance_id

    envelope_dict: dict = {
        "msg_id": msg_id,
        "event_type": FederationEventType.USER_UPDATED,
        "from_instance": from_instance,
        "to_instance": to_instance,
        "timestamp": timestamp,
        "encrypted_payload": encrypted_payload,
        "space_id": space_id,
        "proto_version": 1,
        "sig_suite": "ed25519",
    }

    from socialhome.crypto import sign_ed25519

    envelope_bytes = _dumps(envelope_dict).encode("utf-8")
    sig = sign_ed25519(peer_kp.private_key, envelope_bytes)
    envelope_dict["signatures"] = {"ed25519": b64url_encode(sig)}

    return _dumps(envelope_dict).encode("utf-8")


@pytest.mark.asyncio
async def test_inbound_webhook_validation():
    """A well-formed signed envelope is validated successfully."""
    km = _make_kek_manager()
    fed_repo = InMemoryFederationRepo()
    peer_kp = generate_identity_keypair()

    import os

    session_key = os.urandom(32)
    inst, _ = _make_remote_instance(km, peer_kp=peer_kp, session_key=session_key)
    await fed_repo.save_instance(inst)

    svc, own_kp = _make_service(federation_repo=fed_repo, key_manager=km)

    raw_body = _make_valid_envelope(
        svc=svc,
        peer_kp=peer_kp,
        session_key=session_key,
        km=km,
        peer_inst=inst,
    )

    result = await svc.handle_inbound_webhook(
        webhook_id=inst.local_webhook_id,
        raw_body=raw_body,
    )
    assert result == {"status": "ok"}


@pytest.mark.asyncio
async def test_inbound_replay_rejected():
    """The same msg_id twice is rejected as a replay."""
    km = _make_kek_manager()
    fed_repo = InMemoryFederationRepo()
    peer_kp = generate_identity_keypair()

    import os

    session_key = os.urandom(32)
    inst, _ = _make_remote_instance(km, peer_kp=peer_kp, session_key=session_key)
    await fed_repo.save_instance(inst)

    svc, _ = _make_service(federation_repo=fed_repo, key_manager=km)

    msg_id = str(uuid.uuid4())
    raw_body = _make_valid_envelope(
        svc=svc,
        peer_kp=peer_kp,
        session_key=session_key,
        km=km,
        peer_inst=inst,
        msg_id=msg_id,
    )

    # First delivery.
    result = await svc.handle_inbound_webhook(
        webhook_id=inst.local_webhook_id,
        raw_body=raw_body,
    )
    assert result == {"status": "ok"}

    # Second delivery — same msg_id.
    with pytest.raises(ValueError, match="Replay detected"):
        await svc.handle_inbound_webhook(
            webhook_id=inst.local_webhook_id,
            raw_body=raw_body,
        )


@pytest.mark.asyncio
async def test_inbound_timestamp_skew_rejected():
    """An envelope with a timestamp >300s off is rejected."""
    km = _make_kek_manager()
    fed_repo = InMemoryFederationRepo()
    peer_kp = generate_identity_keypair()

    import os

    session_key = os.urandom(32)
    inst, _ = _make_remote_instance(km, peer_kp=peer_kp, session_key=session_key)
    await fed_repo.save_instance(inst)

    svc, _ = _make_service(federation_repo=fed_repo, key_manager=km)

    # Timestamp 10 minutes in the past.
    old_ts = (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat()
    raw_body = _make_valid_envelope(
        svc=svc,
        peer_kp=peer_kp,
        session_key=session_key,
        km=km,
        peer_inst=inst,
        timestamp=old_ts,
    )

    with pytest.raises(ValueError, match="Timestamp skew"):
        await svc.handle_inbound_webhook(
            webhook_id=inst.local_webhook_id,
            raw_body=raw_body,
        )


@pytest.mark.asyncio
async def test_inbound_bad_signature_rejected():
    """An envelope with a tampered signature is rejected."""
    km = _make_kek_manager()
    fed_repo = InMemoryFederationRepo()
    peer_kp = generate_identity_keypair()

    import os

    session_key = os.urandom(32)
    inst, _ = _make_remote_instance(km, peer_kp=peer_kp, session_key=session_key)
    await fed_repo.save_instance(inst)

    svc, _ = _make_service(federation_repo=fed_repo, key_manager=km)

    raw_body = _make_valid_envelope(
        svc=svc,
        peer_kp=peer_kp,
        session_key=session_key,
        km=km,
        peer_inst=inst,
    )

    # Corrupt the ed25519 signature in the JSON.
    data = _loads(raw_body)
    sig_bytes = b64url_decode(data["signatures"]["ed25519"])
    bad_sig = bytearray(sig_bytes)
    bad_sig[0] ^= 0xFF
    data["signatures"]["ed25519"] = b64url_encode(bytes(bad_sig))

    with pytest.raises(ValueError, match="Invalid envelope signature"):
        await svc.handle_inbound_webhook(
            webhook_id=inst.local_webhook_id,
            raw_body=_dumps(data).encode("utf-8"),
        )


@pytest.mark.asyncio
async def test_inbound_unknown_webhook_rejected():
    """Inbound webhook with unknown webhook_id raises ValueError."""
    km = _make_kek_manager()
    fed_repo = InMemoryFederationRepo()
    svc, _ = _make_service(federation_repo=fed_repo, key_manager=km)

    raw_body = _dumps(
        {
            "msg_id": str(uuid.uuid4()),
            "event_type": "user_updated",
            "from_instance": "abc",
            "to_instance": svc._own_instance_id,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "encrypted_payload": "nonce:ct",
            "sig_suite": "ed25519",
            "signatures": {"ed25519": "sig"},
        }
    ).encode("utf-8")

    with pytest.raises(ValueError, match="No instance found"):
        await svc.handle_inbound_webhook(
            webhook_id="nonexistent-webhook",
            raw_body=raw_body,
        )


# ─── Pairing ──────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_initiate_pairing():
    """initiate_pairing returns a QR payload with expected fields."""
    km = _make_kek_manager()
    fed_repo = InMemoryFederationRepo()
    svc, own_kp = _make_service(federation_repo=fed_repo, key_manager=km)

    result = await svc.initiate_pairing("http://my-instance.local/fed/webhook")

    assert "token" in result
    assert "identity_pk" in result
    assert "dh_pk" in result
    assert "webhook_url" in result
    assert "expires_at" in result

    assert result["identity_pk"] == own_kp.public_key.hex()
    assert result["webhook_url"] == "http://my-instance.local/fed/webhook"

    # The pairing session should be persisted.
    assert len(fed_repo._pairings) == 1
    session = fed_repo._pairings[result["token"]]
    assert session.token == result["token"]
    assert session.status == PairingStatus.PENDING_SENT


@pytest.mark.asyncio
async def test_accept_pairing():
    """accept_pairing processes a QR payload and creates a RemoteInstance."""
    km = _make_kek_manager()
    fed_repo = InMemoryFederationRepo()
    svc, own_kp = _make_service(federation_repo=fed_repo, key_manager=km)

    # Simulate the peer's QR payload.
    peer_kp = generate_identity_keypair()
    peer_dh = generate_x25519_keypair()
    qr_payload = {
        "token": "test-token-123",
        "identity_pk": peer_kp.public_key.hex(),
        "dh_pk": peer_dh.public_key.hex(),
        "webhook_url": "http://peer.local/fed/webhook",
    }

    result = await svc.accept_pairing(qr_payload)

    assert "verification_code" in result
    assert len(result["verification_code"]) == 6
    assert result["verification_code"].isdigit()
    assert "local_webhook_id" in result

    # A RemoteInstance should be saved in PENDING_RECEIVED state.
    peer_id = derive_instance_id(peer_kp.public_key)
    saved = fed_repo._instances.get(peer_id)
    assert saved is not None
    assert saved.status == PairingStatus.PENDING_RECEIVED


@pytest.mark.asyncio
async def test_confirm_pairing():
    """confirm_pairing with correct code moves instance to CONFIRMED."""
    km = _make_kek_manager()
    fed_repo = InMemoryFederationRepo()
    svc, own_kp = _make_service(federation_repo=fed_repo, key_manager=km)

    peer_kp = generate_identity_keypair()
    peer_dh = generate_x25519_keypair()
    qr_payload = {
        "token": "tok-abc",
        "identity_pk": peer_kp.public_key.hex(),
        "dh_pk": peer_dh.public_key.hex(),
        "webhook_url": "http://peer.local/fed/webhook",
    }

    accept_result = await svc.accept_pairing(qr_payload)
    verification_code = accept_result["verification_code"]

    confirmed = await svc.confirm_pairing("tok-abc", verification_code)
    assert confirmed.status == PairingStatus.CONFIRMED

    # Pairing session should be cleaned up.
    assert "tok-abc" not in fed_repo._pairings


@pytest.mark.asyncio
async def test_confirm_pairing_wrong_code():
    """confirm_pairing with wrong code raises ValueError."""
    km = _make_kek_manager()
    fed_repo = InMemoryFederationRepo()
    svc, _ = _make_service(federation_repo=fed_repo, key_manager=km)

    peer_kp = generate_identity_keypair()
    peer_dh = generate_x25519_keypair()
    qr_payload = {
        "token": "tok-xyz",
        "identity_pk": peer_kp.public_key.hex(),
        "dh_pk": peer_dh.public_key.hex(),
        "webhook_url": "http://peer.local/fed/webhook",
    }

    await svc.accept_pairing(qr_payload)

    with pytest.raises(ValueError, match="Verification code mismatch"):
        await svc.confirm_pairing("tok-xyz", "000000")


# ─── broadcast_to_peers ───────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_broadcast_to_peers():
    """broadcast_to_peers sends to multiple instances and aggregates results."""
    km = _make_kek_manager()
    fed_repo = InMemoryFederationRepo()
    outbox_repo = InMemoryOutboxRepo()

    import os

    # Create two peer instances.
    peer1_kp = generate_identity_keypair()
    peer2_kp = generate_identity_keypair()
    session_key1 = os.urandom(32)
    session_key2 = os.urandom(32)
    inst1, _ = _make_remote_instance(km, peer_kp=peer1_kp, session_key=session_key1)
    inst2, _ = _make_remote_instance(km, peer_kp=peer2_kp, session_key=session_key2)
    await fed_repo.save_instance(inst1)
    await fed_repo.save_instance(inst2)

    # Mock HTTP client — both succeed.
    mock_resp = AsyncMock()
    mock_resp.status = 200
    mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
    mock_resp.__aexit__ = AsyncMock(return_value=False)

    mock_http = MagicMock()
    mock_http.post = MagicMock(return_value=mock_resp)

    svc, _ = _make_service(
        federation_repo=fed_repo,
        outbox_repo=outbox_repo,
        key_manager=km,
        http_client=mock_http,
    )

    result = await svc.broadcast_to_peers(
        event_type=FederationEventType.USERS_SYNC,
        payload={"users": []},
        instance_ids=[inst1.id, inst2.id],
    )

    assert result.attempted == 2
    assert result.succeeded == 2
    assert result.failed == 0
    assert result.all_ok is True
    assert mock_http.post.call_count == 2


@pytest.mark.asyncio
async def test_broadcast_to_peers_partial_failure():
    """broadcast_to_peers captures partial failure correctly."""
    km = _make_kek_manager()
    fed_repo = InMemoryFederationRepo()
    outbox_repo = InMemoryOutboxRepo()

    import os

    peer1_kp = generate_identity_keypair()
    peer2_kp = generate_identity_keypair()
    inst1, _ = _make_remote_instance(km, peer_kp=peer1_kp, session_key=os.urandom(32))
    inst2, _ = _make_remote_instance(km, peer_kp=peer2_kp, session_key=os.urandom(32))
    await fed_repo.save_instance(inst1)
    await fed_repo.save_instance(inst2)

    call_count = 0

    class _Resp:
        def __init__(self, status):
            self.status = status

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            return False

    def _post(url, **kwargs):
        nonlocal call_count
        call_count += 1
        # First call succeeds, second fails.
        if call_count == 1:
            return _Resp(200)
        raise Exception("timeout")

    mock_http = MagicMock()
    mock_http.post = _post

    svc, _ = _make_service(
        federation_repo=fed_repo,
        outbox_repo=outbox_repo,
        key_manager=km,
        http_client=mock_http,
    )

    result = await svc.broadcast_to_peers(
        event_type=FederationEventType.USER_UPDATED,
        payload={"user_id": "x"},
        instance_ids=[inst1.id, inst2.id],
    )

    assert result.attempted == 2
    assert result.succeeded == 1
    assert result.failed == 1


@pytest.mark.asyncio
async def test_broadcast_to_all_confirmed_when_no_ids():
    """broadcast_to_peers with instance_ids=None sends to all confirmed peers."""
    km = _make_kek_manager()
    fed_repo = InMemoryFederationRepo()
    outbox_repo = InMemoryOutboxRepo()

    import os

    peers = [generate_identity_keypair() for _ in range(3)]
    for p in peers:
        inst, _ = _make_remote_instance(km, peer_kp=p, session_key=os.urandom(32))
        await fed_repo.save_instance(inst)

    mock_resp = AsyncMock()
    mock_resp.status = 200
    mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
    mock_resp.__aexit__ = AsyncMock(return_value=False)

    mock_http = MagicMock()
    mock_http.post = MagicMock(return_value=mock_resp)

    svc, _ = _make_service(
        federation_repo=fed_repo,
        outbox_repo=outbox_repo,
        key_manager=km,
        http_client=mock_http,
    )

    result = await svc.broadcast_to_peers(
        event_type=FederationEventType.USERS_SYNC,
        payload={"users": []},
    )

    assert result.attempted == 3
    assert result.succeeded == 3


# ─── Dispatch event match arms ────────────────────────────────────────────


async def test_dispatch_users_sync():
    """USERS_SYNC dispatch logs without error."""
    bus = EventBus()
    svc, _ = _make_service(bus=bus)
    from socialhome.domain.federation import FederationEvent, FederationEventType

    event = FederationEvent(
        msg_id="m1",
        event_type=FederationEventType.USERS_SYNC,
        from_instance="peer",
        to_instance="test-instance",
        timestamp=datetime.now(timezone.utc).isoformat(),
        payload={"users": [{"username": "bob"}]},
    )
    await svc._dispatch_event(event)


async def test_dispatch_user_updated():
    """USER_UPDATED dispatch logs without error."""
    bus = EventBus()
    svc, _ = _make_service(bus=bus)
    from socialhome.domain.federation import FederationEvent, FederationEventType

    event = FederationEvent(
        msg_id="m2",
        event_type=FederationEventType.USER_UPDATED,
        from_instance="peer",
        to_instance="test-instance",
        timestamp=datetime.now(timezone.utc).isoformat(),
        payload={"user_id": "u1", "display_name": "Bob"},
    )
    await svc._dispatch_event(event)


async def test_dispatch_user_removed():
    """USER_REMOVED dispatch logs without error."""
    bus = EventBus()
    svc, _ = _make_service(bus=bus)
    from socialhome.domain.federation import FederationEvent, FederationEventType

    event = FederationEvent(
        msg_id="m3",
        event_type=FederationEventType.USER_REMOVED,
        from_instance="peer",
        to_instance="test-instance",
        timestamp=datetime.now(timezone.utc).isoformat(),
        payload={"user_id": "u1"},
    )
    await svc._dispatch_event(event)


async def test_dispatch_space_post_created():
    """SPACE_POST_CREATED dispatch logs without error."""
    bus = EventBus()
    svc, _ = _make_service(bus=bus)
    from socialhome.domain.federation import FederationEvent, FederationEventType

    event = FederationEvent(
        msg_id="m4",
        event_type=FederationEventType.SPACE_POST_CREATED,
        from_instance="peer",
        to_instance="test-instance",
        timestamp=datetime.now(timezone.utc).isoformat(),
        payload={"post_id": "p1"},
        space_id="s1",
    )
    await svc._dispatch_event(event)


async def test_dispatch_space_member_joined():
    """SPACE_MEMBER_JOINED dispatch logs without error."""
    bus = EventBus()
    svc, _ = _make_service(bus=bus)
    from socialhome.domain.federation import FederationEvent, FederationEventType

    event = FederationEvent(
        msg_id="m5",
        event_type=FederationEventType.SPACE_MEMBER_JOINED,
        from_instance="peer",
        to_instance="test-instance",
        timestamp=datetime.now(timezone.utc).isoformat(),
        payload={"user_id": "u1"},
        space_id="s1",
    )
    await svc._dispatch_event(event)


async def test_dispatch_space_config_changed():
    """SPACE_CONFIG_CHANGED publishes SpaceConfigChanged event."""
    bus = EventBus()
    svc, _ = _make_service(bus=bus)
    events_seen = []
    from socialhome.domain.events import SpaceConfigChanged

    async def on_config(e):
        events_seen.append(e)

    bus.subscribe(SpaceConfigChanged, on_config)

    from socialhome.domain.federation import FederationEvent, FederationEventType

    event = FederationEvent(
        msg_id="m6",
        event_type=FederationEventType.SPACE_CONFIG_CHANGED,
        from_instance="peer",
        to_instance="test-instance",
        timestamp=datetime.now(timezone.utc).isoformat(),
        payload={"sequence": 5, "name": "Updated"},
        space_id="s1",
    )
    await svc._dispatch_event(event)
    assert len(events_seen) == 1
    assert events_seen[0].sequence == 5


async def test_dispatch_dm_message():
    """DM_MESSAGE dispatch logs without error."""
    bus = EventBus()
    svc, _ = _make_service(bus=bus)
    from socialhome.domain.federation import FederationEvent, FederationEventType

    event = FederationEvent(
        msg_id="m7",
        event_type=FederationEventType.DM_MESSAGE,
        from_instance="peer",
        to_instance="test-instance",
        timestamp=datetime.now(timezone.utc).isoformat(),
        payload={"content": "hi"},
    )
    await svc._dispatch_event(event)


async def test_dispatch_presence_updated():
    """PRESENCE_UPDATED dispatch logs without error."""
    bus = EventBus()
    svc, _ = _make_service(bus=bus)
    from socialhome.domain.federation import FederationEvent, FederationEventType

    event = FederationEvent(
        msg_id="m8",
        event_type=FederationEventType.PRESENCE_UPDATED,
        from_instance="peer",
        to_instance="test-instance",
        timestamp=datetime.now(timezone.utc).isoformat(),
        payload={"state": "home"},
    )
    await svc._dispatch_event(event)


async def test_dispatch_pairing_event():
    """Pairing lifecycle events dispatch without error."""
    bus = EventBus()
    svc, _ = _make_service(bus=bus)
    from socialhome.domain.federation import FederationEvent, FederationEventType

    for etype in [
        FederationEventType.PAIRING_INTRO,
        FederationEventType.PAIRING_ACCEPT,
        FederationEventType.PAIRING_CONFIRM,
        FederationEventType.UNPAIR,
    ]:
        event = FederationEvent(
            msg_id=f"pair-{etype.value}",
            event_type=etype,
            from_instance="peer",
            to_instance="test-instance",
            timestamp=datetime.now(timezone.utc).isoformat(),
            payload={},
        )
        await svc._dispatch_event(event)


async def test_dispatch_unknown_event():
    """Unknown event type is logged but doesn't crash."""
    bus = EventBus()
    svc, _ = _make_service(bus=bus)
    from socialhome.domain.federation import FederationEvent, FederationEventType

    event = FederationEvent(
        msg_id="m-unknown",
        event_type=FederationEventType.NETWORK_SYNC,
        from_instance="peer",
        to_instance="test-instance",
        timestamp=datetime.now(timezone.utc).isoformat(),
        payload={},
    )
    await svc._dispatch_event(event)
