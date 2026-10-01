"""Tests for socialhome.routes.stickies."""

import pytest

from socialhome.auth import sha256_token_hash

from .conftest import _auth


async def test_create_sticky(client):
    """POST /api/stickies creates a sticky note."""
    r = await client.post(
        "/api/stickies", json={"content": "Remember this"}, headers=_auth(client._tok)
    )
    assert r.status == 201


async def test_list_stickies(client):
    """GET /api/stickies returns household stickies."""
    await client.post(
        "/api/stickies", json={"content": "Note"}, headers=_auth(client._tok)
    )
    r = await client.get("/api/stickies", headers=_auth(client._tok))
    assert r.status == 200
    assert len(await r.json()) >= 1


async def test_update_sticky(client):
    """PATCH /api/stickies/{id} updates content."""
    r = await client.post(
        "/api/stickies", json={"content": "v1"}, headers=_auth(client._tok)
    )
    sid = (await r.json())["id"]
    r2 = await client.patch(
        f"/api/stickies/{sid}", json={"content": "v2"}, headers=_auth(client._tok)
    )
    assert r2.status == 200


async def test_delete_sticky(client):
    """DELETE /api/stickies/{id} removes it."""
    r = await client.post(
        "/api/stickies", json={"content": "tmp"}, headers=_auth(client._tok)
    )
    sid = (await r.json())["id"]
    r2 = await client.delete(f"/api/stickies/{sid}", headers=_auth(client._tok))
    assert r2.status in (200, 204)


# ─── Scope: household routes never reach space stickies, space routes ──
# never reach another space's stickies; subscribers / archived spaces /
# feature-off are read-only (403).


async def _seed_space(client, sid: str, user_id: str, role: str, *, stickies=1):
    await client._db.enqueue(
        "INSERT INTO spaces(id, name, owner_instance_id, owner_username,"
        " identity_public_key, feature_stickies) VALUES(?, ?, 'inst', 'admin', ?, ?)",
        (sid, sid, "ab" * 32, stickies),
    )
    await client._db.enqueue(
        "INSERT INTO space_members(space_id, user_id, role) VALUES(?, ?, ?)",
        (sid, user_id, role),
    )


async def _add_member(client, sid: str, user_id: str, role: str) -> None:
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


async def _space_sticky(client, sid: str, content: str = "secret") -> str:
    r = await client.post(
        f"/api/spaces/{sid}/stickies",
        json={"content": content},
        headers=_auth(client._tok),
    )
    assert r.status == 201
    return (await r.json())["id"]


async def _row(client, sticky_id: str):
    row = await client._db.fetchone(
        "SELECT space_id, content, color, position_x FROM stickies WHERE id=?",
        (sticky_id,),
    )
    return None if row is None else tuple(row)


async def test_household_patch_on_space_sticky_is_404_and_untouched(client):
    """A household user who is not a space member must not edit a space
    sticky through ``/api/stickies/{id}``."""
    await _seed_space(client, "sp-b", client._uid, "owner")
    stid = await _space_sticky(client, "sp-b")
    before = await _row(client, stid)
    bob = await _add_user(client, "bob", "bob-id", "bob-tok")
    for headers in (bob, _auth(client._tok)):
        r = await client.patch(
            f"/api/stickies/{stid}",
            json={"content": "pwned", "color": "#000000", "position_x": 9},
            headers=headers,
        )
        assert r.status == 404
    assert await _row(client, stid) == before


async def test_household_delete_on_space_sticky_is_404_and_untouched(client):
    await _seed_space(client, "sp-b", client._uid, "owner")
    stid = await _space_sticky(client, "sp-b")
    bob = await _add_user(client, "bob", "bob-id", "bob-tok")
    for headers in (bob, _auth(client._tok)):
        r = await client.delete(f"/api/stickies/{stid}", headers=headers)
        assert r.status == 404
    assert await _row(client, stid) is not None


