"""Tests for GET /moderation and approve/reject + bans list/unban routes."""

from __future__ import annotations

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
        row = await db.fetchone(
            "SELECT identity_public_key FROM instance_identity WHERE id='self'",
        )
        pk = bytes.fromhex(row["identity_public_key"])

        class _KP:
            public_key = pk

        admin_uid = derive_user_id(_KP.public_key, "pascal")
        bob_uid = derive_user_id(_KP.public_key, "bob")
        await db.enqueue(
            "INSERT INTO users(username, user_id, display_name, is_admin) VALUES(?,?,?,1)",
            ("pascal", admin_uid, "Pascal"),
        )
        await db.enqueue(
            "INSERT INTO users(username, user_id, display_name, is_admin) VALUES(?,?,?,0)",
            ("bob", bob_uid, "Bob"),
        )
        await db.enqueue(
            "INSERT INTO api_tokens(token_id, user_id, label, token_hash) VALUES(?,?,?,?)",
            ("tid-admin", admin_uid, "test", sha256_token_hash("admin-token")),
        )
        await db.enqueue(
            "INSERT INTO api_tokens(token_id, user_id, label, token_hash) VALUES(?,?,?,?)",
            ("tid-bob", bob_uid, "test", sha256_token_hash("bob-token")),
        )
        tc._admin_token = "admin-token"
        tc._admin_uid = admin_uid
        tc._bob_token = "bob-token"
        tc._bob_uid = bob_uid
        yield tc


async def _seed_moderated_space(client):
    """Build a space, make it MODERATED, add bob as a member. Returns space id."""
    r = await client.post(
        "/api/spaces",
        json={"name": "ModSpace"},
        headers=_auth(client._admin_token),
    )
    sid = (await r.json())["id"]
    # ``POST /members`` now creates a pending invite (parity with the
    # cross-household flow); the invitee accepts before being seated.
    r2 = await client.post(
        f"/api/spaces/{sid}/members",
        json={"user_id": client._bob_uid},
        headers=_auth(client._admin_token),
    )
    invitation_id = (await r2.json())["invitation_id"]
    await client.post(
        f"/api/local_invites/{invitation_id}/accept",
        json={},
        headers=_auth(client._bob_token),
    )
    # Flip posts_access to moderated via PATCH.
    from socialhome.app_keys import space_service_key

    app = client.server.app
    from socialhome.domain.space import SpaceFeatures, SpaceFeatureAccess

    await app[space_service_key].update_config(
        sid,
        actor_username="pascal",
        features=SpaceFeatures(posts_access=SpaceFeatureAccess.MODERATED),
    )
    # Bob submits a post → lands in queue.
    await app[space_service_key].create_post(
        sid,
        author_user_id=client._bob_uid,
        type="text",
        content="please review",
    )
    return sid


async def test_get_moderation_queue_lists_pending(client):
    sid = await _seed_moderated_space(client)
    resp = await client.get(
        f"/api/spaces/{sid}/moderation",
        headers=_auth(client._admin_token),
    )
    assert resp.status == 200
    items = await resp.json()
    assert len(items) == 1
    assert items[0]["status"] == "pending"
    assert items[0]["submitted_by"] == client._bob_uid


async def test_approve_moderation_item(client):
    sid = await _seed_moderated_space(client)
    items = await (
        await client.get(
            f"/api/spaces/{sid}/moderation",
            headers=_auth(client._admin_token),
        )
    ).json()
    item_id = items[0]["id"]
    resp = await client.post(
        f"/api/spaces/{sid}/moderation/{item_id}/approve",
        headers=_auth(client._admin_token),
    )
    assert resp.status == 200
    body = await resp.json()
    assert body["status"] == "approved"
    assert body["post_id"]
    # No more pending.
    assert (
        await (
            await client.get(
                f"/api/spaces/{sid}/moderation",
                headers=_auth(client._admin_token),
            )
        ).json()
    ) == []


async def test_reject_moderation_item_captures_reason(client):
    sid = await _seed_moderated_space(client)
    items = await (
        await client.get(
            f"/api/spaces/{sid}/moderation",
            headers=_auth(client._admin_token),
        )
    ).json()
    item_id = items[0]["id"]
    resp = await client.post(
        f"/api/spaces/{sid}/moderation/{item_id}/reject",
        json={"reason": "off-topic"},
        headers=_auth(client._admin_token),
    )
    assert resp.status == 200
    assert (await resp.json())["status"] == "rejected"


