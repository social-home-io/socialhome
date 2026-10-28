"""Tests for notification routes — /api/notifications/* endpoints."""

from __future__ import annotations

import pytest
from aiohttp.test_utils import TestClient, TestServer

from socialhome.app import create_app
from socialhome.app_keys import db_key as _db_key
from socialhome.auth import sha256_token_hash
from socialhome.config import Config
from socialhome.crypto import derive_user_id


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
async def client(tmp_dir):
    """App client with two users; notifications are triggered by creating posts."""
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
        _row = await db.fetchone(
            "SELECT identity_public_key FROM instance_identity WHERE id='self'"
        )
        _pk = bytes.fromhex(_row["identity_public_key"])

        class _KP:
            public_key = _pk

        kp = _KP()
        uid = derive_user_id(kp.public_key, "pascal")
        await db.enqueue(
            "INSERT INTO users(username, user_id, display_name, is_admin) VALUES(?,?,?,1)",
            ("pascal", uid, "Pascal"),
        )
        raw_token = "test-token-raw"
        await db.enqueue(
            "INSERT INTO api_tokens(token_id, user_id, label, token_hash) VALUES(?,?,?,?)",
            ("tid-1", uid, "test", sha256_token_hash(raw_token)),
        )
        uid2 = derive_user_id(kp.public_key, "bob")
        await db.enqueue(
            "INSERT INTO users(username, user_id, display_name, is_admin) VALUES(?,?,?,0)",
            ("bob", uid2, "Bob"),
        )
        await db.enqueue(
            "INSERT INTO api_tokens(token_id, user_id, label, token_hash) VALUES(?,?,?,?)",
            ("tid-2", uid2, "test", sha256_token_hash("bob-token-raw")),
        )
        tc._admin_token = raw_token
        tc._admin_uid = uid
        tc._bob_token = "bob-token-raw"
        tc._bob_uid = uid2
        yield tc


async def _trigger_notification(client):
    """Create a post to generate a notification for bob."""
    await client.post(
        "/api/feed/posts",
        json={"type": "text", "content": "notification trigger"},
        headers=_auth(client._admin_token),
    )


async def test_list_notifications(client):
    """GET /api/notifications returns a list (may be empty initially)."""
    resp = await client.get(
        "/api/notifications",
        headers=_auth(client._bob_token),
    )
    assert resp.status == 200
    body = await resp.json()
    assert isinstance(body, list)


async def test_list_notifications_after_trigger(client):
    """GET /api/notifications returns at least one notification after a post."""
    await _trigger_notification(client)
    resp = await client.get(
        "/api/notifications",
        headers=_auth(client._bob_token),
    )
    assert resp.status == 200
    body = await resp.json()
    assert len(body) >= 1


async def test_unread_count(client):
    """GET /api/notifications/unread-count returns an unread count."""
    await _trigger_notification(client)
    resp = await client.get(
        "/api/notifications/unread-count",
        headers=_auth(client._bob_token),
    )
    assert resp.status == 200
    body = await resp.json()
    assert "unread" in body
    assert body["unread"] >= 1


async def test_mark_single_read(client):
    """POST /api/notifications/{id}/read marks a specific notification read."""
    await _trigger_notification(client)
    notifs_resp = await client.get(
        "/api/notifications",
        headers=_auth(client._bob_token),
    )
    notifs = await notifs_resp.json()
    if notifs:
        notif_id = notifs[0]["id"]
        resp = await client.post(
            f"/api/notifications/{notif_id}/read",
            headers=_auth(client._bob_token),
        )
        assert resp.status == 200


async def test_mark_all_read(client):
    """POST /api/notifications/read-all marks all notifications read."""
    await _trigger_notification(client)
    resp = await client.post(
        "/api/notifications/read-all",
        headers=_auth(client._bob_token),
    )
    assert resp.status == 200
    # Unread count should now be 0
    resp2 = await client.get(
        "/api/notifications/unread-count",
        headers=_auth(client._bob_token),
    )
    body = await resp2.json()
    assert body["unread"] == 0


