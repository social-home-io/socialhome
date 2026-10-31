"""Space games routes (v_77) — ``/api/spaces/{space_id}/games…`` against the
real app: one household, a space with an owner, a moderator, a member and a
follower; the games list, opening, joining and closing a table."""

from __future__ import annotations

from socialhome.auth import sha256_token_hash
from socialhome.domain.apps import AppManifest, InstalledApp
from socialhome.repositories.app_repo import SqliteAppRepo

from .conftest import _auth

APP = "quiz"


async def _install(db) -> None:
    await SqliteAppRepo(db).install(
        InstalledApp(
            app_id=APP,
            name="Quiz",
            version="1.0.0",
            enabled=True,
            manifest=AppManifest(
                entry="index.html",
                icon=None,
                capabilities=("storage", "people", "federation"),
                players_min=2,
                players_max=4,
                tables=True,
            ),
            bundle_path="apps/quiz/1.0.0",
            bundle_sha256="ab" * 32,
            source_url="https://example/quiz.tgz",
            installed_by=None,
            installed_at="2026-10-01T00:00:00+00:00",
        )
    )


async def _person(db, username: str, space_id: str, role: str) -> str:
    await db.enqueue(
        "INSERT INTO users(username, user_id, display_name) VALUES(?,?,?)",
        (username, f"{username}-id", username.title()),
    )
    raw = f"tok-{username}"
    await db.enqueue(
        "INSERT INTO api_tokens(token_id, user_id, label, token_hash) VALUES(?,?,?,?)",
        (f"t-{username}", f"{username}-id", "t", sha256_token_hash(raw)),
    )
    await db.enqueue(
        "INSERT INTO space_members(space_id, user_id, role) VALUES(?,?,?)",
        (space_id, f"{username}-id", role),
    )
    return raw


async def _space(client) -> tuple[str, dict[str, str]]:
    r = await client.post(
        "/api/spaces", json={"name": "Lakeside"}, headers=_auth(client._tok)
    )
    assert r.status == 201, await r.text()
    sid = (await r.json())["id"]
    db = client._db
    await _install(db)
    toks = {
        "owner": client._tok,
        "mod": await _person(db, "mod", sid, "moderator"),
        "bob": await _person(db, "bob", sid, "member"),
        "fred": await _person(db, "fred", sid, "subscriber"),
    }
    return sid, toks


async def test_the_games_list_is_chosen_by_owner_admins_and_moderators(client):
    sid, tok = await _space(client)
    url = f"/api/spaces/{sid}/games/allowed"
    r = await client.put(url, json={"allowed_apps": []}, headers=_auth(tok["bob"]))
    assert r.status == 403
    r = await client.put(url, json={"allowed_apps": [APP]}, headers=_auth(tok["mod"]))
    assert r.status == 200, await r.text()
    assert (await r.json())["allowed_apps"] == [APP]
    # It rides the space's features (the config every member household gets).
    r = await client.get(f"/api/spaces/{sid}", headers=_auth(tok["owner"]))
    assert (await r.json())["features"]["allowed_apps"] == [APP]
    r = await client.put(
        url, json={"allowed_apps": ["bad id"]}, headers=_auth(tok["owner"])
    )
    assert r.status == 422
    r = await client.put(
        url, json={"allowed_apps": "quiz"}, headers=_auth(tok["owner"])
    )
    assert r.status == 422


async def test_a_moderator_changes_nothing_else_through_the_config(client):
    sid, tok = await _space(client)
    r = await client.patch(
        f"/api/spaces/{sid}",
        json={"features": {"allowed_apps": [APP], "bazaar": True}},
        headers=_auth(tok["mod"]),
    )
    assert r.status == 403
    r = await client.patch(
        f"/api/spaces/{sid}",
        json={"features": {"allowed_apps": [APP]}},
        headers=_auth(tok["mod"]),
    )
    assert r.status == 200, await r.text()


async def test_open_join_and_close_a_table(client):
    sid, tok = await _space(client)
    base = f"/api/spaces/{sid}/games"
    r = await client.post(base, json={"app_id": APP}, headers=_auth(tok["bob"]))
    assert r.status == 409
    assert (await r.json())["error"]["code"] == "SPACE_APP_NOT_ALLOWED"
    await client.put(
        f"{base}/allowed", json={"allowed_apps": [APP]}, headers=_auth(tok["owner"])
    )
    r = await client.post(
        base,
        json={"app_id": APP, "max_players": 3, "note": "Snacks", "lobby_days": 1},
        headers=_auth(tok["bob"]),
    )
    assert r.status == 201, await r.text()
    table = (await r.json())["table"]
    assert (table["seated"], table["max"], table["note"]) == (1, 3, "Snacks")
    assert table["group"]["is_host"] is True
    session = table["session_id"]

    r = await client.get(base, headers=_auth(tok["mod"]))
    listing = await r.json()
    assert listing["can_edit_games"] is True and listing["allowed_apps"] == [APP]
    assert [t["session_id"] for t in listing["tables"]] == [session]
    assert listing["tables"][0]["app"] == {"installed": True, "enabled": True}
    # Followers see no games at all.
    r = await client.get(base, headers=_auth(tok["fred"]))
    assert r.status == 403

    r = await client.post(f"{base}/{session}/join", headers=_auth(tok["mod"]))
    assert r.status == 200, await r.text()
    assert (await r.json())["group"]["my_state"] == "seated"

    # A member can't close someone else's table; a moderator can.
    r = await client.post(f"{base}/{session}/close", headers=_auth(tok["owner"]))
    assert r.status == 200
    r = await client.post(f"{base}/{session}/close", headers=_auth(tok["bob"]))
    assert r.status == 409  # already over


async def test_bad_bodies_are_refused(client):
    sid, tok = await _space(client)
    base = f"/api/spaces/{sid}/games"
    h = _auth(tok["bob"])
    for body in (
        {},
        {"app_id": APP, "max_players": "3"},
        {"app_id": APP, "note": 5},
        {"app_id": APP, "announce": "yes"},
    ):
        r = await client.post(base, json=body, headers=h)
        assert r.status == 400, body
    r = await client.post(f"{base}/nope/join", headers=h)
    assert r.status == 404


async def test_a_plain_member_cant_close_anothers_table(client):
    sid, tok = await _space(client)
    base = f"/api/spaces/{sid}/games"
    await client.put(
        f"{base}/allowed", json={"allowed_apps": [APP]}, headers=_auth(tok["owner"])
    )
    r = await client.post(base, json={"app_id": APP}, headers=_auth(tok["mod"]))
    session = (await r.json())["table"]["session_id"]
    r = await client.post(f"{base}/{session}/close", headers=_auth(tok["bob"]))
    assert r.status == 403
    assert (await r.json())["error"]["code"] == "APP_GROUP_NOT_HOST"