async def test_approve_forbidden_for_non_admin(client):
    sid = await _seed_moderated_space(client)
    items = await (
        await client.get(
            f"/api/spaces/{sid}/moderation",
            headers=_auth(client._admin_token),
        )
    ).json()
    item_id = items[0]["id"]
    resp = await client.post(
        f"/api/spaces/{sid}/moderation/{item_id}/approve",
        headers=_auth(client._bob_token),
    )
    assert resp.status == 403


async def test_double_approve_is_409(client):
    sid = await _seed_moderated_space(client)
    items = await (
        await client.get(
            f"/api/spaces/{sid}/moderation",
            headers=_auth(client._admin_token),
        )
    ).json()
    item_id = items[0]["id"]
    await client.post(
        f"/api/spaces/{sid}/moderation/{item_id}/approve",
        headers=_auth(client._admin_token),
    )
    resp = await client.post(
        f"/api/spaces/{sid}/moderation/{item_id}/approve",
        headers=_auth(client._admin_token),
    )
    assert resp.status == 409


async def test_unknown_item_is_404(client):
    r = await client.post(
        "/api/spaces",
        json={"name": "X"},
        headers=_auth(client._admin_token),
    )
    sid = (await r.json())["id"]
    resp = await client.post(
        f"/api/spaces/{sid}/moderation/does-not-exist/approve",
        headers=_auth(client._admin_token),
    )
    assert resp.status == 404


# ── Ban list + unban ──────────────────────────────────────────────────


async def test_list_bans_then_unban(client):
    # Create, add bob, ban bob, list, unban.
    r = await client.post(
        "/api/spaces",
        json={"name": "BanList"},
        headers=_auth(client._admin_token),
    )
    sid = (await r.json())["id"]
    await client.post(
        f"/api/spaces/{sid}/members",
        json={"user_id": client._bob_uid},
        headers=_auth(client._admin_token),
    )
    await client.post(
        f"/api/spaces/{sid}/ban",
        json={"user_id": client._bob_uid, "reason": "test"},
        headers=_auth(client._admin_token),
    )
    resp = await client.get(
        f"/api/spaces/{sid}/bans",
        headers=_auth(client._admin_token),
    )
    assert resp.status == 200
    bans = await resp.json()
    assert any(b["user_id"] == client._bob_uid for b in bans)
    resp = await client.delete(
        f"/api/spaces/{sid}/bans/{client._bob_uid}",
        headers=_auth(client._admin_token),
    )
    assert resp.status == 200
    bans_after = await (
        await client.get(
            f"/api/spaces/{sid}/bans",
            headers=_auth(client._admin_token),
        )
    ).json()
    assert not any(b["user_id"] == client._bob_uid for b in bans_after)


async def test_ban_list_forbidden_for_non_admin(client):
    r = await client.post(
        "/api/spaces",
        json={"name": "BanList2"},
        headers=_auth(client._admin_token),
    )
    sid = (await r.json())["id"]
    resp = await client.get(
        f"/api/spaces/{sid}/bans",
        headers=_auth(client._bob_token),
    )
    assert resp.status == 403


# ── v_41 moderator seat over HTTP ─────────────────────────────────────


async def _add_user(client, name: str) -> tuple[str, str]:
    """Insert a household user + API token. Returns (user_id, token)."""
    db = client.server.app[_db_key]
    row = await db.fetchone(
        "SELECT identity_public_key FROM instance_identity WHERE id='self'",
    )
    uid = derive_user_id(bytes.fromhex(row["identity_public_key"]), name)
    await db.enqueue(
        "INSERT INTO users(username, user_id, display_name, is_admin) VALUES(?,?,?,0)",
        (name, uid, name.title()),
    )
    token = f"{name}-token"
    await db.enqueue(
        "INSERT INTO api_tokens(token_id, user_id, label, token_hash) VALUES(?,?,?,?)",
        (f"tid-{name}", uid, "test", sha256_token_hash(token)),
    )
    return uid, token


async def _seat(client, sid: str, uid: str, token: str) -> None:
    r = await client.post(
        f"/api/spaces/{sid}/members",
        json={"user_id": uid},
        headers=_auth(client._admin_token),
    )
    invitation_id = (await r.json())["invitation_id"]
    r2 = await client.post(
        f"/api/local_invites/{invitation_id}/accept",
        json={},
        headers=_auth(token),
    )
    assert r2.status == 200, await r2.text()


async def _set_role(client, sid, uid, role, token):
    return await client.patch(
        f"/api/spaces/{sid}/members/{uid}",
        json={"role": role},
        headers=_auth(token),
    )


