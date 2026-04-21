"""Full route coverage for calendar endpoints."""

from datetime import datetime, timezone, timedelta
from .conftest import _auth


async def test_calendar_full_lifecycle(client):
    """Create calendar → create event → list events → delete."""
    h = _auth(client._tok)
    r = await client.post("/api/calendars", json={"name": "Work"}, headers=h)
    assert r.status == 201
    cid = (await r.json())["id"]

    r = await client.get("/api/calendars", headers=h)
    assert r.status == 200
    assert len(await r.json()) >= 1

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

    r = await client.get(
        f"/api/calendars/{cid}/events",
        params={
            "start": (now - timedelta(hours=1)).isoformat(),
            "end": (now + timedelta(hours=2)).isoformat(),
        },
        headers=h,
    )
    assert r.status == 200
    assert len(await r.json()) >= 1

    r = await client.delete(f"/api/calendars/events/{eid}", headers=h)
    assert r.status in (200, 204)
