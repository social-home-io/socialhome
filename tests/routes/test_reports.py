"""Tests for /api/reports + /api/admin/reports."""

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
            ("tid-admin", admin_uid, "t", sha256_token_hash("admin-token")),
        )
        await db.enqueue(
            "INSERT INTO api_tokens(token_id, user_id, label, token_hash) VALUES(?,?,?,?)",
            ("tid-bob", bob_uid, "t", sha256_token_hash("bob-token")),
        )
        tc._admin_token = "admin-token"
        tc._admin_uid = admin_uid
        tc._bob_token = "bob-token"
        tc._bob_uid = bob_uid
        yield tc


async def test_create_report_201(client):
    resp = await client.post(
        "/api/reports",
        json={
            "target_type": "user",
            "target_id": client._admin_uid,
            "category": "spam",
        },
        headers=_auth(client._bob_token),
    )
    assert resp.status == 201
    body = await resp.json()
    assert body["status"] == "pending"
    # ``federated`` is always returned; false for a target with no hosting peer.
    assert body["federated"] is False


async def test_create_report_missing_fields_422(client):
    resp = await client.post(
        "/api/reports",
        json={"target_type": "post"},
        headers=_auth(client._bob_token),
    )
    assert resp.status == 422


async def test_duplicate_report_409(client):
    payload = {
        "target_type": "user",
        "target_id": client._admin_uid,
        "category": "spam",
    }
    r = await client.post(
        "/api/reports", json=payload, headers=_auth(client._bob_token)
    )
    assert r.status == 201
    r2 = await client.post(
        "/api/reports", json=payload, headers=_auth(client._bob_token)
    )
    assert r2.status == 409


async def test_admin_list_reports(client):
    await client.post(
        "/api/reports",
        json={
            "target_type": "user",
            "target_id": client._admin_uid,
            "category": "spam",
        },
        headers=_auth(client._bob_token),
    )
    resp = await client.get(
        "/api/admin/reports",
        headers=_auth(client._admin_token),
    )
    assert resp.status == 200
    body = await resp.json()
    assert len(body) == 1
    assert body[0]["status"] == "pending"


async def test_admin_list_forbidden_for_non_admin(client):
    resp = await client.get(
        "/api/admin/reports",
        headers=_auth(client._bob_token),
    )
    assert resp.status == 403


async def test_admin_resolve_report(client):
    r = await client.post(
        "/api/reports",
        json={
            "target_type": "user",
            "target_id": client._admin_uid,
            "category": "spam",
        },
        headers=_auth(client._bob_token),
    )
    report_id = (await r.json())["id"]
    resp = await client.post(
        f"/api/admin/reports/{report_id}/resolve",
        headers=_auth(client._admin_token),
    )
    assert resp.status == 200
    listed = await (
        await client.get(
            "/api/admin/reports",
            headers=_auth(client._admin_token),
        )
    ).json()
    assert listed == []


async def test_admin_resolve_twice_409(client):
    r = await client.post(
        "/api/reports",
        json={
            "target_type": "user",
            "target_id": client._admin_uid,
            "category": "spam",
        },
        headers=_auth(client._bob_token),
    )
    report_id = (await r.json())["id"]
    await client.post(
        f"/api/admin/reports/{report_id}/resolve",
        headers=_auth(client._admin_token),
    )
    resp = await client.post(
        f"/api/admin/reports/{report_id}/resolve",
        headers=_auth(client._admin_token),
    )
    assert resp.status == 409


# ── Space-scoped reports: the space's content authority triages ──────────


async def _person(client, name, *, admin=False):
    db = client.app[_db_key]
    row = await db.fetchone(
        "SELECT identity_public_key FROM instance_identity WHERE id='self'",
    )
    uid = derive_user_id(bytes.fromhex(row["identity_public_key"]), name)
    await db.enqueue(
        "INSERT INTO users(username, user_id, display_name, is_admin) VALUES(?,?,?,?)",
        (name, uid, name.title(), 1 if admin else 0),
    )
    await db.enqueue(
        "INSERT INTO api_tokens(token_id, user_id, label, token_hash) VALUES(?,?,?,?)",
        (f"tid-{name}", uid, "t", sha256_token_hash(f"{name}-token")),
    )
    return uid, f"{name}-token"


