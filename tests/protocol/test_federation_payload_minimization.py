"""§27.9: federation envelopes carry only routing fields in plaintext.

Verifies that ``FederationService.send_event`` packs every domain
field (content, sender, recipients, etc.) into ``encrypted_payload``
and never leaks values to the routing layer.
"""

from __future__ import annotations

import json

import pytest

from socialhome.crypto import (
    derive_instance_id,
    generate_identity_keypair,
)
from socialhome.db.database import AsyncDatabase
from socialhome.domain.federation import (
    FederationEventType,
    InstanceSource,
    PairingStatus,
    RemoteInstance,
)
from socialhome.federation.federation_service import FederationService
from socialhome.infrastructure import EventBus, KeyManager
from socialhome.repositories import (
    SqliteFederationRepo,
    SqliteOutboxRepo,
)


pytestmark = pytest.mark.security


class _Resp:
    def __init__(self):
        self.status = 200

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


class _Capture:
    def __init__(self):
        self.bodies: list[dict] = []

    def post(self, url, *, json=None, **kw):
        self.bodies.append(json)
        return _Resp()


@pytest.fixture
async def env(tmp_dir):
    db = AsyncDatabase(tmp_dir / "t.db", batch_timeout_ms=10)
    await db.startup()
    own_kp = generate_identity_keypair()
    own_iid = derive_instance_id(own_kp.public_key)
    await db.enqueue(
        "INSERT INTO instance_identity(instance_id, identity_private_key,"
        " identity_public_key, routing_secret) VALUES(?,?,?,?)",
        (own_iid, own_kp.private_key.hex(), own_kp.public_key.hex(), "aa" * 32),
    )
    fed_repo = SqliteFederationRepo(db)
    outbox = SqliteOutboxRepo(db)
    bus = EventBus()
    kek = KeyManager.from_data_dir(tmp_dir)
    svc = FederationService(
        db=db,
        federation_repo=fed_repo,
        outbox_repo=outbox,
        key_manager=kek,
        bus=bus,
        own_instance_id=own_iid,
        own_identity_seed=own_kp.private_key,
        own_identity_pk=own_kp.public_key,
    )
    peer_kp = generate_identity_keypair()
    wrapped = kek.encrypt(b"\x05" * 32)
    peer = RemoteInstance(
        id=derive_instance_id(peer_kp.public_key),
        display_name="peer",
        remote_identity_pk=peer_kp.public_key.hex(),
        key_self_to_remote=wrapped,
        key_remote_to_self=wrapped,
        remote_webhook_url="https://x/wh",
        local_webhook_id="wh",
        status=PairingStatus.CONFIRMED,
        source=InstanceSource.MANUAL,
    )
    await fed_repo.save_instance(peer)
    capture = _Capture()
    svc._http_client = capture
    yield svc, peer, capture
    await db.shutdown()


_ALLOWED_PLAINTEXT_FIELDS: frozenset[str] = frozenset(
    {
        "msg_id",
        "event_type",
        "from_instance",
        "to_instance",
        "timestamp",
        "encrypted_payload",
        "sig_suite",
        "signatures",
        "space_id",
        "epoch",
        "proto_version",
    }
)


async def test_envelope_has_no_extra_plaintext_fields(env):
    svc, peer, capture = env
    await svc.send_event(
        to_instance_id=peer.id,
        event_type=FederationEventType.SPACE_POST_CREATED,
        payload={"id": "p1", "author": "alice", "content": "secret"},
        space_id="sp-1",
    )
    envelope = capture.bodies[0]
    extras = set(envelope.keys()) - _ALLOWED_PLAINTEXT_FIELDS
    assert extras == set(), (
        f"Envelope leaks unexpected plaintext fields: {sorted(extras)}"
    )


async def test_payload_content_is_not_in_envelope(env):
    svc, peer, capture = env
    await svc.send_event(
        to_instance_id=peer.id,
        event_type=FederationEventType.SPACE_POST_CREATED,
        payload={"content": "very-distinctive-secret-xyz"},
        space_id="sp-1",
    )
    raw = json.dumps(capture.bodies[0])
    assert "very-distinctive-secret-xyz" not in raw


async def test_payload_author_is_not_in_envelope(env):
    svc, peer, capture = env
    await svc.send_event(
        to_instance_id=peer.id,
        event_type=FederationEventType.SPACE_COMMENT_CREATED,
        payload={"author": "alice-very-unique-id"},
        space_id="sp-1",
    )
    raw = json.dumps(capture.bodies[0])
    assert "alice-very-unique-id" not in raw


async def test_envelope_signature_is_present(env):
    """The signatures map is not optional — every envelope carries one
    entry per algorithm in ``sig_suite``."""
    svc, peer, capture = env
    await svc.send_event(
        to_instance_id=peer.id,
        event_type=FederationEventType.PRESENCE_UPDATED,
        payload={},
    )
    envelope = capture.bodies[0]
    assert envelope.get("sig_suite") == "ed25519"
    sigs = envelope.get("signatures") or {}
    assert set(sigs) == {"ed25519"}
    # Plain b64 → string of ≥44 chars typically.
    assert len(sigs["ed25519"]) > 40
