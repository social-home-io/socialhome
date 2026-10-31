"""Group-table routes (v_76) — ``/api/apps/{app_id}/groups…`` against the
real app: a table of three people of one household (no envelope leaves)."""

from __future__ import annotations

from socialhome.auth import sha256_token_hash
from socialhome.domain.apps import AppManifest, InstalledApp
from socialhome.repositories.app_repo import SqliteAppRepo

from .conftest import _auth

APP = "quiz"


async def _install(db, *, capabilities=("storage", "people"), tables=True) -> None:
    await SqliteAppRepo(db).install(
        InstalledApp(
            app_id=APP,
            name="Quiz",
            version="1.0.0",
            enabled=True,
            manifest=AppManifest(
                entry="index.html",
                icon=None,
                capabilities=capabilities,
                players_min=3,
                players_max=6,
                tables=tables,
            ),
            bundle_path="apps/quiz/1.0.0",
            bundle_sha256="ab" * 32,
            source_url="https://example/quiz.tgz",
            installed_by=None,
            installed_at="2026-10-01T00:00:00+00:00",
        )
    )


async def _member(db, username: str) -> str:
    await db.enqueue(
        "INSERT INTO users(username, user_id, display_name) VALUES(?,?,?)",
        (username, f"{username}-id", username.title()),
    )
    raw = f"tok-{username}"
    await db.enqueue(
        "INSERT INTO api_tokens(token_id, user_id, label, token_hash) VALUES(?,?,?,?)",
        (f"t-{username}", f"{username}-id", "t", sha256_token_hash(raw)),
    )
    return raw


def _local(username: str) -> dict:
    return {"instance_id": "", "user_ref": f"{username}-id", "is_local": True}


async def _open(client, own: str, **body):
    targets = [
        {**_local("bob"), "instance_id": own},
        {**_local("carol"), "instance_id": own},
    ]
    return await client.post(
        f"/api/apps/{APP}/groups",
        json={"targets": targets, **body},
        headers=_auth(client._tok),
    )


async def _own(client) -> str:
    row = await client._db.fetchone("SELECT instance_id FROM instance_identity")
    return row["instance_id"]


async def test_a_whole_table_over_the_routes(client):
    db = client._db
    await _install(db)
    bob, carol = await _member(db, "bob"), await _member(db, "carol")
    own = await _own(client)

    r = await _open(client, own, note="Snacks", lobby_days=1)
    assert r.status == 201, await r.text()
    group = (await r.json())["group"]
    sid = group["session_id"]
    assert group["is_host"] and group["note"] == "Snacks" and group["invited"] == 2

    r = await client.get(f"/api/apps/{APP}/groups", headers=_auth(bob))
    listed = (await r.json())["groups"]
    assert [g["my_state"] for g in listed] == ["invited"]
    assert "user" not in listed[0]["players"][0]  # seats, never usernames

    base = f"/api/apps/{APP}/groups/{sid}"
    r = await client.post(f"{base}/start", headers=_auth(client._tok))
    assert r.status == 409
    assert (await r.json())["error"]["code"] == "APP_GROUP_TOO_FEW"
    r = await client.post(f"{base}/start", headers=_auth(bob))
    assert r.status == 403  # not the host
    for tok in (bob, carol):
        r = await client.post(f"{base}/join", headers=_auth(tok))
        assert r.status == 200, await r.text()
    r = await client.post(f"{base}/start", headers=_auth(client._tok))
    assert (await r.json())["group"]["state"] == "playing"

    r = await client.post(
        f"{base}/remove", json={"seat": 3}, headers=_auth(client._tok)
    )
    assert r.status == 200
    r = await client.get(base, headers=_auth(carol))
    assert r.status == 404
    r = await client.post(f"{base}/leave", headers=_auth(bob))
    assert r.status == 200
    r = await client.post(f"{base}/close", headers=_auth(client._tok))
    assert r.status == 200
    r = await client.get(base, headers=_auth(client._tok))
    body = (await r.json())["group"]
    assert (body["state"], body["end_reason"]) == ("ended", "cancelled")


async def test_decline(client):
    db = client._db
    await _install(db)
    bob = await _member(db, "bob")
    await _member(db, "carol")
    sid = (await (await _open(client, await _own(client))).json())["group"][
        "session_id"
    ]
    r = await client.post(f"/api/apps/{APP}/groups/{sid}/decline", headers=_auth(bob))
    assert r.status == 200
    r = await client.post(f"/api/apps/{APP}/groups/{sid}/decline", headers=_auth(bob))
    assert r.status == 404


async def test_bodies_out_of_shape_are_refused(client):
    db = client._db
    await _install(db)
    await _member(db, "bob")
    await _member(db, "carol")
    own = await _own(client)
    for body in (
        {},
        {"targets": []},
        {"targets": ["bob"]},
        {"targets": [_local("bob")], "max_players": "4"},
        {"targets": [_local("bob")], "lobby_days": True},
        {"targets": [_local("bob")], "note": 7},
    ):
        r = await client.post(
            f"/api/apps/{APP}/groups", json=body, headers=_auth(client._tok)
        )
        assert r.status == 400, body
    r = await _open(client, own, lobby_days=2)
    assert r.status == 422
    r = await _open(client, own, note="x" * 81)
    assert r.status == 422
    sid = (await (await _open(client, own)).json())["group"]["session_id"]
    for seat in (0, "2", None):
        r = await client.post(
            f"/api/apps/{APP}/groups/{sid}/remove",
            json={"seat": seat},
            headers=_auth(client._tok),
        )
        assert r.status == 400


async def test_an_app_without_tables_or_people_is_refused(client):
    db = client._db
    await _install(db, tables=False)
    await _member(db, "bob")
    await _member(db, "carol")
    r = await _open(client, await _own(client))
    assert (await r.json())["error"]["code"] == "APP_GROUPS_NOT_SUPPORTED"
    await SqliteAppRepo(db).uninstall(APP)
    await _install(db, capabilities=("storage",))
    r = await client.get(f"/api/apps/{APP}/groups", headers=_auth(client._tok))
    assert r.status == 403
    assert (await r.json())["error"]["code"] == "APP_PERMISSION_DENIED"


async def test_unknown_table_is_not_found(client):
    await _install(client._db)
    r = await client.get(f"/api/apps/{APP}/groups/nope", headers=_auth(client._tok))
    assert r.status == 404
    assert (await r.json())["error"]["code"] == "APP_GROUP_NOT_FOUND"


async def test_groups_need_auth(client):
    r = await client.get(f"/api/apps/{APP}/groups")
    assert r.status == 401
