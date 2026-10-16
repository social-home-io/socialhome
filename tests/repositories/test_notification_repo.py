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
    from socialhome.crypto import derive_instance_id, generate_identity_keypair
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


async def test_save_or_bump_unread_collapses_same_link(env):
    """Two ``save_or_bump_unread`` calls with the same
    ``(user_id, type, link_url)`` produce exactly one row, with the
    second call's ``created_at`` + ``title`` overwriting the first."""
    await env.db.enqueue(
        "INSERT INTO users(username, user_id, display_name) VALUES(?,?,?)",
        ("notif_user_b", "uid-notif-b", "NotifUserB"),
    )
    uid = "uid-notif-b"
    first = new_notification(
        user_id=uid,
        type="dm_message",
        title="Alice messaged you",
        link_url="/dms/c-1",
    )
    second = new_notification(
        user_id=uid,
        type="dm_message",
        title="Alice messaged you",
        link_url="/dms/c-1",
    )
    saved_first = await env.notif_repo.save_or_bump_unread(first)
    saved_second = await env.notif_repo.save_or_bump_unread(second)
    # Same id back — the second call returned the bumped first row.
    assert saved_second.id == saved_first.id
    # Only one row in the user's list.
    rows = await env.notif_repo.list(uid, limit=50)
    assert len([r for r in rows if r.link_url == "/dms/c-1"]) == 1


async def test_save_or_bump_unread_does_not_span_read(env):
    """A read row with the same key does not absorb a new event —
    the next ``save_or_bump_unread`` creates a fresh unread row."""
    await env.db.enqueue(
        "INSERT INTO users(username, user_id, display_name) VALUES(?,?,?)",
        ("notif_user_c", "uid-notif-c", "NotifUserC"),
    )
    uid = "uid-notif-c"
    first = new_notification(
        user_id=uid,
        type="dm_message",
        title="A",
        link_url="/dms/c-1",
    )
    saved = await env.notif_repo.save_or_bump_unread(first)
    await env.notif_repo.mark_read(saved.id, uid)
    second = new_notification(
        user_id=uid,
        type="dm_message",
        title="B",
        link_url="/dms/c-1",
    )
    saved_second = await env.notif_repo.save_or_bump_unread(second)
    # Different id this time — the read row didn't absorb.
    assert saved_second.id != saved.id
    rows = await env.notif_repo.list(uid, limit=50)
    assert len([r for r in rows if r.link_url == "/dms/c-1"]) == 2
    unread = [r for r in rows if r.read_at is None]
    assert len(unread) == 1


async def test_save_or_bump_unread_falls_back_to_save_when_no_link(env):
    """Without a ``link_url`` the repo can't dedupe — it falls back to
    the plain append-only insert path so non-DM call sites keep
    their existing shape."""
    await env.db.enqueue(
        "INSERT INTO users(username, user_id, display_name) VALUES(?,?,?)",
        ("notif_user_d", "uid-notif-d", "NotifUserD"),
    )
    uid = "uid-notif-d"
    for i in range(3):
        await env.notif_repo.save_or_bump_unread(
            new_notification(user_id=uid, type="t", title=f"N{i}"),
        )
    rows = await env.notif_repo.list(uid, limit=50)
    assert len(rows) == 3


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


# ── Per-space notification preferences ─────────────────────────────────────


async def test_get_space_notif_level_default_is_all(env):
    await env.db.enqueue(
        "INSERT INTO users(username, user_id, display_name) VALUES(?,?,?)",
        ("u1", "uid-u1", "U1"),
    )
    await env.db.enqueue(
        "INSERT INTO spaces(id, name, owner_instance_id, owner_username,"
        " identity_public_key) VALUES(?,?,?,?,?)",
        ("sp-n", "N", "iid", "u1", "aabb" * 16),
    )
    level = await env.notif_repo.get_space_notif_level(
        user_id="uid-u1", space_id="sp-n"
    )
    assert level == "all"


async def test_set_and_get_space_notif_level(env):
    await env.db.enqueue(
        "INSERT INTO users(username, user_id, display_name) VALUES(?,?,?)",
        ("u2", "uid-u2", "U2"),
    )
    await env.db.enqueue(
        "INSERT INTO spaces(id, name, owner_instance_id, owner_username,"
        " identity_public_key) VALUES(?,?,?,?,?)",
        ("sp-n2", "N2", "iid", "u2", "aabb" * 16),
    )
    await env.notif_repo.set_space_notif_level(
        user_id="uid-u2", space_id="sp-n2", level="muted"
    )
    assert (
        await env.notif_repo.get_space_notif_level(user_id="uid-u2", space_id="sp-n2")
        == "muted"
    )
    # Upsert flips to mentions.
    await env.notif_repo.set_space_notif_level(
        user_id="uid-u2", space_id="sp-n2", level="mentions"
    )
    assert (
        await env.notif_repo.get_space_notif_level(user_id="uid-u2", space_id="sp-n2")
        == "mentions"
    )
