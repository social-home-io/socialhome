"""Route-level tests for /api/spaces/{id}/pages/{pid}/resolve-conflict (§4.4.4.1)."""

from __future__ import annotations


from social_home.app_keys import (
    page_conflict_service_key,
    page_repo_key,
)
from social_home.repositories.page_repo import new_page

from .conftest import _auth


async def _seed_conflict(client):
    """Seed a page in sp-1 with an active conflict."""
    db = client._db
    await db.enqueue(
        "INSERT INTO spaces(id, name, owner_instance_id, owner_username, "
        "identity_public_key, space_type) "
        "VALUES('sp-1', 'test', 'iid', 'admin', ?, 'household')",
        ("aa" * 32,),
    )
    repo = client.app[page_repo_key]
    page = new_page(
        title="t", content="mine-version", created_by=client._uid, space_id="sp-1"
    )
    await repo.save(page)
    conflict_svc = client.app[page_conflict_service_key]
    await conflict_svc.record_base(
        page_id=page.id,
        space_id="sp-1",
        body="original",
        author_user_id=client._uid,
    )
    await conflict_svc.merge_remote_body(
        page_id=page.id,
        space_id="sp-1",
        remote_body="theirs-version",
        remote_author_user_id="u2",
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
    db = client._db
    await db.enqueue(
        "INSERT INTO spaces(id, name, owner_instance_id, owner_username, "
        "identity_public_key, space_type) "
        "VALUES('sp-1', 'test', 'iid', 'admin', ?, 'household')",
        ("aa" * 32,),
    )
    page = new_page(title="t", content="c", created_by=client._uid, space_id="sp-1")
    await client.app[page_repo_key].save(page)
    r = await client.post(
        f"/api/spaces/sp-1/pages/{page.id}/resolve-conflict",
        json={"resolution": "mine"},
        headers=_auth(client._tok),
    )
    assert r.status == 409


async def test_resolve_conflict_bad_json_400(client):
    r = await client.post(
        "/api/spaces/sp-1/pages/ghost/resolve-conflict",
        data="not-json",
        headers={**_auth(client._tok), "Content-Type": "application/json"},
    )
    assert r.status == 400
