"""A follower's (``subscriber``) view of a space is reduced (owner
decision 13, 2026-10-18).

A follower reads the posts, but is an outsider to the people: elsewhere
the space chat refuses them, the GFS listing is hidden from them and the
roster never syncs to a follower-only household. So:

* ``GET /api/spaces/{id}/members`` shows a follower the space's members
  only — never another follower — by name, picture and role, without
  session presence (online / idle / last seen), join time or the
  location-share opt-in.
* ``GET /api/spaces/{id}/proposals``, ``/presence`` (the map pins) and
  ``/zones`` are for members only: a follower gets the same 403 as an
  outsider.
* ``GET /api/spaces/{id}`` carries ``my_role`` so the SPA learns the
  viewer's own role without finding itself in the roster.
"""

import pytest

from .test_space_read_gate import _add_user, _forbidden, _seat, _seed_space

_OWNER = ("olive", "uid-olive")
_MEMBER = ("mia", "uid-mia")
_FOLLOWER = ("fern", "uid-fern")
_OTHER_FOLLOWER = ("fred", "uid-fred")

#: Presence / bookkeeping fields a follower never sees on a roster row.
_WITHHELD = (
    "is_online",
    "is_idle",
    "last_seen_at",
    "joined_at",
    "location_share_enabled",
)

_SID = "sp-glob"


@pytest.fixture
async def seeded(client):
    """Global space ``sp-glob``: olive owns it, mia is a member, fern and
    fred follow; another household holds a member seat and a follower
    seat. Location sharing is on and olive shares."""
    hdr = {
        "owner": await _add_user(client, *_OWNER),
        "member": await _add_user(client, *_MEMBER),
        "follower": await _add_user(client, *_FOLLOWER),
    }
    await _add_user(client, *_OTHER_FOLLOWER)
    await _seed_space(client, _SID, "global")
    await _seat(client, _SID, _OWNER[1], "owner")
    await _seat(client, _SID, _MEMBER[1], "member")
    await _seat(client, _SID, _FOLLOWER[1], "subscriber")
    await _seat(client, _SID, _OTHER_FOLLOWER[1], "subscriber")
    for uid, role in (("uid-r-member", "member"), ("uid-r-follower", "subscriber")):
        await client._db.enqueue(
            "INSERT INTO space_remote_members(space_id, instance_id, user_id,"
            " display_name, role) VALUES(?, 'inst-remote', ?, ?, ?)",
            (_SID, uid, uid, role),
        )
    await client._db.enqueue("UPDATE spaces SET feature_location=1 WHERE id=?", (_SID,))
    await client._db.enqueue(
        "UPDATE space_members SET location_share_enabled=1"
        " WHERE space_id=? AND user_id=?",
        (_SID, _OWNER[1]),
    )
    return client, hdr


async def _roster(client, headers) -> list[dict]:
    r = await client.get(f"/api/spaces/{_SID}/members", headers=headers)
    assert r.status == 200, await r.text()
    return await r.json()


async def test_follower_roster_lists_members_without_presence(seeded):
    client, hdr = seeded
    rows = await _roster(client, hdr["follower"])
    assert {r["user_id"] for r in rows} == {
        _OWNER[1],
        _MEMBER[1],
        "uid-r-member",
    }
    for row in rows:
        for key in _WITHHELD:
            assert key not in row, (row["user_id"], key)
        assert row["role"] != "subscriber"
    by_id = {r["user_id"]: r for r in rows}
    assert by_id[_OWNER[1]]["role"] == "owner"
    assert by_id[_OWNER[1]]["is_owner"] is True
    assert by_id[_MEMBER[1]]["display_name"] == "Mia"
    assert by_id["uid-r-member"]["household_name"] is None


async def test_member_roster_is_unchanged(seeded):
    """A member still sees every seat — followers included — with
    presence."""
    client, hdr = seeded
    for who in ("owner", "member"):
        rows = await _roster(client, hdr[who])
        assert {r["user_id"] for r in rows} == {
            _OWNER[1],
            _MEMBER[1],
            _FOLLOWER[1],
            _OTHER_FOLLOWER[1],
            "uid-r-member",
            "uid-r-follower",
        }
        for row in rows:
            assert "is_online" in row and "last_seen_at" in row


@pytest.mark.parametrize("path", ["/proposals", "/presence", "/zones"])
async def test_member_only_reads_refuse_a_follower(seeded, path):
    client, hdr = seeded
    url = f"/api/spaces/{_SID}{path}"
    await _forbidden(await client.get(url, headers=hdr["follower"]))
    for who in ("owner", "member"):
        r = await client.get(url, headers=hdr[who])
        assert r.status == 200, (who, await r.text())


@pytest.mark.parametrize("path", ["/presence", "/zones"])
async def test_member_only_reads_hide_unknown_ids(seeded, path):
    """An unknown id answers the outsider's 403 — no existence oracle."""
    client, hdr = seeded
    await _forbidden(
        await client.get(f"/api/spaces/sp-nope{path}", headers=hdr["member"])
    )


@pytest.mark.parametrize(
    ("who", "role"),
    [("owner", "owner"), ("member", "member"), ("follower", "subscriber")],
)
async def test_detail_carries_my_role(seeded, who, role):
    client, hdr = seeded
    r = await client.get(f"/api/spaces/{_SID}", headers=hdr[who])
    assert r.status == 200, await r.text()
    assert (await r.json())["my_role"] == role
