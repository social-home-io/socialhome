"""Settings → Notifications routes (redesign D4, D9, D10).

``/api/me/notification-settings`` (kinds + "Muted and changed"), the muted
posts under it, and the space level with a duration on
``/api/spaces/{id}/notif-prefs``.
"""

from __future__ import annotations

from datetime import UTC, datetime
from urllib.parse import quote

from socialhome.domain.conversation import MUTED_FOREVER


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


async def _space(client, sid: str = "sp1", space_type: str = "household") -> str:
    await client._db.enqueue(
        "INSERT INTO spaces(id, name, owner_instance_id, owner_username,"
        " identity_public_key, space_type) VALUES(?, ?, 'iid', 'admin', ?, ?)",
        (sid, f"Space {sid}", "aa" * 32, space_type),
    )
    await client._db.enqueue(
        "INSERT INTO space_members(space_id, user_id, role) VALUES(?, ?, 'member')",
        (sid, client._uid),
    )
    return sid


async def _settings(client) -> dict:
    r = await client.get("/api/me/notification-settings", headers=_auth(client._tok))
    assert r.status == 200, await r.text()
    return await r.json()


# ── Kinds ─────────────────────────────────────────────────────────────


async def test_kinds_start_at_their_defaults(client):
    body = await _settings(client)
    kinds = {k["kind"]: k for k in body["kinds"]}
    assert list(kinds) == [
        "messages",
        "replies",
        "needs_you",
        "plans",
        "about_you",
        "reactions",
        "activity",
    ]
    assert kinds["reactions"]["choice"] == "in_app"
    assert kinds["replies"]["choice"] == "push"
    assert kinds["needs_you"]["choices"] == ["push", "in_app"]
    assert kinds["messages"]["choices"] == ["push", "off"]
    assert body["changed"] == []


async def test_a_kind_choice_is_saved(client):
    r = await client.patch(
        "/api/me/notification-settings",
        json={"kind": "reactions", "choice": "push"},
        headers=_auth(client._tok),
    )
    assert r.status == 200
    kinds = {k["kind"]: k["choice"] for k in (await r.json())["kinds"]}
    assert kinds["reactions"] == "push"
    after = {k["kind"]: k["choice"] for k in (await _settings(client))["kinds"]}
    assert after["reactions"] == "push"


async def test_a_choice_a_kind_does_not_offer_is_refused(client):
    for body in (
        {"kind": "needs_you", "choice": "off"},
        {"kind": "messages", "choice": "in_app"},
        {"kind": "nope", "choice": "push"},
        {"kind": "replies"},
    ):
        r = await client.patch(
            "/api/me/notification-settings", json=body, headers=_auth(client._tok)
        )
        assert r.status == 422, body


async def test_settings_need_a_login(client):
    r = await client.get("/api/me/notification-settings")
    assert r.status == 401


# ── Space levels with a duration (D9) ─────────────────────────────────


async def test_nothing_for_a_while_then_back_to_the_level_before(client):
    sid = await _space(client, space_type="public")
    r = await client.put(
        f"/api/spaces/{sid}/notif-prefs",
        json={"level": "muted", "duration": "8h"},
        headers=_auth(client._tok),
    )
    assert r.status == 200
    body = await r.json()
    assert body["level"] == "muted"
    assert body["then_level"] == "mentions"  # the public-space default
    assert body["default_level"] == "mentions"
    until = datetime.fromisoformat(body["muted_until"])
    assert 7.9 * 3600 < (until - datetime.now(UTC)).total_seconds() <= 8 * 3600
    r = await client.get(f"/api/spaces/{sid}/notif-prefs", headers=_auth(client._tok))
    assert (await r.json())["level"] == "muted"
    changed = (await _settings(client))["changed"]
    assert [(c["target"], c["id"], c["level"], c["then_level"]) for c in changed] == [
        ("space", sid, "muted", "mentions")
    ]
    # Undo: back to the level from before; nothing left in the list.
    r = await client.put(
        f"/api/spaces/{sid}/notif-prefs",
        json={"level": "mentions"},
        headers=_auth(client._tok),
    )
    assert (await r.json())["muted_until"] is None
    assert (await _settings(client))["changed"] == []


