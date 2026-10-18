"""A follower can't share their location in a space.

Owner decision 13: a follower is an outsider to the space's people — they
see no map pins, and their own whereabouts are not the members' to see
either. ``PATCH …/members/me/location-sharing {"enabled": true}`` took any
seat, so a follower's GPS pin was fanned to the members; the setting showed
in the space's 🔔 menu and in Settings → Privacy for them too.
"""

import pytest

from .test_space_read_gate import _add_user, _seat, _seed_space

_SID = "sp-loc"


@pytest.fixture
async def seeded(client):
    hdr = {
        "owner": await _add_user(client, "olive", "uid-olive"),
        "bob": await _add_user(client, "bob", "uid-bob"),
        "fern": await _add_user(client, "fern", "uid-fern"),
    }
    await _seed_space(client, _SID, "public")
    await client._db.enqueue(
        "UPDATE spaces SET feature_location = 1 WHERE id=?", (_SID,)
    )
    await _seat(client, _SID, "uid-olive", "owner")
    await _seat(client, _SID, "uid-bob", "member")
    await _seat(client, _SID, "uid-fern", "subscriber")
    return client, hdr


async def _flag(client, uid: str) -> int:
    rows = await client._db.fetchall(
        "SELECT location_share_enabled FROM space_members WHERE space_id=? AND user_id=?",
        (_SID, uid),
    )
    return rows[0][0]


async def _patch(client, hdr, enabled: bool):
    return await client.patch(
        f"/api/spaces/{_SID}/members/me/location-sharing",
        json={"enabled": enabled},
        headers=hdr,
    )


async def test_a_follower_cannot_turn_sharing_on(seeded):
    client, hdr = seeded
    r = await _patch(client, hdr["fern"], True)
    assert r.status == 403, await r.text()
    body = await r.json()
    assert body["error"]["code"] == "SUBSCRIBER_READ_ONLY"
    assert await _flag(client, "uid-fern") == 0


async def test_a_follower_may_still_turn_it_off(seeded):
    client, hdr = seeded
    r = await _patch(client, hdr["fern"], False)
    assert r.status == 200, await r.text()
    assert await r.json() == {"location_share_enabled": False}


async def test_a_member_still_shares(seeded):
    client, hdr = seeded
    r = await _patch(client, hdr["bob"], True)
    assert r.status == 200, await r.text()
    assert await _flag(client, "uid-bob") == 1


async def test_the_follower_sees_no_sharing_setting_for_the_space(seeded):
    client, hdr = seeded
    r = await client.get("/api/me/space-location-sharing", headers=hdr["fern"])
    assert r.status == 200
    assert (await r.json())["spaces"] == []
    r = await client.get("/api/me/space-location-sharing", headers=hdr["bob"])
    assert [s["space_id"] for s in (await r.json())["spaces"]] == [_SID]


async def test_notif_prefs_tell_the_spa_whether_sharing_is_offered(seeded):
    client, hdr = seeded
    r = await client.get(f"/api/spaces/{_SID}/notif-prefs", headers=hdr["fern"])
    body = await r.json()
    assert body["location_share_allowed"] is False
    r = await client.get(f"/api/spaces/{_SID}/notif-prefs", headers=hdr["bob"])
    body = await r.json()
    assert body["location_share_allowed"] is True
