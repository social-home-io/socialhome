"""Comprehensive route test — hits every endpoint at least once.

Uses aiohttp_client (pytest-aiohttp) for speed. One client fixture
covers all route modules in a single test session.
"""

from datetime import datetime, timezone, timedelta
from .conftest import _auth


async def test_healthz(client):
    """GET /healthz returns 200."""
    r = await client.get("/healthz")
    assert r.status == 200


# ── Feed routes ───────────────────────────────────────────────────────────


async def test_feed_crud(client):
    """Full feed lifecycle: create → list → edit → react → comment → delete."""
    h = _auth(client._tok)
    # Create
    r = await client.post(
        "/api/feed/posts", json={"type": "text", "content": "Hello"}, headers=h
    )
    assert r.status == 201
    pid = (await r.json())["id"]
    # List
    r = await client.get("/api/feed", headers=h)
    assert r.status == 200
    # List with before cursor
    posts = await r.json()
    if posts:
        r = await client.get(f"/api/feed?before={posts[-1]['created_at']}", headers=h)
        assert r.status == 200
    # Edit
    r = await client.patch(
        f"/api/feed/posts/{pid}", json={"content": "Edited"}, headers=h
    )
    assert r.status == 200
    # Edit missing content field → 422
    r = await client.patch(f"/api/feed/posts/{pid}", json={}, headers=h)
    assert r.status == 422
    # Reaction add
    r = await client.post(
        f"/api/feed/posts/{pid}/reactions", json={"emoji": "👍"}, headers=h
    )
    assert r.status == 200
    # Reaction remove
    r = await client.delete(f"/api/feed/posts/{pid}/reactions/👍", headers=h)
    assert r.status == 200
    # Comment
    r = await client.post(
        f"/api/feed/posts/{pid}/comments", json={"content": "Nice!"}, headers=h
    )
    assert r.status == 201
    # List comments
    r = await client.get(f"/api/feed/posts/{pid}/comments", headers=h)
    assert r.status == 200
    # Delete
    r = await client.delete(f"/api/feed/posts/{pid}", headers=h)
    assert r.status == 204
    # Errors
    r = await client.post(
        "/api/feed/posts", json={"type": "text", "content": "  "}, headers=h
    )
    assert r.status == 422
    r = await client.post("/api/feed/posts", json={"type": "bogus"}, headers=h)
    assert r.status == 422
    r = await client.patch(
        "/api/feed/posts/nonexistent", json={"content": "x"}, headers=h
    )
    assert r.status == 404
    r = await client.post(
        "/api/feed/posts/{pid}/reactions", json={"emoji": ""}, headers=h
    )
    assert r.status in (404, 422)
    # Invalid JSON
    r = await client.post(
        "/api/feed/posts",
        data="not json",
        headers={**h, "Content-Type": "application/json"},
    )
    assert r.status == 400


# ── User routes ───────────────────────────────────────────────────────────


async def test_user_routes(client):
    """GET /api/me, PATCH /api/me, GET /api/users, tokens."""
    h = _auth(client._tok)
    r = await client.get("/api/me", headers=h)
    assert r.status == 200
    body = await r.json()
    assert "password_hash" not in body
    r = await client.patch("/api/me", json={"display_name": "Admin Updated"}, headers=h)
    assert r.status == 200
    r = await client.get("/api/users", headers=h)
    assert r.status == 200
    r = await client.post("/api/me/tokens", json={"label": "dev"}, headers=h)
    assert r.status == 201
    tid = (await r.json())["token_id"]
    r = await client.delete(f"/api/me/tokens/{tid}", headers=h)
    assert r.status in (200, 204)
    # No auth
    r = await client.get("/api/me")
    assert r.status == 401


# ── Space routes ──────────────────────────────────────────────────────────