async def _moderator_space(client):
    """ModSpace (bob's post queued) + cara as moderator, olga as admin."""
    sid = await _seed_moderated_space(client)
    cara_uid, cara = await _add_user(client, "cara")
    olga_uid, olga = await _add_user(client, "olga")
    await _seat(client, sid, cara_uid, cara)
    await _seat(client, sid, olga_uid, olga)
    r = await _set_role(client, sid, cara_uid, "moderator", client._admin_token)
    assert r.status == 200, await r.text()
    assert (await r.json())["role"] == "moderator"
    r = await _set_role(client, sid, olga_uid, "admin", client._admin_token)
    assert r.status == 200, await r.text()
    return sid, (cara_uid, cara), (olga_uid, olga)


async def test_a_moderator_lists_and_approves_the_queue(client):
    sid, (_cuid, cara), _olga = await _moderator_space(client)
    resp = await client.get(f"/api/spaces/{sid}/moderation", headers=_auth(cara))
    assert resp.status == 200
    [item] = await resp.json()
    resp = await client.post(
        f"/api/spaces/{sid}/moderation/{item['id']}/approve",
        headers=_auth(cara),
    )
    assert resp.status == 200
    assert (await resp.json())["status"] == "approved"


async def test_a_moderator_rejects_from_the_queue(client):
    sid, (_cuid, cara), _olga = await _moderator_space(client)
    [item] = await (
        await client.get(f"/api/spaces/{sid}/moderation", headers=_auth(cara))
    ).json()
    resp = await client.post(
        f"/api/spaces/{sid}/moderation/{item['id']}/reject",
        json={"reason": "no"},
        headers=_auth(cara),
    )
    assert resp.status == 200


async def test_a_moderator_gets_403_on_settings_and_roles(client):
    sid, (_cuid, cara), _olga = await _moderator_space(client)
    r = await client.patch(
        f"/api/spaces/{sid}", json={"name": "Mine"}, headers=_auth(cara)
    )
    assert r.status == 403
    r = await _set_role(client, sid, client._bob_uid, "moderator", cara)
    assert r.status == 403
    r = await client.get(f"/api/spaces/{sid}/bans", headers=_auth(cara))
    assert r.status == 403
    r = await client.delete(
        f"/api/spaces/{sid}/members/{client._bob_uid}", headers=_auth(cara)
    )
    assert r.status == 403


async def test_an_admin_promotes_a_member_to_moderator_but_not_to_admin(client):
    sid, _cara, (_ouid, olga) = await _moderator_space(client)
    r = await _set_role(client, sid, client._bob_uid, "moderator", olga)
    assert r.status == 200, await r.text()
    r = await _set_role(client, sid, client._bob_uid, "member", olga)
    assert r.status == 200, await r.text()
    r = await _set_role(client, sid, client._bob_uid, "admin", olga)
    assert r.status == 403
    body = await r.json()
    assert "cannot change" in str(body)


async def test_member_role_route_rejects_unknown_roles(client):
    sid = await _seed_moderated_space(client)
    for role in ("owner", "subscriber", "overlord"):
        r = await _set_role(client, sid, client._bob_uid, role, client._admin_token)
        assert r.status == 422, role


async def test_remote_member_role_route_accepts_moderator(client):
    """The remote-member PATCH passes ``moderator`` to the service (404 for
    a seat that doesn't exist — not the 422 of an unknown role)."""
    sid = await _seed_moderated_space(client)
    r = await client.patch(
        f"/api/spaces/{sid}/remote-members/peer-x/ru-1",
        json={"role": "moderator"},
        headers=_auth(client._admin_token),
    )
    assert r.status != 422, await r.text()
    r = await client.patch(
        f"/api/spaces/{sid}/remote-members/peer-x/ru-1",
        json={"role": "owner"},
        headers=_auth(client._admin_token),
    )
    assert r.status == 422


async def test_promoting_a_behind_household_answers_a_stable_code(client):
    """The SPA maps the refusal by code to translated copy — never by the
    server's English sentence."""
    sid = await _seed_moderated_space(client)
    db = client.server.app[_db_key]
    await db.enqueue(
        "INSERT INTO space_remote_members(space_id, instance_id, user_id, role)"
        " VALUES(?,?,?,'member')",
        (sid, "peer-old", "ru-old"),
    )
    r = await client.patch(
        f"/api/spaces/{sid}/remote-members/peer-old/ru-old",
        json={"role": "moderator"},
        headers=_auth(client._admin_token),
    )
    assert r.status == 403
    body = await r.json()
    assert body["error"]["code"] == "HOUSEHOLD_UPGRADE_REQUIRED"
