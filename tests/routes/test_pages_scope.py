"""Page scope + write-gate tests for socialhome.routes.pages.

The household ``/api/pages[/{id}…]`` routes only act on household pages
— a space page id there is 404 and its row is untouched, even for a
member of that space. The space routes act only on the PATH space's
pages (an id from another space, or a household page, is 404), refuse
non-members (403), read-only subscribers (403 on writes) and archived
spaces (403 on writes), and a space-page delete federates to the
space's member households.
"""

from unittest.mock import AsyncMock, patch

from socialhome.app_keys import federation_service_key
from socialhome.auth import sha256_token_hash
from socialhome.domain.federation import FederationEventType
from socialhome.federation.federation_service import FederationService

from .conftest import _auth


async def _seed_space(client, sid: str, user_id: str, role: str) -> None:
    await client._db.enqueue(
        "INSERT OR IGNORE INTO spaces(id, name, owner_instance_id, owner_username,"
        " identity_public_key, feature_pages) VALUES(?, ?, 'inst', 'admin', ?, 1)",
        (sid, sid, "ab" * 32),
    )
    await client._db.enqueue(
        "INSERT INTO space_members(space_id, user_id, role) VALUES(?, ?, ?)",
        (sid, user_id, role),
    )


async def _add_user(client, username: str, user_id: str) -> dict:
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


async def _create_space_page(client, sid: str, headers: dict) -> str:
    r = await client.post(
        f"/api/spaces/{sid}/pages",
        json={"title": f"{sid} wiki", "content": "secret"},
        headers=headers,
    )
    assert r.status == 201, await r.text()
    return (await r.json())["id"]


async def _space_page_rows(client) -> list[tuple]:
    rows = await client._db.fetchall(
        "SELECT id, space_id, title, content, locked_by, delete_requested_by,"
        " delete_approved_by FROM space_pages ORDER BY id"
    )
    return [tuple(r) for r in rows]


async def _history_count(client, page_id: str) -> int:
    row = await client._db.fetchone(
        "SELECT COUNT(*) AS n FROM page_edit_history WHERE page_id=?", (page_id,)
    )
    return int(row["n"])


# ─── Household routes never reach a space page ──────────────────────────


async def test_household_page_routes_on_space_page_are_404_and_untouched(client):
    admin = _auth(client._tok)
    # The caller is even a member of the space — the household surface
    # still must not act on (or reveal) a space page.
    await _seed_space(client, "sp-b", client._uid, "owner")
    pid = await _create_space_page(client, "sp-b", admin)
    # Seed one history row so /versions and /revert have something to leak.
    r = await client.patch(
        f"/api/spaces/sp-b/pages/{pid}", json={"content": "v2"}, headers=admin
    )
    assert r.status == 200
    before = await _space_page_rows(client)
    history_before = await _history_count(client, pid)
    base = f"/api/pages/{pid}"
    calls = [
        client.get(base, headers=admin),
        client.patch(base, json={"title": "pwned"}, headers=admin),
        client.get(f"{base}/lock", headers=admin),
        client.post(f"{base}/lock", headers=admin),
        client.post(f"{base}/lock/refresh", headers=admin),
        client.delete(f"{base}/lock", headers=admin),
        client.get(f"{base}/versions", headers=admin),
        client.post(f"{base}/revert", json={"version": 1}, headers=admin),
        client.post(f"{base}/delete-request", headers=admin),
        client.post(f"{base}/delete-approve", headers=admin),
        client.post(f"{base}/delete-cancel", headers=admin),
        client.delete(base, headers=admin),
    ]
    for req in calls:
        r = await req
        assert r.status == 404, (r.method, r.url, await r.text())
        assert "secret" not in await r.text()
        assert "v2" not in await r.text()
    assert await _space_page_rows(client) == before
    assert await _history_count(client, pid) == history_before


