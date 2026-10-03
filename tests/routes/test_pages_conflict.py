"""Route-level tests for /api/spaces/{id}/pages/{pid}/resolve-conflict (§4.4.4.1)."""

from __future__ import annotations

from dataclasses import replace


from socialhome.app_keys import (
    event_bus_key,
    federation_service_key,
    page_conflict_service_key,
    page_repo_key,
)
from socialhome.domain.events import PageUpdated
from socialhome.domain.page_version import version_hash
from socialhome.repositories.page_repo import new_page

from .conftest import _auth


async def _seed_space(client, *, member: bool = True) -> None:
    """A space this household hosts (v_48: it sequences the pages)."""
    db = client._db
    own = client.app[federation_service_key]._own_instance_id
    await db.enqueue(
        "INSERT INTO spaces(id, name, owner_instance_id, owner_username, "
        "identity_public_key, space_type) "
        "VALUES('sp-1', 'test', ?, 'admin', ?, 'household')",
        (own, "aa" * 32),
    )
    if member:
        await db.enqueue(
            "INSERT INTO space_members(space_id, user_id, role)"
            " VALUES('sp-1', ?, 'owner')",
            (client._uid,),
        )


async def _seed_conflict(client, *, member: bool = True):
    """Seed a page in sp-1 with an open conflict between "mine-version"
    (displayed) and "theirs-version" by u2."""
    await _seed_space(client, member=member)
    repo = client.app[page_repo_key]
    page = replace(
        new_page(
            title="t", content="mine-version", created_by=client._uid, space_id="sp-1"
        ),
        seq=1,
    )
    await repo.save(page, space_id=page.space_id)
    for body, by, side in (
        ("mine-version", client._uid, "mine"),
        ("theirs-version", "u2", "theirs"),
    ):
        await repo.insert_snapshot(
            page_id=page.id,
            space_id="sp-1",
            body=body,
            title="t",
            author_user_id=by,
            side=side,
            conflict=True,
        )
    return page


async def test_resolve_conflict_mine(client):
    page = await _seed_conflict(client)
    r = await client.post(
        f"/api/spaces/sp-1/pages/{page.id}/resolve-conflict",
        json={"resolution": "mine"},
        headers=_auth(client._tok),
    )
    assert r.status == 200
    data = await r.json()
    assert data["ok"] is True
    assert data["content"] == "mine-version"


async def test_resolve_conflict_theirs(client):
    page = await _seed_conflict(client)
    r = await client.post(
        f"/api/spaces/sp-1/pages/{page.id}/resolve-conflict",
        json={"resolution": "theirs"},
        headers=_auth(client._tok),
    )
    assert r.status == 200
    assert (await r.json())["content"] == "theirs-version"


async def test_resolve_conflict_merged_applies_body(client):
    page = await _seed_conflict(client)
    r = await client.post(
        f"/api/spaces/sp-1/pages/{page.id}/resolve-conflict",
        json={"resolution": "merged_content", "content": "joined!"},
        headers=_auth(client._tok),
    )
    assert r.status == 200
    assert (await r.json())["content"] == "joined!"


async def test_resolve_conflict_missing_content_422(client):
    page = await _seed_conflict(client)
    r = await client.post(
        f"/api/spaces/sp-1/pages/{page.id}/resolve-conflict",
        json={"resolution": "merged_content"},
        headers=_auth(client._tok),
    )
    assert r.status == 422


async def test_resolve_conflict_unknown_resolution_422(client):
    page = await _seed_conflict(client)
    r = await client.post(
        f"/api/spaces/sp-1/pages/{page.id}/resolve-conflict",
        json={"resolution": "nope"},
        headers=_auth(client._tok),
    )
    assert r.status == 422


async def test_resolve_conflict_no_active_conflict_409(client):
    # Seed a page but never record a conflict.
    await _seed_space(client)
    page = new_page(title="t", content="c", created_by=client._uid, space_id="sp-1")
    await client.app[page_repo_key].save(page, space_id="sp-1")
    r = await client.post(
        f"/api/spaces/sp-1/pages/{page.id}/resolve-conflict",
        json={"resolution": "mine"},
        headers=_auth(client._tok),
    )
    assert r.status == 409


async def test_resolve_conflict_bad_json_400(client):
    await _seed_space(client)
    r = await client.post(
        "/api/spaces/sp-1/pages/ghost/resolve-conflict",
        data="not-json",
        headers={**_auth(client._tok), "Content-Type": "application/json"},
    )
    assert r.status == 400


async def test_a_subscriber_cannot_write_space_pages(client):
    """Subscribers read a space; they never create, edit, delete pages or
    settle a page conflict."""
    page = await _seed_conflict(client)
    await client._db.enqueue(
        "UPDATE space_members SET role='subscriber' WHERE space_id='sp-1'"
        " AND user_id=?",
        (client._uid,),
    )
    base = "/api/spaces/sp-1/pages"
    headers = _auth(client._tok)
    attempts = [
        client.post(base, json={"title": "t", "content": "c"}, headers=headers),
        client.patch(f"{base}/{page.id}", json={"content": "x"}, headers=headers),
        client.delete(f"{base}/{page.id}", headers=headers),
        client.post(
            f"{base}/{page.id}/resolve-conflict",
            json={"resolution": "theirs"},
            headers=headers,
        ),
    ]
    for attempt in attempts:
        assert (await attempt).status == 403
    current = await client.app[page_repo_key].get_space_page(page.id, space_id="sp-1")
    assert current.content == "mine-version"
    # Reading stays open.
    r = await client.get(f"{base}/{page.id}", headers=headers)
    assert r.status == 200


