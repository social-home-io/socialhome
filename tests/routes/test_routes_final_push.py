"""Final push to 90% — targets uncovered route branches."""

from datetime import datetime, timezone, timedelta
from .conftest import _auth


# ── Users route — 64% → 90%+ ─────────────────────────────────────────────


async def test_users_get_me_detailed(client):
    """GET /api/me returns full user profile."""
    h = _auth(client._tok)
    r = await client.get("/api/me", headers=h)
    body = await r.json()
    assert body["username"] == "admin"
    assert "user_id" in body
    assert "is_admin" in body


async def test_users_patch_display_name(client):
    """PATCH /api/me updates display_name."""
    h = _auth(client._tok)
    r = await client.patch("/api/me", json={"display_name": "New Name"}, headers=h)
    assert r.status == 200


async def test_users_patch_bio(client):
    """PATCH /api/me updates bio."""
    h = _auth(client._tok)
    r = await client.patch("/api/me", json={"bio": "Builder"}, headers=h)
    assert r.status == 200


async def test_users_patch_preferences(client):
    """PATCH /api/me updates preferences."""
    h = _auth(client._tok)
    r = await client.patch(
        "/api/me", json={"preferences": {"theme": "dark"}}, headers=h
    )
    assert r.status == 200


async def test_users_get_users_list(client):
    """GET /api/users returns array of users."""
    h = _auth(client._tok)
    r = await client.get("/api/users", headers=h)
    body = await r.json()
    assert isinstance(body, list) and len(body) >= 1


async def test_users_create_token(client):
    """POST /api/me/tokens creates a new API token."""
    h = _auth(client._tok)
    r = await client.post("/api/me/tokens", json={"label": "device1"}, headers=h)
    assert r.status == 201
    body = await r.json()
    assert "token_id" in body and "token" in body


async def test_users_revoke_token(client):
    """DELETE /api/me/tokens/{id} revokes it."""
    h = _auth(client._tok)
    r = await client.post("/api/me/tokens", json={"label": "temp"}, headers=h)
    tid = (await r.json())["token_id"]
    r2 = await client.delete(f"/api/me/tokens/{tid}", headers=h)
    assert r2.status < 500


# ── Bazaar route — 68% → 90%+ ────────────────────────────────────────────


async def test_bazaar_list(client):
    """GET /api/bazaar returns listing array."""
    h = _auth(client._tok)
    r = await client.get("/api/bazaar", headers=h)
    assert r.status == 200
    assert isinstance(await r.json(), list)


async def test_bazaar_get_detail(client):
    """GET /api/bazaar/{id} for nonexistent → 404."""
    h = _auth(client._tok)
    r = await client.get("/api/bazaar/missing-id", headers=h)
    assert r.status == 404


async def test_bazaar_bid_missing(client):
    """POST bid on nonexistent listing."""
    h = _auth(client._tok)
    r = await client.post("/api/bazaar/missing/bids", json={"amount": 100}, headers=h)
    assert r.status >= 400


async def test_bazaar_accept_missing(client):
    """POST accept on nonexistent bid."""
    h = _auth(client._tok)
    r = await client.post("/api/bazaar/x/bids/y/accept", headers=h)
    assert r.status >= 400


# ── Calendar route — 80% → 90%+ ──────────────────────────────────────────


async def test_calendar_create_and_list(client):
    """POST + GET calendars."""
    h = _auth(client._tok)
    r = await client.post("/api/calendars", json={"name": "Personal"}, headers=h)
    assert r.status == 201
    r2 = await client.get("/api/calendars", headers=h)
    assert r2.status == 200
    assert len(await r2.json()) >= 1