async def test_household_page_routes_still_work_for_household_pages(client):
    admin = _auth(client._tok)
    r = await client.post("/api/pages", json={"title": "Wiki"}, headers=admin)
    pid = (await r.json())["id"]
    assert (await client.get(f"/api/pages/{pid}", headers=admin)).status == 200
    assert (await client.post(f"/api/pages/{pid}/lock", headers=admin)).status == 200
    r = await client.get(f"/api/pages/{pid}/lock", headers=admin)
    assert (await r.json())["locked_by"] == client._uid
    r = await client.post(f"/api/pages/{pid}/lock/refresh", headers=admin)
    assert r.status == 204
    assert (await client.delete(f"/api/pages/{pid}/lock", headers=admin)).status == 200
    r = await client.post(f"/api/pages/{pid}/delete-request", headers=admin)
    assert r.status == 200
    r = await client.post(f"/api/pages/{pid}/delete-cancel", headers=admin)
    assert r.status == 200
    assert (await client.delete(f"/api/pages/{pid}", headers=admin)).status == 200
    assert (await client.get(f"/api/pages/{pid}", headers=admin)).status == 404


async def test_household_lock_routes_unknown_page_404(client):
    admin = _auth(client._tok)
    assert (await client.get("/api/pages/nope/lock", headers=admin)).status == 404
    assert (await client.delete("/api/pages/nope/lock", headers=admin)).status == 404
    assert (await client.get("/api/pages/nope/versions", headers=admin)).status == 404


# ─── Space routes: cross-space + household ids are 404 ──────────────────


async def _cross_space_env(client):
    """Admin owns ``sp-b`` (one page); bob is a member of ``sp-a`` only."""
    admin = _auth(client._tok)
    await _seed_space(client, "sp-b", client._uid, "owner")
    bob = await _add_user(client, "bob", "bob-id")
    await _seed_space(client, "sp-a", "bob-id", "member")
    pid = await _create_space_page(client, "sp-b", admin)
    return bob, pid


async def test_space_page_from_another_space_is_404_and_untouched(client):
    bob, pid = await _cross_space_env(client)
    before = await _space_page_rows(client)
    base = f"/api/spaces/sp-a/pages/{pid}"
    calls = [
        client.get(base, headers=bob),
        client.patch(base, json={"title": "pwned"}, headers=bob),
        client.post(
            f"{base}/resolve-conflict",
            json={"resolution": "merged_content", "content": "pwned"},
            headers=bob,
        ),
        client.delete(base, headers=bob),
    ]
    for req in calls:
        r = await req
        assert r.status == 404, (r.method, r.url, await r.text())
    assert await _space_page_rows(client) == before


async def test_household_page_under_space_path_is_404(client):
    admin = _auth(client._tok)
    await _seed_space(client, "sp-b", client._uid, "owner")
    r = await client.post("/api/pages", json={"title": "Home"}, headers=admin)
    pid = (await r.json())["id"]
    base = f"/api/spaces/sp-b/pages/{pid}"
    assert (await client.get(base, headers=admin)).status == 404
    r = await client.patch(base, json={"title": "X"}, headers=admin)
    assert r.status == 404
    assert (await client.delete(base, headers=admin)).status == 404
    r = await client.get(f"/api/pages/{pid}", headers=admin)
    assert (await r.json())["title"] == "Home"


async def test_space_pages_non_member_403(client):
    bob, pid = await _cross_space_env(client)
    r = await client.get("/api/spaces/sp-b/pages", headers=bob)
    assert r.status == 403
    r = await client.get(f"/api/spaces/sp-b/pages/{pid}", headers=bob)
    assert r.status == 403
    assert "secret" not in await r.text()


# ─── Subscribers read, never write ───────────────────────────────────────


async def test_space_page_subscriber_reads_but_never_writes(client):
    admin = _auth(client._tok)
    await _seed_space(client, "sp-b", client._uid, "owner")
    carol = await _add_user(client, "carol", "carol-id")
    await _seed_space(client, "sp-b", "carol-id", "subscriber")
    pid = await _create_space_page(client, "sp-b", admin)
    assert (await client.get("/api/spaces/sp-b/pages", headers=carol)).status == 200
    r = await client.get(f"/api/spaces/sp-b/pages/{pid}", headers=carol)
    assert r.status == 200
    before = await _space_page_rows(client)
    base = f"/api/spaces/sp-b/pages/{pid}"
    writes = [
        client.post("/api/spaces/sp-b/pages", json={"title": "X"}, headers=carol),
        client.patch(base, json={"title": "X"}, headers=carol),
        client.post(
            f"{base}/resolve-conflict", json={"resolution": "mine"}, headers=carol
        ),
        client.delete(base, headers=carol),
    ]
    for req in writes:
        r = await req
        assert r.status == 403, await r.text()
    assert await _space_page_rows(client) == before