@pytest.fixture
async def space_client(client):
    """Space ``spA`` owned by olga, moderated by mona, bob a member;
    pascal (household admin) holds NO seat. Space ``spB`` has its own
    moderator, max."""
    db = client.app[_db_key]
    own = await db.fetchone("SELECT instance_id FROM instance_identity WHERE id='self'")
    olga, olga_t = await _person(client, "olga")
    mona, mona_t = await _person(client, "mona")
    maxi, max_t = await _person(client, "max")
    for sid in ("spA", "spB"):
        await db.enqueue(
            "INSERT INTO spaces(id, name, owner_instance_id, owner_username,"
            " identity_public_key) VALUES(?,?,?,?,?)",
            (sid, f"Space {sid}", own["instance_id"], "olga", "ab" * 32),
        )
    for sid, uid, role in (
        ("spA", olga, "owner"),
        ("spA", mona, "moderator"),
        ("spA", client._bob_uid, "member"),
        ("spB", olga, "owner"),
        ("spB", maxi, "moderator"),
    ):
        await db.enqueue(
            "INSERT INTO space_members(space_id, user_id, role) VALUES(?,?,?)",
            (sid, uid, role),
        )
    await db.enqueue(
        "INSERT INTO space_posts(id, space_id, author, type, content)"
        " VALUES('pA','spA',?,'text','hello')",
        ("u-author",),
    )
    client._tok = {"olga": olga_t, "mona": mona_t, "max": max_t}
    client._mona_uid = mona
    return client


async def _file(c, **body):
    return await c.post(
        "/api/reports",
        json={"category": "spam", **body},
        headers=_auth(c._bob_token),
    )


async def test_space_post_report_goes_to_space_moderators(space_client):
    c = space_client
    resp = await _file(c, target_type="post", target_id="pA", notes="rude")
    assert resp.status == 201
    body = await resp.json()
    assert body["space_id"] == "spA"
    rid = body["id"]

    # The moderator sees it, with the reporter's name.
    resp = await c.get("/api/spaces/spA/reports", headers=_auth(c._tok["mona"]))
    assert resp.status == 200
    rows = await resp.json()
    assert [r["id"] for r in rows] == [rid]
    assert rows[0]["reporter_name"] == "Bob"
    assert rows[0]["space_id"] == "spA"
    assert rows[0]["notes"] == "rude"
    assert rows[0]["target_preview"] == "hello"
    assert rows[0]["target_gone"] is False

    # The household admin's queue never shows it.
    resp = await c.get("/api/admin/reports", headers=_auth(c._admin_token))
    assert all(r["id"] != rid for r in await resp.json())

    # The moderator resolves it.
    resp = await c.post(
        f"/api/spaces/spA/reports/{rid}/resolve",
        json={"dismissed": False},
        headers=_auth(c._tok["mona"]),
    )
    assert resp.status == 200
    assert (await resp.json())["status"] == "resolved"
    resp = await c.get("/api/spaces/spA/reports", headers=_auth(c._tok["olga"]))
    assert await resp.json() == []
    resp = await c.post(
        f"/api/spaces/spA/reports/{rid}/resolve",
        headers=_auth(c._tok["olga"]),
    )
    assert resp.status == 409


async def test_space_reports_forbidden_for_plain_member(space_client):
    c = space_client
    rid = (await (await _file(c, target_type="post", target_id="pA")).json())["id"]
    resp = await c.get("/api/spaces/spA/reports", headers=_auth(c._bob_token))
    assert resp.status == 403
    resp = await c.post(
        f"/api/spaces/spA/reports/{rid}/resolve", headers=_auth(c._bob_token)
    )
    assert resp.status == 403


async def test_household_admin_without_seat_cannot_see_space_reports(space_client):
    c = space_client
    rid = (await (await _file(c, target_type="post", target_id="pA")).json())["id"]
    resp = await c.get("/api/spaces/spA/reports", headers=_auth(c._admin_token))
    assert resp.status == 403
    resp = await c.post(
        f"/api/spaces/spA/reports/{rid}/resolve", headers=_auth(c._admin_token)
    )
    assert resp.status == 403
    # Nor through the household admin endpoint.
    resp = await c.post(
        f"/api/admin/reports/{rid}/resolve", headers=_auth(c._admin_token)
    )
    assert resp.status == 404


