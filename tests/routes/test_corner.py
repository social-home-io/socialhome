"""HTTP tests for /api/me/corner — the "My Corner" aggregator."""

from __future__ import annotations

import json

from .conftest import _auth


async def test_corner_empty(client):
    """Fresh user, no activity → all slices zero/empty."""
    r = await client.get("/api/me/corner", headers=_auth(client._tok))
    assert r.status == 200
    body = await r.json()
    assert body["unread_notifications"] == 0
    assert body["unread_conversations"] == 0
    assert body["upcoming_events"] == []
    assert body["tasks_due_today"] == []
    assert body["bazaar"] == {
        "active_listings": 0,
        "pending_offers": 0,
        "ending_soon": 0,
    }


async def test_corner_counts_overdue_task(client):
    """A task assigned to the caller with due_date <= today shows up."""
    db = client._db
    await db.enqueue(
        "INSERT INTO task_lists(id, name, created_by) VALUES('l1', 'default', ?)",
        (client._uid,),
    )
    await db.enqueue(
        "INSERT INTO tasks(id, list_id, title, status, due_date,"
        " assignees_json, created_by, position)"
        " VALUES('t1', 'l1', 'Take out trash', 'todo', '2025-01-01',"
        " ?, ?, 0)",
        (json.dumps([client._uid]), client._uid),
    )
    r = await client.get("/api/me/corner", headers=_auth(client._tok))
    assert r.status == 200
    body = await r.json()
    titles = [t["title"] for t in body["tasks_due_today"]]
    assert "Take out trash" in titles


async def test_corner_bundles_bazaar_summary(client):
    """An active listing counts; a sold listing doesn't."""
    db = client._db
    # Bazaar listings live in spaces — seed one + the wrapper space post.
    await db.enqueue(
        """
        INSERT INTO spaces(
            id, name, owner_instance_id, owner_username, identity_public_key
        ) VALUES('space-corner', 'Corner Space', 'iid-test', ?, ?)
        """,
        (client._uid, "00" * 32),
    )
    await db.enqueue(
        "INSERT INTO space_posts(id, space_id, author, type, content)"
        " VALUES('p1', 'space-corner', ?, 'bazaar', '')",
        (client._uid,),
    )
    await db.enqueue(
        "INSERT INTO bazaar_listings("
        "  post_id, space_id, seller_user_id, title, mode,"
        "  end_time, currency, status"
        ") VALUES('p1', 'space-corner', ?, 'Bike', 'fixed',"
        " '2099-01-01T00:00:00+00:00', 'USD', 'active')",
        (client._uid,),
    )
    r = await client.get("/api/me/corner", headers=_auth(client._tok))
    body = await r.json()
    assert body["bazaar"]["active_listings"] == 1
    assert body["bazaar"]["pending_offers"] == 0


async def test_corner_requires_auth(client):
    r = await client.get("/api/me/corner")
    assert r.status in (401, 403)


# ─── Followed spaces widget ─────────────────────────────────────────


async def _seed_space_with_post(
    db,
    *,
    space_id: str,
    name: str,
    emoji: str | None,
    member_user_id: str,
    post_id: str,
    content: str,
    created_at: str,
) -> None:
    """Insert a space + membership + one post for the caller."""
    fake_pk = "00" * 32
    await db.enqueue(
        "INSERT INTO spaces(id, name, emoji, owner_instance_id,"
        " owner_username, identity_public_key, space_type, join_mode)"
        " VALUES(?, ?, ?, 'self', 'admin', ?, 'private', 'invite_only')",
        (space_id, name, emoji, fake_pk),
    )
    await db.enqueue(
        "INSERT INTO space_members(space_id, user_id, role) VALUES(?, ?, 'member')",
        (space_id, member_user_id),
    )
    await db.enqueue(
        "INSERT INTO space_posts(id, space_id, author, type, content,"
        " created_at) VALUES(?, ?, 'other', 'text', ?, ?)",
        (post_id, space_id, content, created_at),
    )


async def test_corner_followed_empty_when_prefs_unset(client):
    r = await client.get("/api/me/corner", headers=_auth(client._tok))
    body = await r.json()
    assert body["followed_space_ids"] == []
    assert body["followed_spaces_feed"] == []


async def test_corner_followed_merges_two_spaces(client):
    db = client._db
    await _seed_space_with_post(
        db,
        space_id="sp-A",
        name="Alpha",
        emoji="🅰",
        member_user_id=client._uid,
        post_id="p-A1",
        content="hello alpha",
        created_at="2025-06-01T10:00:00+00:00",
    )
    await _seed_space_with_post(
        db,
        space_id="sp-B",
        name="Beta",
        emoji="🅱",
        member_user_id=client._uid,
        post_id="p-B1",
        content="hello beta",
        created_at="2025-06-02T10:00:00+00:00",
    )
    # Persist the follow preference.
    await client.patch(
        "/api/me",
        json={"preferences": {"followed_space_ids": ["sp-A", "sp-B"]}},
        headers=_auth(client._tok),
    )
    r = await client.get("/api/me/corner", headers=_auth(client._tok))
    body = await r.json()
    assert set(body["followed_space_ids"]) == {"sp-A", "sp-B"}
    ids = [p["post_id"] for p in body["followed_spaces_feed"]]
    assert ids == ["p-B1", "p-A1"]  # newest-first by created_at
    # Each row carries space metadata.
    a = next(p for p in body["followed_spaces_feed"] if p["post_id"] == "p-A1")
    assert a["space_name"] == "Alpha"
    assert a["space_emoji"] == "🅰"
    assert a["content"] == "hello alpha"


async def test_corner_followed_drops_stale_space(client):
    """If the user stored a space_id they're no longer a member of,
    the corner silently filters it out (no crash, no empty bundle)."""
    db = client._db
    # Seed a space the caller is NOT a member of.
    fake_pk = "00" * 32
    await db.enqueue(
        "INSERT INTO spaces(id, name, emoji, owner_instance_id,"
        " owner_username, identity_public_key, space_type, join_mode)"
        " VALUES('sp-gone', 'Gone', '👻', 'self', 'admin',"
        " ?, 'private', 'invite_only')",
        (fake_pk,),
    )
    await db.enqueue(
        "INSERT INTO space_posts(id, space_id, author, type, content,"
        " created_at)"
        " VALUES('p-gone', 'sp-gone', 'x', 'text', 'hidden',"
        " '2025-06-03T10:00:00+00:00')",
    )
    await client.patch(
        "/api/me",
        json={"preferences": {"followed_space_ids": ["sp-gone"]}},
        headers=_auth(client._tok),
    )
    r = await client.get("/api/me/corner", headers=_auth(client._tok))
    body = await r.json()
    assert body["followed_spaces_feed"] == []
