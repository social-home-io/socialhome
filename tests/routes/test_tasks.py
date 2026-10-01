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


# ─── Priority, labels, null-clears, edit rights, reorder (v_40) ─────────


async def _household_list(client) -> str:
    r = await client.post(
        "/api/tasks/lists", json={"name": "L"}, headers=_auth(client._tok)
    )
    return (await r.json())["id"]


async def test_create_task_with_status_priority_labels(client):
    lid = await _household_list(client)
    r = await client.post(
        f"/api/tasks/lists/{lid}/tasks",
        json={
            "title": "Quick add",
            "status": "in_progress",
            "priority": "high",
            "labels": ["Car", "car", " Bills "],
        },
        headers=_auth(client._tok),
    )
    assert r.status == 201
    body = await r.json()
    assert body["status"] == "in_progress"
    assert body["priority"] == "high"
    assert body["labels"] == ["Car", "Bills"]
    r = await client.get(f"/api/tasks/lists/{lid}/tasks", headers=_auth(client._tok))
    (row,) = await r.json()
    assert row["priority"] == "high" and row["labels"] == ["Car", "Bills"]


async def test_task_without_priority_serialises_null_and_empty_labels(client):
    lid = await _household_list(client)
    r = await client.post(
        f"/api/tasks/lists/{lid}/tasks",
        json={"title": "Plain"},
        headers=_auth(client._tok),
    )
    body = await r.json()
    assert body["priority"] is None
    assert body["labels"] == []


async def test_patch_null_clears_due_date_description_priority(client):
    lid = await _household_list(client)
    r = await client.post(
        f"/api/tasks/lists/{lid}/tasks",
        json={
            "title": "T",
            "description": "d",
            "due_date": "2026-10-03",
            "priority": "low",
            "labels": ["x"],
        },
        headers=_auth(client._tok),
    )
    tid = (await r.json())["id"]
    r = await client.patch(
        f"/api/tasks/{tid}", json={"title": "T2"}, headers=_auth(client._tok)
    )
    body = await r.json()
    assert body["due_date"] == "2026-10-03" and body["priority"] == "low"
    r = await client.patch(
        f"/api/tasks/{tid}",
        json={"due_date": None, "description": None, "priority": None, "labels": []},
        headers=_auth(client._tok),
    )
    assert r.status == 200
    body = await r.json()
    assert body["due_date"] is None
    assert body["description"] is None
    assert body["priority"] is None
    assert body["labels"] == []


async def test_bad_priority_or_labels_is_422(client):
    lid = await _household_list(client)
    for payload in (
        {"title": "T", "priority": "critical"},
        {"title": "T", "labels": "x"},
        {"title": "T", "labels": [1]},
        {"title": "T", "status": "blocked"},
    ):
        r = await client.post(
            f"/api/tasks/lists/{lid}/tasks", json=payload, headers=_auth(client._tok)
        )
        assert r.status == 422, payload
    r = await client.post(
        f"/api/tasks/lists/{lid}/tasks", json={"title": "T"}, headers=_auth(client._tok)
    )
    tid = (await r.json())["id"]
    r = await client.patch(
        f"/api/tasks/{tid}", json={"priority": 7}, headers=_auth(client._tok)
    )
    assert r.status == 422


async def test_household_edit_rights_creator_assignee_admin(client):
    """The admin creates a task assigned to bob: bob (assignee) may edit
    it, carol (neither creator nor assignee nor admin) may not."""
    bob = await _add_user(client, "bob", "bob-id", "bob-tok")
    carol = await _add_user(client, "carol", "carol-id", "carol-tok")
    lid = await _household_list(client)
    r = await client.post(
        f"/api/tasks/lists/{lid}/tasks",
        json={"title": "T", "assignees": ["bob-id"]},
        headers=_auth(client._tok),
    )
    assert r.status == 201
    tid = (await r.json())["id"]
    r = await client.patch(f"/api/tasks/{tid}", json={"status": "done"}, headers=bob)
    assert r.status == 200
    r = await client.patch(f"/api/tasks/{tid}", json={"title": "x"}, headers=carol)
    assert r.status == 403
    # carol's own task: the admin may still edit it.
    r = await client.post(
        f"/api/tasks/lists/{lid}/tasks", json={"title": "C"}, headers=carol
    )
    cid = (await r.json())["id"]
    r = await client.patch(
        f"/api/tasks/{cid}", json={"title": "C2"}, headers=_auth(client._tok)
    )
    assert r.status == 200


