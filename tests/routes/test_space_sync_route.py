"""Tests for POST /api/spaces/{id}/sync (admin-only space-sync trigger)."""

from __future__ import annotations

import pytest
from aiohttp.test_utils import TestClient, TestServer

from socialhome.app import create_app
from socialhome.app_keys import (
    db_key as _db_key,
    federation_repo_key,
)
from socialhome.auth import sha256_token_hash
from socialhome.config import Config
from socialhome.crypto import derive_user_id


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
async def client(tmp_dir):
    cfg = Config(
        data_dir=str(tmp_dir),
        db_path=str(tmp_dir / "test.db"),
        media_path=str(tmp_dir / "media"),
        mode="standalone",
        log_level="WARNING",
        db_write_batch_timeout_ms=10,
    )
    app = create_app(cfg)
    async with TestClient(TestServer(app)) as tc:
        db = app[_db_key]
        row = await db.fetchone(
            "SELECT identity_public_key FROM instance_identity WHERE id='self'",
        )
        pk = bytes.fromhex(row["identity_public_key"])

        class _KP:
            public_key = pk

        kp = _KP()
        admin_uid = derive_user_id(kp.public_key, "pascal")
        await db.enqueue(
            "INSERT INTO users(username, user_id, display_name, is_admin) VALUES(?,?,?,1)",
            ("pascal", admin_uid, "Pascal"),
        )
        await db.enqueue(
            "INSERT INTO api_tokens(token_id, user_id, label, token_hash) VALUES(?,?,?,?)",
            ("tid-admin", admin_uid, "test", sha256_token_hash("admin-token")),
        )
        bob_uid = derive_user_id(kp.public_key, "bob")
        await db.enqueue(
            "INSERT INTO users(username, user_id, display_name, is_admin) VALUES(?,?,?,0)",
            ("bob", bob_uid, "Bob"),
        )
        await db.enqueue(
            "INSERT INTO api_tokens(token_id, user_id, label, token_hash) VALUES(?,?,?,?)",
            ("tid-bob", bob_uid, "test", sha256_token_hash("bob-token")),
        )
        tc._admin_token = "admin-token"
        tc._admin_uid = admin_uid
        tc._bob_token = "bob-token"
        tc._bob_uid = bob_uid
        yield tc


async def _create_space(client, name="Family"):
    r = await client.post(
        "/api/spaces",
        json={"name": name},
        headers=_auth(client._admin_token),
    )
    assert r.status == 201
    return (await r.json())["id"]


async def test_sync_trigger_enqueues_for_confirmed_peers(client):
    space_id = await _create_space(client)

    # Simulate peer membership + confirmed pairing.
    app = client.server.app
    fed_repo = app[federation_repo_key]

    from socialhome.domain.federation import (
        InstanceSource,
        PairingStatus,
        RemoteInstance,
    )

    await fed_repo.save_instance(
        RemoteInstance(
            id="peer-a",
            display_name="Peer A",
            remote_identity_pk="aa" * 32,
            key_self_to_remote="enc",
            key_remote_to_self="enc",
            remote_webhook_url="https://peer/wh",
            local_webhook_id="wh-peer-a",
            status=PairingStatus.CONFIRMED,
            source=InstanceSource.MANUAL,
        )
    )
    # Mark instance as a member of this space.
    await app[_db_key].enqueue(
        "INSERT INTO space_instances(space_id, instance_id) VALUES(?,?)",
        (space_id, "peer-a"),
    )

    resp = await client.post(
        f"/api/spaces/{space_id}/sync",
        headers=_auth(client._admin_token),
    )
    assert resp.status == 202
    body = await resp.json()
    assert "peer-a" in body["targets"]


async def test_sync_trigger_skips_unconfirmed_peers(client):
    space_id = await _create_space(client)
    app = client.server.app
    fed_repo = app[federation_repo_key]

    from socialhome.domain.federation import (
        InstanceSource,
        PairingStatus,
        RemoteInstance,
    )

    await fed_repo.save_instance(
        RemoteInstance(
            id="peer-new",
            display_name="Peer New",
            remote_identity_pk="bb" * 32,
            key_self_to_remote="enc",
            key_remote_to_self="enc",
            remote_webhook_url="https://peer/wh",
            local_webhook_id="wh-peer-new",
            status=PairingStatus.PENDING_SENT,
            source=InstanceSource.MANUAL,
        )
    )
    await app[_db_key].enqueue(
        "INSERT INTO space_instances(space_id, instance_id) VALUES(?,?)",
        (space_id, "peer-new"),
    )

    resp = await client.post(
        f"/api/spaces/{space_id}/sync",
        headers=_auth(client._admin_token),
    )
    assert resp.status == 202
    body = await resp.json()
    assert body["targets"] == []


async def test_sync_trigger_forbidden_for_non_admin(client):
    space_id = await _create_space(client)
    resp = await client.post(
        f"/api/spaces/{space_id}/sync",
        headers=_auth(client._bob_token),
    )
    assert resp.status in (401, 403)


async def test_sync_trigger_unknown_space_returns_404(client):
    resp = await client.post(
        "/api/spaces/does-not-exist/sync",
        headers=_auth(client._admin_token),
    )
    assert resp.status == 404
