"""Tests for space sidebar links + per-space notification preferences."""

from __future__ import annotations

import pytest

from socialhome.auth import sha256_token_hash

from .conftest import _auth


async def _seed_space(client, space_id: str = "sp-cust", role: str = "admin"):
    db = client._db
    await db.enqueue(
        "INSERT INTO spaces(id, name, owner_instance_id, owner_username, "
        "identity_public_key, space_type) "
        "VALUES(?, 'Cust', 'iid', 'admin', ?, 'household')",
        (space_id, "aa" * 32),
    )
    await db.enqueue(
        "INSERT INTO space_members(space_id, user_id, role) VALUES(?, ?, ?)",
        (space_id, client._uid, role),
    )


async def _seed_outsider(client, *, token: str = "out-tok", uid: str = "out-id"):
    await client._db.enqueue(
        "INSERT INTO users(username, user_id, display_name, is_admin) "
        "VALUES('outsider',?,?,0)",
        (uid, "Out"),
    )
    await client._db.enqueue(
        "INSERT INTO api_tokens(token_id, user_id, label, token_hash) "
        "VALUES(?, ?, 't', ?)",
        (f"t-{uid}", uid, sha256_token_hash(token)),
    )


# ─── Links ──────────────────────────────────────────────────────────────


async def test_create_and_list_links_as_admin(client):
    await _seed_space(client)
    r = await client.post(
        "/api/spaces/sp-cust/links",
        json={"label": "Wiki", "url": "https://wiki", "position": 0},
        headers=_auth(client._tok),
    )
    assert r.status == 201
    created = await r.json()
    assert created["label"] == "Wiki"
    assert created["id"]

    r2 = await client.get(
        "/api/spaces/sp-cust/links",
        headers=_auth(client._tok),
    )
    body = await r2.json()
    assert [link["label"] for link in body["links"]] == ["Wiki"]


async def test_create_link_non_admin_forbidden(client):
    await _seed_space(client, role="member")
    r = await client.post(
        "/api/spaces/sp-cust/links",
        json={"label": "Wiki", "url": "https://wiki"},
        headers=_auth(client._tok),
    )
    assert r.status == 403


async def test_list_links_non_member_not_allowed(client):
    await _seed_space(client)
    await _seed_outsider(client)
    r = await client.get(
        "/api/spaces/sp-cust/links",
        headers={"Authorization": "Bearer out-tok"},
    )
    # Non-member triggers KeyError (SpacePermissionError) in service —
    # maps to 403 via BaseView._iter.
    assert r.status in (403, 404)


async def test_create_link_empty_label_422(client):
    await _seed_space(client)
    r = await client.post(
        "/api/spaces/sp-cust/links",
        json={"label": "", "url": "https://wiki"},
        headers=_auth(client._tok),
    )
    assert r.status == 422


@pytest.mark.parametrize(
    "bad",
    [
        "javascript:alert(document.domain)",
        " JavaScript:alert(1)",
        "data:text/html,<script>alert(1)</script>",
        "vbscript:msgbox(1)",
        "ftp://files.example/x",
        "mailto:a@b.example",
        "/spaces/sp-cust",
        "wiki.example",
        "https://",
        "https://user:pw@wiki.example/",
        "https://wiki.example/a b",
    ],
)
async def test_create_link_rejects_a_non_http_url(client, bad):
    """Quick links render as ``<a href>`` for every member — only
    absolute http(s) URLs are stored; the refusal says why."""
    await _seed_space(client)
    r = await client.post(
        "/api/spaces/sp-cust/links",
        json={"label": "Wiki", "url": bad},
        headers=_auth(client._tok),
    )
    assert r.status == 422
    body = await r.json()
    assert body["error"]["code"] == "INVALID_LINK"
    r2 = await client.get("/api/spaces/sp-cust/links", headers=_auth(client._tok))
    assert (await r2.json())["links"] == []


async def test_patch_link_rejects_a_non_http_url(client):
    await _seed_space(client)
    r = await client.post(
        "/api/spaces/sp-cust/links",
        json={"label": "Wiki", "url": "http://wiki.example/"},
        headers=_auth(client._tok),
    )
    assert r.status == 201
    link_id = (await r.json())["id"]
    r2 = await client.patch(
        f"/api/spaces/sp-cust/links/{link_id}",
        json={"url": "javascript:alert(1)"},
        headers=_auth(client._tok),
    )
    assert r2.status == 422
    assert (await r2.json())["error"]["code"] == "INVALID_LINK"
    r3 = await client.get("/api/spaces/sp-cust/links", headers=_auth(client._tok))
    assert [link["url"] for link in (await r3.json())["links"]] == [
        "http://wiki.example/"
    ]


