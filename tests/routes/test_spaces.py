"""Tests for space routes — /api/spaces/* endpoints."""

from __future__ import annotations

import aiohttp
import pytest
from aiohttp.test_utils import TestClient, TestServer

from socialhome.app import create_app
from socialhome.app_keys import db_key as _db_key
from socialhome.auth import sha256_token_hash
from socialhome.config import Config
from socialhome.crypto import derive_user_id


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
async def client(tmp_dir):
    """App client with admin (pascal) and regular user (bob)."""
    cfg = Config(
        data_dir=str(tmp_dir),
        db_path=str(tmp_dir / "test.db"),
        media_path=str(tmp_dir / "media"),
        mode="standalone",
        log_level="WARNING",
        db_write_batch_timeout_ms=10,
    )
    app = create_app(cfg)
    async with TestClient(TestServer(app)) as tc:
        db = app[_db_key]
        _row = await db.fetchone(
            "SELECT identity_public_key FROM instance_identity WHERE id='self'"
        )
        _pk = bytes.fromhex(_row["identity_public_key"])

        class _KP:
            public_key = _pk

        kp = _KP()
        uid = derive_user_id(kp.public_key, "pascal")
        await db.enqueue(
            "INSERT INTO users(username, user_id, display_name, is_admin) VALUES(?,?,?,1)",
            ("pascal", uid, "Pascal"),
        )
        raw_token = "test-token-raw"
        await db.enqueue(
            "INSERT INTO api_tokens(token_id, user_id, label, token_hash) VALUES(?,?,?,?)",
            ("tid-1", uid, "test", sha256_token_hash(raw_token)),
        )
        uid2 = derive_user_id(kp.public_key, "bob")
        await db.enqueue(
            "INSERT INTO users(username, user_id, display_name, is_admin) VALUES(?,?,?,0)",
            ("bob", uid2, "Bob"),
        )
        await db.enqueue(
            "INSERT INTO api_tokens(token_id, user_id, label, token_hash) VALUES(?,?,?,?)",
            ("tid-2", uid2, "test", sha256_token_hash("bob-token-raw")),
        )
        tc._admin_token = raw_token
        tc._admin_uid = uid
        tc._bob_token = "bob-token-raw"
        tc._bob_uid = uid2
        yield tc


async def test_create_space(client):
    """POST /api/spaces creates a space and returns 201."""
    resp = await client.post(
        "/api/spaces",
        json={"name": "Family", "emoji": "🏠"},
        headers=_auth(client._admin_token),
    )
    assert resp.status == 201
    body = await resp.json()
    assert body["name"] == "Family"
    assert "id" in body


async def test_get_space(client):
    """GET /api/spaces/{id} returns the space details."""
    r = await client.post(
        "/api/spaces",
        json={"name": "GetMe"},
        headers=_auth(client._admin_token),
    )
    sid = (await r.json())["id"]
    resp = await client.get(
        f"/api/spaces/{sid}",
        headers=_auth(client._admin_token),
    )
    assert resp.status == 200
    body = await resp.json()
    assert body["name"] == "GetMe"


async def test_update_space(client):
    """PATCH /api/spaces/{id} updates the space name."""
    r = await client.post(
        "/api/spaces",
        json={"name": "Old Name"},
        headers=_auth(client._admin_token),
    )
    sid = (await r.json())["id"]
    resp = await client.patch(
        f"/api/spaces/{sid}",
        json={"name": "New Name"},
        headers=_auth(client._admin_token),
    )
    assert resp.status == 200
    assert (await resp.json())["name"] == "New Name"


async def test_patch_sets_retention_days(client):
    """PATCH /api/spaces/{id} accepts retention_days and persists it."""
    r = await client.post(
        "/api/spaces",
        json={"name": "RetentionSpace"},
        headers=_auth(client._admin_token),
    )
    sid = (await r.json())["id"]
    resp = await client.patch(
        f"/api/spaces/{sid}",
        json={"retention_days": 30},
        headers=_auth(client._admin_token),
    )
    assert resp.status == 200
    got = await client.get(
        f"/api/spaces/{sid}",
        headers=_auth(client._admin_token),
    )
    body = await got.json()
    assert body["retention_days"] == 30


