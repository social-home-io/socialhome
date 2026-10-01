"""Tests for socialhome.routes.tasks."""

from socialhome.auth import sha256_token_hash

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


# ─── Space tasks: cross-space scope + subscriber write gate ─────────────


async def _seed_space_with_member(client, sid: str, user_id: str, role: str) -> None:
    await client._db.enqueue(
        "INSERT INTO spaces(id, name, owner_instance_id, owner_username,"
        " identity_public_key) VALUES(?, ?, 'inst', 'admin', ?)",
        (sid, sid, "ab" * 32),
    )
    await client._db.enqueue(
        "INSERT INTO space_members(space_id, user_id, role) VALUES(?, ?, ?)",
        (sid, user_id, role),
    )


async def _add_user(client, username: str, user_id: str, token: str) -> dict:
    await client._db.enqueue(
        "INSERT INTO users(username, user_id, display_name, is_admin) "
        "VALUES(?, ?, ?, 0)",
        (username, user_id, username.title()),
    )
    await client._db.enqueue(
        "INSERT INTO api_tokens(token_id, user_id, label, token_hash) "
        "VALUES(?, ?, 't', ?)",
        (f"t-{username}", user_id, sha256_token_hash(token)),
    )
    return {"Authorization": f"Bearer {token}"}


async def _cross_space_env(client):
    """Admin owns a list + task in ``sp-b``; bob is a member of ``sp-a`` only.

    Returns ``(bob_headers, list_b_id, task_b_id)``.
    """
    admin = _auth(client._tok)
    await _seed_space_with_member(client, "sp-b", client._uid, "owner")
    bob = await _add_user(client, "bob", "bob-id", "bob-tok")
    await _seed_space_with_member(client, "sp-a", "bob-id", "member")
    r = await client.post(
        "/api/spaces/sp-b/tasks/lists", json={"name": "B"}, headers=admin
    )
    assert r.status == 201
    lid = (await r.json())["id"]
    r = await client.post(
        f"/api/spaces/sp-b/tasks/lists/{lid}/tasks",
        json={"title": "TB"},
        headers=admin,
    )
    assert r.status == 201
    tid = (await r.json())["id"]
    return bob, lid, tid


async def _space_b_state(client) -> tuple[list, list]:
    lists = await client._db.fetchall(
        "SELECT id, name FROM space_task_lists WHERE space_id='sp-b'"
    )
    tasks = await client._db.fetchall(
        "SELECT id, title, status, archived_at FROM space_tasks WHERE space_id='sp-b'"
    )
    return [tuple(r) for r in lists], [tuple(r) for r in tasks]


async def test_space_task_from_another_space_is_404_and_untouched(client):
    bob, lid, tid = await _cross_space_env(client)
    before = await _space_b_state(client)
    r = await client.patch(
        f"/api/spaces/sp-a/tasks/{tid}",
        json={"title": "pwned", "status": "done"},
        headers=bob,
    )
    assert r.status == 404
    r = await client.post(f"/api/spaces/sp-a/tasks/{tid}/archive", headers=bob)
    assert r.status == 404
    r = await client.delete(f"/api/spaces/sp-a/tasks/{tid}/archive", headers=bob)
    assert r.status == 404
    r = await client.delete(f"/api/spaces/sp-a/tasks/{tid}", headers=bob)
    assert r.status == 404
    assert await _space_b_state(client) == before


async def test_space_task_list_from_another_space_is_404_and_untouched(client):
    bob, lid, tid = await _cross_space_env(client)
    before = await _space_b_state(client)
    r = await client.get(f"/api/spaces/sp-a/tasks/lists/{lid}/tasks", headers=bob)
    assert r.status == 404
    r = await client.patch(
        f"/api/spaces/sp-a/tasks/lists/{lid}",
        json={"name": "pwned"},
        headers=bob,
    )
    assert r.status == 404
    r = await client.post(
        f"/api/spaces/sp-a/tasks/lists/{lid}/tasks",
        json={"title": "planted"},
        headers=bob,
    )
    assert r.status == 404
    r = await client.delete(f"/api/spaces/sp-a/tasks/lists/{lid}", headers=bob)
    assert r.status == 404
    assert await _space_b_state(client) == before


