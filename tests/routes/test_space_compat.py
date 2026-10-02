"""Tests for ``GET /api/spaces/{id}/compat`` (per-space version banner, #319 ¶5).

Owner / admin only. Surfaces member households that lag behind this build's
advertised ``proto_version`` and the shared-space features that breaks.
A member household that has never advertised capabilities (NULL
``capabilities_seen_at``) is EXCLUDED — it's mid-handshake, not behind.
"""

from __future__ import annotations

import pytest
from aiohttp.test_utils import TestClient, TestServer

from socialhome.app import create_app
from socialhome.app_keys import db_key as _db_key
from socialhome.auth import sha256_token_hash
from socialhome.config import Config
from socialhome.crypto import derive_user_id
from socialhome.domain.federation_capabilities import (
    OURS,
    space_features_missing_below,
)


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
async def client(tmp_dir):
    """App client with admin (pascal) and regular member (bob)."""
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
        pk = bytes.fromhex(_row["identity_public_key"])
        uid = derive_user_id(pk, "pascal")
        await db.enqueue(
            "INSERT INTO users(username, user_id, display_name, is_admin) VALUES(?,?,?,1)",
            ("pascal", uid, "Pascal"),
        )
        await db.enqueue(
            "INSERT INTO api_tokens(token_id, user_id, label, token_hash) VALUES(?,?,?,?)",
            ("tid-1", uid, "test", sha256_token_hash("admin-token")),
        )
        uid2 = derive_user_id(pk, "bob")
        await db.enqueue(
            "INSERT INTO users(username, user_id, display_name, is_admin) VALUES(?,?,?,0)",
            ("bob", uid2, "Bob"),
        )
        await db.enqueue(
            "INSERT INTO api_tokens(token_id, user_id, label, token_hash) VALUES(?,?,?,?)",
            ("tid-2", uid2, "test", sha256_token_hash("bob-token")),
        )
        tc._db = db
        tc._admin_token = "admin-token"
        tc._bob_token = "bob-token"
        tc._bob_uid = uid2
        yield tc


async def _make_space(client) -> str:
    resp = await client.post(
        "/api/spaces",
        json={"name": "Family", "emoji": "🏠"},
        headers=_auth(client._admin_token),
    )
    assert resp.status == 201
    return (await resp.json())["id"]