async def test_space_routes(client):
    """Full space lifecycle: create → get → update → members → posts → feed → invite → dissolve."""
    h = _auth(client._tok)
    r = await client.post(
        "/api/spaces", json={"name": "TestSpace", "emoji": "🏠"}, headers=h
    )
    assert r.status == 201
    sid = (await r.json())["id"]
    r = await client.get(f"/api/spaces/{sid}", headers=h)
    assert r.status == 200
    r = await client.patch(f"/api/spaces/{sid}", json={"name": "Updated"}, headers=h)
    assert r.status == 200
    r = await client.get(f"/api/spaces/{sid}/members", headers=h)
    assert r.status == 200
    r = await client.get(f"/api/spaces/{sid}/feed", headers=h)
    assert r.status == 200
    r = await client.post(
        f"/api/spaces/{sid}/posts", json={"type": "text", "content": "hello"}, headers=h
    )
    assert r.status == 201
    r = await client.post(
        f"/api/spaces/{sid}/invite-tokens", json={"uses": 1}, headers=h
    )
    assert r.status == 201
    r = await client.delete(f"/api/spaces/{sid}", headers=h)
    assert r.status == 200
    # Errors
    r = await client.post("/api/spaces", json={"name": "  "}, headers=h)
    assert r.status == 422
    r = await client.get("/api/spaces/nonexistent", headers=h)
    assert r.status == 404


# ── Conversation routes ───────────────────────────────────────────────────


async def test_conversation_routes(client):
    """DM lifecycle: create → send → list → mark read → unread."""
    h = _auth(client._tok)
    # Need a second user
    await client._db.enqueue(
        "INSERT OR IGNORE INTO users(username, user_id, display_name) VALUES(?,?,?)",
        ("bob", "uid-bob", "Bob"),
    )
    r = await client.post("/api/conversations/dm", json={"username": "bob"}, headers=h)
    assert r.status == 201
    cid = (await r.json())["id"]
    r = await client.post(
        f"/api/conversations/{cid}/messages", json={"content": "hi bob"}, headers=h
    )
    assert r.status == 201
    r = await client.get(f"/api/conversations/{cid}/messages", headers=h)
    assert r.status == 200
    r = await client.post(f"/api/conversations/{cid}/read", headers=h)
    assert r.status == 200
    r = await client.get(f"/api/conversations/{cid}/unread", headers=h)
    assert r.status == 200
    r = await client.get("/api/conversations", headers=h)
    assert r.status == 200
    # Group DM
    await client._db.enqueue(
        "INSERT OR IGNORE INTO users(username, user_id, display_name) VALUES(?,?,?)",
        ("carl", "uid-carl", "Carl"),
    )
    r = await client.post(
        "/api/conversations/group",
        json={"members": ["bob", "carl"], "name": "Crew"},
        headers=h,
    )
    assert r.status == 201
    # Errors
    r = await client.post(
        "/api/conversations/dm", json={"username": "admin"}, headers=h
    )
    assert r.status == 422


# ── Notification routes ───────────────────────────────────────────────────


async def test_notification_routes(client):
    """List → unread count → mark read → mark all read."""
    h = _auth(client._tok)
    r = await client.get("/api/notifications", headers=h)
    assert r.status == 200
    r = await client.get("/api/notifications/unread-count", headers=h)
    assert r.status == 200
    r = await client.post("/api/notifications/read-all", headers=h)
    assert r.status == 200
    # Mark individual (may not exist)
    r = await client.post("/api/notifications/fake-id/read", headers=h)
    assert r.status == 200  # no-op


# ── Shopping routes ───────────────────────────────────────────────────────


async def test_shopping_routes(client):
    """Add → list → complete → uncomplete → clear → delete."""
    h = _auth(client._tok)
    r = await client.post("/api/shopping", json={"text": "Milk"}, headers=h)
    assert r.status == 201
    item = await r.json()
    r = await client.get("/api/shopping", headers=h)
    assert r.status == 200
    r = await client.patch(f"/api/shopping/{item['id']}/complete", headers=h)
    assert r.status == 200
    r = await client.patch(f"/api/shopping/{item['id']}/uncomplete", headers=h)
    assert r.status == 200
    r = await client.patch(f"/api/shopping/{item['id']}/complete", headers=h)
    assert r.status == 200
    r = await client.post("/api/shopping/clear-completed", headers=h)
    assert r.status == 200
    # Item was already cleared — add a new one to test delete
    r = await client.post("/api/shopping", json={"text": "Eggs"}, headers=h)
    item2 = await r.json()
    r = await client.delete(f"/api/shopping/{item2['id']}", headers=h)
    assert r.status in (200, 204)
    # Error
    r = await client.post("/api/shopping", json={"text": "  "}, headers=h)
    assert r.status == 422