async def _subscriber_env(client):
    """Admin owns a list + task in ``sp-s``; bob is a subscriber there."""
    admin = _auth(client._tok)
    await _seed_space_with_member(client, "sp-s", client._uid, "owner")
    bob = await _add_user(client, "bob", "bob-id", "bob-tok")
    await client._db.enqueue(
        "INSERT INTO space_members(space_id, user_id, role) VALUES(?, ?, ?)",
        ("sp-s", "bob-id", "subscriber"),
    )
    r = await client.post(
        "/api/spaces/sp-s/tasks/lists", json={"name": "S"}, headers=admin
    )
    lid = (await r.json())["id"]
    r = await client.post(
        f"/api/spaces/sp-s/tasks/lists/{lid}/tasks",
        json={"title": "TS"},
        headers=admin,
    )
    tid = (await r.json())["id"]
    return bob, lid, tid


async def test_space_task_subscriber_reads_but_never_writes(client):
    bob, lid, tid = await _subscriber_env(client)
    base = "/api/spaces/sp-s/tasks"
    assert (await client.get(f"{base}/lists", headers=bob)).status == 200
    assert (await client.get(f"{base}/lists/{lid}/tasks", headers=bob)).status == 200
    writes = [
        client.post(f"{base}/lists", json={"name": "X"}, headers=bob),
        client.patch(f"{base}/lists/{lid}", json={"name": "X"}, headers=bob),
        client.post(f"{base}/lists/{lid}/tasks", json={"title": "X"}, headers=bob),
        client.patch(f"{base}/{tid}", json={"title": "X"}, headers=bob),
        client.post(f"{base}/{tid}/archive", headers=bob),
        client.delete(f"{base}/{tid}/archive", headers=bob),
        client.delete(f"{base}/{tid}", headers=bob),
        client.delete(f"{base}/lists/{lid}", headers=bob),
    ]
    for req in writes:
        r = await req
        assert r.status == 403, await r.text()
    lists = await client._db.fetchall(
        "SELECT name FROM space_task_lists WHERE space_id='sp-s'"
    )
    tasks = await client._db.fetchall(
        "SELECT title, archived_at FROM space_tasks WHERE space_id='sp-s'"
    )
    assert [r["name"] for r in lists] == ["S"]
    assert [(r["title"], r["archived_at"]) for r in tasks] == [("TS", None)]


async def test_space_task_member_writes_are_allowed(client):
    """A plain ``member`` (not owner/admin) may write space tasks."""
    await _seed_space_with_member(client, "sp-m", client._uid, "owner")
    bob = await _add_user(client, "bob", "bob-id", "bob-tok")
    await client._db.enqueue(
        "INSERT INTO space_members(space_id, user_id, role) VALUES(?, ?, ?)",
        ("sp-m", "bob-id", "member"),
    )
    base = "/api/spaces/sp-m/tasks"
    r = await client.post(f"{base}/lists", json={"name": "L"}, headers=bob)
    assert r.status == 201
    lid = (await r.json())["id"]
    r = await client.post(f"{base}/lists/{lid}/tasks", json={"title": "T"}, headers=bob)
    assert r.status == 201
    tid = (await r.json())["id"]
    r = await client.patch(f"{base}/{tid}", json={"title": "T2"}, headers=bob)
    assert r.status == 200
    assert (await r.json())["title"] == "T2"
    assert (await client.post(f"{base}/{tid}/archive", headers=bob)).status == 200
    assert (await client.delete(f"{base}/{tid}/archive", headers=bob)).status == 200
    assert (await client.delete(f"{base}/{tid}", headers=bob)).status == 200
    assert (await client.delete(f"{base}/lists/{lid}", headers=bob)).status == 200


