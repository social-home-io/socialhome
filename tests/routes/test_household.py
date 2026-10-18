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


async def test_put_preferences_toggles_household_chat(client):
    r = await client.get("/api/household/preferences", headers=_auth(client._tok))
    assert (await r.json())["feat_household_chat"] is True
    r = await client.put(
        "/api/household/preferences",
        json={"toggles": {"feat_household_chat": False}},
        headers=_auth(client._tok),
    )
    assert r.status == 200
    assert (await r.json())["feat_household_chat"] is False


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


# ─── Household chat (GET /api/household/chat + /api/conversations/{id}/…) ──


async def _add_member(client, username: str = "bob") -> str:
    """Seed an active local user with a token; returns the raw token."""
    db = client._db
    await db.enqueue(
        "INSERT INTO users(username, user_id, display_name, is_admin)"
        " VALUES(?, ?, ?, 0)",
        (username, f"{username}-id", username.title()),
    )
    raw = f"{username}-tok"
    await db.enqueue(
        "INSERT INTO api_tokens(token_id, user_id, label, token_hash)"
        " VALUES(?, ?, 't', ?)",
        (f"t-{username}", f"{username}-id", sha256_token_hash(raw)),
    )
    return raw


async def _chat(client, tok: str) -> dict:
    r = await client.get("/api/household/chat", headers=_auth(tok))
    assert r.status == 200
    return await r.json()


async def test_household_chat_requires_auth(client):
    r = await client.get("/api/household/chat")
    assert r.status == 401


async def test_household_chat_is_created_once_for_everyone(client):
    bob = await _add_member(client)
    mine = await _chat(client, client._tok)
    assert mine["enabled"] is True
    assert mine["conversation_id"]
    assert mine["unread"] == 0
    assert mine["notif_level"] == "all"
    assert mine["muted_until"] is None
    assert (await _chat(client, bob))["conversation_id"] == mine["conversation_id"]


async def test_household_members_talk_through_the_conversation_routes(client):
    bob = await _add_member(client)
    cid = (await _chat(client, client._tok))["conversation_id"]
    r = await client.post(
        f"/api/conversations/{cid}/messages",
        json={"content": "dinner at 7"},
        headers=_auth(client._tok),
    )
    assert r.status == 201
    mid = (await r.json())["id"]
    assert (await _chat(client, bob))["unread"] == 1
    r = await client.get(f"/api/conversations/{cid}/messages", headers=_auth(bob))
    assert r.status == 200
    assert [m["content"] for m in await r.json()] == ["dinner at 7"]
    r = await client.put(
        f"/api/conversations/{cid}/messages/{mid}/reactions/%F0%9F%91%8D",
        headers=_auth(bob),
    )
    assert r.status == 200
    r = await client.patch(
        f"/api/conversations/{cid}/messages/{mid}",
        json={"content": "dinner at 8"},
        headers=_auth(client._tok),
    )
    assert r.status == 200
    r = await client.get(f"/api/conversations/{cid}/members", headers=_auth(bob))
    assert r.status == 200
    assert {m["username"] for m in await r.json()} >= {"admin", "bob"}
    r = await client.put(
        f"/api/conversations/{cid}/notif-prefs",
        json={"level": "mentions"},
        headers=_auth(bob),
    )
    assert r.status == 200
    r = await client.get(f"/api/conversations/{cid}/notif-prefs", headers=_auth(bob))
    assert (await r.json())["level"] == "mentions"
    r = await client.post(f"/api/conversations/{cid}/read", headers=_auth(bob))
    assert r.status == 200
    after = await _chat(client, bob)
    assert after["unread"] == 0 and after["notif_level"] == "mentions"
    assert after["last_read_at"]
    r = await client.delete(
        f"/api/conversations/{cid}/messages/{mid}", headers=_auth(client._tok)
    )
    assert r.status == 200


async def test_household_chat_stays_out_of_the_inbox_and_badge(client):
    bob = await _add_member(client)
    cid = (await _chat(client, client._tok))["conversation_id"]
    await client.post(
        f"/api/conversations/{cid}/messages",
        json={"content": "hi"},
        headers=_auth(client._tok),
    )
    r = await client.get("/api/conversations", headers=_auth(bob))
    assert r.status == 200
    assert cid not in {c["id"] for c in await r.json()}
    r = await client.get("/api/me/corner", headers=_auth(bob))
    assert r.status == 200
    assert (await r.json())["unread_conversations"] == 0


