"""Full route coverage for tasks endpoints."""

from .conftest import _auth


async def test_task_full_lifecycle(client):
    """Create list → create task → list tasks → update → delete."""
    h = _auth(client._tok)
    r = await client.post("/api/tasks/lists", json={"name": "Chores"}, headers=h)
    assert r.status == 201
    lid = (await r.json())["id"]

    r = await client.get(f"/api/tasks/lists/{lid}", headers=h)
    assert r.status == 200

    r = await client.post(
        f"/api/tasks/lists/{lid}/tasks", json={"title": "Vacuum"}, headers=h
    )
    assert r.status == 201
    tid = (await r.json())["id"]

    r = await client.get(f"/api/tasks/lists/{lid}/tasks", headers=h)
    assert r.status == 200
    assert len(await r.json()) >= 1

    r = await client.patch(f"/api/tasks/{tid}", json={"status": "done"}, headers=h)
    assert r.status == 200

    r = await client.delete(f"/api/tasks/{tid}", headers=h)
    assert r.status in (200, 204)

    r = await client.delete(f"/api/tasks/lists/{lid}", headers=h)
    assert r.status in (200, 204)