async def test_space_sticky_from_another_space_is_404_and_untouched(client):
    await _seed_space(client, "sp-b", client._uid, "owner")
    stid = await _space_sticky(client, "sp-b")
    before = await _row(client, stid)
    bob = await _add_user(client, "bob", "bob-id", "bob-tok")
    await _seed_space(client, "sp-a", "bob-id", "member")
    r = await client.patch(
        f"/api/spaces/sp-a/stickies/{stid}", json={"content": "x"}, headers=bob
    )
    assert r.status == 404
    r = await client.delete(f"/api/spaces/sp-a/stickies/{stid}", headers=bob)
    assert r.status == 404
    assert await _row(client, stid) == before


async def test_space_route_never_reaches_household_sticky(client):
    """A household sticky id under a space path is 404."""
    await _seed_space(client, "sp-a", client._uid, "owner")
    r = await client.post(
        "/api/stickies", json={"content": "home"}, headers=_auth(client._tok)
    )
    hid = (await r.json())["id"]
    before = await _row(client, hid)
    r = await client.patch(
        f"/api/spaces/sp-a/stickies/{hid}",
        json={"content": "x"},
        headers=_auth(client._tok),
    )
    assert r.status == 404
    r = await client.delete(
        f"/api/spaces/sp-a/stickies/{hid}", headers=_auth(client._tok)
    )
    assert r.status == 404
    assert await _row(client, hid) == before


async def test_space_sticky_subscriber_reads_but_never_writes(client):
    await _seed_space(client, "sp-s", client._uid, "owner")
    stid = await _space_sticky(client, "sp-s")
    before = await _row(client, stid)
    bob = await _add_user(client, "bob", "bob-id", "bob-tok")
    await _add_member(client, "sp-s", "bob-id", "subscriber")
    base = "/api/spaces/sp-s/stickies"
    r = await client.get(base, headers=bob)
    assert r.status == 200
    assert [s["id"] for s in await r.json()] == [stid]
    for resp in (
        await client.post(base, json={"content": "sub"}, headers=bob),
        await client.patch(f"{base}/{stid}", json={"content": "x"}, headers=bob),
        await client.delete(f"{base}/{stid}", headers=bob),
    ):
        assert resp.status == 403
    assert await _row(client, stid) == before
    rows = await client._db.fetchall(
        "SELECT id FROM stickies WHERE space_id='sp-s'",
    )
    assert len(rows) == 1


async def test_space_sticky_member_writes_are_allowed(client):
    await _seed_space(client, "sp-m", client._uid, "owner")
    bob = await _add_user(client, "bob", "bob-id", "bob-tok")
    await _add_member(client, "sp-m", "bob-id", "member")
    base = "/api/spaces/sp-m/stickies"
    r = await client.post(base, json={"content": "hi"}, headers=bob)
    assert r.status == 201
    stid = (await r.json())["id"]
    r = await client.patch(
        f"{base}/{stid}", json={"content": "hi2", "position_x": 3}, headers=bob
    )
    assert r.status == 200
    body = await r.json()
    assert body["content"] == "hi2" and body["position_x"] == 3.0
    assert (await client.delete(f"{base}/{stid}", headers=bob)).status == 200
    assert await _row(client, stid) is None


async def test_space_sticky_writes_in_an_archived_space_are_403(client):
    await _seed_space(client, "sp-ar", client._uid, "owner")
    stid = await _space_sticky(client, "sp-ar")
    before = await _row(client, stid)
    await client._db.enqueue("UPDATE spaces SET archived=1 WHERE id='sp-ar'")
    h = _auth(client._tok)
    base = "/api/spaces/sp-ar/stickies"
    assert (await client.get(base, headers=h)).status == 200
    for resp in (
        await client.post(base, json={"content": "n"}, headers=h),
        await client.patch(f"{base}/{stid}", json={"content": "x"}, headers=h),
        await client.delete(f"{base}/{stid}", headers=h),
    ):
        assert resp.status == 403
    assert await _row(client, stid) == before


