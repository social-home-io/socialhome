"""A personal bot posts only while its maker may post in the space.

Regression for two holes found on a live instance:

* a **follower** (read-only ``subscriber`` seat) created a personal bot
  (``POST /api/spaces/{id}/bots`` → 201) and posted into the space through
  ``POST /api/bot-bridge/spaces/{id}`` with its token;
* a member who was **banned** kept posting through the personal bot they
  made before the ban — the token outlives the seat.
"""

import pytest

from .test_space_read_gate import _add_user, _seat, _seed_space

_SID = "sp-bots"


@pytest.fixture
async def seeded(client):
    hdr = {
        "owner": await _add_user(client, "olive", "uid-olive"),
        "bob": await _add_user(client, "bob", "uid-bob"),
        "fern": await _add_user(client, "fern", "uid-fern"),
    }
    await _seed_space(client, _SID, "public")
    await client._db.enqueue(
        "UPDATE spaces SET bot_enabled = 1, owner_instance_id ="
        " (SELECT instance_id FROM instance_identity) WHERE id=?",
        (_SID,),
    )
    await _seat(client, _SID, "uid-olive", "owner")
    await _seat(client, _SID, "uid-bob", "member")
    await _seat(client, _SID, "uid-fern", "subscriber")
    return client, hdr


async def _make_bot(client, hdr) -> tuple[int, dict]:
    r = await client.post(
        f"/api/spaces/{_SID}/bots",
        json={"scope": "member", "slug": "mine", "name": "Mine", "icon": "🤖"},
        headers=hdr,
    )
    return r.status, await r.json()


async def _bot_post(client, token: str):
    return await client.post(
        f"/api/bot-bridge/spaces/{_SID}",
        json={"message": "hello from my bot"},
        headers={"Authorization": f"Bearer {token}"},
    )


async def _bot_posts(client) -> list:
    return await client._db.fetchall(
        "SELECT id FROM space_posts WHERE space_id=? AND bot_id IS NOT NULL", (_SID,)
    )


async def test_a_follower_makes_no_bot(seeded):
    client, hdr = seeded
    status, body = await _make_bot(client, hdr["fern"])
    assert status == 403, body
    assert body["error"]["code"] == "SUBSCRIBER_READ_ONLY"
    assert await client._db.fetchall("SELECT 1 FROM space_bots") == []


async def test_a_members_bot_posts_until_the_member_is_banned(seeded):
    client, hdr = seeded
    status, bot = await _make_bot(client, hdr["bob"])
    assert status == 201, bot
    r = await _bot_post(client, bot["token"])
    assert r.status == 201, await r.text()

    r = await client.post(
        f"/api/spaces/{_SID}/ban", json={"user_id": "uid-bob"}, headers=hdr["owner"]
    )
    assert r.status in (200, 204), await r.text()

    r = await _bot_post(client, bot["token"])
    assert r.status == 403, await r.text()
    assert len(await _bot_posts(client)) == 1


async def test_a_members_bot_stops_when_the_member_leaves(seeded):
    client, hdr = seeded
    status, bot = await _make_bot(client, hdr["bob"])
    assert status == 201, bot
    r = await client.delete(f"/api/spaces/{_SID}/members/me", headers=hdr["bob"])
    assert r.status == 200, await r.text()
    r = await _bot_post(client, bot["token"])
    assert r.status == 403, await r.text()
    assert await _bot_posts(client) == []