async def test_patch_retention_days_zero_clears(client):
    """PATCH retention_days=0 clears the setting (coerced to null)."""
    r = await client.post(
        "/api/spaces",
        json={"name": "RetClear"},
        headers=_auth(client._admin_token),
    )
    sid = (await r.json())["id"]
    await client.patch(
        f"/api/spaces/{sid}",
        json={"retention_days": 14},
        headers=_auth(client._admin_token),
    )
    resp = await client.patch(
        f"/api/spaces/{sid}",
        json={"retention_days": 0},
        headers=_auth(client._admin_token),
    )
    assert resp.status == 200
    got = await client.get(
        f"/api/spaces/{sid}",
        headers=_auth(client._admin_token),
    )
    assert (await got.json())["retention_days"] is None


async def test_patch_sets_retention_exempt_types(client):
    """PATCH retention_exempt_types is persisted and echoed on GET."""
    r = await client.post(
        "/api/spaces",
        json={"name": "ExemptSpace"},
        headers=_auth(client._admin_token),
    )
    sid = (await r.json())["id"]
    resp = await client.patch(
        f"/api/spaces/{sid}",
        json={"retention_exempt_types": ["list", "poll"]},
        headers=_auth(client._admin_token),
    )
    assert resp.status == 200
    got = await client.get(
        f"/api/spaces/{sid}",
        headers=_auth(client._admin_token),
    )
    body = await got.json()
    assert set(body["retention_exempt_types"]) == {"list", "poll"}


async def test_get_includes_retention_fields(client):
    """Fresh spaces report null retention_days + empty exempt list."""
    r = await client.post(
        "/api/spaces",
        json={"name": "FreshSpace"},
        headers=_auth(client._admin_token),
    )
    sid = (await r.json())["id"]
    got = await client.get(
        f"/api/spaces/{sid}",
        headers=_auth(client._admin_token),
    )
    body = await got.json()
    assert body["retention_days"] is None
    assert body["retention_exempt_types"] == []


async def test_create_space_accepts_retention_days(client):
    """POST /api/spaces with retention_days stores it."""
    r = await client.post(
        "/api/spaces",
        json={"name": "BornWithRetention", "retention_days": 7},
        headers=_auth(client._admin_token),
    )
    assert r.status == 201
    sid = (await r.json())["id"]
    got = await client.get(
        f"/api/spaces/{sid}",
        headers=_auth(client._admin_token),
    )
    assert (await got.json())["retention_days"] == 7


async def test_dissolve_space(client):
    """DELETE /api/spaces/{id} dissolves the space."""
    r = await client.post(
        "/api/spaces",
        json={"name": "Temp"},
        headers=_auth(client._admin_token),
    )
    sid = (await r.json())["id"]
    resp = await client.delete(
        f"/api/spaces/{sid}",
        headers=_auth(client._admin_token),
    )
    assert resp.status == 200


async def test_add_and_list_members(client):
    """POST + GET /api/spaces/{id}/members manages space membership."""
    r = await client.post(
        "/api/spaces",
        json={"name": "MemberSpace"},
        headers=_auth(client._admin_token),
    )
    sid = (await r.json())["id"]
    r2 = await client.post(
        f"/api/spaces/{sid}/members",
        json={"user_id": client._bob_uid},
        headers=_auth(client._admin_token),
    )
    assert r2.status == 201
    resp = await client.get(
        f"/api/spaces/{sid}/members",
        headers=_auth(client._admin_token),
    )
    members = await resp.json()
    assert len(members) == 2


async def test_remove_member(client):
    """DELETE /api/spaces/{id}/members/{user_id} removes the member."""
    r = await client.post(
        "/api/spaces",
        json={"name": "RemoveMember"},
        headers=_auth(client._admin_token),
    )
    sid = (await r.json())["id"]
    await client.post(
        f"/api/spaces/{sid}/members",
        json={"user_id": client._bob_uid},
        headers=_auth(client._admin_token),
    )
    resp = await client.delete(
        f"/api/spaces/{sid}/members/{client._bob_uid}",
        headers=_auth(client._admin_token),
    )
    assert resp.status == 200