async def test_space_task_patch_bad_status_is_422(client):
    """Domain ValueError still maps to 422 once the view try/except is gone."""
    await _seed_space_with_member(client, "sp-v", client._uid, "owner")
    h = _auth(client._tok)
    r = await client.post("/api/spaces/sp-v/tasks/lists", json={"name": "L"}, headers=h)
    lid = (await r.json())["id"]
    r = await client.post(
        f"/api/spaces/sp-v/tasks/lists/{lid}/tasks", json={"title": "T"}, headers=h
    )
    tid = (await r.json())["id"]
    r = await client.patch(
        f"/api/spaces/sp-v/tasks/{tid}", json={"status": "bogus"}, headers=h
    )
    assert r.status == 422


async def test_space_task_non_member_assignee_is_422_and_never_notified(client):
    """A member must not leak a task's title to a non-member by
    assigning it to them (TaskAssigned → notification + WS frame)."""
    admin = _auth(client._tok)
    await _seed_space_with_member(client, "sp-b", client._uid, "owner")
    await _add_user(client, "eve", "eve-id", "eve-tok")  # not a member
    r = await client.post(
        "/api/spaces/sp-b/tasks/lists", json={"name": "B"}, headers=admin
    )
    lid = (await r.json())["id"]
    r = await client.post(
        f"/api/spaces/sp-b/tasks/lists/{lid}/tasks",
        json={"title": "SECRET", "assignees": ["eve-id"]},
        headers=admin,
    )
    assert r.status == 422
    r = await client.post(
        f"/api/spaces/sp-b/tasks/lists/{lid}/tasks",
        json={"title": "SECRET"},
        headers=admin,
    )
    tid = (await r.json())["id"]
    r = await client.patch(
        f"/api/spaces/sp-b/tasks/{tid}",
        json={"assignees": ["eve-id"]},
        headers=admin,
    )
    assert r.status == 422
    r = await client.patch(
        f"/api/spaces/sp-b/tasks/{tid}",
        json={"assignees": "eve-id"},
        headers=admin,
    )
    assert r.status == 422
    rows = await client._db.fetchall(
        "SELECT title FROM notifications WHERE user_id='eve-id'"
    )
    assert rows == []
    task = await client._db.fetchone(
        "SELECT assignees_json FROM space_tasks WHERE id=?", (tid,)
    )
    assert "eve-id" not in task["assignees_json"]


async def test_space_task_writes_in_an_archived_space_are_403(client):
    h = _auth(client._tok)
    await _seed_space_with_member(client, "sp-ar", client._uid, "owner")
    r = await client.post(
        "/api/spaces/sp-ar/tasks/lists", json={"name": "L"}, headers=h
    )
    lid = (await r.json())["id"]
    r = await client.post(
        f"/api/spaces/sp-ar/tasks/lists/{lid}/tasks", json={"title": "T"}, headers=h
    )
    tid = (await r.json())["id"]
    await client._db.enqueue("UPDATE spaces SET archived=1 WHERE id='sp-ar'")
    base = "/api/spaces/sp-ar/tasks"
    writes = [
        client.post(f"{base}/lists", json={"name": "X"}, headers=h),
        client.patch(f"{base}/lists/{lid}", json={"name": "X"}, headers=h),
        client.post(f"{base}/lists/{lid}/tasks", json={"title": "X"}, headers=h),
        client.patch(f"{base}/{tid}", json={"title": "X"}, headers=h),
        client.post(f"{base}/{tid}/archive", headers=h),
        client.delete(f"{base}/{tid}/archive", headers=h),
        client.delete(f"{base}/{tid}", headers=h),
        client.delete(f"{base}/lists/{lid}", headers=h),
    ]
    for req in writes:
        r = await req
        assert r.status == 403, await r.text()
    # Archive is read-only, not hidden.
    assert (await client.get(f"{base}/lists", headers=h)).status == 200
    assert (await client.get(f"{base}/lists/{lid}/tasks", headers=h)).status == 200
    row = await client._db.fetchone("SELECT title FROM space_tasks WHERE id=?", (tid,))
    assert row["title"] == "T"