async def test_space_sticky_feature_off_is_403(client):
    await _seed_space(client, "sp-f", client._uid, "owner")
    stid = await _space_sticky(client, "sp-f")
    await client._db.enqueue("UPDATE spaces SET feature_stickies=0 WHERE id='sp-f'")
    h = _auth(client._tok)
    base = "/api/spaces/sp-f/stickies"
    for resp in (
        await client.get(base, headers=h),
        await client.post(base, json={"content": "n"}, headers=h),
        await client.patch(f"{base}/{stid}", json={"content": "x"}, headers=h),
        await client.delete(f"{base}/{stid}", headers=h),
    ):
        assert resp.status == 403
        assert (await resp.json())["error"]["code"] == "FEATURE_DISABLED"
    assert (await _row(client, stid))[1] == "secret"


async def test_space_sticky_non_member_is_403(client):
    await _seed_space(client, "sp-n", client._uid, "owner")
    stid = await _space_sticky(client, "sp-n")
    bob = await _add_user(client, "bob", "bob-id", "bob-tok")
    base = "/api/spaces/sp-n/stickies"
    for resp in (
        await client.get(base, headers=bob),
        await client.post(base, json={"content": "n"}, headers=bob),
        await client.patch(f"{base}/{stid}", json={"content": "x"}, headers=bob),
        await client.delete(f"{base}/{stid}", headers=bob),
    ):
        assert resp.status == 403
    assert (await _row(client, stid))[1] == "secret"


async def test_household_sticky_empty_content_is_rejected(client):
    r = await client.post(
        "/api/stickies", json={"content": "v1"}, headers=_auth(client._tok)
    )
    sid = (await r.json())["id"]
    r = await client.patch(
        f"/api/stickies/{sid}", json={"content": "   "}, headers=_auth(client._tok)
    )
    assert r.status in (400, 422)
    assert (await _row(client, sid))[1] == "v1"


async def test_sticky_bad_field_types_are_422_and_untouched(client):
    h = _auth(client._tok)
    r = await client.post("/api/stickies", json={"content": "v1"}, headers=h)
    sid = (await r.json())["id"]
    before = await _row(client, sid)
    for body in (
        {"content": 5},
        {"color": ["#000"]},
        {"position_x": "left"},
        {"position_y": True},
        ["not", "an", "object"],
    ):
        r = await client.patch(f"/api/stickies/{sid}", json=body, headers=h)
        assert r.status == 422
    r = await client.post("/api/stickies", json=["x"], headers=h)
    assert r.status == 422
    r = await client.post(
        "/api/stickies", json={"content": "ok", "position_x": {}}, headers=h
    )
    assert r.status == 422
    assert await _row(client, sid) == before


# ─── Field rules: colour, coordinates, content ─────────────────────────


@pytest.mark.parametrize(
    "color",
    [
        "url(https://evil.example/t.png)",
        "red;background-image:url(x)",
        "#" + "A" * 300,
        "yellow",
    ],
)
async def test_sticky_non_hex_color_is_422_on_create_and_patch(client, color):
    h = _auth(client._tok)
    r = await client.post(
        "/api/stickies", json={"content": "x", "color": color}, headers=h
    )
    assert r.status == 422
    r = await client.post("/api/stickies", json={"content": "v1"}, headers=h)
    sid = (await r.json())["id"]
    before = await _row(client, sid)
    r = await client.patch(f"/api/stickies/{sid}", json={"color": color}, headers=h)
    assert r.status == 422
    assert await _row(client, sid) == before


async def test_sticky_hex_color_round_trips_canonical(client):
    h = _auth(client._tok)
    r = await client.post(
        "/api/stickies", json={"content": "x", "color": "#abc"}, headers=h
    )
    assert r.status == 201
    body = await r.json()
    assert body["color"] == "#AABBCC"
    r = await client.patch(
        f"/api/stickies/{body['id']}", json={"color": "#ffd6e0"}, headers=h
    )
    assert (await r.json())["color"] == "#FFD6E0"