async def test_ban_member(client):
    """POST /api/spaces/{id}/ban bans a user from the space."""
    r = await client.post(
        "/api/spaces",
        json={"name": "BanSpace"},
        headers=_auth(client._admin_token),
    )
    sid = (await r.json())["id"]
    await client.post(
        f"/api/spaces/{sid}/members",
        json={"user_id": client._bob_uid},
        headers=_auth(client._admin_token),
    )
    resp = await client.post(
        f"/api/spaces/{sid}/ban",
        json={"user_id": client._bob_uid, "reason": "spam"},
        headers=_auth(client._admin_token),
    )
    assert resp.status == 200


async def test_create_invite_token(client):
    """POST /api/spaces/{id}/invite-tokens creates an invite token."""
    r = await client.post(
        "/api/spaces",
        json={"name": "InviteSpace"},
        headers=_auth(client._admin_token),
    )
    sid = (await r.json())["id"]
    resp = await client.post(
        f"/api/spaces/{sid}/invite-tokens",
        json={"uses": 1},
        headers=_auth(client._admin_token),
    )
    assert resp.status == 201
    body = await resp.json()
    assert "token" in body


async def test_join_via_invite_token(client):
    """POST /api/spaces/join with a valid token adds the user as member."""
    r = await client.post(
        "/api/spaces",
        json={"name": "JoinSpace"},
        headers=_auth(client._admin_token),
    )
    sid = (await r.json())["id"]
    r2 = await client.post(
        f"/api/spaces/{sid}/invite-tokens",
        json={"uses": 1},
        headers=_auth(client._admin_token),
    )
    token = (await r2.json())["token"]
    resp = await client.post(
        "/api/spaces/join",
        json={"token": token},
        headers=_auth(client._bob_token),
    )
    assert resp.status == 200
    assert (await resp.json())["role"] == "member"


async def test_create_space_post(client):
    """POST /api/spaces/{id}/posts creates a post in the space."""
    r = await client.post(
        "/api/spaces",
        json={"name": "PostSpace"},
        headers=_auth(client._admin_token),
    )
    sid = (await r.json())["id"]
    resp = await client.post(
        f"/api/spaces/{sid}/posts",
        json={"type": "text", "content": "space hello"},
        headers=_auth(client._admin_token),
    )
    assert resp.status == 201


async def test_get_space_feed(client):
    """GET /api/spaces/{id}/feed returns the space feed."""
    r = await client.post(
        "/api/spaces",
        json={"name": "FeedSpace"},
        headers=_auth(client._admin_token),
    )
    sid = (await r.json())["id"]
    await client.post(
        f"/api/spaces/{sid}/posts",
        json={"type": "text", "content": "feed post"},
        headers=_auth(client._admin_token),
    )
    resp = await client.get(
        f"/api/spaces/{sid}/feed",
        headers=_auth(client._admin_token),
    )
    assert resp.status == 200
    feed = await resp.json()
    assert len(feed) == 1


async def test_create_space_empty_name_422(client):
    """POST /api/spaces with an empty name returns 422."""
    resp = await client.post(
        "/api/spaces",
        json={"name": ""},
        headers=_auth(client._admin_token),
    )
    assert resp.status == 422


async def test_get_nonexistent_space_404(client):
    """GET /api/spaces/{id} for unknown id returns 404."""
    resp = await client.get(
        "/api/spaces/no-such-space-id",
        headers=_auth(client._admin_token),
    )
    assert resp.status == 404


async def test_non_owner_cannot_dissolve_403(client):
    """DELETE /api/spaces/{id} by a non-owner returns 403."""
    r = await client.post(
        "/api/spaces",
        json={"name": "OwnerOnly"},
        headers=_auth(client._admin_token),
    )
    sid = (await r.json())["id"]
    resp = await client.delete(
        f"/api/spaces/{sid}",
        headers=_auth(client._bob_token),
    )
    assert resp.status == 403


