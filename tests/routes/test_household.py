"""HTTP tests for /api/household/features."""

from __future__ import annotations


from socialhome.auth import sha256_token_hash

from .conftest import _auth


async def test_get_features_requires_auth(client):
    r = await client.get("/api/household/features")
    assert r.status == 401


async def test_get_features_returns_defaults(client):
    r = await client.get("/api/household/features", headers=_auth(client._tok))
    assert r.status == 200
    body = await r.json()
    assert body["household_name"] == "Home"
    assert body["feat_feed"] is True


async def test_put_features_admin_renames_household(client):
    r = await client.put(
        "/api/household/features",
        json={"household_name": "Pascal's Place"},
        headers=_auth(client._tok),
    )
    assert r.status == 200
    assert (await r.json())["household_name"] == "Pascal's Place"


async def test_put_features_admin_toggles_feature(client):
    r = await client.put(
        "/api/household/features",
        json={"toggles": {"feat_pages": False}},
        headers=_auth(client._tok),
    )
    assert r.status == 200
    assert (await r.json())["feat_pages"] is False


async def test_put_features_non_admin_403(client):
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
        "/api/household/features",
        json={"household_name": "Hijack"},
        headers=_auth(raw),
    )
    assert r.status == 403


async def test_put_features_bad_json_400(client):
    r = await client.put(
        "/api/household/features",
        data="bad",
        headers={**_auth(client._tok), "Content-Type": "application/json"},
    )
    assert r.status == 400


async def test_put_features_invalid_value_422(client):
    r = await client.put(
        "/api/household/features",
        json={"toggles": {"feat_feed": "not-a-bool"}},
        headers=_auth(client._tok),
    )
    assert r.status == 422


async def test_put_features_empty_name_422(client):
    r = await client.put(
        "/api/household/features",
        json={"household_name": ""},
        headers=_auth(client._tok),
    )
    assert r.status == 422


# ─── Cross-route enforcement (§18) ───────────────────────────────────────
#
# These tests verify that flipping a toggle off actually blocks the
# relevant mutating endpoint with HTTP 403 + ``FEATURE_DISABLED``.


async def _disable(client, **toggles):
    r = await client.put(
        "/api/household/features",
        json={"toggles": toggles},
        headers=_auth(client._tok),
    )
    assert r.status == 200


async def test_disabled_pages_blocks_post(client):
    await _disable(client, feat_pages=False)
    r = await client.post(
        "/api/pages",
        json={"title": "Nope", "content": ""},
        headers=_auth(client._tok),
    )
    assert r.status == 403
    body = await r.json()
    assert body["error"]["code"] == "FEATURE_DISABLED"
    assert body["error"]["section"] == "pages"


async def test_disabled_stickies_blocks_post(client):
    await _disable(client, feat_stickies=False)
    r = await client.post(
        "/api/stickies",
        json={"content": "hey", "position_x": 0, "position_y": 0},
        headers=_auth(client._tok),
    )
    assert r.status == 403


async def test_disabled_tasks_blocks_create_list(client):
    await _disable(client, feat_tasks=False)
    r = await client.post(
        "/api/tasks/lists",
        json={"name": "Groceries"},
        headers=_auth(client._tok),
    )
    assert r.status == 403


async def test_disabled_calendar_blocks_create(client):
    await _disable(client, feat_calendar=False)
    r = await client.post(
        "/api/calendars",
        json={"name": "Fam"},
        headers=_auth(client._tok),
    )
    assert r.status == 403


async def test_disallowed_video_post_type_blocks_feed(client):
    await _disable(client, allow_video=False)
    r = await client.post(
        "/api/feed/posts",
        json={
            "type": "video",
            "content": "clip",
            "media_url": "http://x/y.mp4",
            "file_meta": {
                "filename": "v.mp4",
                "mime": "video/mp4",
                "size_bytes": 100,
                "hash": "abc",
            },
        },
        headers=_auth(client._tok),
    )
    assert r.status == 403
    body = await r.json()
    assert "post_type:video" in body["error"]["section"]


async def test_text_post_still_allowed_when_video_disabled(client):
    await _disable(client, allow_video=False)
    r = await client.post(
        "/api/feed/posts",
        json={"type": "text", "content": "hello"},
        headers=_auth(client._tok),
    )
    assert r.status == 201


async def test_toggle_change_is_live(client):
    """Re-enabling a toggle immediately lifts the block."""
    await _disable(client, feat_pages=False)
    r = await client.post(
        "/api/pages",
        json={"title": "x", "content": ""},
        headers=_auth(client._tok),
    )
    assert r.status == 403
    # Re-enable.
    await client.put(
        "/api/household/features",
        json={"toggles": {"feat_pages": True}},
        headers=_auth(client._tok),
    )
    r = await client.post(
        "/api/pages",
        json={"title": "ok", "content": ""},
        headers=_auth(client._tok),
    )
    assert r.status == 201