async def test_household_unknown_assignee_is_422(client):
    lid = await _household_list(client)
    r = await client.post(
        f"/api/tasks/lists/{lid}/tasks",
        json={"title": "T", "assignees": ["nobody"]},
        headers=_auth(client._tok),
    )
    assert r.status == 422


async def test_household_reorder_checks_rights_on_the_dragged_card(client):
    """bob may drag his own card among the admin's (200), not the admin's
    card (403); a reorder without a ``moved_id`` from the order is 422."""
    bob = await _add_user(client, "bob", "bob-id", "bob-tok")
    lid = await _household_list(client)
    ids = []
    for title in ("A", "B"):
        r = await client.post(
            f"/api/tasks/lists/{lid}/tasks",
            json={"title": title},
            headers=_auth(client._tok),
        )
        ids.append((await r.json())["id"])
    r = await client.post(
        f"/api/tasks/lists/{lid}/tasks", json={"title": "Bob's"}, headers=bob
    )
    mine = (await r.json())["id"]
    order = [mine, *ids]
    r = await client.post(
        f"/api/tasks/lists/{lid}/reorder",
        json={"order": order, "moved_id": ids[0]},
        headers=bob,
    )
    assert r.status == 403
    r = await client.post(
        f"/api/tasks/lists/{lid}/reorder",
        json={"order": order, "moved_id": mine},
        headers=bob,
    )
    assert r.status == 200
    assert (await r.json())["count"] == 3
    r = await client.get(f"/api/tasks/lists/{lid}/tasks", headers=bob)
    assert [t["id"] for t in await r.json()] == order
    r = await client.post(
        f"/api/tasks/lists/{lid}/reorder", json={"order": order}, headers=bob
    )
    assert r.status == 422


async def test_household_delete_needs_edit_rights(client):
    bob = await _add_user(client, "bob", "bob-id", "bob-tok")
    lid = await _household_list(client)
    r = await client.post(
        f"/api/tasks/lists/{lid}/tasks",
        json={"title": "T"},
        headers=_auth(client._tok),
    )
    tid = (await r.json())["id"]
    assert (await client.delete(f"/api/tasks/{tid}", headers=bob)).status == 403
    r = await client.delete(f"/api/tasks/{tid}", headers=_auth(client._tok))
    assert r.status == 200


async def test_space_reorder_moves_positions(client):
    await _seed_space_with_member(client, "sp-r", client._uid, "owner")
    admin = _auth(client._tok)
    base = "/api/spaces/sp-r/tasks"
    r = await client.post(f"{base}/lists", json={"name": "L"}, headers=admin)
    lid = (await r.json())["id"]
    ids = []
    for title in ("A", "B", "C"):
        r = await client.post(
            f"{base}/lists/{lid}/tasks",
            json={"title": title, "priority": "medium", "labels": ["x"]},
            headers=admin,
        )
        body = await r.json()
        assert body["priority"] == "medium" and body["labels"] == ["x"]
        ids.append(body["id"])
    r = await client.post(
        f"{base}/lists/{lid}/reorder",
        json={"order": ids[::-1], "moved_id": ids[2]},
        headers=admin,
    )
    assert r.status == 200
    assert (await r.json())["count"] == 2  # B keeps position 1
    r = await client.get(f"{base}/lists/{lid}/tasks", headers=admin)
    assert [t["id"] for t in await r.json()] == ids[::-1]
    r = await client.post(
        f"{base}/lists/{lid}/reorder", json={"order": "nope"}, headers=admin
    )
    assert r.status == 422


async def test_space_reorder_subscriber_is_403(client):
    bob, lid, tid = await _subscriber_env(client)
    r = await client.post(
        f"/api/spaces/sp-s/tasks/lists/{lid}/reorder",
        json={"order": [tid], "moved_id": tid},
        headers=bob,
    )
    assert r.status == 403