# ─── New admin wiring: list / role / leave-me / ownership / join-requests / reactions ──


async def test_list_spaces_returns_empty_when_no_memberships(client):
    r = await client.get("/api/spaces", headers=_auth(client._bob_token))
    assert r.status == 200
    assert await r.json() == []


async def test_list_spaces_returns_members_spaces(client):
    r = await client.post(
        "/api/spaces",
        json={"name": "Crew"},
        headers=_auth(client._admin_token),
    )
    sid = (await r.json())["id"]
    r = await client.get("/api/spaces", headers=_auth(client._admin_token))
    assert r.status == 200
    rows = await r.json()
    assert any(row["id"] == sid for row in rows)


async def test_leave_via_members_me(client):
    r = await client.post(
        "/api/spaces",
        json={"name": "LeaveMe"},
        headers=_auth(client._admin_token),
    )
    sid = (await r.json())["id"]
    await client.post(
        f"/api/spaces/{sid}/members",
        json={"user_id": client._bob_uid},
        headers=_auth(client._admin_token),
    )
    r = await client.delete(
        f"/api/spaces/{sid}/members/me",
        headers=_auth(client._bob_token),
    )
    assert r.status == 200


async def test_set_role_owner_promotes_admin(client):
    r = await client.post(
        "/api/spaces",
        json={"name": "Promote"},
        headers=_auth(client._admin_token),
    )
    sid = (await r.json())["id"]
    await client.post(
        f"/api/spaces/{sid}/members",
        json={"user_id": client._bob_uid},
        headers=_auth(client._admin_token),
    )
    r = await client.patch(
        f"/api/spaces/{sid}/members/{client._bob_uid}",
        json={"role": "admin"},
        headers=_auth(client._admin_token),
    )
    assert r.status == 200
    assert (await r.json())["role"] == "admin"


async def test_set_role_non_owner_forbidden(client):
    r = await client.post(
        "/api/spaces",
        json={"name": "RoleGuard"},
        headers=_auth(client._admin_token),
    )
    sid = (await r.json())["id"]
    await client.post(
        f"/api/spaces/{sid}/members",
        json={"user_id": client._bob_uid},
        headers=_auth(client._admin_token),
    )
    r = await client.patch(
        f"/api/spaces/{sid}/members/{client._admin_uid}",
        json={"role": "admin"},
        headers=_auth(client._bob_token),
    )
    assert r.status == 403


async def test_set_role_invalid_value_422(client):
    r = await client.post(
        "/api/spaces",
        json={"name": "BadRole"},
        headers=_auth(client._admin_token),
    )
    sid = (await r.json())["id"]
    await client.post(
        f"/api/spaces/{sid}/members",
        json={"user_id": client._bob_uid},
        headers=_auth(client._admin_token),
    )
    r = await client.patch(
        f"/api/spaces/{sid}/members/{client._bob_uid}",
        json={"role": "overlord"},
        headers=_auth(client._admin_token),
    )
    assert r.status == 422


async def test_ownership_transfer_owner_only(client):
    r = await client.post(
        "/api/spaces",
        json={"name": "Transfer"},
        headers=_auth(client._admin_token),
    )
    sid = (await r.json())["id"]
    await client.post(
        f"/api/spaces/{sid}/members",
        json={"user_id": client._bob_uid},
        headers=_auth(client._admin_token),
    )
    r = await client.post(
        f"/api/spaces/{sid}/ownership",
        json={"to_user_id": client._bob_uid},
        headers=_auth(client._admin_token),
    )
    assert r.status == 200
    body = await r.json()
    assert body["new_owner_user_id"] == client._bob_uid


async def test_ownership_transfer_requires_to_user_id(client):
    r = await client.post(
        "/api/spaces",
        json={"name": "TransferBad"},
        headers=_auth(client._admin_token),
    )
    sid = (await r.json())["id"]
    r = await client.post(
        f"/api/spaces/{sid}/ownership",
        json={},
        headers=_auth(client._admin_token),
    )
    assert r.status == 422