async def test_patch_link_updates(client):
    await _seed_space(client)
    r = await client.post(
        "/api/spaces/sp-cust/links",
        json={"label": "Wiki", "url": "https://wiki"},
        headers=_auth(client._tok),
    )
    created = await r.json()
    link_id = created["id"]
    r2 = await client.patch(
        f"/api/spaces/sp-cust/links/{link_id}",
        json={"label": "Wiki v2"},
        headers=_auth(client._tok),
    )
    assert r2.status == 200
    patched = await r2.json()
    assert patched["label"] == "Wiki v2"
    assert patched["url"] == "https://wiki"  # preserved


async def test_patch_link_404_when_other_space(client):
    await _seed_space(client, space_id="sp-a")
    await _seed_space(client, space_id="sp-b")
    r = await client.post(
        "/api/spaces/sp-a/links",
        json={"label": "L", "url": "https://l"},
        headers=_auth(client._tok),
    )
    link_id = (await r.json())["id"]
    r2 = await client.patch(
        f"/api/spaces/sp-b/links/{link_id}",
        json={"label": "X"},
        headers=_auth(client._tok),
    )
    assert r2.status == 404


async def test_delete_link(client):
    await _seed_space(client)
    r = await client.post(
        "/api/spaces/sp-cust/links",
        json={"label": "Wiki", "url": "https://wiki"},
        headers=_auth(client._tok),
    )
    link_id = (await r.json())["id"]
    r2 = await client.delete(
        f"/api/spaces/sp-cust/links/{link_id}",
        headers=_auth(client._tok),
    )
    assert r2.status == 200
    r3 = await client.get(
        "/api/spaces/sp-cust/links",
        headers=_auth(client._tok),
    )
    assert (await r3.json())["links"] == []


async def test_delete_link_non_admin_forbidden(client):
    await _seed_space(client)
    # Create link as admin, then demote self.
    r = await client.post(
        "/api/spaces/sp-cust/links",
        json={"label": "Wiki", "url": "https://wiki"},
        headers=_auth(client._tok),
    )
    link_id = (await r.json())["id"]
    await client._db.enqueue(
        "UPDATE space_members SET role='member' WHERE space_id='sp-cust' AND user_id=?",
        (client._uid,),
    )
    r2 = await client.delete(
        f"/api/spaces/sp-cust/links/{link_id}",
        headers=_auth(client._tok),
    )
    assert r2.status == 403


# ─── Notification preferences ───────────────────────────────────────────


async def test_get_notif_prefs_default_is_all(client):
    await _seed_space(client, role="member")
    r = await client.get(
        "/api/spaces/sp-cust/notif-prefs",
        headers=_auth(client._tok),
    )
    assert r.status == 200
    body = await r.json()
    assert body["level"] == "all"
    # Defaults: feature is off (schema default), member hasn't opted in.
    assert body["feature_location"] is False
    assert body["location_share_enabled"] is False


async def test_get_notif_prefs_returns_location_state(client):
    """When the admin has turned the feature on AND the caller has opted
    in, the GET surfaces both flags so the SPA's per-space @-menu can
    render the location toggle without a second round-trip."""
    await _seed_space(client, role="member")
    await client._db.enqueue(
        "UPDATE spaces SET feature_location=1 WHERE id='sp-cust'",
    )
    await client._db.enqueue(
        "UPDATE space_members SET location_share_enabled=1"
        " WHERE space_id='sp-cust' AND user_id=?",
        (client._uid,),
    )
    r = await client.get(
        "/api/spaces/sp-cust/notif-prefs",
        headers=_auth(client._tok),
    )
    assert r.status == 200
    body = await r.json()
    assert body["feature_location"] is True
    assert body["location_share_enabled"] is True


async def test_put_notif_prefs_sets_level(client):
    await _seed_space(client, role="member")
    r = await client.put(
        "/api/spaces/sp-cust/notif-prefs",
        json={"level": "muted"},
        headers=_auth(client._tok),
    )
    assert r.status == 200
    assert (await r.json())["level"] == "muted"

    r2 = await client.get(
        "/api/spaces/sp-cust/notif-prefs",
        headers=_auth(client._tok),
    )
    assert (await r2.json())["level"] == "muted"


async def test_put_notif_prefs_invalid_422(client):
    await _seed_space(client, role="member")
    r = await client.put(
        "/api/spaces/sp-cust/notif-prefs",
        json={"level": "bogus"},
        headers=_auth(client._tok),
    )
    assert r.status == 422


async def test_notif_prefs_non_member_forbidden(client):
    await _seed_space(client)
    await _seed_outsider(client)
    r = await client.get(
        "/api/spaces/sp-cust/notif-prefs",
        headers={"Authorization": "Bearer out-tok"},
    )
    assert r.status == 403
