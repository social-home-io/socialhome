"""Tests for socialhome.repositories.notification_repo."""

from __future__ import annotations

import pytest

from socialhome.repositories.notification_repo import (
    SqliteNotificationRepo,
    new_notification,
)


@pytest.fixture
async def env(tmp_dir):
    """Minimal env with a notification repo over a real SQLite database."""
    from socialhome.crypto import generate_identity_keypair, derive_instance_id
    from socialhome.db.database import AsyncDatabase

    kp = generate_identity_keypair()
    iid = derive_instance_id(kp.public_key)
    db = AsyncDatabase(tmp_dir / "test.db", batch_timeout_ms=10)
    await db.startup()
    await db.enqueue(
        "INSERT INTO instance_identity(instance_id, identity_private_key,"
        " identity_public_key, routing_secret) VALUES(?,?,?,?)",
        (iid, kp.private_key.hex(), kp.public_key.hex(), "aa" * 32),
    )

    class Env:
        pass

    e = Env()
    e.db = db
    e.iid = iid
    e.notif_repo = SqliteNotificationRepo(db, max_per_user=10)
    yield e
    await db.shutdown()


async def test_notification_cap(env):
    """Notifications are capped at max_per_user (10 in this env)."""
    await env.db.enqueue(
        "INSERT INTO users(username, user_id, display_name) VALUES(?,?,?)",
        ("notif_user", "uid-notif-user", "NotifUser"),
    )
    uid = "uid-notif-user"

    for i in range(15):
        await env.notif_repo.save(
            new_notification(
                user_id=uid,
                type="test",
                title=f"Notification {i}",
            )
        )

    notes = await env.notif_repo.list(uid, limit=50)
    assert len(notes) == 10


async def test_notification_mark_read(env):
    """mark_read flags one notification read without affecting others."""
    await env.db.enqueue(
        "INSERT INTO users(username, user_id, display_name) VALUES(?,?,?)",
        ("notif_user2", "uid-notif2", "NotifUser2"),
    )
    uid = "uid-notif2"
    n1 = new_notification(user_id=uid, type="x", title="N1")
    n2 = new_notification(user_id=uid, type="x", title="N2")
    await env.notif_repo.save(n1)
    await env.notif_repo.save(n2)
    await env.notif_repo.mark_read(n1.id, uid)
    assert (await env.notif_repo.get(n1.id)).is_read
    assert not (await env.notif_repo.get(n2.id)).is_read
    assert await env.notif_repo.count_unread(uid) == 1


async def test_notification_delete_old(env):
    """delete_old removes notifications past the threshold."""
    await env.db.enqueue(
        "INSERT INTO users(username, user_id, display_name) VALUES(?,?,?)",
        ("old_user", "uid-old", "OldUser"),
    )
    uid = "uid-old"
    n = new_notification(user_id=uid, type="x", title="Old")
    await env.notif_repo.save(n)
    await env.db.enqueue(
        "UPDATE notifications SET created_at='2000-01-01T00:00:00Z' WHERE id=?",
        (n.id,),
    )
    purged = await env.notif_repo.delete_old(older_than_days=30)
    assert purged >= 1