async def test_space_reorder_list_from_another_space_is_404(client):
    bob, lid, tid = await _cross_space_env(client)
    before = await _space_b_state(client)
    r = await client.post(
        f"/api/spaces/sp-a/tasks/lists/{lid}/reorder",
        json={"order": [tid], "moved_id": tid},
        headers=bob,
    )
    assert r.status == 404
    assert await _space_b_state(client) == before


async def test_space_patch_null_clears_due_date(client):
    await _seed_space_with_member(client, "sp-n", client._uid, "owner")
    admin = _auth(client._tok)
    base = "/api/spaces/sp-n/tasks"
    r = await client.post(f"{base}/lists", json={"name": "L"}, headers=admin)
    lid = (await r.json())["id"]
    r = await client.post(
        f"{base}/lists/{lid}/tasks",
        json={"title": "T", "due_date": "2026-10-03", "status": "done"},
        headers=admin,
    )
    body = await r.json()
    assert body["status"] == "done" and body["due_date"] == "2026-10-03"
    r = await client.patch(
        f"{base}/{body['id']}", json={"due_date": None}, headers=admin
    )
    assert r.status == 200
    assert (await r.json())["due_date"] is None


# ─── Adversarial-review regressions (routes) ────────────────────────────


async def test_reorder_cannot_swap_others_cards_via_own_moved_id(client):
    """I3: a non-editor naming his own card as ``moved_id`` while swapping
    the admin's cards is 403; duplicates are 422 (both scopes)."""
    bob = await _add_user(client, "bob", "bob-id", "bob-tok")
    lid = await _household_list(client)
    ids = []
    for title in ("A1", "A2"):
        r = await client.post(
            f"/api/tasks/lists/{lid}/tasks",
            json={"title": title},
            headers=_auth(client._tok),
        )
        ids.append((await r.json())["id"])
    r = await client.post(
        f"/api/tasks/lists/{lid}/tasks", json={"title": "B"}, headers=bob
    )
    mine = (await r.json())["id"]
    r = await client.post(
        f"/api/tasks/lists/{lid}/reorder",
        json={"order": [mine, ids[1], ids[0]], "moved_id": mine},
        headers=bob,
    )
    assert r.status == 403
    r = await client.post(
        f"/api/tasks/lists/{lid}/reorder",
        json={"order": [mine, mine], "moved_id": mine},
        headers=bob,
    )
    assert r.status == 422


async def test_space_reorder_duplicates_are_422(client):
    await _seed_space_with_member(client, "sp-d", client._uid, "owner")
    admin = _auth(client._tok)
    base = "/api/spaces/sp-d/tasks"
    r = await client.post(f"{base}/lists", json={"name": "L"}, headers=admin)
    lid = (await r.json())["id"]
    r = await client.post(
        f"{base}/lists/{lid}/tasks", json={"title": "A"}, headers=admin
    )
    tid = (await r.json())["id"]
    r = await client.post(
        f"{base}/lists/{lid}/reorder",
        json={"order": [tid, tid], "moved_id": tid},
        headers=admin,
    )
    assert r.status == 422


async def test_patch_position_infinity_or_huge_is_422(client):
    """M3: used to raise OverflowError (500)."""
    lid = await _household_list(client)
    r = await client.post(
        f"/api/tasks/lists/{lid}/tasks", json={"title": "T"}, headers=_auth(client._tok)
    )
    tid = (await r.json())["id"]
    for raw in ('{"position": Infinity}', '{"position": 1e30}'):
        r = await client.patch(
            f"/api/tasks/{tid}",
            data=raw,
            headers={**_auth(client._tok), "Content-Type": "application/json"},
        )
        assert r.status == 422, raw


async def test_overlong_or_invisible_text_is_422(client):
    """M4: REST text caps."""
    lid = await _household_list(client)
    for body in (
        {"title": "T" * 201},
        {"title": "​‮"},
        {"title": "T", "description": "d" * 5001},
    ):
        r = await client.post(
            f"/api/tasks/lists/{lid}/tasks", json=body, headers=_auth(client._tok)
        )
        assert r.status == 422, body
    r = await client.post(
        "/api/tasks/lists", json={"name": "N" * 101}, headers=_auth(client._tok)
    )
    assert r.status == 422