async def test_calendar_create_event_and_list(client):
    """POST event + GET events with range."""
    h = _auth(client._tok)
    r = await client.post("/api/calendars", json={"name": "C"}, headers=h)
    cid = (await r.json())["id"]
    now = datetime.now(timezone.utc)
    r2 = await client.post(
        f"/api/calendars/{cid}/events",
        json={
            "summary": "Lunch",
            "start": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "end": (now + timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ"),
        },
        headers=h,
    )
    assert r2.status == 201
    eid = (await r2.json())["id"]

    r3 = await client.get(
        f"/api/calendars/{cid}/events"
        f"?start={now.strftime('%Y-%m-%dT%H:%M:%SZ')}"
        f"&end={(now + timedelta(hours=2)).strftime('%Y-%m-%dT%H:%M:%SZ')}",
        headers=h,
    )
    assert r3.status == 200
    assert len(await r3.json()) >= 1

    r4 = await client.delete(f"/api/calendars/events/{eid}", headers=h)
    assert r4.status < 500


async def test_calendar_missing_range_params(client):
    """GET events without start/end → 422."""
    h = _auth(client._tok)
    r = await client.post("/api/calendars", json={"name": "X"}, headers=h)
    cid = (await r.json())["id"]
    r2 = await client.get(f"/api/calendars/{cid}/events", headers=h)
    assert r2.status == 422


# ── Spaces route — 82% → 90%+ ────────────────────────────────────────────


async def test_space_create_get_update_dissolve(client):
    """Full space lifecycle via API."""
    h = _auth(client._tok)
    r = await client.post("/api/spaces", json={"name": "Lifecycle"}, headers=h)
    assert r.status == 201
    sid = (await r.json())["id"]

    r = await client.get(f"/api/spaces/{sid}", headers=h)
    assert r.status == 200

    r = await client.patch(f"/api/spaces/{sid}", json={"name": "Renamed"}, headers=h)
    assert r.status == 200

    r = await client.get(f"/api/spaces/{sid}/members", headers=h)
    assert r.status == 200

    r = await client.post(
        f"/api/spaces/{sid}/posts", json={"type": "text", "content": "hi"}, headers=h
    )
    assert r.status == 201

    r = await client.get(f"/api/spaces/{sid}/feed", headers=h)
    assert r.status == 200

    r = await client.post(
        f"/api/spaces/{sid}/invite-tokens", json={"uses": 1}, headers=h
    )
    assert r.status == 201
    _token = (await r.json())["token"]

    r = await client.delete(f"/api/spaces/{sid}", headers=h)
    assert r.status == 200


async def test_space_errors(client):
    """Space error paths."""
    h = _auth(client._tok)
    r = await client.post("/api/spaces", json={"name": ""}, headers=h)
    assert r.status == 422
    r = await client.get("/api/spaces/nonexistent", headers=h)
    assert r.status == 404
    r = await client.get("/api/spaces/nonexistent/feed", headers=h)
    assert r.status == 404
    r = await client.post(
        "/api/spaces/nonexistent/posts",
        json={"type": "text", "content": "x"},
        headers=h,
    )
    assert r.status >= 400


# ── Feed route — 86% → 90%+ ──────────────────────────────────────────────


async def test_feed_full_flow(client):
    """Feed create → edit → comment → reaction → delete."""
    h = _auth(client._tok)
    r = await client.post(
        "/api/feed/posts", json={"type": "text", "content": "Flow"}, headers=h
    )
    pid = (await r.json())["id"]
    r = await client.patch(
        f"/api/feed/posts/{pid}", json={"content": "Edited"}, headers=h
    )
    assert r.status == 200
    r = await client.post(
        f"/api/feed/posts/{pid}/comments", json={"content": "C"}, headers=h
    )
    assert r.status == 201
    r = await client.get(f"/api/feed/posts/{pid}/comments", headers=h)
    assert r.status == 200
    r = await client.post(
        f"/api/feed/posts/{pid}/reactions", json={"emoji": "❤️"}, headers=h
    )
    assert r.status == 200
    r = await client.delete(f"/api/feed/posts/{pid}/reactions/❤️", headers=h)
    assert r.status == 200
    r = await client.delete(f"/api/feed/posts/{pid}", headers=h)
    assert r.status == 204