async def test_cross_space_report_id_is_404(space_client):
    c = space_client
    rid = (await (await _file(c, target_type="post", target_id="pA")).json())["id"]
    # max moderates spB, not spA: resolving spA's report through spB is 404.
    resp = await c.post(
        f"/api/spaces/spB/reports/{rid}/resolve", headers=_auth(c._tok["max"])
    )
    assert resp.status == 404
    resp = await c.get("/api/spaces/spB/reports", headers=_auth(c._tok["max"]))
    assert await resp.json() == []


async def test_unknown_space_reports_404(space_client):
    c = space_client
    resp = await c.get("/api/spaces/nope/reports", headers=_auth(c._tok["mona"]))
    assert resp.status == 404


async def test_member_report_names_the_space(space_client):
    c = space_client
    resp = await _file(c, target_type="user", target_id=c._mona_uid, space_id="spA")
    assert resp.status == 201
    resp = await c.get("/api/spaces/spA/reports", headers=_auth(c._tok["olga"]))
    rows = await resp.json()
    assert rows[0]["target_type"] == "user"
    assert rows[0]["target_name"] == "Mona"
    assert rows[0]["target_gone"] is False


async def test_report_naming_the_wrong_space_404(space_client):
    c = space_client
    resp = await _file(c, target_type="post", target_id="pA", space_id="spB")
    assert resp.status == 404


async def test_report_space_id_must_be_string_422(space_client):
    resp = await _file(space_client, target_type="post", target_id="pA", space_id=3)
    assert resp.status == 422


async def test_deleted_post_report_reads_gone(space_client):
    c = space_client
    await _file(c, target_type="post", target_id="pA")
    await c.app[_db_key].enqueue("UPDATE space_posts SET deleted=1 WHERE id='pA'")
    resp = await c.get("/api/spaces/spA/reports", headers=_auth(c._tok["mona"]))
    rows = await resp.json()
    assert rows[0]["target_gone"] is True
    assert rows[0]["target_preview"] is None


async def test_notes_over_1000_chars_422(space_client):
    resp = await _file(
        space_client, target_type="post", target_id="pA", notes="x" * 1001
    )
    assert resp.status == 422
    resp = await _file(space_client, target_type="post", target_id="pA", notes=5)
    assert resp.status == 422


async def test_forwarded_to_gfs_reflects_what_happened(space_client):
    # No GFS is paired here: nothing is forwarded, whatever was asked.
    body = await (await _file(space_client, target_type="post", target_id="pA")).json()
    assert body["forwarded_to_gfs"] is False


async def test_unknown_post_is_404_like_a_post_in_a_space_you_are_not_in(space_client):
    c = space_client
    r1 = await c.post(
        "/api/reports",
        json={"target_type": "post", "target_id": "pA", "category": "spam"},
        headers=_auth(c._tok["max"]),  # not in spA
    )
    r2 = await c.post(
        "/api/reports",
        json={"target_type": "post", "target_id": "nope", "category": "spam"},
        headers=_auth(c._tok["max"]),
    )
    assert r1.status == r2.status == 404
    assert await r1.json() == await r2.json()


async def test_sole_owner_sees_a_report_about_themself_anonymously(space_client):
    c = space_client
    db = c.app[_db_key]
    olga = (await db.fetchone("SELECT user_id FROM users WHERE username='olga'"))[
        "user_id"
    ]
    await db.enqueue(
        "UPDATE space_members SET role='member' WHERE space_id='spA' AND role='moderator'"
    )
    rid = (
        await (
            await _file(
                c,
                target_type="user",
                target_id=olga,
                space_id="spA",
                notes="it was me, Bob, from Tuesday",
            )
        ).json()
    )["id"]
    rows = await (
        await c.get("/api/spaces/spA/reports", headers=_auth(c._tok["olga"]))
    ).json()
    assert rows[0]["id"] == rid
    assert rows[0]["anonymous"] is True and rows[0]["dismiss_only"] is True
    assert rows[0]["reporter_user_id"] is None and rows[0]["reporter_name"] is None
    # N1 — not their words either: notes could name the reporter.
    assert rows[0]["notes"] is None
    assert rows[0]["reporter_instance_id"] is None
    resp = await c.post(
        f"/api/spaces/spA/reports/{rid}/resolve", json={}, headers=_auth(c._tok["olga"])
    )
    assert resp.status == 403
    resp = await c.post(
        f"/api/spaces/spA/reports/{rid}/resolve",
        json={"dismissed": True},
        headers=_auth(c._tok["olga"]),
    )
    assert resp.status == 200
