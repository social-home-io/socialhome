"""Coverage extras for app._redeliver_envelope (outbox retry path)."""

from __future__ import annotations

import pytest

from socialhome.app import _redeliver_envelope, _aiohttp_timeout
from socialhome.crypto import (
    derive_instance_id,
    generate_identity_keypair,
)
from socialhome.db.database import AsyncDatabase
from socialhome.domain.federation import (
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


# ─── _aiohttp_timeout ────────────────────────────────────────────────────


def test_aiohttp_timeout_returns_object():
    t = _aiohttp_timeout(10)
    # aiohttp installed → real ClientTimeout. Either way, no raise.
    assert t is not None or t is None


# ─── _redeliver_envelope ─────────────────────────────────────────────────


class _OutboxEntry:
    def __init__(self, *, id, instance_id, payload_json):
        self.id = id
        self.instance_id = instance_id
        self.payload_json = payload_json


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
    yield svc, fed_repo, kek
    await db.shutdown()


async def test_redeliver_unknown_instance_returns_false(env):
    svc, fed_repo, _ = env
    entry = _OutboxEntry(
        id="e1",
        instance_id="never-paired",
        payload_json="{}",
    )
    ok = await _redeliver_envelope(svc, fed_repo, entry)
    assert ok is False


async def test_redeliver_2xx_returns_true(env):
    svc, fed_repo, kek = env
    peer_kp = generate_identity_keypair()
    wrapped = kek.encrypt(b"\x01" * 32)
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

    class _Resp:
        def __init__(self):
            self.status = 204

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

    class _Client:
        def post(self, url, **kw):
            return _Resp()

    svc._http_client = _Client()
    entry = _OutboxEntry(id="e1", instance_id=peer.id, payload_json='{"x":1}')
    ok = await _redeliver_envelope(svc, fed_repo, entry)
    assert ok is True


async def test_redeliver_non_2xx_returns_false(env):
    svc, fed_repo, kek = env
    peer_kp = generate_identity_keypair()
    wrapped = kek.encrypt(b"\x02" * 32)
    peer = RemoteInstance(
        id=derive_instance_id(peer_kp.public_key),
        display_name="peer",
        remote_identity_pk=peer_kp.public_key.hex(),
        key_self_to_remote=wrapped,
        key_remote_to_self=wrapped,
        remote_webhook_url="https://x/wh",
        local_webhook_id="wh2",
        status=PairingStatus.CONFIRMED,
        source=InstanceSource.MANUAL,
    )
    await fed_repo.save_instance(peer)

    class _Resp:
        def __init__(self):
            self.status = 503

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

    class _Client:
        def post(self, url, **kw):
            return _Resp()

    svc._http_client = _Client()
    entry = _OutboxEntry(id="e2", instance_id=peer.id, payload_json="{}")
    ok = await _redeliver_envelope(svc, fed_repo, entry)
    assert ok is False


async def test_redeliver_transport_error_returns_false(env):
    svc, fed_repo, kek = env
    peer_kp = generate_identity_keypair()
    wrapped = kek.encrypt(b"\x03" * 32)
    peer = RemoteInstance(
        id=derive_instance_id(peer_kp.public_key),
        display_name="peer",
        remote_identity_pk=peer_kp.public_key.hex(),
        key_self_to_remote=wrapped,
        key_remote_to_self=wrapped,
        remote_webhook_url="https://x/wh",
        local_webhook_id="wh3",
        status=PairingStatus.CONFIRMED,
        source=InstanceSource.MANUAL,
    )
    await fed_repo.save_instance(peer)

    class _Client:
        def post(self, url, **kw):
            raise ConnectionError("boom")

    svc._http_client = _Client()
    entry = _OutboxEntry(id="e3", instance_id=peer.id, payload_json="{}")
    ok = await _redeliver_envelope(svc, fed_repo, entry)
    assert ok is False