async def test_read_by_link_reads_the_rows_that_open_the_page(client):
    """Viewing a post used to leave its notification unread until it was
    clicked in the bell; the page now says it was seen. The post's page
    reads its comments' row, the feed reads the new-posts row (D5)."""
    resp = await client.post(
        "/api/feed/posts",
        json={"type": "text", "content": "seen on the page"},
        headers=_auth(client._admin_token),
    )
    post_id = (await resp.json())["id"]
    await client.post(
        f"/api/feed/posts/{post_id}/comments",
        json={"content": "hi"},
        headers=_auth(client._admin_token),
    )
    count = await client.get(
        "/api/notifications/unread-count", headers=_auth(client._bob_token)
    )
    before = (await count.json())["unread"]
    assert before >= 2

    for link in (f"/post/{post_id}", "/feed"):
        resp = await client.post(
            "/api/notifications/read-by-link",
            json={"links": [link]},
            headers=_auth(client._bob_token),
        )
        assert resp.status == 200
        assert (await resp.json()) == {"ok": True, "marked": 1}
    count = await client.get(
        "/api/notifications/unread-count", headers=_auth(client._bob_token)
    )
    assert (await count.json())["unread"] == before - 2


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"links": []},
        {"links": "/post/p1"},
        {"links": ["post/p1"]},
        {"links": [1]},
        {"links": ["/x"] * 21},
        {"links": ["/" + "a" * 600]},
        ["/post/p1"],
    ],
)
async def test_read_by_link_rejects_a_malformed_body(client, body):
    resp = await client.post(
        "/api/notifications/read-by-link",
        json=body,
        headers=_auth(client._bob_token),
    )
    assert resp.status == 422


async def test_read_by_link_requires_auth(client):
    resp = await client.post(
        "/api/notifications/read-by-link", json={"links": ["/post/p1"]}
    )
    assert resp.status == 401


async def test_list_pages_with_before_cursor(client):
    """``before`` walks the list page by page, newest first, no row twice.

    The Notifications page's "Show older" passes the oldest loaded row's
    ``created_at`` as ``before``; each page holds only the caller's rows.
    """
    db = client.app[_db_key]
    for i in range(5):
        await db.enqueue(
            "INSERT INTO notifications(id, user_id, type, title, created_at)"
            " VALUES(?,?,?,?,?)",
            (
                f"bob-{i}",
                client._bob_uid,
                "post_created",
                f"row {i}",
                f"2026-10-27T10:0{i}:00.000000+00:00",
            ),
        )
    await db.enqueue(
        "INSERT INTO notifications(id, user_id, type, title, created_at)"
        " VALUES(?,?,?,?,?)",
        (
            "pascal-x",
            client._admin_uid,
            "post_created",
            "not bob's",
            "2026-10-27T10:02:30.000000+00:00",
        ),
    )
    seen: list[str] = []
    before: str | None = None
    for _ in range(5):  # bounded: an ignored cursor must fail, not hang
        params = {"limit": "2"}
        if before is not None:
            # Encoded like the SPA's encodeURIComponent: the "+" of the
            # UTC offset must survive the query string.
            params["before"] = before
        resp = await client.get(
            "/api/notifications", params=params, headers=_auth(client._bob_token)
        )
        assert resp.status == 200
        page = await resp.json()
        seen.extend(n["id"] for n in page)
        if len(page) < 2:
            break
        before = page[-1]["created_at"]
    assert seen == ["bob-4", "bob-3", "bob-2", "bob-1", "bob-0"]


async def test_list_unread_only(client):
    """``?unread=1`` lists only the caller's unread rows (the Unread tab)."""
    db = client.app[_db_key]
    for i, read_at in enumerate((None, "2026-10-27T11:00:00+00:00", None)):
        await db.enqueue(
            "INSERT INTO notifications(id, user_id, type, title, created_at, read_at)"
            " VALUES(?,?,?,?,?,?)",
            (
                f"bob-u{i}",
                client._bob_uid,
                "post_created",
                f"row {i}",
                f"2026-10-27T10:0{i}:00.000000+00:00",
                read_at,
            ),
        )
    resp = await client.get(
        "/api/notifications",
        params={"unread": "1"},
        headers=_auth(client._bob_token),
    )
    assert resp.status == 200
    assert [n["id"] for n in await resp.json()] == ["bob-u2", "bob-u0"]
    resp = await client.get("/api/notifications", headers=_auth(client._bob_token))
    assert {"bob-u0", "bob-u1", "bob-u2"} <= {n["id"] for n in await resp.json()}


# ── Who, what and where (0129) ────────────────────────────────────────


async def _post(client) -> str:
    resp = await client.post(
        "/api/feed/posts",
        json={"type": "text", "content": "about something"},
        headers=_auth(client._admin_token),
    )
    return (await resp.json())["id"]