async def test_resolve_conflict_requires_space_membership(client):
    """A signed-in user who is not in the space cannot settle its conflict."""
    page = await _seed_conflict(client, member=False)
    r = await client.post(
        f"/api/spaces/sp-1/pages/{page.id}/resolve-conflict",
        json={"resolution": "theirs"},
        headers=_auth(client._tok),
    )
    assert r.status == 403
    current = await client.app[page_repo_key].get_space_page(page.id, space_id="sp-1")
    assert current.content == "mine-version"


# ─── v_48: conflict surfaced on reads, edits refused, N-side resolve ──


async def test_get_shows_the_conflict_and_the_list_flags_it(client):
    page = await _seed_conflict(client)
    r = await client.get(
        f"/api/spaces/sp-1/pages/{page.id}", headers=_auth(client._tok)
    )
    assert r.status == 200
    data = await r.json()
    assert data["in_conflict"] is True
    conflict = data["conflict"]
    assert conflict["current_hash"] == version_hash("t", "mine-version")
    assert [(s["content"], s["by"]) for s in conflict["sides"]] == [
        ("mine-version", client._uid),
        ("theirs-version", "u2"),
    ]
    assert all(s["hash"].startswith("sha256:") and s["at"] for s in conflict["sides"])
    r = await client.get("/api/spaces/sp-1/pages", headers=_auth(client._tok))
    assert [(p["id"], p["in_conflict"]) for p in await r.json()] == [(page.id, True)]


async def test_get_without_a_conflict(client):
    await _seed_space(client)
    page = new_page(title="t", content="c", created_by=client._uid, space_id="sp-1")
    await client.app[page_repo_key].save(page, space_id="sp-1")
    r = await client.get(
        f"/api/spaces/sp-1/pages/{page.id}", headers=_auth(client._tok)
    )
    data = await r.json()
    assert (data["in_conflict"], data["conflict"]) == (False, None)


async def test_patch_while_conflicted_is_allowed(client):
    """v_48: a conflict never blocks an edit — the sides stay until resolved."""
    page = await _seed_conflict(client)
    r = await client.patch(
        f"/api/spaces/sp-1/pages/{page.id}",
        json={"content": "my edit"},
        headers=_auth(client._tok),
    )
    assert r.status == 200, await r.text()
    data = await r.json()
    assert data["content"] == "my edit"
    assert (data["seq"], data["base_seq"], data["pending"]) == (2, None, False)
    assert await client.app[page_conflict_service_key].has_active_conflict(
        page.id, space_id="sp-1"
    )


async def test_resolve_with_stale_sides_is_409(client):
    page = await _seed_conflict(client)
    r = await client.post(
        f"/api/spaces/sp-1/pages/{page.id}/resolve-conflict",
        json={
            "resolution": "side",
            "side": version_hash("t", "theirs-version"),
            "sides": [version_hash("t", "theirs-version")],
        },
        headers=_auth(client._tok),
    )
    assert r.status == 409
    err = (await r.json())["error"]
    assert err["code"] == "STALE"
    assert len(err["sides"]) == 2
    assert await client.app[page_conflict_service_key].has_active_conflict(
        page.id, space_id="sp-1"
    )


async def test_resolve_by_side_emits_the_sequenced_version(client):
    page = await _seed_conflict(client)
    seen: list = []

    async def _on(event):
        seen.append(event)

    client.app[event_bus_key].subscribe(PageUpdated, _on)
    sides = [version_hash("t", "mine-version"), version_hash("t", "theirs-version")]
    r = await client.post(
        f"/api/spaces/sp-1/pages/{page.id}/resolve-conflict",
        json={"resolution": "side", "side": sides[1], "sides": sides},
        headers=_auth(client._tok),
    )
    assert r.status == 200, await r.text()
    data = await r.json()
    assert data["content"] == "theirs-version"
    assert data["page"]["id"] == page.id
    (event,) = seen
    assert event.actor_user_id == client._uid
    # This household hosts the space: sequenced, every side retired.
    assert event.canonical["conflict"] == []
    assert event.canonical["seq"] == data["page"]["seq"]
    # The conflict is gone; edits work again.
    r = await client.patch(
        f"/api/spaces/sp-1/pages/{page.id}",
        json={"content": "after"},
        headers=_auth(client._tok),
    )
    assert r.status == 200


async def test_resolve_side_needs_a_side_and_sides_a_list(client):
    page = await _seed_conflict(client)
    for body in (
        {"resolution": "side"},
        {"resolution": "mine", "sides": "nope"},
        {"resolution": "mine", "sides": [1]},
    ):
        r = await client.post(
            f"/api/spaces/sp-1/pages/{page.id}/resolve-conflict",
            json=body,
            headers=_auth(client._tok),
        )
        assert r.status == 422, body
