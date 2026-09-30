"""HTTP tests for /api/household/preferences."""

from __future__ import annotations


from socialhome.auth import sha256_token_hash

from .conftest import _auth


async def test_get_preferences_requires_auth(client):
    r = await client.get("/api/household/preferences")
    assert r.status == 401


async def test_get_preferences_returns_defaults(client):
    r = await client.get("/api/household/preferences", headers=_auth(client._tok))
    assert r.status == 200
    body = await r.json()
    assert body["household_name"] == "Home"
    assert body["feat_feed"] is True
    assert body["feat_presence"] is True
    assert body["feat_gallery"] is True
    assert body["feat_timetable"] is True


async def test_put_preferences_toggles_timetable(client):
    r = await client.put(
        "/api/household/preferences",
        json={"toggles": {"feat_timetable": False}},
        headers=_auth(client._tok),
    )
    assert r.status == 200
    assert (await r.json())["feat_timetable"] is False
    r = await client.get("/api/household/preferences", headers=_auth(client._tok))
    assert (await r.json())["feat_timetable"] is False


async def test_put_preferences_admin_renames_household(client):
    r = await client.put(
        "/api/household/preferences",
        json={"household_name": "Pascal's Place"},
        headers=_auth(client._tok),
    )
    assert r.status == 200
    assert (await r.json())["household_name"] == "Pascal's Place"


async def test_put_preferences_admin_toggles_feature(client):
    r = await client.put(
        "/api/household/preferences",
        json={"toggles": {"feat_pages": False}},
        headers=_auth(client._tok),
    )
    assert r.status == 200
    assert (await r.json())["feat_pages"] is False


async def test_put_preferences_non_admin_403(client):
    db = client._db
    await db.enqueue(
        "INSERT INTO users(username, user_id, display_name, is_admin)"
        " VALUES('bob', 'bob-id', 'Bob', 0)",
    )
    raw = "bob-tok"
    await db.enqueue(
        "INSERT INTO api_tokens(token_id, user_id, label, token_hash)"
        " VALUES('tb', 'bob-id', 't', ?)",
        (sha256_token_hash(raw),),
    )
    r = await client.put(
        "/api/household/preferences",
        json={"household_name": "Hijack"},
        headers=_auth(raw),
    )
    assert r.status == 403


async def test_put_preferences_bad_json_400(client):
    r = await client.put(
        "/api/household/preferences",
        data="bad",
        headers={**_auth(client._tok), "Content-Type": "application/json"},
    )
    assert r.status == 400


async def test_put_preferences_invalid_value_422(client):
    r = await client.put(
        "/api/household/preferences",
        json={"toggles": {"feat_feed": "not-a-bool"}},
        headers=_auth(client._tok),
    )
    assert r.status == 422


async def test_put_preferences_empty_name_422(client):
    r = await client.put(
        "/api/household/preferences",
        json={"household_name": ""},
        headers=_auth(client._tok),
    )
    assert r.status == 422


# ─── Cross-route enforcement for presence + gallery (§18) ────────────────
#
# These tests verify that the NEW preferences_service gate works end-to-end
# for the two features newly gated in this task.
#
# NOTE: Pages, stickies, tasks, calendar, and feed-post-type gates are
# enforced via PreferencesService (same as presence + gallery).


async def _disable(client, **toggles):
    r = await client.put(
        "/api/household/preferences",
        json={"toggles": toggles},
        headers=_auth(client._tok),
    )
    assert r.status == 200


async def test_disabled_presence_blocks_get(client):
    await _disable(client, feat_presence=False)
    r = await client.get("/api/presence", headers=_auth(client._tok))
    assert r.status == 403
    body = await r.json()
    assert body["error"]["code"] == "FEATURE_DISABLED"
    assert body["error"]["section"] == "presence"


async def test_disabled_gallery_blocks_list_albums(client):
    await _disable(client, feat_gallery=False)
    r = await client.get("/api/gallery/albums", headers=_auth(client._tok))
    assert r.status == 403
    body = await r.json()
    assert body["error"]["code"] == "FEATURE_DISABLED"
    assert body["error"]["section"] == "gallery"