async def test_nothing_without_a_duration_is_until_turned_back_on(client):
    sid = await _space(client)
    r = await client.put(
        f"/api/spaces/{sid}/notif-prefs",
        json={"level": "muted"},
        headers=_auth(client._tok),
    )
    body = await r.json()
    assert (body["level"], body["muted_until"], body["then_level"]) == (
        "muted",
        MUTED_FOREVER,
        "all",
    )


async def test_a_bad_duration_is_refused(client):
    sid = await _space(client)
    for body in (
        {"level": "muted", "duration": "1y"},
        {"level": "all", "duration": "1h"},
        {"level": "muted", "duration": 3},
    ):
        r = await client.put(
            f"/api/spaces/{sid}/notif-prefs", json=body, headers=_auth(client._tok)
        )
        assert r.status == 422, body


async def test_a_level_off_the_default_is_listed(client):
    sid = await _space(client, "fam")  # household: default Everything
    await client.put(
        f"/api/spaces/{sid}/notif-prefs",
        json={"level": "mentions"},
        headers=_auth(client._tok),
    )
    changed = (await _settings(client))["changed"]
    assert changed[0]["level"] == "mentions"
    assert changed[0]["default_level"] == "all"
    assert changed[0]["name"] == "Space fam"
    assert changed[0]["place"] == "household"


# ── Muted posts (D10) ─────────────────────────────────────────────────


async def _feed_post(client, content: str = "Flohmarkt am Samstag") -> str:
    r = await client.post(
        "/api/feed/posts",
        json={"type": "text", "content": content},
        headers=_auth(client._tok),
    )
    assert r.status == 201, await r.text()
    return (await r.json())["id"]


async def test_mute_a_post_lists_it_and_unmute_takes_it_off(client):
    pid = await _feed_post(client)
    key = f"post:{pid}"
    r = await client.post(
        "/api/me/notification-settings/muted-posts",
        json={"object_key": key},
        headers=_auth(client._tok),
    )
    assert r.status == 200
    changed = (await _settings(client))["changed"]
    assert [(c["target"], c["id"], c["snippet"]) for c in changed] == [
        ("post", key, "Flohmarkt am Samstag")
    ]
    r = await client.delete(
        f"/api/me/notification-settings/muted-posts/{quote(key, safe='')}",
        headers=_auth(client._tok),
    )
    assert r.status == 200
    assert (await _settings(client))["changed"] == []


async def test_only_a_real_post_can_be_muted(client):
    for key, status in (
        ("dm:abc", 422),
        ("post:missing", 404),
        ("space_post:missing", 404),
        (5, 422),
    ):
        r = await client.post(
            "/api/me/notification-settings/muted-posts",
            json={"object_key": key},
            headers=_auth(client._tok),
        )
        assert r.status == status, key


async def test_a_space_post_outside_your_spaces_can_not_be_muted(client):
    await client._db.enqueue(
        "INSERT INTO spaces(id, name, owner_instance_id, owner_username,"
        " identity_public_key) VALUES('other', 'Other', 'iid', 'x', ?)",
        ("aa" * 32,),
    )
    await client._db.enqueue(
        "INSERT INTO space_posts(id, space_id, author, type, content)"
        " VALUES('sp-x', 'other', 'someone', 'text', 'not yours')"
    )
    r = await client.post(
        "/api/me/notification-settings/muted-posts",
        json={"object_key": "space_post:sp-x"},
        headers=_auth(client._tok),
    )
    assert r.status == 404


async def test_unmuting_what_was_not_muted_is_fine(client):
    r = await client.delete(
        "/api/me/notification-settings/muted-posts/post%3Anope",
        headers=_auth(client._tok),
    )
    assert r.status == 200