async def test_household_chat_management_routes_are_not_found(client):
    await _add_member(client)
    cid = (await _chat(client, client._tok))["conversation_id"]
    h = _auth(client._tok)
    calls = [
        client.patch(f"/api/conversations/{cid}", json={"name": "x"}, headers=h),
        client.post(f"/api/conversations/{cid}/leave", headers=h),
        client.post(
            f"/api/conversations/{cid}/members", json={"usernames": ["bob"]}, headers=h
        ),
        client.delete(f"/api/conversations/{cid}/members/bob-id", headers=h),
    ]
    for call in calls:
        r = await call
        assert r.status == 404


async def test_household_chat_off_hides_it_and_refuses_writes(client):
    cid = (await _chat(client, client._tok))["conversation_id"]
    await client.post(
        f"/api/conversations/{cid}/messages",
        json={"content": "kept"},
        headers=_auth(client._tok),
    )
    await _disable(client, feat_household_chat=False)
    off = await _chat(client, client._tok)
    assert off == {
        "enabled": False,
        "conversation_id": None,
        "unread": 0,
        "notif_level": None,
        "muted_until": None,
        "last_read_at": None,
    }
    r = await client.post(
        f"/api/conversations/{cid}/messages",
        json={"content": "nope"},
        headers=_auth(client._tok),
    )
    assert r.status == 403
    r = await client.get(
        f"/api/conversations/{cid}/messages", headers=_auth(client._tok)
    )
    assert r.status == 403
    r = await client.put(
        "/api/household/preferences",
        json={"toggles": {"feat_household_chat": True}},
        headers=_auth(client._tok),
    )
    assert r.status == 200
    r = await client.get(
        f"/api/conversations/{cid}/messages", headers=_auth(client._tok)
    )
    assert [m["content"] for m in await r.json()] == ["kept"]


async def test_inactive_account_cannot_read_the_household_chat(client):
    bob = await _add_member(client)
    cid = (await _chat(client, client._tok))["conversation_id"]
    await client._db.enqueue("UPDATE users SET state='inactive' WHERE username='bob'")
    r = await client.get(f"/api/conversations/{cid}/messages", headers=_auth(bob))
    assert r.status in (401, 403)


# ─── Household spaces: everyone in the home is a member ──────────────────
#
# A ``household`` space seats every active local user of the household that
# hosts it, and Browse › Your household (``GET /api/household/spaces``)
# lists the spaces this household hosts that the caller may find, with real
# member counts.


async def _local_user(client, username: str, *, active: bool = True) -> dict:
    """A local user + bearer token; returns ``{user_id, headers}``."""
    user_id = f"uid-{username}"
    await client._db.enqueue(
        "INSERT INTO users(username, user_id, display_name, state, deleted_at)"
        " VALUES(?, ?, ?, ?, ?)",
        (
            username,
            user_id,
            username.title(),
            "active" if active else "inactive",
            None if active else "2026-10-01T00:00:00+00:00",
        ),
    )
    token = f"{username}-tok"
    await client._db.enqueue(
        "INSERT INTO api_tokens(token_id, user_id, label, token_hash)"
        " VALUES(?, ?, 't', ?)",
        (f"t-{username}", user_id, sha256_token_hash(token)),
    )
    return {"user_id": user_id, "headers": _auth(token)}


async def _create_space(client, headers, **body) -> dict:
    r = await client.post("/api/spaces", json={"name": "S", **body}, headers=headers)
    assert r.status == 201, await r.text()
    return await r.json()


async def _member_ids(client, sid: str) -> set[str]:
    rows = await client._db.fetchall(
        "SELECT user_id FROM space_members WHERE space_id=?", (sid,)
    )
    return {r["user_id"] for r in rows}


async def _directory(client, headers) -> dict[str, dict]:
    r = await client.get("/api/household/spaces", headers=headers)
    assert r.status == 200, await r.text()
    return {row["space_id"]: row for row in await r.json()}


async def _new_user(client, username: str) -> str:
    """Provision a user through the admin API (publishes UserProvisioned)."""
    r = await client.post(
        "/api/admin/users",
        json={"username": username, "password": "correct-horse"},
        headers=_auth(client._tok),
    )
    assert r.status == 201, await r.text()
    return (await r.json())["user_id"]


async def test_household_directory_requires_auth(client):
    r = await client.get("/api/household/spaces")
    assert r.status == 401