# ─── Archived spaces are read-only ───────────────────────────────────────


async def test_archived_space_pages_are_read_only(client):
    admin = _auth(client._tok)
    await _seed_space(client, "sp-b", client._uid, "owner")
    pid = await _create_space_page(client, "sp-b", admin)
    await client._db.enqueue("UPDATE spaces SET archived=1 WHERE id='sp-b'")
    before = await _space_page_rows(client)
    base = f"/api/spaces/sp-b/pages/{pid}"
    writes = [
        client.post("/api/spaces/sp-b/pages", json={"title": "late"}, headers=admin),
        client.patch(base, json={"title": "X"}, headers=admin),
        client.post(
            f"{base}/resolve-conflict", json={"resolution": "mine"}, headers=admin
        ),
        client.delete(base, headers=admin),
    ]
    for req in writes:
        r = await req
        assert r.status == 403, await r.text()
    assert await _space_page_rows(client) == before
    assert (await client.get(base, headers=admin)).status == 200
    assert (await client.get("/api/spaces/sp-b/pages", headers=admin)).status == 200


# ─── A space-page delete federates ───────────────────────────────────────


async def test_space_page_delete_federates_to_space_members(client):
    admin = _auth(client._tok)
    await _seed_space(client, "sp-b", client._uid, "owner")
    pid = await _create_space_page(client, "sp-b", admin)
    fed = client.app[federation_service_key]
    assert isinstance(fed, FederationService)
    with patch.object(
        FederationService, "broadcast_to_space_members", new=AsyncMock()
    ) as bcast:
        r = await client.delete(f"/api/spaces/sp-b/pages/{pid}", headers=admin)
        assert r.status == 200
    deletes = [
        c
        for c in bcast.await_args_list
        if c.args[1] == FederationEventType.SPACE_PAGE_DELETED
    ]
    assert len(deletes) == 1
    assert deletes[0].args[0] == "sp-b"
    assert deletes[0].args[2]["page_id"] == pid


async def test_household_page_delete_never_federates(client):
    admin = _auth(client._tok)
    r = await client.post("/api/pages", json={"title": "Home"}, headers=admin)
    pid = (await r.json())["id"]
    with patch.object(
        FederationService, "broadcast_to_space_members", new=AsyncMock()
    ) as bcast:
        r = await client.delete(f"/api/pages/{pid}", headers=admin)
        assert r.status == 200
    assert bcast.await_count == 0


async def test_resolve_conflict_with_another_spaces_page_id_is_404(client):
    """Even with an open conflict on space B's page, resolving it under
    space A's path is 404 and the conflict stays open."""
    bob, pid = await _cross_space_env(client)
    await client._db.enqueue(
        "INSERT INTO space_page_snapshots(page_id, space_id, body, snapshot_by,"
        " side, conflict) VALUES(?, 'sp-b', 'theirs', 'u', 'theirs', 1)",
        (pid,),
    )
    before = await _space_page_rows(client)
    r = await client.post(
        f"/api/spaces/sp-a/pages/{pid}/resolve-conflict",
        json={"resolution": "merged_content", "content": "pwned"},
        headers=bob,
    )
    assert r.status == 404
    assert await _space_page_rows(client) == before
    row = await client._db.fetchone(
        "SELECT COUNT(*) AS n FROM space_page_snapshots"
        " WHERE page_id=? AND space_id='sp-b' AND conflict=1",
        (pid,),
    )
    assert row["n"] == 1


# ─── Space page history (read-only) ──────────────────────────────────────