async def test_list_rows_name_their_object_actor_and_counts(client):
    await _post(client)
    resp = await client.get("/api/notifications", headers=_auth(client._bob_token))
    [row] = [n for n in await resp.json() if n["type"] == "post_created"]
    assert row["object_key"] == "feed:household"  # new posts group per feed
    assert row["space_id"] is None
    assert row["params"] == {
        "actors": [{"id": client._admin_uid, "name": "Pascal"}],
        "actor_count": 1,
        "new": 1,
        "total": 1,
    }
    assert (row["new_count"], row["total_count"]) == (1, 1)
    # The old fields stay.
    assert row["link_url"] == "/feed"
    assert row["title"] and row["created_at"] and row["read_at"] is None


async def test_comments_on_one_post_share_one_row(client):
    post_id = await _post(client)
    for text in ("one", "two"):
        resp = await client.post(
            f"/api/feed/posts/{post_id}/comments",
            json={"content": text},
            headers=_auth(client._admin_token),
        )
        assert resp.status in (200, 201)
    resp = await client.get("/api/notifications", headers=_auth(client._bob_token))
    rows = [n for n in await resp.json() if n["type"] == "comment_added"]
    assert len(rows) == 1
    assert (rows[0]["new_count"], rows[0]["total_count"]) == (2, 2)
    count = await client.get(
        "/api/notifications/unread-count", headers=_auth(client._bob_token)
    )
    assert (await count.json())["unread"] == 2  # the post row + the comment row


async def test_a_row_from_before_0129_lists_with_nulls(client):
    db = client.app[_db_key]
    await db.enqueue(
        "INSERT INTO notifications(id, user_id, type, title, created_at)"
        " VALUES('old', ?, 'x', 'Old row', '2026-10-01T00:00:00+00:00')",
        (client._bob_uid,),
    )
    resp = await client.get("/api/notifications", headers=_auth(client._bob_token))
    [row] = [n for n in await resp.json() if n["id"] == "old"]
    assert (row["object_key"], row["space_id"], row["params"]) == (None, None, None)
    assert (row["new_count"], row["total_count"]) == (1, 1)


async def test_read_by_object_reads_every_row_about_the_thing(client):
    post_id = await _post(client)
    await client.post(
        f"/api/feed/posts/{post_id}/comments",
        json={"content": "hi"},
        headers=_auth(client._admin_token),
    )
    other = await _post(client)
    await client.post(
        f"/api/feed/posts/{other}/comments",
        json={"content": "hi"},
        headers=_auth(client._admin_token),
    )
    resp = await client.post(
        "/api/notifications/read-by-object",
        json={"object_keys": [f"post:{post_id}", "feed:household"]},
        headers=_auth(client._bob_token),
    )
    assert resp.status == 200
    assert (await resp.json()) == {"ok": True, "marked": 2}
    resp = await client.get("/api/notifications", headers=_auth(client._bob_token))
    unread = [n["object_key"] for n in await resp.json() if n["read_at"] is None]
    assert unread == [f"post:{other}"]


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"object_keys": []},
        {"object_keys": "post:p1"},
        {"object_keys": ["p1"]},
        {"object_keys": [1]},
        {"object_keys": ["post:p"] * 21},
        {"object_keys": ["post:" + "a" * 300]},
        ["post:p1"],
    ],
)
async def test_read_by_object_rejects_a_malformed_body(client, body):
    resp = await client.post(
        "/api/notifications/read-by-object",
        json=body,
        headers=_auth(client._bob_token),
    )
    assert resp.status == 422


async def test_read_by_object_requires_auth(client):
    resp = await client.post(
        "/api/notifications/read-by-object", json={"object_keys": ["post:p1"]}
    )
    assert resp.status == 401


async def test_deleting_a_post_takes_its_rows_away(client):
    """Its comments' row goes; the feed's new-posts row (D5) stays."""
    post_id = await _post(client)
    await client.post(
        f"/api/feed/posts/{post_id}/comments",
        json={"content": "hi"},
        headers=_auth(client._admin_token),
    )
    resp = await client.delete(
        f"/api/feed/posts/{post_id}", headers=_auth(client._admin_token)
    )
    assert resp.status in (200, 204)
    resp = await client.get("/api/notifications", headers=_auth(client._bob_token))
    rows = await resp.json()
    assert [n for n in rows if n["object_key"] == f"post:{post_id}"] == []
    assert [n["object_key"] for n in rows] == ["feed:household"]
    count = await client.get(
        "/api/notifications/unread-count", headers=_auth(client._bob_token)
    )
    assert (await count.json())["unread"] == 1