async def _seed_member_household(
    client,
    space_id: str,
    *,
    instance_id: str,
    display_name: str,
    proto_version: int,
    capabilities_seen_at: str | None,
) -> None:
    db = client._db
    await db.enqueue(
        """
        INSERT INTO remote_instances(
            id, display_name, remote_identity_pk,
            key_self_to_remote, key_remote_to_self,
            remote_inbox_url, local_inbox_id, status, source,
            proto_version, capabilities_seen_at
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            instance_id,
            display_name,
            "ab" * 32,
            "00",
            "00",
            f"https://{instance_id}.example/inbox/x",
            instance_id + "_local",
            "confirmed",
            "manual",
            proto_version,
            capabilities_seen_at,
        ),
    )
    await db.enqueue(
        "INSERT INTO space_instances(space_id, instance_id) VALUES(?,?)",
        (space_id, instance_id),
    )


async def test_compat_flags_behind_member_household(client):
    """A member household at v13 surfaces in behind_members with the three
    space features it lacks; lagging + min reflect the weakest member."""
    sid = await _make_space(client)
    await _seed_member_household(
        client,
        sid,
        instance_id="peer-13",
        display_name="Brother's house",
        proto_version=13,
        capabilities_seen_at="2026-06-01T00:00:00+00:00",
    )
    resp = await client.get(
        f"/api/spaces/{sid}/compat", headers=_auth(client._admin_token)
    )
    assert resp.status == 200
    body = await resp.json()
    assert body["ours"] == OURS
    assert body["min_member_proto_version"] == 13
    assert body["lagging_features"] == space_features_missing_below(13)
    assert body["lagging_features"]  # non-empty
    assert len(body["behind_members"]) == 1
    bm = body["behind_members"][0]
    assert bm["instance_id"] == "peer-13"
    assert bm["display_name"] == "Brother's house"
    assert bm["proto_version"] == 13
    assert bm["lacking_features"] == space_features_missing_below(13)


async def test_compat_excludes_mid_handshake_member(client):
    """A member that never advertised capabilities is excluded entirely."""
    sid = await _make_space(client)
    await _seed_member_household(
        client,
        sid,
        instance_id="peer-up",
        display_name="Up to date",
        proto_version=OURS,
        capabilities_seen_at="2026-06-01T00:00:00+00:00",
    )
    await _seed_member_household(
        client,
        sid,
        instance_id="peer-mystery",
        display_name="Mid handshake",
        proto_version=1,
        capabilities_seen_at=None,
    )
    resp = await client.get(
        f"/api/spaces/{sid}/compat", headers=_auth(client._admin_token)
    )
    body = await resp.json()
    assert body["min_member_proto_version"] == OURS
    assert body["lagging_features"] == []
    assert body["behind_members"] == []


async def test_compat_requires_admin(client):
    """A non-admin space member is refused (403)."""
    sid = await _make_space(client)
    # Seat bob as a plain member.
    await client.post(
        f"/api/spaces/{sid}/members",
        json={"user_id": client._bob_uid},
        headers=_auth(client._admin_token),
    )
    resp = await client.get(
        f"/api/spaces/{sid}/compat", headers=_auth(client._bob_token)
    )
    assert resp.status == 403


async def test_compat_unknown_space_404(client):
    """An unknown space id maps to 404."""
    resp = await client.get(
        "/api/spaces/does-not-exist/compat", headers=_auth(client._admin_token)
    )
    assert resp.status == 404


# ─── PATCH features: PEERS_TOO_OLD (§4.3, v_42) ──────────────────────────


async def test_raising_an_access_level_with_an_old_household_is_409(client):
    """A member household below v_42 neither enforces an access level for
    its people nor names the actor of its writes: the admin is told which
    households lag, and nothing changes until they apply it anyway."""
    sid = await _make_space(client)
    await _seed_member_household(
        client,
        sid,
        instance_id="peer-41",
        display_name="Granny's house",
        proto_version=41,
        capabilities_seen_at="2026-06-01T00:00:00+00:00",
    )
    admin = _auth(client._admin_token)
    resp = await client.patch(
        f"/api/spaces/{sid}",
        json={"features": {"tasks_access": "admin_only"}},
        headers=admin,
    )
    assert resp.status == 409, await resp.text()
    err = (await resp.json())["error"]
    assert err["code"] == "PEERS_TOO_OLD"
    assert err["households"] == [
        {
            "instance_id": "peer-41",
            "display_name": "Granny's house",
            "proto_version": 41,
        }
    ]
    space = await (await client.get(f"/api/spaces/{sid}", headers=admin)).json()
    assert space["features"]["tasks_access"] == "open"

    resp = await client.patch(
        f"/api/spaces/{sid}",
        json={"features": {"tasks_access": "admin_only"}, "force": True},
        headers=admin,
    )
    assert resp.status == 200, await resp.text()
    space = await (await client.get(f"/api/spaces/{sid}", headers=admin)).json()
    assert space["features"]["tasks_access"] == "admin_only"


async def test_other_feature_edits_are_never_held_up(client):
    sid = await _make_space(client)
    await _seed_member_household(
        client,
        sid,
        instance_id="peer-41",
        display_name="Granny's house",
        proto_version=41,
        capabilities_seen_at="2026-06-01T00:00:00+00:00",
    )
    resp = await client.patch(
        f"/api/spaces/{sid}",
        json={"features": {"bazaar": False}, "name": "Renamed"},
        headers=_auth(client._admin_token),
    )
    assert resp.status == 200, await resp.text()


async def test_an_edit_forwarded_to_the_host_says_so(client):
    """On a space hosted by another household the PATCH only forwards: the
    answer carries ``forwarded`` so the SPA never shows it as applied."""
    from unittest.mock import AsyncMock, patch

    from socialhome.services.space_service import SpaceService

    db = client._db
    await db.enqueue(
        "INSERT INTO spaces(id, name, owner_instance_id, owner_username,"
        " identity_public_key) VALUES('sp-remote', 'R', 'inst-host', 'h', ?)",
        ("ab" * 32,),
    )
    row = await db.fetchone("SELECT user_id FROM users WHERE username='pascal'")
    await db.enqueue(
        "INSERT INTO space_members(space_id, user_id, role)"
        " VALUES('sp-remote', ?, 'admin')",
        (row["user_id"],),
    )
    fwd = AsyncMock(return_value=True)
    with patch.object(SpaceService, "_forward_admin_action_if_remote", fwd):
        resp = await client.patch(
            "/api/spaces/sp-remote",
            json={"features": {"tasks_access": "admin_only"}},
            headers=_auth(client._admin_token),
        )
    assert resp.status == 200, await resp.text()
    assert (await resp.json())["forwarded"] is True
    assert fwd.await_count == 1
    sid = await _make_space(client)
    resp = await client.patch(
        f"/api/spaces/{sid}", json={"name": "Local"}, headers=_auth(client._admin_token)
    )
    assert (await resp.json()).get("forwarded") is False