async def test_creating_a_household_space_seats_everyone_at_home(client):
    ben = await _local_user(client, "ben")
    cleo = await _local_user(client, "cleo")
    gone = await _local_user(client, "gone", active=False)
    space = await _create_space(
        client,
        _auth(client._tok),
        name="Home",
        space_type="household",
        join_mode="request",
    )
    r = await client.get(f"/api/spaces/{space['id']}", headers=_auth(client._tok))
    assert (await r.json())["join_mode"] == "open"
    members = await _member_ids(client, space["id"])
    assert members == {client._uid, ben["user_id"], cleo["user_id"]}
    assert gone["user_id"] not in members
    # The seats are real: ben reads the member list.
    r = await client.get(f"/api/spaces/{space['id']}/members", headers=ben["headers"])
    assert r.status == 200
    assert len(await r.json()) == 3


async def test_a_new_user_is_seated_in_household_spaces_only(client):
    home = await _create_space(client, _auth(client._tok), space_type="household")
    secret = await _create_space(client, _auth(client._tok), space_type="private")
    dora = await _new_user(client, "dora")
    assert dora in await _member_ids(client, home["id"])
    assert dora not in await _member_ids(client, secret["id"])


async def test_a_member_who_left_a_household_space_stays_out(client):
    ben = await _local_user(client, "ben")
    home = await _create_space(client, _auth(client._tok), space_type="household")
    r = await client.delete(
        f"/api/spaces/{home['id']}/members/{ben['user_id']}", headers=ben["headers"]
    )
    assert r.status == 200, await r.text()
    await _new_user(client, "dora")
    assert ben["user_id"] not in await _member_ids(client, home["id"])
    # …and finds it again under Browse › Your household, joinable at once.
    row = (await _directory(client, ben["headers"]))[home["id"]]
    assert row["role"] is None
    assert row["member_count"] == 2
    r = await client.post(
        f"/api/spaces/{home['id']}/join-requests", json={}, headers=ben["headers"]
    )
    assert r.status == 201
    assert (await r.json())["joined"] is True
    assert ben["user_id"] in await _member_ids(client, home["id"])


async def test_household_directory_lists_findable_spaces_with_counts(client):
    ben = await _local_user(client, "ben")
    admin = _auth(client._tok)
    home = await _create_space(client, admin, name="Home", space_type="household")
    priv_open = await _create_space(
        client, admin, name="Private", space_type="private", join_mode="open"
    )
    pub_open = await _create_space(
        client, admin, name="Pub open", space_type="public", join_mode="open"
    )
    pub_req = await _create_space(
        client, admin, name="Pub req", space_type="public", join_mode="request"
    )
    pub_invite = await _create_space(
        client, admin, name="Pub invite", space_type="public", join_mode="invite_only"
    )
    rows = await _directory(client, ben["headers"])
    # Home: ben is seated automatically; two members (admin + ben).
    assert rows[home["id"]]["role"] == "member"
    assert rows[home["id"]]["member_count"] == 2
    assert rows[home["id"]]["space_type"] == "household"
    # Joinable public spaces ben isn't in, with their real seat counts.
    assert rows[pub_open["id"]]["role"] is None
    assert rows[pub_open["id"]]["member_count"] == 1
    assert rows[pub_req["id"]]["join_mode"] == "request"
    # Never a private space, never an invite-only one ben isn't in.
    assert priv_open["id"] not in rows
    assert pub_invite["id"] not in rows
    # The admin (a member of all of them) sees every non-private one.
    mine = await _directory(client, admin)
    assert {home["id"], pub_open["id"], pub_req["id"], pub_invite["id"]} <= set(mine)
    assert priv_open["id"] not in mine


async def test_household_directory_hides_bans_and_foreign_household_spaces(client):
    ben = await _local_user(client, "ben")
    admin = _auth(client._tok)
    home = await _create_space(client, admin, space_type="household")
    r = await client.post(
        f"/api/spaces/{home['id']}/ban",
        json={"user_id": ben["user_id"]},
        headers=admin,
    )
    assert r.status in (200, 201, 204), await r.text()
    # A household-tier stub another household hosts.
    await client._db.enqueue(
        "INSERT INTO spaces(id, name, owner_instance_id, owner_username,"
        " identity_public_key, space_type, join_mode)"
        " VALUES('theirs', 'Theirs', 'other-inst', 'x', ?, 'household', 'open')",
        ("ab" * 32,),
    )
    rows = await _directory(client, ben["headers"])
    assert home["id"] not in rows
    assert "theirs" not in rows