async def test_join_request_lifecycle(client):
    r = await client.post(
        "/api/spaces",
        json={"name": "OpenJoin", "join_mode": "request"},
        headers=_auth(client._admin_token),
    )
    sid = (await r.json())["id"]
    r = await client.post(
        f"/api/spaces/{sid}/join-requests",
        json={"message": "please"},
        headers=_auth(client._bob_token),
    )
    assert r.status == 201
    request_id = (await r.json())["request_id"]
    r = await client.get(
        f"/api/spaces/{sid}/join-requests",
        headers=_auth(client._admin_token),
    )
    assert r.status == 200
    assert len(await r.json()) == 1
    r = await client.post(
        f"/api/spaces/{sid}/join-requests/{request_id}/approve",
        headers=_auth(client._admin_token),
    )
    assert r.status == 200
    r = await client.get(
        f"/api/spaces/{sid}/members",
        headers=_auth(client._admin_token),
    )
    assert any(m["user_id"] == client._bob_uid for m in await r.json())


async def test_join_request_deny_closes_flow(client):
    r = await client.post(
        "/api/spaces",
        json={"name": "DenyMe", "join_mode": "request"},
        headers=_auth(client._admin_token),
    )
    sid = (await r.json())["id"]
    r = await client.post(
        f"/api/spaces/{sid}/join-requests",
        json={},
        headers=_auth(client._bob_token),
    )
    request_id = (await r.json())["request_id"]
    r = await client.post(
        f"/api/spaces/{sid}/join-requests/{request_id}/deny",
        headers=_auth(client._admin_token),
    )
    assert r.status == 200
    assert (await r.json())["status"] == "denied"


async def test_join_request_unknown_action_422(client):
    r = await client.post(
        "/api/spaces",
        json={"name": "Bad", "join_mode": "request"},
        headers=_auth(client._admin_token),
    )
    sid = (await r.json())["id"]
    r = await client.post(
        f"/api/spaces/{sid}/join-requests",
        json={},
        headers=_auth(client._bob_token),
    )
    request_id = (await r.json())["request_id"]
    r = await client.post(
        f"/api/spaces/{sid}/join-requests/{request_id}/smite",
        headers=_auth(client._admin_token),
    )
    assert r.status == 422


async def test_join_requests_list_requires_admin(client):
    r = await client.post(
        "/api/spaces",
        json={"name": "Guarded"},
        headers=_auth(client._admin_token),
    )
    sid = (await r.json())["id"]
    r = await client.get(
        f"/api/spaces/{sid}/join-requests",
        headers=_auth(client._bob_token),
    )
    assert r.status == 403


async def test_space_post_reaction_add_and_remove(client):
    r = await client.post(
        "/api/spaces",
        json={"name": "Reacts"},
        headers=_auth(client._admin_token),
    )
    sid = (await r.json())["id"]
    r = await client.post(
        f"/api/spaces/{sid}/posts",
        json={"type": "text", "content": "Hi"},
        headers=_auth(client._admin_token),
    )
    pid = (await r.json())["id"]
    r = await client.post(
        f"/api/spaces/{sid}/posts/{pid}/reactions",
        json={"emoji": "👍"},
        headers=_auth(client._admin_token),
    )
    assert r.status == 201
    from urllib.parse import quote

    r = await client.delete(
        f"/api/spaces/{sid}/posts/{pid}/reactions/{quote('👍')}",
        headers=_auth(client._admin_token),
    )
    assert r.status == 200


async def test_space_post_comment_happy_path(client):
    r = await client.post(
        "/api/spaces",
        json={"name": "Chatty"},
        headers=_auth(client._admin_token),
    )
    sid = (await r.json())["id"]
    r = await client.post(
        f"/api/spaces/{sid}/posts",
        json={"type": "text", "content": "Topic"},
        headers=_auth(client._admin_token),
    )
    pid = (await r.json())["id"]
    r = await client.post(
        f"/api/spaces/{sid}/posts/{pid}/comments",
        json={"content": "Nice!"},
        headers=_auth(client._admin_token),
    )
    assert r.status == 201
    assert (await r.json())["content"] == "Nice!"


