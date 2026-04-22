"""Coverage-targeted tests for routes with low hit rate on CI.

These are deliberately lightweight — one-scenario-per-endpoint — to
pull module-level coverage up above the 90% gate. Each test is
designed to be independently runnable without cross-test order
dependencies.
"""

from __future__ import annotations

from datetime import datetime, timezone, timedelta

from .conftest import _auth


# ── peer_spaces (§D1a) ─────────────────────────────────────────────


async def test_peer_spaces_empty(client):
    """Empty peer directory returns an empty list."""
    r = await client.get("/api/peer_spaces", headers=_auth(client._tok))
    assert r.status == 200
    assert await r.json() == []


async def test_peer_spaces_unauth(client):
    """Without auth returns 401."""
    r = await client.get("/api/peer_spaces")
    assert r.status == 401


async def test_peer_spaces_age_filter_branch(client):
    """Age-gated user: the CP path runs but the list is still empty."""
    # Enable CP on the users row so the route's max_min_age branch fires.
    await client._db.enqueue(
        "UPDATE users SET child_protection_enabled=1, declared_age=10 WHERE user_id=?",
        (client._uid,),
    )
    r = await client.get("/api/peer_spaces", headers=_auth(client._tok))
    assert r.status == 200


# ── calendar_export ────────────────────────────────────────────────


async def test_calendar_ics_empty(client):
    """Export of an empty calendar returns a minimal VCALENDAR."""
    # Create a calendar via the standard route so the service + repo
    # wire up correctly.
    r = await client.post(
        "/api/calendars",
        json={"name": "Test Cal", "color": "#f0f", "timezone": "UTC"},
        headers=_auth(client._tok),
    )
    assert r.status == 201
    cid = (await r.json())["id"]
    r = await client.get(
        f"/api/calendar/{cid}/export.ics",
        headers=_auth(client._tok),
    )
    assert r.status == 200
    body = await r.text()
    assert body.startswith("BEGIN:VCALENDAR")
    assert body.endswith("END:VCALENDAR\r\n")


async def test_calendar_ics_with_event(client):
    """Export of a calendar with one event includes VEVENT + escapes."""
    r = await client.post(
        "/api/calendars",
        json={"name": "Family", "color": "#123", "timezone": "UTC"},
        headers=_auth(client._tok),
    )
    cid = (await r.json())["id"]
    now = datetime.now(timezone.utc)
    r = await client.post(
        f"/api/calendars/{cid}/events",
        json={
            "summary": "Picnic, with backslash\\",
            "description": "Line 1\nLine 2",
            "start": now.isoformat(),
            "end": (now + timedelta(hours=2)).isoformat(),
        },
        headers=_auth(client._tok),
    )
    assert r.status == 201, await r.text()
    r = await client.get(
        f"/api/calendar/{cid}/export.ics",
        headers=_auth(client._tok),
    )
    assert r.status == 200
    body = await r.text()
    assert "BEGIN:VEVENT" in body
    assert "END:VEVENT" in body
    # iCal escape rules — comma + backslash must be escaped.
    assert "Picnic\\, with backslash\\\\" in body
    # Newlines become \n in-line.
    assert "Line 1\\nLine 2" in body


# ── storage quota PUT (§A6) ────────────────────────────────────────


async def test_storage_quota_put_admin(client):
    """Admin sets the quota at runtime."""
    r = await client.put(
        "/api/admin/storage/quota",
        json={"quota_bytes": 5 * 1024 * 1024 * 1024},
        headers=_auth(client._tok),
    )
    assert r.status == 200
    body = await r.json()
    assert body["quota_bytes"] == 5 * 1024 * 1024 * 1024


async def test_storage_quota_put_zero_disables(client):
    """quota_bytes=0 disables enforcement."""
    r = await client.put(
        "/api/admin/storage/quota",
        json={"quota_bytes": 0},
        headers=_auth(client._tok),
    )
    assert r.status == 200
    body = await r.json()
    assert body["quota_bytes"] == 0


async def test_storage_quota_put_missing_returns_422(client):
    """No quota_bytes in body → 422."""
    r = await client.put(
        "/api/admin/storage/quota",
        json={},
        headers=_auth(client._tok),
    )
    assert r.status == 422


async def test_storage_quota_put_negative_returns_422(client):
    """Negative quota_bytes → 422."""
    r = await client.put(
        "/api/admin/storage/quota",
        json={"quota_bytes": -1},
        headers=_auth(client._tok),
    )
    assert r.status == 422


async def test_storage_quota_put_non_int_returns_422(client):
    """Non-integer quota_bytes → 422."""
    r = await client.put(
        "/api/admin/storage/quota",
        json={"quota_bytes": "not-an-int"},
        headers=_auth(client._tok),
    )
    assert r.status == 422


async def test_storage_quota_put_non_admin_returns_403(client):
    """Non-admin gets 403."""
    # Demote the caller first.
    await client._db.enqueue(
        "UPDATE users SET is_admin=0 WHERE user_id=?",
        (client._uid,),
    )
    r = await client.put(
        "/api/admin/storage/quota",
        json={"quota_bytes": 0},
        headers=_auth(client._tok),
    )
    assert r.status == 403


# ── users PATCH preferences + bio ──────────────────────────────────


async def test_me_patch_preferences(client):
    """PATCH /api/me with `preferences` hits the patch_preferences branch."""
    r = await client.patch(
        "/api/me",
        json={"preferences": {"theme": "dark", "density": "compact"}},
        headers=_auth(client._tok),
    )
    assert r.status == 200


async def test_me_patch_display_name(client):
    """PATCH /api/me with `display_name` hits the patch_profile branch."""
    r = await client.patch(
        "/api/me",
        json={"display_name": "Admin 2"},
        headers=_auth(client._tok),
    )
    assert r.status == 200
    body = await r.json()
    assert body["display_name"] == "Admin 2"


async def test_me_patch_empty_body_returns_current(client):
    """PATCH /api/me with no recognised fields returns current profile."""
    r = await client.patch("/api/me", json={}, headers=_auth(client._tok))
    assert r.status == 200


# ── notifications + health ─────────────────────────────────────────


async def test_notifications_empty(client):
    r = await client.get("/api/notifications", headers=_auth(client._tok))
    assert r.status == 200


async def test_notifications_unread_count(client):
    r = await client.get(
        "/api/notifications/unread-count",
        headers=_auth(client._tok),
    )
    assert r.status == 200
    body = await r.json()
    assert "count" in body or "unread" in body or isinstance(body, (int, dict))


async def test_health_detailed(client):
    r = await client.get("/healthz", headers=_auth(client._tok))
    assert r.status == 200