async def test_space_page_versions_lists_this_spaces_history(client):
    admin = _auth(client._tok)
    await _seed_space(client, "sp-b", client._uid, "owner")
    carol = await _add_user(client, "carol", "carol-id")
    await _seed_space(client, "sp-b", "carol-id", "subscriber")
    pid = await _create_space_page(client, "sp-b", admin)
    for body in ("v2", "v3"):
        r = await client.patch(
            f"/api/spaces/sp-b/pages/{pid}", json={"content": body}, headers=admin
        )
        assert r.status == 200
    # A stray history row for the same page id under another scope must
    # never be served here.
    await client._db.enqueue(
        "INSERT INTO page_edit_history(id, page_id, space_id, title, content,"
        " edited_by, version) VALUES('h-x', ?, 'sp-other', 't', 'leak', 'u', 99)",
        (pid,),
    )
    for headers in (admin, carol):
        r = await client.get(f"/api/spaces/sp-b/pages/{pid}/versions", headers=headers)
        assert r.status == 200
        rows = await r.json()
        assert [v["content"] for v in rows] == ["secret", "v2"]
        assert {v["space_id"] for v in rows} == {"sp-b"}


async def test_space_page_versions_gate_order(client):
    bob, pid = await _cross_space_env(client)
    admin = _auth(client._tok)
    # Non-member of the path space: 403 (before feature / id checks).
    r = await client.get(f"/api/spaces/sp-b/pages/{pid}/versions", headers=bob)
    assert r.status == 403
    r = await client.get("/api/spaces/sp-b/pages/nope/versions", headers=bob)
    assert r.status == 403
    # Another space's page id under the member's own space: 404.
    r = await client.get(f"/api/spaces/sp-a/pages/{pid}/versions", headers=bob)
    assert r.status == 404
    # A household page id under a space path: 404.
    r = await client.post("/api/pages", json={"title": "Home"}, headers=admin)
    hid = (await r.json())["id"]
    r = await client.get(f"/api/spaces/sp-b/pages/{hid}/versions", headers=admin)
    assert r.status == 404
    # Feature off: 403 for a member.
    await client._db.enqueue("UPDATE spaces SET feature_pages=0 WHERE id='sp-b'")
    r = await client.get(f"/api/spaces/sp-b/pages/{pid}/versions", headers=admin)
    assert r.status == 403


async def test_household_versions_never_include_space_history(client):
    admin = _auth(client._tok)
    r = await client.post("/api/pages", json={"title": "Home"}, headers=admin)
    hid = (await r.json())["id"]
    r = await client.patch(f"/api/pages/{hid}", json={"content": "v2"}, headers=admin)
    assert r.status == 200
    await client._db.enqueue(
        "INSERT INTO page_edit_history(id, page_id, space_id, title, content,"
        " edited_by, version) VALUES('h-s', ?, 'sp-x', 't', 'space-leak', 'u', 99)",
        (hid,),
    )
    r = await client.get(f"/api/pages/{hid}/versions", headers=admin)
    assert r.status == 200
    rows = await r.json()
    assert [v["space_id"] for v in rows] == [None]
    assert "space-leak" not in await r.text()


async def test_a_stale_space_page_patch_is_409_with_the_current_page(client):
    """The space wiki keeps the household routes' optimistic concurrency:
    a PATCH based on an old ``updated_at`` changes nothing and answers 409
    ``stale_update`` with the page as it now is (§23.72)."""
    admin = _auth(client._tok)
    await _seed_space(client, "sp-c", client._uid, "owner")
    r = await client.post(
        "/api/spaces/sp-c/pages", json={"title": "Wiki", "content": "v1"}, headers=admin
    )
    first = await r.json()
    r = await client.patch(
        f"/api/spaces/sp-c/pages/{first['id']}", json={"content": "v2"}, headers=admin
    )
    assert r.status == 200
    r = await client.patch(
        f"/api/spaces/sp-c/pages/{first['id']}",
        json={"content": "v3", "base_updated_at": first["updated_at"]},
        headers=admin,
    )
    assert r.status == 409
    body = await r.json()
    assert body["error"] == "stale_update"
    assert body["current"]["content"] == "v2"


async def test_a_blank_space_page_title_is_422(client):
    admin = _auth(client._tok)
    await _seed_space(client, "sp-t", client._uid, "owner")
    r = await client.post("/api/spaces/sp-t/pages", json={"title": "  "}, headers=admin)
    assert r.status == 422
    pid = await _create_space_page(client, "sp-t", admin)
    r = await client.patch(
        f"/api/spaces/sp-t/pages/{pid}", json={"title": ""}, headers=admin
    )
    assert r.status == 422
