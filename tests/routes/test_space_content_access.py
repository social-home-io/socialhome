"""§4.3 feature access levels on the REST surface: under ``ADMIN_ONLY`` a
member or moderator gets ``403 ACCESS_ADMIN_ONLY`` (with the feature) for
every write to posts, pages, tasks, stickies and the calendar, nothing
changes, and the owner / admins still write. Comments, reactions and RSVPs
are never gated."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from socialhome.auth import sha256_token_hash

from .conftest import _auth

SID = "sp-ao"


async def _user(client, username: str, user_id: str) -> dict:
    token = f"{username}-tok"
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


@pytest.fixture
async def space(client):
    """``sp-ao`` with every feature on: the test client's admin user owns it;
    plus an admin, a moderator and a member."""
    row = await client._db.fetchone("SELECT instance_id FROM instance_identity")
    await client._db.enqueue(
        "INSERT INTO spaces(id, name, owner_instance_id, owner_username,"
        " identity_public_key, feature_pages, feature_stickies,"
        " feature_calendar, feature_todo) VALUES(?, 'AO', ?, 'admin', ?, 1, 1, 1, 1)",
        (SID, row["instance_id"], "ab" * 32),
    )
    heads = {"owner": _auth(client._tok)}
    await client._db.enqueue(
        "INSERT INTO space_members(space_id, user_id, role) VALUES(?, ?, 'owner')",
        (SID, client._uid),
    )
    for name, role in (("adm", "admin"), ("mod", "moderator"), ("mem", "member")):
        heads[name] = await _user(client, name, f"uid-{name}")
        await client._db.enqueue(
            "INSERT INTO space_members(space_id, user_id, role) VALUES(?, ?, ?)",
            (SID, f"uid-{name}", role),
        )
    return heads


async def _set_access(client, feature: str, level: str) -> None:
    await client._db.enqueue(
        f"UPDATE spaces SET {feature}_access=? WHERE id=?", (level, SID)
    )


async def _snapshot(client) -> dict:
    out = {}
    for table in (
        "space_posts",
        "space_pages",
        "space_task_lists",
        "space_tasks",
        "stickies",
        "space_calendar_events",
    ):
        rows = await client._db.fetchall(f"SELECT * FROM {table} ORDER BY id")
        out[table] = [tuple(r) for r in rows]
    return out


async def _seed_content(client, space) -> dict:
    """One item per feature, made by the member while everything is OPEN."""
    mem = space["mem"]
    ids = {}
    r = await client.post(
        f"/api/spaces/{SID}/posts", json={"type": "text", "content": "p"}, headers=mem
    )
    assert r.status in (200, 201), await r.text()
    ids["post"] = (await r.json())["id"]
    r = await client.post(
        f"/api/spaces/{SID}/pages", json={"title": "T", "content": "c"}, headers=mem
    )
    assert r.status == 201, await r.text()
    ids["page"] = (await r.json())["id"]
    r = await client.post(
        f"/api/spaces/{SID}/tasks/lists", json={"name": "L"}, headers=mem
    )
    assert r.status == 201, await r.text()
    ids["list"] = (await r.json())["id"]
    r = await client.post(
        f"/api/spaces/{SID}/tasks/lists/{ids['list']}/tasks",
        json={"title": "T"},
        headers=mem,
    )
    assert r.status == 201, await r.text()
    ids["task"] = (await r.json())["id"]
    r = await client.post(
        f"/api/spaces/{SID}/stickies", json={"content": "s"}, headers=mem
    )
    assert r.status == 201, await r.text()
    ids["sticky"] = (await r.json())["id"]
    start = datetime.now(timezone.utc) + timedelta(days=1)
    r = await client.post(
        f"/api/spaces/{SID}/calendar/events",
        json={
            "summary": "E",
            "start": start.isoformat(),
            "end": (start + timedelta(hours=1)).isoformat(),
        },
        headers=mem,
    )
    assert r.status == 201, await r.text()
    ids["event"] = (await r.json())["id"]
    return ids


def _writes(ids: dict) -> list[tuple[str, str, str, dict | None]]:
    """(feature, method, path, body) for every gated write."""
    start = datetime.now(timezone.utc) + timedelta(days=2)
    ev = {
        "summary": "N",
        "start": start.isoformat(),
        "end": (start + timedelta(hours=1)).isoformat(),
    }
    lst, task = ids["list"], ids["task"]
    return [
        ("posts", "post", f"/api/spaces/{SID}/posts", {"type": "text", "content": "x"}),
        ("posts", "patch", f"/api/spaces/{SID}/posts/{ids['post']}", {"content": "x"}),
        ("posts", "delete", f"/api/spaces/{SID}/posts/{ids['post']}", None),
        ("pages", "post", f"/api/spaces/{SID}/pages", {"title": "N"}),
        ("pages", "patch", f"/api/spaces/{SID}/pages/{ids['page']}", {"content": "x"}),
        ("pages", "delete", f"/api/spaces/{SID}/pages/{ids['page']}", None),
        (
            "pages",
            "post",
            f"/api/spaces/{SID}/pages/{ids['page']}/resolve-conflict",
            {"resolution": "mine"},
        ),
        ("tasks", "post", f"/api/spaces/{SID}/tasks/lists", {"name": "N"}),
        ("tasks", "patch", f"/api/spaces/{SID}/tasks/lists/{lst}", {"name": "N"}),
        ("tasks", "delete", f"/api/spaces/{SID}/tasks/lists/{lst}", None),
        ("tasks", "post", f"/api/spaces/{SID}/tasks/lists/{lst}/tasks", {"title": "N"}),
        ("tasks", "patch", f"/api/spaces/{SID}/tasks/{task}", {"status": "done"}),
        ("tasks", "patch", f"/api/spaces/{SID}/tasks/{task}", {"position": 3}),
        (
            "tasks",
            "post",
            f"/api/spaces/{SID}/tasks/lists/{lst}/reorder",
            {"order": [task], "moved_id": task},
        ),
        ("tasks", "post", f"/api/spaces/{SID}/tasks/{task}/archive", None),
        ("tasks", "delete", f"/api/spaces/{SID}/tasks/{task}", None),
        ("stickies", "post", f"/api/spaces/{SID}/stickies", {"content": "N"}),
        (
            "stickies",
            "patch",
            f"/api/spaces/{SID}/stickies/{ids['sticky']}",
            {"content": "N"},
        ),
        (
            "stickies",
            "patch",
            f"/api/spaces/{SID}/stickies/{ids['sticky']}",
            {"position_x": 40},
        ),
        ("stickies", "delete", f"/api/spaces/{SID}/stickies/{ids['sticky']}", None),
        ("calendar", "post", f"/api/spaces/{SID}/calendar/events", ev),
        (
            "calendar",
            "patch",
            f"/api/spaces/{SID}/calendar/events/{ids['event']}",
            {"summary": "N"},
        ),
        (
            "calendar",
            "delete",
            f"/api/spaces/{SID}/calendar/events/{ids['event']}",
            None,
        ),
    ]


async def _send(client, method, path, body, headers):
    kw = {"headers": headers}
    if body is not None:
        kw["json"] = body
    return await getattr(client, method)(path, **kw)


@pytest.mark.parametrize("who", ["mem", "mod"])
@pytest.mark.parametrize("feature", ["posts", "pages", "tasks", "stickies", "calendar"])
async def test_admin_only_writes_are_403_for_members_and_moderators(
    client, space, who, feature
):
    ids = await _seed_content(client, space)
    await _set_access(client, feature, "admin_only")
    before = await _snapshot(client)
    for feat, method, path, body in _writes(ids):
        if feat != feature:
            continue
        r = await _send(client, method, path, body, space[who])
        assert r.status == 403, (method, path, await r.text())
        err = (await r.json())["error"]
        assert err["code"] == "ACCESS_ADMIN_ONLY", (method, path, err)
        assert err["feature"] == feature
    assert await _snapshot(client) == before


@pytest.mark.parametrize("who", ["owner", "adm"])
@pytest.mark.parametrize("feature", ["posts", "pages", "tasks", "stickies", "calendar"])
async def test_admin_only_writes_pass_for_the_owner_and_admins(
    client, space, who, feature
):
    ids = await _seed_content(client, space)
    await _set_access(client, feature, "admin_only")
    writes = [
        w
        for w in _writes(ids)
        # No open conflict to resolve: that answers 409, not the gate.
        if w[0] == feature and "resolve-conflict" not in w[2]
    ]
    # Deletes last (a task list after its tasks), so every write finds its row.
    writes.sort(key=lambda w: (w[1] == "delete", "/lists/" in w[2]))
    for _feat, method, path, body in writes:
        r = await _send(client, method, path, body, space[who])
        assert r.status in (200, 201, 204), (method, path, await r.text())


async def test_other_features_stay_open(client, space):
    """One feature's level never leaks into another's writes."""
    await _set_access(client, "tasks", "admin_only")
    r = await client.post(
        f"/api/spaces/{SID}/pages", json={"title": "Fine"}, headers=space["mem"]
    )
    assert r.status == 201


async def test_comments_reactions_and_rsvps_are_never_gated(client, space):
    ids = await _seed_content(client, space)
    for feature in ("posts", "calendar"):
        await _set_access(client, feature, "admin_only")
    mem = space["mem"]
    r = await client.post(
        f"/api/spaces/{SID}/posts/{ids['post']}/comments",
        json={"content": "nice"},
        headers=mem,
    )
    assert r.status in (200, 201), await r.text()
    r = await client.post(
        f"/api/spaces/{SID}/posts/{ids['post']}/reactions",
        json={"emoji": "👍"},
        headers=mem,
    )
    assert r.status in (200, 201), await r.text()
    r = await client.post(
        f"/api/calendars/events/{ids['event']}/rsvp",
        json={"status": "going"},
        headers=mem,
    )
    assert r.status in (200, 201), await r.text()


@pytest.mark.parametrize(
    ("level", "who", "reason"),
    [
        ("admin_only", "mem", "admin_only"),
        ("moderated", "mem", "queued"),
        ("admin_only", "adm", None),
    ],
)
async def test_a_dropped_announcement_is_reported_to_its_creator(
    client, space, level, who, reason
):
    """The event saves, but its feed card is dropped when the posts level
    keeps the creator from posting — and the response says so (and why).
    Under MODERATED posts the card waits in the review queue instead."""
    await _set_access(client, "posts", level)
    start = datetime.now(timezone.utc) + timedelta(days=1)
    r = await client.post(
        f"/api/spaces/{SID}/calendar/events",
        json={
            "summary": "Picnic",
            "start": start.isoformat(),
            "end": (start + timedelta(hours=1)).isoformat(),
            "announce_in_feed": True,
        },
        headers=space[who],
    )
    assert r.status == 201, await r.text()
    body = await r.json()
    if reason is None:
        assert "announce_suppressed" not in body
        assert body["announce_in_feed"] is True
    elif reason == "queued":
        assert body["announce_queued"] is True and body["announce_item_id"]
        assert "announce_suppressed" not in body
        assert body["announce_in_feed"] is False
    else:
        assert body["announce_suppressed"] is True
        assert body["announce_suppressed_reason"] == reason
        assert body["announce_in_feed"] is False


async def test_the_event_detail_says_whether_the_viewer_may_edit(client, space):
    ids = await _seed_content(client, space)
    url = f"/api/calendars/events/{ids['event']}"
    assert (await (await client.get(url, headers=space["mem"])).json())["can_edit"]
    await _set_access(client, "calendar", "admin_only")
    assert not (await (await client.get(url, headers=space["mem"])).json())["can_edit"]
    assert (await (await client.get(url, headers=space["adm"])).json())["can_edit"]
