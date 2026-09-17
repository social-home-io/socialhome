"""Tests for socialhome.federation.FederationService.

All tests use in-memory stubs — no network, no real disk.
"""

from __future__ import annotations

import dataclasses
import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from socialhome.crypto import (
    b64url_decode,
    b64url_encode,
    derive_instance_id,
    generate_identity_keypair,
    generate_x25519_keypair,
)
from socialhome.domain.federation import (
    DELIVERY_ERROR_QUEUED,
    DELIVERY_ERROR_ROUTE_COOLDOWN,
    DeliveryResult,
    FederationEventType,
    PairingSession,
    PairingStatus,
    RemoteInstance,
)
from socialhome.domain.federation_capabilities import FederationCapability
from socialhome.federation import FederationService
from socialhome.federation.encoder import FederationEncoder
from socialhome.federation.media_framing import (
    MEDIA_AEAD_SUITE_AESGCM_256,
    UnsupportedMediaAeadSuite,
)
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
        remote_inbox_url="http://peer.example.com/fed/inbox",
        local_inbox_id="local-wh-id-abc",
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
        self._space_members: set[tuple[str, str]] = set()
        self.reachable_calls: list[str] = []
        self.unreachable_calls: list[str] = []

    def add_space_member(self, space_id: str, instance_id: str) -> None:
        """Test helper — not part of AbstractFederationRepo."""
        self._space_members.add((space_id, instance_id))

    async def get_instance(self, instance_id: str) -> RemoteInstance | None:
        return self._instances.get(instance_id)

    async def get_instance_by_local_inbox_id(
        self,
        local_inbox_id: str,
    ) -> RemoteInstance | None:
        for inst in self._instances.values():
            if inst.local_inbox_id == local_inbox_id:
                return inst
        return None

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

    async def list_instances_in_space(
        self,
        space_id: str,
    ) -> list[RemoteInstance]:
        out: list[RemoteInstance] = []
        for inst in self._instances.values():
            if (space_id, inst.id) not in self._space_members:
                continue
            if inst.status.value != "confirmed":
                continue
            if (space_id, inst.id) in self._bans:
                continue
            out.append(inst)
        return out

    async def list_member_instance_ids(self, space_id: str) -> list[str]:
        # Mirrors the production query: every instance in
        # ``space_instances`` for this space minus bans, no filter on
        # ``remote_instances.status`` so a mesh-only member surfaces.
        out: list[str] = []
        for sid, iid in self._space_members:
            if sid != space_id:
                continue
            if (space_id, iid) in self._bans:
                continue
            out.append(iid)
        return sorted(out)

    async def delete_instance(self, instance_id: str) -> None:
        self._instances.pop(instance_id, None)

    async def mark_reachable(self, instance_id: str) -> None:
        self.reachable_calls.append(instance_id)

    async def mark_unreachable(self, instance_id: str) -> None:
        self.unreachable_calls.append(instance_id)

    async def update_inbox(self, instance_id: str, new_url: str) -> None:
        pass

    async def update_alias(self, instance_id: str, alias: str | None) -> None:
        raise NotImplementedError(
            "update_alias not implemented in InMemoryFederationRepo"
        )

    async def set_proto_version(self, instance_id: str, proto_version: int) -> None:
        if instance_id in self._instances:
            self._instances[instance_id] = dataclasses.replace(
                self._instances[instance_id], proto_version=proto_version
            )

    async def update_instance_home(
        self,
        instance_id: str,
        *,
        latitude: float | None,
        longitude: float | None,
    ) -> None:
        if instance_id in self._instances:
            lat_db = round(float(latitude), 4) if latitude is not None else None
            lon_db = round(float(longitude), 4) if longitude is not None else None
            self._instances[instance_id] = dataclasses.replace(
                self._instances[instance_id],
                home_lat=lat_db,
                home_lon=lon_db,
            )

    async def set_share_home(
        self,
        instance_id: str,
        *,
        value: bool,
    ) -> None:
        if instance_id in self._instances:
            self._instances[instance_id] = dataclasses.replace(
                self._instances[instance_id], share_home=value
            )

    async def cleanup_expired_pairings(self) -> int:
        return 0

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

    async def get_local_identity(self) -> dict | None:
        return None


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

    async def purge_terminal(self, cutoff_iso: str, *, limit: int = 5000) -> int:
        return 0

    async def count_pending_for(self, instance_id: str) -> int:
        return 0

    async def count_failed_for(self, instance_id: str) -> int:
        return 0

    async def evict_oldest_droppable(self, instance_id: str) -> bool:
        return False


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
    from socialhome.domain.events import ConnectionUnreachable

    km = _make_kek_manager()
    fed_repo = InMemoryFederationRepo()
    outbox_repo = InMemoryOutboxRepo()
    peer_kp = generate_identity_keypair()

    inst, session_key = _make_remote_instance(km, peer_kp=peer_kp)
    await fed_repo.save_instance(inst)

    # Simulate HTTP error.
    mock_http = MagicMock()
    mock_http.post = MagicMock(side_effect=Exception("connection refused"))

    bus = EventBus()
    unreachable: list[str] = []

    async def _capture(e: ConnectionUnreachable) -> None:
        unreachable.append(e.instance_id)

    bus.subscribe(ConnectionUnreachable, _capture)

    svc, _ = _make_service(
        federation_repo=fed_repo,
        outbox_repo=outbox_repo,
        key_manager=km,
        http_client=mock_http,
        bus=bus,
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
    # reachable → unreachable edge fired exactly once so the SPA flips red live
    assert unreachable == [inst.id]


@pytest.mark.asyncio
async def test_send_event_failure_when_already_unreachable_no_duplicate_event():
    """A repeat failure against an already-unreachable peer re-marks it but
    does NOT re-publish ConnectionUnreachable (edge-triggered, no noise)."""
    from dataclasses import replace

    from socialhome.domain.events import ConnectionUnreachable

    km = _make_kek_manager()
    fed_repo = InMemoryFederationRepo()
    outbox_repo = InMemoryOutboxRepo()
    inst, _ = _make_remote_instance(km, peer_kp=generate_identity_keypair())
    # Already unreachable → no edge transition on this failure.
    await fed_repo.save_instance(replace(inst, unreachable_since="2026-05-01"))

    mock_http = MagicMock()
    mock_http.post = MagicMock(side_effect=Exception("still down"))

    bus = EventBus()
    fired: list[str] = []

    async def _capture(e: ConnectionUnreachable) -> None:
        fired.append(e.instance_id)

    bus.subscribe(ConnectionUnreachable, _capture)

    svc, _ = _make_service(
        federation_repo=fed_repo,
        outbox_repo=outbox_repo,
        key_manager=km,
        http_client=mock_http,
        bus=bus,
    )

    result = await svc.send_event(
        to_instance_id=inst.id,
        event_type=FederationEventType.USER_UPDATED,
        payload={"user_id": "abc123"},
    )

    assert result.ok is False
    assert inst.id in fed_repo.unreachable_calls
    assert fired == []  # no duplicate edge event


@pytest.mark.asyncio
async def test_send_event_prefers_attached_transport():
    """When a FederationTransport facade is attached, send_event delegates to it.

    The inbox HTTP client is wired to raise — if send_event is
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


# ─── Inbound inbox ──────────────────────────────────────────────────────


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
async def test_inbound_inbox_validation():
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

    result = await svc.handle_inbound_envelope(
        inbox_id=inst.local_inbox_id,
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
    result = await svc.handle_inbound_envelope(
        inbox_id=inst.local_inbox_id,
        raw_body=raw_body,
    )
    assert result == {"status": "ok"}

    # Second delivery — same msg_id.
    with pytest.raises(ValueError, match="Replay detected"):
        await svc.handle_inbound_envelope(
            inbox_id=inst.local_inbox_id,
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
        await svc.handle_inbound_envelope(
            inbox_id=inst.local_inbox_id,
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
        await svc.handle_inbound_envelope(
            inbox_id=inst.local_inbox_id,
            raw_body=_dumps(data).encode("utf-8"),
        )


@pytest.mark.asyncio
async def test_inbound_unknown_inbox_rejected():
    """Inbound inbox with unknown inbox_id raises ValueError."""
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
        await svc.handle_inbound_envelope(
            inbox_id="nonexistent-inbox",
            raw_body=raw_body,
        )


# ─── Pairing ──────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_initiate_pairing():
    """initiate_pairing returns a QR payload with expected fields."""
    km = _make_kek_manager()
    fed_repo = InMemoryFederationRepo()
    svc, own_kp = _make_service(federation_repo=fed_repo, key_manager=km)

    result = await svc.initiate_pairing("http://my-instance.local/fed/inbox")

    assert "token" in result
    assert "identity_pk" in result
    assert "dh_pk" in result
    assert "inbox_url" in result
    assert "expires_at" in result

    assert result["identity_pk"] == own_kp.public_key.hex()
    # The advertised URL = base + "/" + own_local_inbox_id (generated
    # server-side). Any POST that peer later makes will hit the addon's
    # /federation/inbox/{inbox_id} route and resolve via that id.
    assert result["inbox_url"].startswith("http://my-instance.local/fed/inbox/")
    advertised_id = result["inbox_url"].rsplit("/", 1)[1]
    assert advertised_id  # non-empty

    # The pairing session should be persisted with that id.
    assert len(fed_repo._pairings) == 1
    session = fed_repo._pairings[result["token"]]
    assert session.token == result["token"]
    assert session.status == PairingStatus.PENDING_SENT
    assert session.own_local_inbox_id == advertised_id


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
        "inbox_url": "http://peer.local/fed/inbox",
    }

    result = await svc.accept_pairing(qr_payload)

    assert "verification_code" in result
    assert len(result["verification_code"]) == 6
    assert result["verification_code"].isdigit()
    assert "local_inbox_id" in result

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
        "inbox_url": "http://peer.local/fed/inbox",
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
        "inbox_url": "http://peer.local/fed/inbox",
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


@pytest.mark.asyncio
async def test_broadcast_to_space_members_filters_non_members():
    """Only peers that are space members receive the event."""
    km = _make_kek_manager()
    fed_repo = InMemoryFederationRepo()
    outbox_repo = InMemoryOutboxRepo()

    import os

    member_kp = generate_identity_keypair()
    outsider_kp = generate_identity_keypair()
    member_inst, _ = _make_remote_instance(
        km, peer_kp=member_kp, session_key=os.urandom(32)
    )
    outsider_inst, _ = _make_remote_instance(
        km, peer_kp=outsider_kp, session_key=os.urandom(32)
    )
    await fed_repo.save_instance(member_inst)
    await fed_repo.save_instance(outsider_inst)

    space_id = "space-x"
    fed_repo.add_space_member(space_id, member_inst.id)

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

    result = await svc.broadcast_to_space_members(
        space_id=space_id,
        event_type=FederationEventType.SPACE_POST_CREATED,
        payload={"post_id": "p1"},
    )

    assert result.attempted == 1
    assert result.succeeded == 1
    assert mock_http.post.call_count == 1


@pytest.mark.asyncio
async def test_broadcast_to_space_members_skips_banned():
    """Banned peers are excluded even if they are space members."""
    km = _make_kek_manager()
    fed_repo = InMemoryFederationRepo()
    outbox_repo = InMemoryOutboxRepo()

    import os

    banned_kp = generate_identity_keypair()
    ok_kp = generate_identity_keypair()
    banned_inst, _ = _make_remote_instance(
        km, peer_kp=banned_kp, session_key=os.urandom(32)
    )
    ok_inst, _ = _make_remote_instance(km, peer_kp=ok_kp, session_key=os.urandom(32))
    await fed_repo.save_instance(banned_inst)
    await fed_repo.save_instance(ok_inst)

    space_id = "space-y"
    fed_repo.add_space_member(space_id, banned_inst.id)
    fed_repo.add_space_member(space_id, ok_inst.id)
    await fed_repo.ban_instance_from_space(space_id, banned_inst.id)

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

    result = await svc.broadcast_to_space_members(
        space_id=space_id,
        event_type=FederationEventType.SPACE_POST_CREATED,
        payload={"post_id": "p1"},
    )

    assert result.attempted == 1
    assert result.succeeded == 1


# ─── send_with_mesh_fallback ──────────────────────────────────────────────


def _make_mesh_pair(
    *,
    route: tuple[list[str], str] | None = None,
) -> tuple[MagicMock, MagicMock, AsyncMock, AsyncMock]:
    """Build (route_service, routed_handler, discover_route, send_routed) mocks."""
    discover_route = AsyncMock(return_value=route)
    send_routed = AsyncMock(return_value="route-id-mock")
    route_service = MagicMock()
    route_service.discover_route = discover_route
    # ``cooldown_remaining`` is a *sync* read consulted before every probe
    # (the negative-cooldown branch of ``send_with_mesh_fallback``); default
    # to "no cooldown armed" so a plain mesh pair behaves like a warm one.
    route_service.cooldown_remaining = MagicMock(return_value=0.0)
    # ``invalidate`` is awaited on the #648 paths (mesh BEGIN admission and
    # the retry after a failed routed ship), so it must be an AsyncMock —
    # a bare MagicMock attribute would return a non-awaitable.
    route_service.invalidate = AsyncMock()
    routed_handler = MagicMock()
    routed_handler.send_routed = send_routed
    return route_service, routed_handler, discover_route, send_routed


@pytest.mark.asyncio
async def test_send_with_mesh_fallback_uses_direct_for_confirmed_peer():
    """A CONFIRMED peer ships via :meth:`send_event` — mesh untouched."""
    km = _make_kek_manager()
    fed_repo = InMemoryFederationRepo()
    outbox_repo = InMemoryOutboxRepo()
    inst, _ = _make_remote_instance(km)
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
    route_service, routed_handler, discover_route, send_routed = _make_mesh_pair(
        route=(["self", inst.id], "eph-pk"),
    )
    svc.attach_mesh(route_service=route_service, routed_handler=routed_handler)

    result = await svc.send_with_mesh_fallback(
        to_instance_id=inst.id,
        event_type=FederationEventType.SPACE_POST_CREATED,
        payload={"post_id": "p1"},
    )

    assert result.ok is True
    assert result.instance_id == inst.id
    # Direct path → HTTP POST hit, mesh untouched.
    assert mock_http.post.call_count == 1
    discover_route.assert_not_awaited()
    send_routed.assert_not_awaited()


@pytest.mark.asyncio
async def test_send_with_mesh_fallback_routes_via_mesh_for_unconfirmed_peer():
    """A non-CONFIRMED peer triggers route discovery + SPACE_ROUTED."""
    km = _make_kek_manager()
    fed_repo = InMemoryFederationRepo()
    outbox_repo = InMemoryOutboxRepo()
    inst, _ = _make_remote_instance(km)
    # Flip to a non-confirmed state.
    inst = dataclasses.replace(inst, status=PairingStatus.PENDING_SENT)
    await fed_repo.save_instance(inst)

    svc, _ = _make_service(
        federation_repo=fed_repo,
        outbox_repo=outbox_repo,
        key_manager=km,
    )
    path = [svc.own_instance_id, "hop", inst.id]
    route_service, routed_handler, discover_route, send_routed = _make_mesh_pair(
        route=(path, "eph-pk-b64"),
    )
    svc.attach_mesh(route_service=route_service, routed_handler=routed_handler)

    payload = {"space_id": "sp-1", "post_id": "p1"}
    result = await svc.send_with_mesh_fallback(
        to_instance_id=inst.id,
        event_type=FederationEventType.SPACE_POST_CREATED,
        payload=payload,
    )

    assert result.ok is True
    assert result.instance_id == inst.id
    discover_route.assert_awaited_once_with(inst.id)
    send_routed.assert_awaited_once()
    call = send_routed.call_args
    assert call.kwargs["path"] == path
    assert call.kwargs["target_eph_pk_b64"] == "eph-pk-b64"
    assert call.kwargs["inner_event_type"] == FederationEventType.SPACE_POST_CREATED
    assert call.kwargs["inner_payload"] == payload


@pytest.mark.asyncio
async def test_send_with_mesh_fallback_returns_failure_when_no_route_found():
    """Discovery returning ``None`` yields ``ok=False, error='no_route'``."""
    km = _make_kek_manager()
    fed_repo = InMemoryFederationRepo()
    outbox_repo = InMemoryOutboxRepo()
    inst, _ = _make_remote_instance(km)
    inst = dataclasses.replace(inst, status=PairingStatus.PENDING_SENT)
    await fed_repo.save_instance(inst)

    svc, _ = _make_service(
        federation_repo=fed_repo,
        outbox_repo=outbox_repo,
        key_manager=km,
    )
    route_service, routed_handler, _, send_routed = _make_mesh_pair(route=None)
    svc.attach_mesh(route_service=route_service, routed_handler=routed_handler)

    result = await svc.send_with_mesh_fallback(
        to_instance_id=inst.id,
        event_type=FederationEventType.SPACE_PRIVATE_INVITE,
        payload={"x": 1},
    )

    assert result.ok is False
    assert result.error == "no_route"
    send_routed.assert_not_awaited()


@pytest.mark.asyncio
async def test_send_with_mesh_fallback_returns_failure_when_mesh_not_attached():
    """No ``attach_mesh`` call → ``ok=False, error='not_confirmed'``."""
    km = _make_kek_manager()
    fed_repo = InMemoryFederationRepo()
    outbox_repo = InMemoryOutboxRepo()
    inst, _ = _make_remote_instance(km)
    inst = dataclasses.replace(inst, status=PairingStatus.PENDING_SENT)
    await fed_repo.save_instance(inst)

    svc, _ = _make_service(
        federation_repo=fed_repo,
        outbox_repo=outbox_repo,
        key_manager=km,
    )
    # Deliberately do NOT call attach_mesh.

    result = await svc.send_with_mesh_fallback(
        to_instance_id=inst.id,
        event_type=FederationEventType.SPACE_PRIVATE_INVITE,
        payload={"x": 1},
    )

    assert result.ok is False
    assert result.error == "not_confirmed"


# ─── begin_mesh_catchup_sync ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_begin_mesh_catchup_sync_registers_session_and_sends_for_mesh_host():
    """A non-confirmed (mesh) host: register a requester HTTPS receive
    session and ship SPACE_SYNC_BEGIN(prefer_direct=False) via mesh fallback."""
    svc, _ = _make_service()
    sync_manager = MagicMock()
    sync_manager.register_requester_https_session = MagicMock(return_value=True)
    sync_manager.close_session = MagicMock()
    svc.attach_sync_manager(sync_manager)

    send_mock = AsyncMock(
        return_value=DeliveryResult(instance_id="host-1", ok=True),
    )
    with (
        patch.object(
            FederationService, "is_confirmed_peer", AsyncMock(return_value=False)
        ),
        patch.object(FederationService, "send_with_mesh_fallback", send_mock),
    ):
        await svc.begin_mesh_catchup_sync(space_id="sp-1", host_instance_id="host-1")

    # Registered a requester-side receive session for this sync.
    sync_manager.register_requester_https_session.assert_called_once()
    reg_kwargs = sync_manager.register_requester_https_session.call_args.kwargs
    assert reg_kwargs["space_id"] == "sp-1"
    assert reg_kwargs["requester_instance_id"] == svc._own_instance_id
    assert reg_kwargs["provider_instance_id"] == "host-1"
    sync_id = reg_kwargs["sync_id"]
    assert sync_id

    # Sent SPACE_SYNC_BEGIN with prefer_direct=False via mesh fallback.
    send_mock.assert_awaited_once()
    send_kwargs = send_mock.call_args.kwargs
    assert send_kwargs["to_instance_id"] == "host-1"
    assert send_kwargs["event_type"] is FederationEventType.SPACE_SYNC_BEGIN
    assert send_kwargs["space_id"] == "sp-1"
    assert send_kwargs["payload"]["sync_id"] == sync_id
    assert send_kwargs["payload"]["space_id"] == "sp-1"
    assert send_kwargs["payload"]["sync_mode"] == "initial"
    assert send_kwargs["payload"]["prefer_direct"] is False
    # Success → session NOT closed (the chunk replies still need it).
    sync_manager.close_session.assert_not_called()


@pytest.mark.asyncio
async def test_begin_mesh_catchup_sync_noop_for_confirmed_host():
    """A CONFIRMED host is a no-op — the SpaceSyncScheduler already syncs it."""
    svc, _ = _make_service()
    sync_manager = MagicMock()
    sync_manager.register_requester_https_session = MagicMock(return_value=True)
    svc.attach_sync_manager(sync_manager)

    send_mock = AsyncMock()
    with (
        patch.object(
            FederationService, "is_confirmed_peer", AsyncMock(return_value=True)
        ),
        patch.object(FederationService, "send_with_mesh_fallback", send_mock),
    ):
        await svc.begin_mesh_catchup_sync(space_id="sp-1", host_instance_id="host-1")

    sync_manager.register_requester_https_session.assert_not_called()
    send_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_begin_mesh_catchup_sync_closes_session_on_send_failure():
    """If the mesh send fails, the registered receive session is closed
    (no dangling session leak) and no exception propagates."""
    svc, _ = _make_service()
    sync_manager = MagicMock()
    sync_manager.register_requester_https_session = MagicMock(return_value=True)
    sync_manager.close_session = MagicMock()
    svc.attach_sync_manager(sync_manager)

    send_mock = AsyncMock(
        return_value=DeliveryResult(instance_id="host-1", ok=False, error="no_route"),
    )
    with (
        patch.object(
            FederationService, "is_confirmed_peer", AsyncMock(return_value=False)
        ),
        patch.object(FederationService, "send_with_mesh_fallback", send_mock),
    ):
        await svc.begin_mesh_catchup_sync(space_id="sp-1", host_instance_id="host-1")

    sync_id = sync_manager.register_requester_https_session.call_args.kwargs["sync_id"]
    sync_manager.close_session.assert_called_once_with(sync_id)


@pytest.mark.asyncio
async def test_begin_mesh_catchup_sync_noop_when_sync_manager_unwired():
    """No sync manager attached → no-op, no raise."""
    svc, _ = _make_service()
    assert svc._sync_manager is None
    send_mock = AsyncMock()
    with patch.object(FederationService, "send_with_mesh_fallback", send_mock):
        await svc.begin_mesh_catchup_sync(space_id="sp-1", host_instance_id="host-1")

    send_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_broadcast_to_space_members_uses_mesh_fallback(monkeypatch):
    """Every per-peer ship in ``broadcast_to_space_members`` routes
    through :meth:`send_with_mesh_fallback` — so a future widening of
    ``list_instances_in_space`` to include non-CONFIRMED members will
    light up mesh delivery without further wiring. The test seeds two
    members (one CONFIRMED, one PENDING) and asserts each lands on
    the right transport.
    """
    km = _make_kek_manager()
    fed_repo = InMemoryFederationRepo()
    outbox_repo = InMemoryOutboxRepo()
    import os

    confirmed_kp = generate_identity_keypair()
    pending_kp = generate_identity_keypair()
    confirmed_inst, _ = _make_remote_instance(
        km,
        peer_kp=confirmed_kp,
        session_key=os.urandom(32),
    )
    pending_inst, _ = _make_remote_instance(
        km,
        peer_kp=pending_kp,
        session_key=os.urandom(32),
    )
    pending_inst = dataclasses.replace(
        pending_inst,
        status=PairingStatus.PENDING_SENT,
    )
    await fed_repo.save_instance(confirmed_inst)
    await fed_repo.save_instance(pending_inst)
    space_id = "space-mesh"
    fed_repo.add_space_member(space_id, confirmed_inst.id)
    fed_repo.add_space_member(space_id, pending_inst.id)

    # The ``list_member_instance_ids`` query is the unfiltered surface
    # that ``broadcast_to_space_members`` consults — no
    # ``remote_instances.status`` filter — so the mesh branch sees
    # PENDING members too. The InMemory fake already does the right
    # thing (no status filter); just confirm by overriding to return
    # both ids deterministically here.
    async def _list_member_ids(space):
        return [confirmed_inst.id, pending_inst.id]

    monkeypatch.setattr(fed_repo, "list_member_instance_ids", _list_member_ids)

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
    route_service, routed_handler, discover_route, send_routed = _make_mesh_pair(
        route=([svc.own_instance_id, "hop", pending_inst.id], "eph-pk-b64"),
    )
    svc.attach_mesh(route_service=route_service, routed_handler=routed_handler)

    result = await svc.broadcast_to_space_members(
        space_id=space_id,
        event_type=FederationEventType.SPACE_POST_CREATED,
        payload={"post_id": "p1"},
    )

    assert result.attempted == 2
    assert result.succeeded == 2
    # CONFIRMED member shipped via HTTP, PENDING member via mesh.
    assert mock_http.post.call_count == 1
    send_routed.assert_awaited_once()
    discover_route.assert_awaited_once_with(pending_inst.id)


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


# ─── Binary media channel (fed-media-v1) ───────────────────────────────────
#
# These exercise send_media_chunk (sender) + handle_inbound_media_frame
# (receiver) end-to-end across two services A→B that share a session key,
# plus the §24.11 + per-chunk-binding negative paths.

import hashlib  # noqa: E402
import types  # noqa: E402

import orjson  # noqa: E402

from socialhome.crypto import b64url_encode as _b64url_encode  # noqa: E402


class _CaptureMediaTransport:
    """Stub transport: records the binary media frame and the JSON
    fallback envelope so a test can see which path the sender chose."""

    def __init__(self) -> None:
        self.media: tuple[dict, bytes] | None = None
        self.json_envelopes: list[dict] = []
        self.media_ready = True
        self.media_returns = True

    def is_media_ready(self, instance_id: str) -> bool:
        return self.media_ready

    async def send_media(self, *, instance, header_dict, payload_bytes) -> bool:
        self.media = (header_dict, payload_bytes)
        return self.media_returns

    async def send(self, *, instance, envelope_dict):
        self.json_envelopes.append(envelope_dict)
        return types.SimpleNamespace(ok=True, via="rtc", status_code=None, error=None)


def _svc_with(own_kp, repo, km) -> FederationService:
    return FederationService(
        db=MagicMock(),
        federation_repo=repo,
        outbox_repo=InMemoryOutboxRepo(),
        key_manager=km,
        bus=EventBus(),
        own_instance_id=derive_instance_id(own_kp.public_key),
        own_identity_seed=own_kp.private_key,
        own_identity_pk=own_kp.public_key,
        http_client=None,
    )


def _paired(*, peer_proto_version: int = FederationCapability.MIN_FOR_MEDIA_CHANNEL):
    """Two services A and B that share a session key and each list the
    other as a CONFIRMED peer. A's transport is a capture stub."""
    import os

    a_kp = generate_identity_keypair()
    b_kp = generate_identity_keypair()
    a_id = derive_instance_id(a_kp.public_key)
    b_id = derive_instance_id(b_kp.public_key)
    session = os.urandom(32)
    km_a = _make_kek_manager()
    km_b = _make_kek_manager()
    repo_a = InMemoryFederationRepo()
    repo_b = InMemoryFederationRepo()

    inst_b_for_a, _ = _make_remote_instance(km_a, peer_kp=b_kp, session_key=session)
    repo_a._instances[b_id] = dataclasses.replace(
        inst_b_for_a, proto_version=peer_proto_version
    )
    inst_a_for_b, _ = _make_remote_instance(km_b, peer_kp=a_kp, session_key=session)
    repo_b._instances[a_id] = inst_a_for_b

    svc_a = _svc_with(a_kp, repo_a, km_a)
    svc_b = _svc_with(b_kp, repo_b, km_b)
    cap = _CaptureMediaTransport()
    svc_a.attach_transport(cap)

    captured: list = []

    async def _record(event):
        captured.append(event)

    svc_b._event_registry.register(FederationEventType.DM_MEDIA_BLOB, _record)
    svc_b._event_registry.register(FederationEventType.SPACE_MEDIA_BLOB, _record)

    return types.SimpleNamespace(
        a_kp=a_kp,
        b_kp=b_kp,
        a_id=a_id,
        b_id=b_id,
        session=session,
        svc_a=svc_a,
        svc_b=svc_b,
        repo_a=repo_a,
        repo_b=repo_b,
        cap=cap,
        received=captured,
    )


def _make_frame(
    p,
    *,
    event_type=FederationEventType.DM_MEDIA_BLOB,
    payload=None,
    raw=b"the full media bytes",
    suite=MEDIA_AEAD_SUITE_AESGCM_256,
    sha_override=None,
    corrupt_sig=False,
    space_id=None,
) -> tuple[bytes, bytes]:
    """Build a media frame (header_bytes, payload_bytes) signed by A, the
    way the production sender would, with knobs for the negative paths."""
    enc_a = FederationEncoder(p.a_kp.private_key)
    sha = sha_override or _b64url_encode(hashlib.sha256(raw).digest())
    meta = {**(payload or {"media_blob_id": "m1", "message_id": "m1"})}
    meta["chunk_sha256"] = sha
    meta["media_aead_suite"] = suite
    encrypted_payload = enc_a.encrypt_payload(orjson.dumps(meta).decode(), p.session)
    envelope = {
        "msg_id": str(uuid.uuid4()),
        "event_type": event_type.value,
        "from_instance": p.a_id,
        "to_instance": p.b_id,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "encrypted_payload": encrypted_payload,
        "space_id": space_id,
        "proto_version": 1,
        "sig_suite": "ed25519",
    }
    envelope_bytes = orjson.dumps(envelope)
    envelope["signatures"] = enc_a.sign_envelope_all(envelope_bytes, suite="ed25519")
    if corrupt_sig:
        envelope["signatures"]["ed25519"] = b64url_encode(b"\x00" * 64)
    payload_bytes = enc_a.encrypt_bytes(raw, p.session)
    return orjson.dumps(envelope), payload_bytes


@pytest.mark.asyncio
async def test_send_media_chunk_uses_binary_when_peer_supports():
    """CONFIRMED v_14 peer with an open channel → binary frame, no base64."""
    p = _paired()
    raw = b"\x89PNG\r\n" + b"binary-payload" * 50
    result = await p.svc_a.send_media_chunk(
        to_instance_id=p.b_id,
        event_type=FederationEventType.DM_MEDIA_BLOB,
        payload={
            "media_blob_id": "m1",
            "message_id": "m1",
            "chunk_index": 0,
            "chunk_count": 1,
            "final": True,
        },
        raw_chunk=raw,
    )
    assert result.ok
    assert p.cap.json_envelopes == []  # JSON path NOT used
    assert p.cap.media is not None
    header, payload_bytes = p.cap.media
    # Payload is raw AEAD bytes (nonce+ct+tag), not base64.
    assert len(payload_bytes) == 12 + len(raw) + 16
    assert p.svc_b._encoder.decrypt_bytes(payload_bytes, p.session) == raw
    # Header is a normal signed envelope; metadata carries the binding +
    # suite and NOT the bytes.
    assert header["event_type"] == "dm_media_blob"
    assert header["from_instance"] == p.a_id
    assert header["proto_version"] == 1
    assert "signatures" in header
    meta = orjson.loads(
        p.svc_b._encoder.decrypt_payload(header["encrypted_payload"], p.session)
    )
    assert "bytes_b64" not in meta
    assert meta["media_aead_suite"] == MEDIA_AEAD_SUITE_AESGCM_256
    assert meta["chunk_sha256"] == _b64url_encode(hashlib.sha256(raw).digest())


@pytest.mark.asyncio
async def test_media_frame_round_trip_dispatches_with_raw_bytes():
    """A's binary frame validates on B and dispatches with media_bytes set."""
    p = _paired()
    raw = b"round-trip-bytes" * 64
    await p.svc_a.send_media_chunk(
        to_instance_id=p.b_id,
        event_type=FederationEventType.DM_MEDIA_BLOB,
        payload={"media_blob_id": "m1", "message_id": "m1"},
        raw_chunk=raw,
    )
    header, payload_bytes = p.cap.media
    res = await p.svc_b.handle_inbound_media_frame(
        p.a_id, orjson.dumps(header), payload_bytes
    )
    assert res == {"status": "ok"}
    assert len(p.received) == 1
    event = p.received[0]
    assert event.event_type is FederationEventType.DM_MEDIA_BLOB
    assert event.media_bytes == raw
    assert event.payload["media_blob_id"] == "m1"


@pytest.mark.asyncio
async def test_space_media_frame_round_trip():
    """SPACE_MEDIA_BLOB rides the same binary path."""
    p = _paired()
    raw = b"space-media" * 32
    await p.svc_a.send_media_chunk(
        to_instance_id=p.b_id,
        event_type=FederationEventType.SPACE_MEDIA_BLOB,
        payload={"filename": "img.webp", "transfer_id": "t1"},
        raw_chunk=raw,
    )
    header, payload_bytes = p.cap.media
    await p.svc_b.handle_inbound_media_frame(
        p.a_id, orjson.dumps(header), payload_bytes
    )
    assert len(p.received) == 1
    assert p.received[0].event_type is FederationEventType.SPACE_MEDIA_BLOB
    assert p.received[0].media_bytes == raw


@pytest.mark.asyncio
async def test_send_media_chunk_falls_back_when_peer_unsupported():
    """Sub-v_14 peer → JSON send_event path with a base64 bytes_b64."""
    p = _paired(peer_proto_version=1)
    raw = b"fallback-bytes"
    result = await p.svc_a.send_media_chunk(
        to_instance_id=p.b_id,
        event_type=FederationEventType.DM_MEDIA_BLOB,
        payload={"media_blob_id": "m1", "message_id": "m1"},
        raw_chunk=raw,
    )
    assert result.ok
    assert p.cap.media is None  # binary channel NOT used
    assert len(p.cap.json_envelopes) == 1
    # The JSON envelope's encrypted payload carries the base64 bytes.
    env = p.cap.json_envelopes[0]
    meta = orjson.loads(
        p.svc_b._encoder.decrypt_payload(env["encrypted_payload"], p.session)
    )
    assert base64_b64decode_eq(meta["bytes_b64"], raw)


def base64_b64decode_eq(b64: str, raw: bytes) -> bool:
    import base64 as _b64

    return _b64.b64decode(b64) == raw


@pytest.mark.asyncio
async def test_send_media_chunk_falls_back_when_channel_not_ready():
    """Channel down → JSON path even though the peer supports v_14."""
    p = _paired()
    p.cap.media_ready = False
    result = await p.svc_a.send_media_chunk(
        to_instance_id=p.b_id,
        event_type=FederationEventType.DM_MEDIA_BLOB,
        payload={"media_blob_id": "m1", "message_id": "m1"},
        raw_chunk=b"x",
    )
    assert result.ok
    assert p.cap.media is None
    assert len(p.cap.json_envelopes) == 1


@pytest.mark.asyncio
async def test_media_frame_rejects_sha256_mismatch():
    """Valid GCM payload whose plaintext hash ≠ committed sha256 → reject."""
    p = _paired()
    raw = b"the committed bytes"
    header, _ = _make_frame(p, raw=raw, payload={"media_blob_id": "m1"})
    # Re-encrypt DIFFERENT bytes under the session key — GCM tag is valid
    # but the plaintext no longer matches the signed chunk_sha256.
    other = p.svc_b._encoder.encrypt_bytes(b"tampered different bytes", p.session)
    with pytest.raises(ValueError, match="sha256 mismatch"):
        await p.svc_b.handle_inbound_media_frame(p.a_id, header, other)
    assert p.received == []


@pytest.mark.asyncio
async def test_media_frame_rejects_bad_signature():
    p = _paired()
    header, payload_bytes = _make_frame(p, corrupt_sig=True)
    with pytest.raises(ValueError, match="signature"):
        await p.svc_b.handle_inbound_media_frame(p.a_id, header, payload_bytes)
    assert p.received == []


@pytest.mark.asyncio
async def test_media_frame_rejects_replay():
    p = _paired()
    header, payload_bytes = _make_frame(p, raw=b"once")
    await p.svc_b.handle_inbound_media_frame(p.a_id, header, payload_bytes)
    assert len(p.received) == 1
    with pytest.raises(ValueError, match="[Rr]eplay"):
        await p.svc_b.handle_inbound_media_frame(p.a_id, header, payload_bytes)
    assert len(p.received) == 1  # not dispatched twice


@pytest.mark.asyncio
async def test_media_frame_rejects_unknown_aead_suite():
    p = _paired()
    header, payload_bytes = _make_frame(p, suite="chacha-bogus")
    with pytest.raises(UnsupportedMediaAeadSuite):
        await p.svc_b.handle_inbound_media_frame(p.a_id, header, payload_bytes)
    assert p.received == []


@pytest.mark.asyncio
async def test_send_media_chunk_space_non_confirmed_uses_mesh_fallback():
    """A non-CONFIRMED space target never uses the binary channel; with no
    mesh attached it surfaces not_confirmed (the mesh-fallback path)."""
    p = _paired()
    # Demote the peer to non-CONFIRMED in A's repo.
    p.repo_a._instances[p.b_id] = dataclasses.replace(
        p.repo_a._instances[p.b_id], status=PairingStatus.PENDING_SENT
    )
    result = await p.svc_a.send_media_chunk(
        to_instance_id=p.b_id,
        event_type=FederationEventType.SPACE_MEDIA_BLOB,
        payload={"filename": "f.webp", "transfer_id": "t1"},
        raw_chunk=b"x",
        mesh_fallback=True,
    )
    assert p.cap.media is None  # binary channel never used for non-CONFIRMED
    assert not result.ok
    assert result.error == "not_confirmed"


@pytest.mark.asyncio
async def test_send_media_chunk_falls_back_when_binary_send_returns_false():
    """transport.send_media returns False (channel raced closed / over HWM)
    → transparent JSON fallback."""
    p = _paired()
    p.cap.media_returns = False
    result = await p.svc_a.send_media_chunk(
        to_instance_id=p.b_id,
        event_type=FederationEventType.DM_MEDIA_BLOB,
        payload={"media_blob_id": "m1", "message_id": "m1"},
        raw_chunk=b"bytes",
    )
    assert result.ok
    assert p.cap.media is not None  # binary was attempted
    assert len(p.cap.json_envelopes) == 1  # then fell back to JSON


@pytest.mark.asyncio
async def test_send_media_chunk_falls_back_when_session_key_undecryptable():
    """A corrupt stored session key → binary build fails → JSON fallback
    (which then also fails the key decrypt and reports it)."""
    p = _paired()
    p.repo_a._instances[p.b_id] = dataclasses.replace(
        p.repo_a._instances[p.b_id], key_self_to_remote="not-a-valid-encrypted-key"
    )
    result = await p.svc_a.send_media_chunk(
        to_instance_id=p.b_id,
        event_type=FederationEventType.DM_MEDIA_BLOB,
        payload={"media_blob_id": "m1", "message_id": "m1"},
        raw_chunk=b"bytes",
    )
    assert p.cap.media is None  # binary never shipped a frame
    assert not result.ok
    assert result.error == "key_decrypt_error"


@pytest.mark.asyncio
async def test_handle_inbound_rtc_dispatches_validated_event():
    """The (non-media) RTC entry point validates + dispatches."""
    p = _paired()
    header, _payload = _make_frame(p, payload={"media_blob_id": "m1"})
    res = await p.svc_b.handle_inbound_rtc(p.a_id, header)
    assert res == {"status": "ok"}
    assert len(p.received) == 1


@pytest.mark.asyncio
async def test_media_frame_rejects_corrupt_payload():
    """A payload whose GCM tag fails (tampered ciphertext) → reject."""
    p = _paired()
    header, payload_bytes = _make_frame(p, raw=b"good bytes")
    corrupt = bytearray(payload_bytes)
    corrupt[-1] ^= 0x01  # break the GCM tag
    with pytest.raises(ValueError, match="decrypt media chunk"):
        await p.svc_b.handle_inbound_media_frame(p.a_id, header, bytes(corrupt))
    assert p.received == []


@pytest.mark.asyncio
async def test_media_frame_idempotency_short_circuits_before_assembly():
    """Two frames sharing an idempotency_key: the second is deduped by the
    pipeline (early_response) and never re-dispatched."""
    from socialhome.infrastructure.idempotency import IdempotencyCache

    p = _paired()
    p.svc_b.attach_idempotency_cache(IdempotencyCache(ttl_seconds=60))
    payload = {"media_blob_id": "m1", "idempotency_key": "dedup-key-1"}
    h1, pl1 = _make_frame(p, payload=dict(payload), raw=b"first")
    h2, pl2 = _make_frame(p, payload=dict(payload), raw=b"second")
    r1 = await p.svc_b.handle_inbound_media_frame(p.a_id, h1, pl1)
    r2 = await p.svc_b.handle_inbound_media_frame(p.a_id, h2, pl2)
    assert r1 == {"status": "ok"}
    assert r2.get("deduped") is True
    assert len(p.received) == 1  # only the first dispatched


# ─── Binary app channel (fed-app-v1) ───────────────────────────────────────────
#
# These exercise send_app_message (sender) + _app_inbound_handler
# (receiver) end-to-end, plus the gating logic (binary vs JSON fallback).


import types as _types  # noqa: E402


class _CaptureAppTransport(_CaptureMediaTransport):
    """Extends the media stub to also capture app frames."""

    def __init__(self) -> None:
        super().__init__()
        self.app: tuple[dict, bytes] | None = None
        self.app_ready = True
        self.app_returns = True

    def is_app_ready(self, instance_id: str) -> bool:
        return self.app_ready

    async def send_app(self, *, instance, header_dict, payload_bytes) -> bool:
        self.app = (header_dict, payload_bytes)
        return self.app_returns


def _paired_app(*, peer_proto_version: int = FederationCapability.MIN_FOR_APP_CHANNEL):
    """Two services A and B paired for the app-channel tests."""
    import os

    a_kp = generate_identity_keypair()
    b_kp = generate_identity_keypair()
    a_id = derive_instance_id(a_kp.public_key)
    b_id = derive_instance_id(b_kp.public_key)
    session = os.urandom(32)
    km_a = _make_kek_manager()
    km_b = _make_kek_manager()
    repo_a = InMemoryFederationRepo()
    repo_b = InMemoryFederationRepo()

    inst_b_for_a, _ = _make_remote_instance(km_a, peer_kp=b_kp, session_key=session)
    repo_a._instances[b_id] = dataclasses.replace(
        inst_b_for_a, proto_version=peer_proto_version
    )
    inst_a_for_b, _ = _make_remote_instance(km_b, peer_kp=a_kp, session_key=session)
    repo_b._instances[a_id] = inst_a_for_b

    svc_a = _svc_with(a_kp, repo_a, km_a)
    svc_b = _svc_with(b_kp, repo_b, km_b)
    cap = _CaptureAppTransport()
    svc_a.attach_transport(cap)

    captured: list = []

    async def _record(event):
        captured.append(event)

    svc_b._event_registry.register(FederationEventType.APP_MESSAGE, _record)
    svc_b._event_registry.register(FederationEventType.APP_SESSION, _record)

    return _types.SimpleNamespace(
        a_kp=a_kp,
        b_kp=b_kp,
        a_id=a_id,
        b_id=b_id,
        session=session,
        svc_a=svc_a,
        svc_b=svc_b,
        repo_a=repo_a,
        repo_b=repo_b,
        cap=cap,
        received=captured,
    )


from socialhome.federation.app_framing import (  # noqa: E402
    APP_AEAD_SUITE_AESGCM_256,
    UnsupportedAppAeadSuite,
)


@pytest.mark.asyncio
async def test_send_app_message_uses_binary_when_peer_supports():
    """CONFIRMED v_17 peer with an open app channel → binary frame, not JSON."""
    p = _paired_app()
    app_payload = {"move": "e2e4", "clock": 90}
    result = await p.svc_a.send_app_message(
        to_instance_id=p.b_id,
        app_id="chess",
        session_id="sess-1",
        payload=app_payload,
    )
    assert result.ok
    # JSON path NOT used.
    assert p.cap.json_envelopes == []
    # Binary path was used.
    assert p.cap.app is not None
    header, payload_bytes = p.cap.app
    assert header["event_type"] == "app_message"
    assert header["from_instance"] == p.a_id
    assert header["proto_version"] == 1
    assert "signatures" in header
    # Decrypt the metadata — should have app_id / session_id / aead_suite /
    # payload_sha256 (integrity binding — FIX-I1).
    meta = orjson.loads(
        p.svc_b._encoder.decrypt_payload(header["encrypted_payload"], p.session)
    )
    assert meta["app_id"] == "chess"
    assert meta["session_id"] == "sess-1"
    assert meta["app_aead_suite"] == APP_AEAD_SUITE_AESGCM_256
    # payload_sha256 must be present and match the decrypted plaintext.
    assert "payload_sha256" in meta
    # Application payload is sealed — decrypt binary payload.
    raw = p.svc_b._encoder.decrypt_bytes(payload_bytes, p.session)
    assert orjson.loads(raw) == app_payload
    # Verify the sha256 binding.
    import base64 as _b64
    import hashlib as _hashlib

    expected_sha = (
        _b64.urlsafe_b64encode(_hashlib.sha256(raw).digest()).rstrip(b"=").decode()
    )
    assert meta["payload_sha256"] == expected_sha


@pytest.mark.asyncio
async def test_send_app_message_falls_back_to_json_when_peer_unsupported():
    """Sub-v_17 peer → JSON APP_MESSAGE event, payload nested under 'data'."""
    p = _paired_app(peer_proto_version=1)
    app_payload = {"move": "d7d5"}
    result = await p.svc_a.send_app_message(
        to_instance_id=p.b_id,
        app_id="chess",
        session_id="sess-2",
        payload=app_payload,
    )
    assert result.ok
    # Binary path NOT used.
    assert p.cap.app is None
    # JSON path used.
    assert len(p.cap.json_envelopes) == 1
    env = p.cap.json_envelopes[0]
    assert env["event_type"] == "app_message"
    # Decrypt the envelope payload — must contain app_id + session_id + data.
    meta = orjson.loads(
        p.svc_b._encoder.decrypt_payload(env["encrypted_payload"], p.session)
    )
    assert meta["app_id"] == "chess"
    assert meta["session_id"] == "sess-2"
    assert meta["data"] == app_payload
    # ENCRYPTION-FIRST: the raw payload dict must NOT appear in plaintext in
    # the envelope dict itself.
    assert "move" not in str(env.get("encrypted_payload", ""))
    # The key fields that the caller passed must not be in any top-level field
    # of the envelope (they should only be inside the ciphertext).
    assert env.get("app_id") is None
    assert env.get("data") is None


@pytest.mark.asyncio
async def test_send_app_message_json_carries_to_user_from_user_routing():
    """to_user/from_user ride the JSON APP_MESSAGE path (binary has no slot)."""
    p = _paired_app(peer_proto_version=1)  # forces JSON path
    result = await p.svc_a.send_app_message(
        to_instance_id=p.b_id,
        app_id="chess",
        session_id="sess-r",
        payload={"move": "e7e5"},
        to_user="bob",
        from_user="alice",
    )
    assert result.ok
    assert p.cap.app is None
    assert len(p.cap.json_envelopes) == 1
    env = p.cap.json_envelopes[0]
    meta = orjson.loads(
        p.svc_b._encoder.decrypt_payload(env["encrypted_payload"], p.session)
    )
    assert meta["to_user"] == "bob"
    assert meta["from_user"] == "alice"


@pytest.mark.asyncio
async def test_send_app_message_json_omits_routing_when_none():
    """No to_user/from_user supplied → those keys absent from the JSON payload."""
    p = _paired_app(peer_proto_version=1)
    await p.svc_a.send_app_message(
        to_instance_id=p.b_id,
        app_id="chess",
        session_id="sess-r2",
        payload={"move": "e7e5"},
    )
    env = p.cap.json_envelopes[0]
    meta = orjson.loads(
        p.svc_b._encoder.decrypt_payload(env["encrypted_payload"], p.session)
    )
    assert "to_user" not in meta
    assert "from_user" not in meta


@pytest.mark.asyncio
async def test_send_app_message_falls_back_when_channel_not_ready():
    """Channel down → JSON fallback even though peer supports v_17."""
    p = _paired_app()
    p.cap.app_ready = False
    result = await p.svc_a.send_app_message(
        to_instance_id=p.b_id,
        app_id="chess",
        session_id="sess-3",
        payload={"move": "f4"},
    )
    assert result.ok
    assert p.cap.app is None  # binary not attempted
    assert len(p.cap.json_envelopes) == 1


@pytest.mark.asyncio
async def test_send_app_message_falls_back_when_binary_send_returns_false():
    """transport.send_app returns False → JSON fallback."""
    p = _paired_app()
    p.cap.app_returns = False
    result = await p.svc_a.send_app_message(
        to_instance_id=p.b_id,
        app_id="chess",
        session_id="sess-4",
        payload={"x": 1},
    )
    assert result.ok
    assert p.cap.app is not None  # binary was attempted
    assert len(p.cap.json_envelopes) == 1  # then fell back


@pytest.mark.asyncio
async def test_app_inbound_handler_dispatches_to_app_fed():
    """_app_inbound_handler: valid binary frame → on_inbound_message called."""
    import orjson as _orjson

    p = _paired_app()

    # Set up a fake app_fed attached to svc_b.
    inbound_calls: list[tuple] = []

    class _FakeAppFed:
        async def on_inbound_message(self, instance_id, app_id, session_id, payload):
            inbound_calls.append((instance_id, app_id, session_id, payload))

    p.svc_b.attach_apps(_FakeAppFed())

    # Build a valid binary-channel frame from A's perspective.
    await p.svc_a.send_app_message(
        to_instance_id=p.b_id,
        app_id="chess",
        session_id="sess-bin",
        payload={"move": "g1f3"},
    )
    assert p.cap.app is not None
    header, payload_bytes = p.cap.app

    # Feed to svc_b's binary inbound handler.
    await p.svc_b._app_inbound_handler(p.a_id, _orjson.dumps(header), payload_bytes)
    assert len(inbound_calls) == 1
    inst_id, app_id, session_id, payload = inbound_calls[0]
    assert inst_id == p.a_id
    assert app_id == "chess"
    assert session_id == "sess-bin"
    assert payload == {"move": "g1f3"}


@pytest.mark.asyncio
async def test_app_inbound_handler_rejects_unknown_aead_suite():
    """_app_inbound_handler raises UnsupportedAppAeadSuite for unknown suite."""
    import orjson as _orjson

    p = _paired_app()

    class _FakeAppFed:
        async def on_inbound_message(self, *args): ...

    p.svc_b.attach_apps(_FakeAppFed())

    # Build a frame with a bogus suite directly (bypass the normal sender).
    # Reuse the media _make_frame helper structure but for app messages.
    from socialhome.federation.encoder import FederationEncoder

    enc_a = FederationEncoder(p.a_kp.private_key)
    bad_meta = {
        "app_id": "chess",
        "session_id": "s",
        "app_aead_suite": "chacha-bogus",
    }
    encrypted_payload = enc_a.encrypt_payload(
        _orjson.dumps(bad_meta).decode(), p.session
    )
    envelope = {
        "msg_id": str(uuid.uuid4()),
        "event_type": FederationEventType.APP_MESSAGE.value,
        "from_instance": p.a_id,
        "to_instance": p.b_id,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "encrypted_payload": encrypted_payload,
        "space_id": None,
        "proto_version": 1,
        "sig_suite": "ed25519",
    }
    env_bytes = orjson.dumps(envelope)
    envelope["signatures"] = enc_a.sign_envelope_all(env_bytes, suite="ed25519")

    payload_bytes = enc_a.encrypt_bytes(b"{}", p.session)
    with pytest.raises(UnsupportedAppAeadSuite):
        await p.svc_b._app_inbound_handler(
            p.a_id, orjson.dumps(envelope), payload_bytes
        )


@pytest.mark.asyncio
async def test_app_inbound_handler_rejects_tampered_payload():
    """_app_inbound_handler drops frames whose payload doesn't match payload_sha256.

    This verifies FIX-I1: a relay that splices payload_2 under header_1 is
    rejected because sha256(tampered_payload) != payload_sha256 in the
    signed+encrypted metadata.  The handler logs a warning and returns without
    calling on_inbound_message (i.e. the frame is silently dropped).
    """
    import orjson as _orjson

    p = _paired_app()

    inbound_calls: list[tuple] = []

    class _FakeAppFed:
        async def on_inbound_message(self, instance_id, app_id, session_id, payload):
            inbound_calls.append((instance_id, app_id, session_id, payload))

    p.svc_b.attach_apps(_FakeAppFed())

    # Send a legitimate frame from A so we get a valid signed header.
    await p.svc_a.send_app_message(
        to_instance_id=p.b_id,
        app_id="chess",
        session_id="sess-tamper",
        payload={"move": "e2e4"},
    )
    assert p.cap.app is not None
    header, _orig_payload_bytes = p.cap.app

    # Tamper: encrypt a DIFFERENT payload using the same session key, but feed
    # it together with the original signed header whose payload_sha256 binds the
    # original plaintext.  The sha256 of the tampered payload won't match.
    from socialhome.federation.encoder import FederationEncoder

    enc_a = FederationEncoder(p.a_kp.private_key)
    tampered_plaintext = b'{"move": "INJECTED"}'
    tampered_payload_bytes = enc_a.encrypt_bytes(tampered_plaintext, p.session)

    # Feed tampered frame to svc_b — must NOT reach on_inbound_message.
    await p.svc_b._app_inbound_handler(
        p.a_id, _orjson.dumps(header), tampered_payload_bytes
    )
    assert inbound_calls == [], "tampered payload must be dropped, not delivered"


@pytest.mark.asyncio
async def test_attach_apps_registers_event_handlers():
    """attach_apps registers APP_SESSION + APP_MESSAGE in the event registry."""
    svc, _ = _make_service()

    dispatched: list = []

    class _FakeAppFed:
        async def on_inbound_event(self, event):
            dispatched.append(event)

    svc.attach_apps(_FakeAppFed())

    # Fire a synthetic APP_MESSAGE via _dispatch_event — it should reach app_fed.
    fake_event = MagicMock()
    fake_event.event_type = FederationEventType.APP_MESSAGE
    await svc._event_registry.dispatch(fake_event)
    assert len(dispatched) == 1

    # Fire APP_SESSION.
    fake_event2 = MagicMock()
    fake_event2.event_type = FederationEventType.APP_SESSION
    await svc._event_registry.dispatch(fake_event2)
    assert len(dispatched) == 2


@pytest.mark.asyncio
async def test_app_inbound_handler_no_op_when_no_app_fed_attached():
    """_app_inbound_handler is a no-op when app_fed is not attached."""
    p = _paired_app()
    # svc_b has no attach_apps called
    # Should complete without error.
    await p.svc_b._app_inbound_handler(p.a_id, b"{}", b"")
    # No dispatch raised


# ─── resign_for_redelivery (outbox redelivery re-stamp/re-sign) ────────────


def _build_stored_envelope(svc, peer_kp, *, timestamp_iso, msg_id="msg-fixed-1"):
    """Build a fully-signed stored envelope the way send_event would,
    but with a caller-chosen timestamp so a test can age it.

    Mirrors the canonical verifier field order so the produced
    signature verifies against ``make_verify_signature``.
    """
    import orjson

    envelope_dict = {
        "msg_id": msg_id,
        "event_type": FederationEventType.SPACE_DISSOLVED.value,
        "from_instance": svc._own_instance_id,
        "to_instance": derive_instance_id(peer_kp.public_key),
        "timestamp": timestamp_iso,
        "encrypted_payload": "nonce123:ciphertextABC",
        "space_id": "space-xyz",
        "proto_version": 1,
        "sig_suite": "ed25519",
    }
    envelope_bytes = orjson.dumps(envelope_dict)
    envelope_dict["signatures"] = svc._encoder.sign_envelope_all(
        envelope_bytes, suite="ed25519"
    )
    return envelope_dict


def test_resign_for_redelivery_refreshes_timestamp_keeps_identity():
    """resign_for_redelivery: fresh timestamp, same msg_id /
    encrypted_payload / sig_suite / space_id, non-empty signatures."""
    svc, own_kp = _make_service()
    peer_kp = generate_identity_keypair()
    stale_iso = (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat()
    stored = _build_stored_envelope(svc, peer_kp, timestamp_iso=stale_iso)
    payload_json = _dumps(stored)

    out = svc.resign_for_redelivery(payload_json)
    parsed = _loads(out)

    # msg_id / encrypted_payload / sig_suite / space_id preserved.
    assert parsed["msg_id"] == stored["msg_id"]
    assert parsed["encrypted_payload"] == stored["encrypted_payload"]
    assert parsed["sig_suite"] == stored["sig_suite"]
    assert parsed["space_id"] == stored["space_id"]
    assert parsed["event_type"] == stored["event_type"]
    assert parsed["from_instance"] == stored["from_instance"]
    assert parsed["to_instance"] == stored["to_instance"]

    # Fresh timestamp: within a couple of seconds of now.
    new_ts = datetime.fromisoformat(parsed["timestamp"])
    skew = abs((datetime.now(timezone.utc) - new_ts).total_seconds())
    assert skew < 5, f"expected fresh timestamp, skew was {skew}s"

    # Non-empty signatures map with the ed25519 entry.
    assert parsed["signatures"]
    assert "ed25519" in parsed["signatures"]
    # And the timestamp actually changed.
    assert parsed["timestamp"] != stored["timestamp"]


def test_resign_for_redelivery_preserves_msg_id_for_replay_dedup():
    """The re-signed envelope keeps the exact original msg_id so the
    receiver's replay cache still dedupes a genuinely-redelivered event."""
    svc, _ = _make_service()
    peer_kp = generate_identity_keypair()
    stale_iso = (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat()
    stored = _build_stored_envelope(
        svc, peer_kp, timestamp_iso=stale_iso, msg_id="unique-msg-id-42"
    )
    out = svc.resign_for_redelivery(_dumps(stored))
    assert _loads(out)["msg_id"] == "unique-msg-id-42"


@pytest.mark.asyncio
async def test_resign_for_redelivery_passes_real_inbound_timestamp_and_sig():
    """END-TO-END guarantee: a stale stored envelope is rejected by the
    real §24.11 timestamp step (proving the bug), and after
    resign_for_redelivery it passes BOTH the real timestamp step AND the
    real signature-verify step in the verifier's canonical field order.

    This is the canonicalization tripwire: if resign_for_redelivery's
    field order drifts from inbound_validator's envelope_for_verify, the
    signature verification below fails.
    """
    import orjson
    from socialhome.federation.inbound_validator import (
        InboundContext,
        make_check_timestamp,
        make_verify_signature,
    )

    svc, own_kp = _make_service()
    peer_kp = generate_identity_keypair()

    # Build a stale (10-minute-old) signed envelope exactly as the outbox
    # would have stored it from the original send_event call.
    stale_iso = (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat()
    stored = _build_stored_envelope(svc, peer_kp, timestamp_iso=stale_iso)
    payload_json = _dumps(stored)

    ts_step = make_check_timestamp()

    # (a) The STALE original is rejected by the real timestamp step.
    stale_ctx = InboundContext()
    stale_ctx.envelope = stored
    with pytest.raises(ValueError, match="Timestamp skew"):
        await ts_step(stale_ctx)

    # Re-stamp + re-sign for redelivery.
    resigned_json = svc.resign_for_redelivery(payload_json)
    resigned = _loads(resigned_json)

    # (b1) The re-signed envelope passes the real timestamp step.
    fresh_ctx = InboundContext()
    fresh_ctx.envelope = resigned
    await ts_step(fresh_ctx)  # must not raise

    # (b2) The re-signed envelope passes the real signature-verify step
    # against the signer's (our) public key, in the verifier's canonical
    # field order. We use a verifier-side encoder + an _InboxInstance-like
    # wrapper carrying the signer's from_instance + public key.
    class _SignerInstance:
        from_instance = svc._own_instance_id
        remote_identity_pk = own_kp.public_key.hex()
        remote_pq_identity_pk = None

    verifier_encoder = FederationEncoder(generate_identity_keypair().private_key)
    sig_step = make_verify_signature(encoder=verifier_encoder)
    sig_ctx = InboundContext()
    sig_ctx.envelope = resigned
    sig_ctx.instance = _SignerInstance()
    await sig_step(sig_ctx)  # must not raise → signature valid in canonical order

    # Sanity: the canonical bytes the verifier reconstructs match what we
    # signed (independent cross-check of field order).
    envelope_for_verify = {
        "msg_id": resigned["msg_id"],
        "event_type": resigned["event_type"],
        "from_instance": resigned["from_instance"],
        "to_instance": resigned["to_instance"],
        "timestamp": resigned["timestamp"],
        "encrypted_payload": resigned["encrypted_payload"],
        "space_id": resigned.get("space_id"),
        "proto_version": resigned.get("proto_version", 1),
        "sig_suite": resigned["sig_suite"],
    }
    assert verifier_encoder.verify_signatures_all(
        orjson.dumps(envelope_for_verify),
        suite=resigned["sig_suite"],
        signatures=resigned["signatures"],
        ed_public_key=own_kp.public_key,
        pq_public_key=None,
    )


async def test_peer_identity_public_key_returns_pinned_bytes():
    """``peer_identity_public_key`` decodes the pinned ``remote_identity_pk``
    of a known peer to raw Ed25519 bytes — the key inbound handlers verify a
    relayed user-identity binding against."""
    own_kp = generate_identity_keypair()
    peer_kp = generate_identity_keypair()
    km = _make_kek_manager()
    repo = InMemoryFederationRepo()
    inst, _ = _make_remote_instance(km, peer_kp=peer_kp)
    repo._instances[inst.id] = inst
    svc = _svc_with(own_kp, repo, km)

    got = await svc.peer_identity_public_key(inst.id)
    assert got == peer_kp.public_key


async def test_peer_identity_public_key_unknown_peer_returns_none():
    """An unknown / unpaired peer yields ``None`` (fail-soft, no raise)."""
    own_kp = generate_identity_keypair()
    km = _make_kek_manager()
    svc = _svc_with(own_kp, InMemoryFederationRepo(), km)
    assert await svc.peer_identity_public_key("nope") is None
    assert await svc.peer_identity_public_key("") is None


async def test_is_confirmed_peer_true_for_confirmed_instance():
    """A CONFIRMED ``remote_instances`` row reads as a confirmed peer."""
    own_kp = generate_identity_keypair()
    km = _make_kek_manager()
    repo = InMemoryFederationRepo()
    inst, _ = _make_remote_instance(km)  # status defaults to CONFIRMED
    repo._instances[inst.id] = inst
    svc = _svc_with(own_kp, repo, km)

    assert await svc.is_confirmed_peer(inst.id) is True


async def test_is_confirmed_peer_false_for_pending_or_unknown():
    """A pending/unpairing peer, an unknown peer, and an empty id all read False."""
    own_kp = generate_identity_keypair()
    km = _make_kek_manager()
    repo = InMemoryFederationRepo()
    pending, _ = _make_remote_instance(km)
    pending = dataclasses.replace(pending, status=PairingStatus.PENDING_SENT)
    repo._instances[pending.id] = pending
    svc = _svc_with(own_kp, repo, km)

    assert await svc.is_confirmed_peer(pending.id) is False
    assert await svc.is_confirmed_peer("nope") is False
    assert await svc.is_confirmed_peer("") is False


# ── #648: a failed routed ship re-probes once ─────────────────────────


@pytest.mark.asyncio
async def test_routed_ship_failure_invalidates_and_retries_once():
    """A failed routed ship drops the cached path and retries on a fresh one.

    ``discover_route`` is cache-first, so a bare retry would re-use the
    same dead chain — the ``invalidate`` is what makes the retry mean
    anything. This is that method's first production caller.
    """
    km = _make_kek_manager()
    fed_repo = InMemoryFederationRepo()
    outbox_repo = InMemoryOutboxRepo()
    inst, _ = _make_remote_instance(km)
    inst = dataclasses.replace(inst, status=PairingStatus.PENDING_SENT)
    await fed_repo.save_instance(inst)

    svc, _ = _make_service(
        federation_repo=fed_repo,
        outbox_repo=outbox_repo,
        key_manager=km,
    )
    dead_path = [svc.own_instance_id, "dead-hop", inst.id]
    live_path = [svc.own_instance_id, "live-hop", inst.id]
    route_service, routed_handler, discover_route, send_routed = _make_mesh_pair(
        route=(dead_path, "dead-eph"),
    )
    # First discovery hands back the dead path, the retry a fresh one.
    discover_route.side_effect = [
        (dead_path, "dead-eph"),
        (live_path, "live-eph"),
    ]
    # First ship raises (relay gone), the retry succeeds.
    send_routed.side_effect = [RuntimeError("relay unreachable"), "route-id"]
    svc.attach_mesh(route_service=route_service, routed_handler=routed_handler)

    result = await svc.send_with_mesh_fallback(
        to_instance_id=inst.id,
        event_type=FederationEventType.SPACE_SYNC_CHUNK,
        payload={"sync_id": "s1", "chunk": "{}"},
    )

    assert result.ok is True
    route_service.invalidate.assert_awaited_once_with(inst.id)
    assert discover_route.await_count == 2
    assert send_routed.await_count == 2
    # The retry went out on the freshly-discovered path, not the dead one.
    assert send_routed.await_args.kwargs["path"] == live_path
    assert send_routed.await_args.kwargs["target_eph_pk_b64"] == "live-eph"


@pytest.mark.asyncio
async def test_routed_ship_failure_gives_up_after_one_retry():
    """The retry is non-recursive — two failures end it.

    A recursive retry would let one persistently-broken target amplify a
    single send into an unbounded probe storm across the mesh.
    """
    km = _make_kek_manager()
    fed_repo = InMemoryFederationRepo()
    outbox_repo = InMemoryOutboxRepo()
    inst, _ = _make_remote_instance(km)
    inst = dataclasses.replace(inst, status=PairingStatus.PENDING_SENT)
    await fed_repo.save_instance(inst)

    svc, _ = _make_service(
        federation_repo=fed_repo,
        outbox_repo=outbox_repo,
        key_manager=km,
    )
    path = [svc.own_instance_id, "hop", inst.id]
    route_service, routed_handler, discover_route, send_routed = _make_mesh_pair(
        route=(path, "eph"),
    )
    send_routed.side_effect = RuntimeError("still broken")
    svc.attach_mesh(route_service=route_service, routed_handler=routed_handler)

    result = await svc.send_with_mesh_fallback(
        to_instance_id=inst.id,
        event_type=FederationEventType.SPACE_SYNC_CHUNK,
        payload={"sync_id": "s1", "chunk": "{}"},
    )

    assert result.ok is False
    assert result.error == "routed_send_failed"
    assert send_routed.await_count == 2, "retried more than once"
    route_service.invalidate.assert_awaited_once_with(inst.id)


@pytest.mark.asyncio
async def test_routed_ship_failure_reports_no_route_when_rediscovery_fails():
    """If the forced re-discovery finds nothing, say so rather than retrying."""
    km = _make_kek_manager()
    fed_repo = InMemoryFederationRepo()
    outbox_repo = InMemoryOutboxRepo()
    inst, _ = _make_remote_instance(km)
    inst = dataclasses.replace(inst, status=PairingStatus.PENDING_SENT)
    await fed_repo.save_instance(inst)

    svc, _ = _make_service(
        federation_repo=fed_repo,
        outbox_repo=outbox_repo,
        key_manager=km,
    )
    path = [svc.own_instance_id, "hop", inst.id]
    route_service, routed_handler, discover_route, send_routed = _make_mesh_pair(
        route=(path, "eph"),
    )
    discover_route.side_effect = [(path, "eph"), None]
    send_routed.side_effect = RuntimeError("relay unreachable")
    svc.attach_mesh(route_service=route_service, routed_handler=routed_handler)

    result = await svc.send_with_mesh_fallback(
        to_instance_id=inst.id,
        event_type=FederationEventType.SPACE_SYNC_CHUNK,
        payload={"sync_id": "s1", "chunk": "{}"},
    )

    assert result.ok is False
    assert result.error == "no_route"
    assert send_routed.await_count == 1


# ─── Mesh: cooldown reason + visible broadcast failures ───────────────


@pytest.mark.asyncio
async def test_send_with_mesh_fallback_reports_the_route_cooldown_distinctly():
    """A send that failed *because route discovery is in its negative
    cooldown* must be distinguishable from one that probed and found
    nothing.

    ``discover_route`` returns ``None`` for both, but only the first is a
    transient race the caller can wait out — the chunk loop uses the
    distinction to retry instead of burning its failure budget.
    """
    km = _make_kek_manager()
    fed_repo = InMemoryFederationRepo()
    svc, _ = _make_service(federation_repo=fed_repo, key_manager=km)
    route_service, routed_handler, discover_route, send_routed = _make_mesh_pair(
        route=None,
    )
    route_service.cooldown_remaining = MagicMock(return_value=12.5)
    svc.attach_mesh(route_service=route_service, routed_handler=routed_handler)

    result = await svc.send_with_mesh_fallback(
        to_instance_id="mesh-only-peer",
        event_type=FederationEventType.SPACE_POST_CREATED,
        payload={"post_id": "p1"},
    )

    assert result.ok is False
    assert result.error == DELIVERY_ERROR_ROUTE_COOLDOWN
    assert result.retry_after_s == 12.5
    # The cooldown branch must not probe — that's the anti-flood property.
    discover_route.assert_not_awaited()
    send_routed.assert_not_awaited()


@pytest.mark.asyncio
async def test_send_with_mesh_fallback_no_route_when_not_in_cooldown():
    """No cooldown → we actually probed, so the generic reason stands."""
    km = _make_kek_manager()
    fed_repo = InMemoryFederationRepo()
    svc, _ = _make_service(federation_repo=fed_repo, key_manager=km)
    route_service, routed_handler, discover_route, _send_routed = _make_mesh_pair(
        route=None,
    )
    route_service.cooldown_remaining = MagicMock(return_value=0.0)
    svc.attach_mesh(route_service=route_service, routed_handler=routed_handler)

    result = await svc.send_with_mesh_fallback(
        to_instance_id="mesh-only-peer",
        event_type=FederationEventType.SPACE_POST_CREATED,
        payload={"post_id": "p1"},
    )

    assert result.ok is False
    assert result.error == "no_route"
    discover_route.assert_awaited_once()


@pytest.mark.asyncio
async def test_broadcast_to_space_members_warns_about_failed_targets(caplog):
    """The mesh fan-out has no outbox, so a failed target is a permanent,
    invisible loss. The minimum bar is a diagnosable WARNING naming the
    space, the event type, the instance and the reason."""
    km = _make_kek_manager()
    fed_repo = InMemoryFederationRepo()
    svc, _ = _make_service(federation_repo=fed_repo, key_manager=km)
    space_id = "space-warn"
    fed_repo.add_space_member(space_id, "mesh-only-peer")
    route_service, routed_handler, _discover, _send_routed = _make_mesh_pair(
        route=None,
    )
    route_service.cooldown_remaining = MagicMock(return_value=0.0)
    svc.attach_mesh(route_service=route_service, routed_handler=routed_handler)

    with caplog.at_level(logging.WARNING, logger="socialhome"):
        result = await svc.broadcast_to_space_members(
            space_id=space_id,
            event_type=FederationEventType.SPACE_MEMBER_ROLE_CHANGED,
            payload={"space_id": space_id, "role": "admin"},
        )

    assert result.failed == 1
    assert "mesh-only-peer" in caplog.text
    assert space_id in caplog.text
    assert "no_route" in caplog.text


@pytest.mark.asyncio
async def test_broadcast_to_space_members_all_ok_logs_no_warning(caplog):
    """No warning spam on the happy path."""
    km = _make_kek_manager()
    fed_repo = InMemoryFederationRepo()
    svc, _ = _make_service(federation_repo=fed_repo, key_manager=km)
    space_id = "space-quiet"
    fed_repo.add_space_member(space_id, "mesh-only-peer")
    route_service, routed_handler, _discover, _send_routed = _make_mesh_pair(
        route=[svc.own_instance_id, "hop", "mesh-only-peer"],
    )
    route_service.discover_route = AsyncMock(
        return_value=([svc.own_instance_id, "hop", "mesh-only-peer"], "eph-pk"),
    )
    route_service.cooldown_remaining = MagicMock(return_value=0.0)
    svc.attach_mesh(route_service=route_service, routed_handler=routed_handler)

    with caplog.at_level(logging.WARNING, logger="socialhome"):
        result = await svc.broadcast_to_space_members(
            space_id=space_id,
            event_type=FederationEventType.SPACE_MEMBER_ROLE_CHANGED,
            payload={"space_id": space_id, "role": "admin"},
        )

    assert result.all_ok is True
    assert caplog.text == ""


def _broadcast_svc_with_per_peer_results(
    results_by_peer: dict[str, DeliveryResult],
) -> tuple[FederationService, str, Any]:
    """Service + a ``patch.object`` that makes ``send_with_mesh_fallback``
    answer per target from ``results_by_peer`` — lets a broadcast test mix
    direct-path (outbox-queued) and mesh-path (terminal) outcomes without a
    live transport. The service is slotted, so the class attribute is
    patched (an ``AsyncMock`` is not a descriptor: no ``self`` is passed)."""
    km = _make_kek_manager()
    fed_repo = InMemoryFederationRepo()
    svc, _ = _make_service(federation_repo=fed_repo, key_manager=km)
    space_id = "space-mixed"
    for iid in results_by_peer:
        fed_repo.add_space_member(space_id, iid)

    async def _fake_send(*, to_instance_id: str, **_kw: Any) -> DeliveryResult:
        return results_by_peer[to_instance_id]

    patcher = patch.object(
        FederationService,
        "send_with_mesh_fallback",
        AsyncMock(side_effect=_fake_send),
    )
    return svc, space_id, patcher


@pytest.mark.asyncio
async def test_broadcast_to_space_members_queued_failure_logs_no_warning(caplog):
    """A direct-peer ``send_event`` failure is enqueued to the durable outbox
    BEFORE it returns ``ok=False, error=DELIVERY_ERROR_QUEUED`` — the outbox
    redelivers, so it is "queued for retry", not "lost". Warning "did not
    reach" for it is false and noisy (the demo audit flagged exactly this
    during the pair-settle window while every content assertion passed)."""
    svc, space_id, patcher = _broadcast_svc_with_per_peer_results(
        {
            "direct-peer": DeliveryResult(
                instance_id="direct-peer", ok=False, error=DELIVERY_ERROR_QUEUED
            ),
        }
    )

    with patcher, caplog.at_level(logging.WARNING, logger="socialhome"):
        result = await svc.broadcast_to_space_members(
            space_id=space_id,
            event_type=FederationEventType.SPACE_POST_CREATED,
            payload={"space_id": space_id, "post_id": "p1"},
        )

    # ``failed`` still counts every ok=False — only the WARNING is gated.
    assert result.failed == 1
    assert result.terminal_failures == ()
    assert "broadcast_to_space_members" not in caplog.text
    assert "did not reach" not in caplog.text


@pytest.mark.asyncio
async def test_broadcast_to_space_members_mesh_failure_still_warns(caplog):
    """The mesh path has no outbox: ``no_route`` is a single-attempt,
    permanent loss and MUST stay diagnosable."""
    svc, space_id, patcher = _broadcast_svc_with_per_peer_results(
        {
            "mesh-only-peer": DeliveryResult(
                instance_id="mesh-only-peer", ok=False, error="no_route"
            ),
        }
    )

    with patcher, caplog.at_level(logging.WARNING, logger="socialhome"):
        result = await svc.broadcast_to_space_members(
            space_id=space_id,
            event_type=FederationEventType.SPACE_POST_CREATED,
            payload={"space_id": space_id, "post_id": "p1"},
        )

    assert result.failed == 1
    assert len(result.terminal_failures) == 1
    assert "broadcast_to_space_members" in caplog.text
    assert "mesh-only-peer=no_route" in caplog.text
    assert space_id in caplog.text


@pytest.mark.asyncio
async def test_broadcast_to_space_members_mixed_names_only_terminal_peer(caplog):
    """One outbox-queued direct peer + one mesh ``no_route`` peer: the
    WARNING names ONLY the mesh peer, and counts 1 (not 2) as unreached."""
    svc, space_id, patcher = _broadcast_svc_with_per_peer_results(
        {
            "direct-peer": DeliveryResult(
                instance_id="direct-peer", ok=False, error=DELIVERY_ERROR_QUEUED
            ),
            "mesh-only-peer": DeliveryResult(
                instance_id="mesh-only-peer", ok=False, error="no_route"
            ),
            "happy-peer": DeliveryResult(instance_id="happy-peer", ok=True),
        }
    )

    with patcher, caplog.at_level(logging.WARNING, logger="socialhome"):
        result = await svc.broadcast_to_space_members(
            space_id=space_id,
            event_type=FederationEventType.SPACE_POST_CREATED,
            payload={"space_id": space_id, "post_id": "p1"},
        )

    assert result.attempted == 3
    assert result.succeeded == 1
    assert result.failed == 2
    assert [r.instance_id for r in result.terminal_failures] == ["mesh-only-peer"]
    assert "mesh-only-peer=no_route" in caplog.text
    assert "direct-peer" not in caplog.text
    assert "did not reach 1/3" in caplog.text


@pytest.mark.asyncio
async def test_broadcast_to_space_members_skips_own_instance(caplog):
    """``create_space`` adds the host's OWN row to ``space_instances``, so
    ``list_member_instance_ids`` returns the sender itself. Sending to self
    has no ``remote_instances`` row → mesh branch → ``discover_route(self)``
    floods peers for a route to the sender → ``no_route`` (and a 30 s
    negative cooldown keyed on self). The demo audit's four "did not reach"
    warnings all named the sender. Self must be skipped at the loop and
    must not count in ``attempted`` / ``failed`` / ``results``."""
    km = _make_kek_manager()
    fed_repo = InMemoryFederationRepo()
    svc, _ = _make_service(federation_repo=fed_repo, key_manager=km)
    space_id = "space-self"
    fed_repo.add_space_member(space_id, svc.own_instance_id)
    fed_repo.add_space_member(space_id, "happy-peer")
    sent_to: list[str] = []

    async def _fake_send(*, to_instance_id: str, **_kw: Any) -> DeliveryResult:
        sent_to.append(to_instance_id)
        return DeliveryResult(instance_id=to_instance_id, ok=True)

    patcher = patch.object(
        FederationService,
        "send_with_mesh_fallback",
        AsyncMock(side_effect=_fake_send),
    )
    route_service, routed_handler, discover_route, _send_routed = _make_mesh_pair()
    svc.attach_mesh(route_service=route_service, routed_handler=routed_handler)

    with patcher, caplog.at_level(logging.WARNING, logger="socialhome"):
        result = await svc.broadcast_to_space_members(
            space_id=space_id,
            event_type=FederationEventType.SPACE_POST_CREATED,
            payload={"space_id": space_id, "post_id": "p1"},
        )

    assert sent_to == ["happy-peer"]
    assert result.attempted == 1  # len(instances) - 1: self is not a target
    assert result.succeeded == 1
    assert result.failed == 0
    assert [r.instance_id for r in result.results] == ["happy-peer"]
    assert result.all_ok is True
    discover_route.assert_not_awaited()
    assert caplog.text == ""


@pytest.mark.asyncio
async def test_broadcast_reaches_a_member_with_no_remote_instances_row():
    """A mesh-only member — seated via a routed invite, so it has a
    ``space_instances`` row but NO ``remote_instances`` row at all — must
    still receive space-content broadcasts over SPACE_ROUTED.

    ``list_member_instance_ids`` deliberately omits the CONFIRMED join for
    exactly this case; the regression this guards is the fan-out silently
    skipping such a member (a role change that never lands, with a 200 at
    the API).
    """
    km = _make_kek_manager()
    fed_repo = InMemoryFederationRepo()
    svc, _ = _make_service(federation_repo=fed_repo, key_manager=km)
    space_id = "space-mesh-only"
    mesh_only = "m" * 52
    fed_repo.add_space_member(space_id, mesh_only)
    # The defining property of the fixture: no pairing row whatsoever.
    assert await fed_repo.get_instance(mesh_only) is None

    path = [svc.own_instance_id, "relay-hop", mesh_only]
    route_service, routed_handler, discover_route, send_routed = _make_mesh_pair(
        route=(path, "target-eph-pk"),
    )
    route_service.cooldown_remaining = MagicMock(return_value=0.0)
    svc.attach_mesh(route_service=route_service, routed_handler=routed_handler)

    payload = {
        "space_id": space_id,
        "user_id": "u-1",
        "instance_id": mesh_only,
        "role": "admin",
    }
    result = await svc.broadcast_to_space_members(
        space_id=space_id,
        event_type=FederationEventType.SPACE_MEMBER_ROLE_CHANGED,
        payload=payload,
    )

    assert result.attempted == 1
    assert result.succeeded == 1
    discover_route.assert_awaited_once_with(mesh_only)
    send_routed.assert_awaited_once()
    kwargs = send_routed.await_args.kwargs
    assert kwargs["path"] == path
    assert kwargs["target_eph_pk_b64"] == "target-eph-pk"
    assert kwargs["inner_event_type"] is FederationEventType.SPACE_MEMBER_ROLE_CHANGED
    assert kwargs["inner_payload"] == payload