async def test_sticky_huge_integer_coordinate_is_422(client):
    h = {**_auth(client._tok), "Content-Type": "application/json"}
    big = "1" + "0" * 400
    r = await client.post(
        "/api/stickies", data='{"content":"x","position_x":' + big + "}", headers=h
    )
    assert r.status == 422
    r = await client.post("/api/stickies", json={"content": "v1"}, headers=h)
    sid = (await r.json())["id"]
    r = await client.patch(
        f"/api/stickies/{sid}", data='{"position_y":' + big + "}", headers=h
    )
    assert r.status == 422


async def test_sticky_coordinates_are_clamped_into_the_board(client):
    h = _auth(client._tok)
    r = await client.post(
        "/api/stickies",
        json={"content": "x", "position_x": 5000, "position_y": -20},
        headers=h,
    )
    body = await r.json()
    assert (body["position_x"], body["position_y"]) == (1000.0, 0.0)
    r = await client.patch(
        f"/api/stickies/{body['id']}", json={"position_y": 9999}, headers=h
    )
    body = await r.json()
    assert (body["position_x"], body["position_y"]) == (1000.0, 700.0)


async def test_sticky_content_over_cap_is_422(client):
    h = _auth(client._tok)
    r = await client.post("/api/stickies", json={"content": "y" * 2001}, headers=h)
    assert r.status == 422
    r = await client.post("/api/stickies", json={"content": "y" * 2000}, headers=h)
    assert r.status == 201
    sid = (await r.json())["id"]
    r = await client.patch(
        f"/api/stickies/{sid}", json={"content": "z" * 2001}, headers=h
    )
    assert r.status == 422
    assert (await _row(client, sid))[1] == "y" * 2000


async def test_sticky_content_strips_control_and_bidi(client):
    h = _auth(client._tok)
    r = await client.post("/api/stickies", json={"content": "a‮b\x00c\nd"}, headers=h)
    assert (await r.json())["content"] == "abc\nd"
    r = await client.post("/api/stickies", json={"content": "‮​\x00"}, headers=h)
    assert r.status == 422


# ─── Feature gates: household routes ↔ household toggle, space routes ↔
# the space toggle only (mirrors tasks).


async def _disable_household_stickies(client) -> None:
    r = await client.put(
        "/api/household/preferences",
        json={"toggles": {"feat_stickies": False}},
        headers=_auth(client._tok),
    )
    assert r.status == 200


async def test_household_stickies_feature_off_gates_every_household_route(client):
    h = _auth(client._tok)
    r = await client.post("/api/stickies", json={"content": "v1"}, headers=h)
    sid = (await r.json())["id"]
    await _disable_household_stickies(client)
    for resp in (
        await client.get("/api/stickies", headers=h),
        await client.post("/api/stickies", json={"content": "n"}, headers=h),
        await client.patch(f"/api/stickies/{sid}", json={"content": "x"}, headers=h),
        await client.delete(f"/api/stickies/{sid}", headers=h),
    ):
        assert resp.status == 403
        assert (await resp.json())["error"]["code"] == "FEATURE_DISABLED"
    assert (await _row(client, sid))[1] == "v1"


async def test_space_stickies_ignore_the_household_toggle(client):
    await _seed_space(client, "sp-h", client._uid, "owner")
    await _disable_household_stickies(client)
    h = _auth(client._tok)
    base = "/api/spaces/sp-h/stickies"
    r = await client.post(base, json={"content": "space note"}, headers=h)
    assert r.status == 201
    stid = (await r.json())["id"]
    assert (await client.get(base, headers=h)).status == 200
    r = await client.patch(f"{base}/{stid}", json={"content": "y"}, headers=h)
    assert r.status == 200
    assert (await client.delete(f"{base}/{stid}", headers=h)).status == 200
