"""Tests for SqlitePushSubscriptionRepo."""

from __future__ import annotations

import pytest

from socialhome.crypto import (
    derive_instance_id,
    derive_user_id,
    generate_identity_keypair,
)
from socialhome.db.database import AsyncDatabase
from socialhome.repositories.push_subscription_repo import (
    PushSubscription,
    SqlitePushSubscriptionRepo,
)


@pytest.fixture
async def env(tmp_dir):
    kp = generate_identity_keypair()
    iid = derive_instance_id(kp.public_key)
    db = AsyncDatabase(tmp_dir / "t.db", batch_timeout_ms=10)
    await db.startup()
    await db.enqueue(
        "INSERT INTO instance_identity(instance_id, identity_private_key,"
        " identity_public_key, routing_secret) VALUES(?,?,?,?)",
        (iid, kp.private_key.hex(), kp.public_key.hex(), "aa" * 32),
    )
    uid = derive_user_id(kp.public_key, "alice")
    await db.enqueue(
        "INSERT INTO users(username, user_id, display_name, is_admin) VALUES(?,?,?,0)",
        ("alice", uid, "Alice"),
    )
    repo = SqlitePushSubscriptionRepo(db)
    yield db, repo, uid
    await db.shutdown()


def _sub(uid: str, sid: str = "s1", endpoint: str | None = None) -> PushSubscription:
    return PushSubscription(
        id=sid,
        user_id=uid,
        endpoint=endpoint or f"https://push.example.com/{sid}",
        p256dh="dh-key",
        auth_secret="auth-key",
        device_label="iPhone",
    )


async def test_save_and_get(env):
    _, repo, uid = env
    await repo.save(_sub(uid))
    got = await repo.get("s1")
    assert got is not None
    assert got.endpoint == "https://push.example.com/s1"
    assert got.user_id == uid
    assert got.device_label == "iPhone"


async def test_save_upserts_existing_id(env):
    _, repo, uid = env
    await repo.save(_sub(uid))
    # Same id, different endpoint — should upsert.
    await repo.save(
        PushSubscription(
            id="s1",
            user_id=uid,
            endpoint="https://other.example.com/s1",
            p256dh="dh2",
            auth_secret="auth2",
            device_label="Updated",
        )
    )
    got = await repo.get("s1")
    assert got.endpoint == "https://other.example.com/s1"
    assert got.device_label == "Updated"


async def test_list_for_user_returns_all(env):
    _, repo, uid = env
    await repo.save(_sub(uid, "s1"))
    await repo.save(_sub(uid, "s2"))
    subs = await repo.list_for_user(uid)
    assert {s.id for s in subs} == {"s1", "s2"}


async def test_list_for_user_unknown_returns_empty(env):
    _, repo, _ = env
    assert await repo.list_for_user("no-such-user") == []


async def test_delete_owned_by_user(env):
    _, repo, uid = env
    await repo.save(_sub(uid))
    ok = await repo.delete("s1", user_id=uid)
    assert ok is True
    assert await repo.get("s1") is None


async def test_delete_rejects_wrong_user(env):
    _, repo, uid = env
    await repo.save(_sub(uid))
    ok = await repo.delete("s1", user_id="hostile")
    assert ok is False
    assert await repo.get("s1") is not None


async def test_delete_by_endpoint_removes_all_matching(env):
    _, repo, uid = env
    await repo.save(_sub(uid, "s1", "https://a/x"))
    await repo.save(_sub(uid, "s2", "https://a/x"))  # same endpoint
    await repo.save(_sub(uid, "s3", "https://b/x"))
    n = await repo.delete_by_endpoint("https://a/x")
    assert n == 2
    remaining = await repo.list_for_user(uid)
    assert {s.id for s in remaining} == {"s3"}