# ── Task routes ───────────────────────────────────────────────────────────


async def test_task_routes(client):
    """Lists + tasks CRUD."""
    h = _auth(client._tok)
    r = await client.post("/api/tasks/lists", json={"name": "Chores"}, headers=h)
    assert r.status == 201
    lid = (await r.json())["id"]
    r = await client.get("/api/tasks/lists", headers=h)
    assert r.status == 200
    r = await client.get(f"/api/tasks/lists/{lid}", headers=h)
    assert r.status == 200
    r = await client.post(
        f"/api/tasks/lists/{lid}/tasks", json={"title": "Vacuum"}, headers=h
    )
    assert r.status == 201
    tid = (await r.json())["id"]
    r = await client.get(f"/api/tasks/lists/{lid}/tasks", headers=h)
    assert r.status == 200
    r = await client.patch(f"/api/tasks/{tid}", json={"status": "done"}, headers=h)
    assert r.status == 200
    r = await client.delete(f"/api/tasks/{tid}", headers=h)
    assert r.status in (200, 204)
    r = await client.delete(f"/api/tasks/lists/{lid}", headers=h)
    assert r.status in (200, 204)


# ── Calendar routes ───────────────────────────────────────────────────────


async def test_calendar_routes(client):
    """Calendars + events CRUD."""
    h = _auth(client._tok)
    r = await client.post("/api/calendars", json={"name": "Work"}, headers=h)
    assert r.status == 201
    cid = (await r.json())["id"]
    r = await client.get("/api/calendars", headers=h)
    assert r.status == 200
    now = datetime.now(timezone.utc)
    r = await client.post(
        f"/api/calendars/{cid}/events",
        json={
            "summary": "Meeting",
            "start": now.isoformat(),
            "end": (now + timedelta(hours=1)).isoformat(),
        },
        headers=h,
    )
    assert r.status == 201
    eid = (await r.json())["id"]
    # Use Z-suffix timestamps to avoid URL-encoding issues with +00:00
    start_z = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    end_z = (now + timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M:%SZ")
    r = await client.get(
        f"/api/calendars/{cid}/events?start={start_z}&end={end_z}", headers=h
    )
    assert r.status == 200
    r = await client.delete(f"/api/calendars/events/{eid}", headers=h)
    assert r.status in (200, 204)


# ── Page routes ───────────────────────────────────────────────────────────


async def test_page_routes(client):
    """Pages CRUD + lock/unlock."""
    h = _auth(client._tok)
    r = await client.post(
        "/api/pages", json={"title": "Wiki", "content": "Hi"}, headers=h
    )
    assert r.status == 201
    pid = (await r.json())["id"]
    r = await client.get("/api/pages", headers=h)
    assert r.status == 200
    r = await client.get(f"/api/pages/{pid}", headers=h)
    assert r.status == 200
    r = await client.patch(f"/api/pages/{pid}", json={"content": "Updated"}, headers=h)
    assert r.status == 200
    r = await client.post(f"/api/pages/{pid}/lock", headers=h)
    assert r.status == 200
    r = await client.delete(f"/api/pages/{pid}/lock", headers=h)
    assert r.status == 200
    r = await client.delete(f"/api/pages/{pid}", headers=h)
    assert r.status in (200, 204)


# ── Sticky routes ─────────────────────────────────────────────────────────


async def test_sticky_routes(client):
    """Stickies CRUD."""
    h = _auth(client._tok)
    r = await client.post("/api/stickies", json={"content": "Note"}, headers=h)
    assert r.status == 201
    sid = (await r.json())["id"]
    r = await client.get("/api/stickies", headers=h)
    assert r.status == 200
    r = await client.patch(
        f"/api/stickies/{sid}",
        json={"content": "Updated", "color": "#FF0000"},
        headers=h,
    )
    assert r.status == 200
    r = await client.delete(f"/api/stickies/{sid}", headers=h)
    assert r.status in (200, 204)


# ── Bazaar routes ─────────────────────────────────────────────────────────


async def test_bazaar_routes(client):
    """List active listings."""
    h = _auth(client._tok)
    r = await client.get("/api/bazaar", headers=h)
    assert r.status == 200


# ── Presence routes ───────────────────────────────────────────────────────


async def test_presence_routes(client):
    """List + update presence."""
    h = _auth(client._tok)
    r = await client.get("/api/presence", headers=h)
    assert r.status == 200
    r = await client.post(
        "/api/presence/location",
        json={
            "username": "admin",
            "zone_name": "home",
        },
        headers=h,
    )
    assert r.status == 204


# ── Federation webhook ───────────────────────────────────────────────────


async def test_federation_webhook(client):
    """POST /webhook/{id} runs the §24.11 pipeline (now wired).

    A malformed body lands at the JSON/missing-fields rejection
    branches — never the 200 placeholder. Full pipeline coverage
    lives in ``test_federation_webhook.py``.
    """
    r = await client.post("/webhook/test-id", json={"event_type": "test"})
    assert r.status in (400, 404, 410)


# ── Media routes ──────────────────────────────────────────────────────────


async def test_media_get_404(client):
    """GET /api/media/nonexistent returns 404."""
    h = _auth(client._tok)
    r = await client.get("/api/media/nonexistent.webp", headers=h)
    assert r.status == 404


# ═══════════════════════════════════════════════════════════════════════════
# ERROR PATH TESTS — target uncovered branches in each route module
# ═══════════════════════════════════════════════════════════════════════════

# ── Bazaar error paths (30% → target 80%+) ────────────────────────────────


async def test_bazaar_get_nonexistent(client):
    """GET /api/bazaar/{id} for missing listing returns 404."""
    h = _auth(client._tok)
    r = await client.get("/api/bazaar/nonexistent", headers=h)
    assert r.status == 404


async def test_bazaar_place_bid(client):
    """POST /api/bazaar/{id}/bids on nonexistent listing."""
    h = _auth(client._tok)
    r = await client.post(
        "/api/bazaar/nonexistent/bids", json={"amount": 100}, headers=h
    )
    assert r.status >= 400


async def test_bazaar_accept_nonexistent(client):
    """POST /api/bazaar/{id}/bids/{bid}/accept on missing."""
    h = _auth(client._tok)
    r = await client.post("/api/bazaar/x/bids/y/accept", headers=h)
    assert r.status >= 400


# ── Users error paths (58% → target 85%+) ─────────────────────────────────


async def test_users_patch_invalid_json(client):
    """PATCH /api/me with bad JSON returns 400."""
    h = {**_auth(client._tok), "Content-Type": "application/json"}
    r = await client.patch("/api/me", data="not json", headers=h)
    assert r.status >= 400


async def test_users_token_empty_label(client):
    """POST /api/me/tokens with empty label returns 422."""
    h = _auth(client._tok)
    r = await client.post("/api/me/tokens", json={"label": "  "}, headers=h)
    assert r.status == 422


async def test_users_delete_nonexistent_token(client):
    """DELETE /api/me/tokens/{id} for missing token."""
    h = _auth(client._tok)
    r = await client.delete("/api/me/tokens/nonexistent", headers=h)
    assert r.status in (200, 204)  # no-op revoke


async def test_users_query_token_auth(client):
    """Authentication via ?token= query parameter."""
    r = await client.get(f"/api/me?token={client._tok}")
    assert r.status == 200


# ── Spaces error paths (78% → target 90%+) ────────────────────────────────


async def test_spaces_add_member_nonexistent(client):
    """POST /api/spaces/{id}/members for missing space returns 404."""
    h = _auth(client._tok)
    r = await client.post(
        "/api/spaces/nonexistent/members", json={"user_id": "x"}, headers=h
    )
    assert r.status == 404


async def test_spaces_remove_member(client):
    """DELETE /api/spaces/{id}/members/{uid}."""
    h = _auth(client._tok)
    r = await client.post("/api/spaces", json={"name": "Rm"}, headers=h)
    sid = (await r.json())["id"]
    r = await client.delete(f"/api/spaces/{sid}/members/nonexistent", headers=h)
    assert r.status in (200, 403, 404)


async def test_spaces_ban_nonexistent(client):
    """POST /api/spaces/{id}/ban on missing space."""
    h = _auth(client._tok)
    r = await client.post(
        "/api/spaces/nonexistent/ban", json={"user_id": "x"}, headers=h
    )
    assert r.status >= 400


async def test_spaces_join_invalid_token(client):
    """POST /api/spaces/join with invalid token."""
    h = _auth(client._tok)
    r = await client.post("/api/spaces/join", json={"token": "bad"}, headers=h)
    assert r.status == 404


async def test_spaces_feed_nonexistent(client):
    """GET /api/spaces/{id}/feed for missing space."""
    h = _auth(client._tok)
    r = await client.get("/api/spaces/nonexistent/feed", headers=h)
    assert r.status == 404


async def test_spaces_post_to_nonexistent(client):
    """POST /api/spaces/{id}/posts for missing space."""
    h = _auth(client._tok)
    r = await client.post(
        "/api/spaces/nonexistent/posts",
        json={"type": "text", "content": "x"},
        headers=h,
    )
    assert r.status >= 400


# ── Feed error paths (81% → target 90%+) ──────────────────────────────────


async def test_feed_reaction_on_nonexistent(client):
    """POST reactions on nonexistent post."""
    h = _auth(client._tok)
    r = await client.post(
        "/api/feed/posts/nonexistent/reactions", json={"emoji": "👍"}, headers=h
    )
    assert r.status == 404


async def test_feed_remove_reaction_nonexistent(client):
    """DELETE reaction on nonexistent post."""
    h = _auth(client._tok)
    r = await client.delete("/api/feed/posts/nonexistent/reactions/👍", headers=h)
    assert r.status == 404


async def test_feed_comment_invalid_json(client):
    """POST comment with bad JSON."""
    h = {**_auth(client._tok), "Content-Type": "application/json"}
    r = await client.post("/api/feed/posts/x/comments", data="bad", headers=h)
    assert r.status == 400


async def test_feed_reaction_invalid_json(client):
    """POST reaction with bad JSON."""
    h = {**_auth(client._tok), "Content-Type": "application/json"}
    r = await client.post("/api/feed/posts/x/reactions", data="bad", headers=h)
    assert r.status == 400


# ── Tasks error paths (76% → target 90%+) ─────────────────────────────────


async def test_tasks_get_nonexistent_list(client):
    """GET /api/tasks/lists/{id} for missing list."""
    h = _auth(client._tok)
    r = await client.get("/api/tasks/lists/nonexistent", headers=h)
    assert r.status == 404


async def test_tasks_create_in_nonexistent_list(client):
    """POST task in nonexistent list."""
    h = _auth(client._tok)
    r = await client.post(
        "/api/tasks/lists/nonexistent/tasks", json={"title": "T"}, headers=h
    )
    assert r.status >= 400


async def test_tasks_update_nonexistent(client):
    """PATCH nonexistent task."""
    h = _auth(client._tok)
    r = await client.patch("/api/tasks/nonexistent", json={"title": "T"}, headers=h)
    assert r.status == 404


async def test_tasks_delete_nonexistent(client):
    """DELETE nonexistent task."""
    h = _auth(client._tok)
    r = await client.delete("/api/tasks/nonexistent", headers=h)
    assert r.status >= 400


async def test_tasks_delete_nonexistent_list(client):
    """DELETE nonexistent task list."""
    h = _auth(client._tok)
    r = await client.delete("/api/tasks/lists/nonexistent", headers=h)
    assert r.status >= 400


# ── Pages error paths (75% → target 90%+) ─────────────────────────────────


async def test_pages_get_nonexistent(client):
    """GET /api/pages/{id} for missing page."""
    h = _auth(client._tok)
    r = await client.get("/api/pages/nonexistent", headers=h)
    assert r.status == 404


async def test_pages_lock_nonexistent(client):
    """POST lock on nonexistent page."""
    h = _auth(client._tok)
    r = await client.post("/api/pages/nonexistent/lock", headers=h)
    assert r.status >= 400


async def test_pages_delete_nonexistent(client):
    """DELETE nonexistent page."""
    h = _auth(client._tok)
    r = await client.delete("/api/pages/nonexistent", headers=h)
    assert r.status < 500


# ── Calendar error paths (74% → target 90%+) ──────────────────────────────


async def test_calendar_events_missing_params(client):
    """GET events without start/end returns 422."""
    h = _auth(client._tok)
    r = await client.post("/api/calendars", json={"name": "C"}, headers=h)
    cid = (await r.json())["id"]
    r = await client.get(f"/api/calendars/{cid}/events", headers=h)
    assert r.status == 422


async def test_calendar_delete_nonexistent_event(client):
    """DELETE nonexistent event."""
    h = _auth(client._tok)
    r = await client.delete("/api/calendars/events/nonexistent", headers=h)
    assert r.status >= 400


# ── Stickies error paths (74% → target 90%+) ──────────────────────────────


async def test_stickies_empty_content(client):
    """POST sticky with empty content returns 422."""
    h = _auth(client._tok)
    r = await client.post("/api/stickies", json={"content": "  "}, headers=h)
    assert r.status == 422


async def test_stickies_delete_nonexistent(client):
    """DELETE nonexistent sticky."""
    h = _auth(client._tok)
    r = await client.delete("/api/stickies/nonexistent", headers=h)
    assert r.status < 500


# ── Presence error paths (72% → target 90%+) ──────────────────────────────


async def test_presence_missing_username(client):
    """POST location without username returns 422."""
    h = _auth(client._tok)
    r = await client.post("/api/presence/location", json={"state": "home"}, headers=h)
    assert r.status == 422


async def test_presence_invalid_state(client):
    """POST location with invalid state returns 422."""
    h = _auth(client._tok)
    r = await client.post(
        "/api/presence/location",
        json={"username": "admin", "state": "flying"},
        headers=h,
    )
    assert r.status == 422


async def test_presence_with_gps(client):
    """POST location with latitude/longitude coordinates."""
    h = _auth(client._tok)
    r = await client.post(
        "/api/presence/location",
        json={
            "username": "admin",
            "zone_name": "Work",
            "latitude": 52.37654,
            "longitude": 4.89567,
        },
        headers=h,
    )
    assert r.status == 204


# ── Conversations error paths (86% → target 90%+) ─────────────────────────


async def test_conversations_send_empty(client):
    """POST empty message returns 422."""
    h = _auth(client._tok)
    # Create a DM first
    await client._db.enqueue(
        "INSERT OR IGNORE INTO users(username, user_id, display_name) VALUES(?,?,?)",
        ("dm_bob", "uid-dm-bob", "Bob"),
    )
    r = await client.post(
        "/api/conversations/dm", json={"username": "dm_bob"}, headers=h
    )
    cid = (await r.json())["id"]
    r = await client.post(
        f"/api/conversations/{cid}/messages", json={"content": ""}, headers=h
    )
    assert r.status == 422


async def test_conversations_nonexistent(client):
    """POST message to nonexistent conversation returns 404."""
    h = _auth(client._tok)
    r = await client.post(
        "/api/conversations/nonexistent/messages", json={"content": "hi"}, headers=h
    )
    assert r.status == 404


async def test_conversations_group_too_few(client):
    """POST group DM with <3 members returns 422."""
    h = _auth(client._tok)
    r = await client.post(
        "/api/conversations/group", json={"members": ["dm_bob"]}, headers=h
    )
    assert r.status == 422


# ── Shopping error paths (86% → target 90%+) ──────────────────────────────


async def test_shopping_delete_nonexistent(client):
    """DELETE nonexistent shopping item."""
    h = _auth(client._tok)
    r = await client.delete("/api/shopping/nonexistent", headers=h)
    assert r.status < 500


async def test_shopping_complete_nonexistent(client):
    """PATCH complete on nonexistent item."""
    h = _auth(client._tok)
    r = await client.patch("/api/shopping/nonexistent/complete", headers=h)
    assert r.status in (200, 404)
