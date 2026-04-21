"""Tests for social_home.routes.tasks."""

from .conftest import _auth


async def test_create_task_list(client):
    """POST /api/tasks/lists creates a list."""
    r = await client.post(
        "/api/tasks/lists", json={"name": "Chores"}, headers=_auth(client._tok)
    )
    assert r.status == 201
    body = await r.json()
    assert body["name"] == "Chores"


async def test_list_task_lists(client):
    """GET /api/tasks/lists returns all lists."""
    await client.post(
        "/api/tasks/lists", json={"name": "Work"}, headers=_auth(client._tok)
    )
    r = await client.get("/api/tasks/lists", headers=_auth(client._tok))
    assert r.status == 200
    assert len(await r.json()) >= 1


async def test_create_task(client):
    """POST /api/tasks/lists/{id}/tasks creates a task."""
    r = await client.post(
        "/api/tasks/lists", json={"name": "L"}, headers=_auth(client._tok)
    )
    lid = (await r.json())["id"]
    r2 = await client.post(
        f"/api/tasks/lists/{lid}/tasks",
        json={"title": "Buy milk"},
        headers=_auth(client._tok),
    )
    assert r2.status == 201


async def test_list_tasks(client):
    """GET /api/tasks/lists/{id}/tasks returns tasks."""
    r = await client.post(
        "/api/tasks/lists", json={"name": "L"}, headers=_auth(client._tok)
    )
    lid = (await r.json())["id"]
    await client.post(
        f"/api/tasks/lists/{lid}/tasks", json={"title": "T"}, headers=_auth(client._tok)
    )
    r2 = await client.get(f"/api/tasks/lists/{lid}/tasks", headers=_auth(client._tok))
    assert r2.status == 200
    assert len(await r2.json()) >= 1