async def test_space_post_comment_empty_content_422(client):
    r = await client.post(
        "/api/spaces",
        json={"name": "EmptyComment"},
        headers=_auth(client._admin_token),
    )
    sid = (await r.json())["id"]
    r = await client.post(
        f"/api/spaces/{sid}/posts",
        json={"type": "text", "content": "Topic"},
        headers=_auth(client._admin_token),
    )
    pid = (await r.json())["id"]
    r = await client.post(
        f"/api/spaces/{sid}/posts/{pid}/comments",
        json={"content": ""},
        headers=_auth(client._admin_token),
    )
    assert r.status == 422


# ─── About markdown + cover image ───────────────────────────────────────

# Tiny valid PNG so the ImageProcessor validates magic bytes and Pillow
# can decode it into a WebP. 1×1 pixel fully transparent.
_TINY_PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4"
    "890000000a49444154789c63000100000500010d0a2db40000000049454e44ae"
    "426082"
)


async def test_space_about_markdown_roundtrips(client):
    r = await client.post(
        "/api/spaces",
        json={"name": "WithAbout"},
        headers=_auth(client._admin_token),
    )
    sid = (await r.json())["id"]
    patch = await client.patch(
        f"/api/spaces/{sid}",
        json={"about_markdown": "## Welcome\n\n**bold** text."},
        headers=_auth(client._admin_token),
    )
    assert patch.status == 200
    get = await client.get(
        f"/api/spaces/{sid}",
        headers=_auth(client._admin_token),
    )
    body = await get.json()
    assert body["about_markdown"] == "## Welcome\n\n**bold** text."


async def test_space_cover_upload_and_fetch(client, tmp_path):
    r = await client.post(
        "/api/spaces",
        json={"name": "WithCover"},
        headers=_auth(client._admin_token),
    )
    sid = (await r.json())["id"]

    # Upload via multipart.
    form = aiohttp.FormData()
    form.add_field(
        "file",
        _TINY_PNG,
        filename="x.png",
        content_type="image/png",
    )
    up = await client.post(
        f"/api/spaces/{sid}/cover",
        data=form,
        headers=_auth(client._admin_token),
    )
    assert up.status == 200
    body = await up.json()
    assert body["cover_hash"]
    assert body["cover_url"].startswith(f"/api/spaces/{sid}/cover?v=")

    # GET streams WebP bytes.
    fetch = await client.get(
        f"/api/spaces/{sid}/cover",
        headers=_auth(client._admin_token),
    )
    assert fetch.status == 200
    assert fetch.headers["Content-Type"] == "image/webp"
    payload = await fetch.read()
    assert payload[:4] == b"RIFF"  # WebP magic

    # GET space detail shows cover_url.
    detail = await client.get(
        f"/api/spaces/{sid}",
        headers=_auth(client._admin_token),
    )
    dbody = await detail.json()
    assert dbody["cover_hash"] == body["cover_hash"]
    assert dbody["cover_url"] == body["cover_url"]

    # DELETE clears it.
    rm = await client.delete(
        f"/api/spaces/{sid}/cover",
        headers=_auth(client._admin_token),
    )
    assert rm.status == 204
    fetch2 = await client.get(
        f"/api/spaces/{sid}/cover",
        headers=_auth(client._admin_token),
    )
    assert fetch2.status == 404


async def test_space_cover_non_admin_forbidden(client):
    r = await client.post(
        "/api/spaces",
        json={"name": "LockedCover"},
        headers=_auth(client._admin_token),
    )
    sid = (await r.json())["id"]
    # Add bob as a plain member.
    await client.post(
        f"/api/spaces/{sid}/members",
        json={"user_id": client._bob_uid},
        headers=_auth(client._admin_token),
    )
    form = aiohttp.FormData()
    form.add_field(
        "file",
        _TINY_PNG,
        filename="x.png",
        content_type="image/png",
    )
    r2 = await client.post(
        f"/api/spaces/{sid}/cover",
        data=form,
        headers=_auth(client._bob_token),
    )
    assert r2.status == 403
