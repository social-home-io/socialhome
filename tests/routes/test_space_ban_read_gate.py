"""A banned person can no longer read the space they were banned from.

Banning drops the seat and records the ban in one transaction now; and
every seat check (the read gate, the space seat gate in front of every
``/api/spaces/{id}/…`` route) refuses a banned user even if a seat row
survived — the same 403 as no seat at all.
"""

import pytest

from .test_space_read_gate import _add_user, _forbidden, _seat, _seed_space

_SID = "sp-priv"


@pytest.fixture
async def seeded(client):
    hdr = {
        "owner": await _add_user(client, "olive", "uid-olive"),
        "bob": await _add_user(client, "bob", "uid-bob"),
    }
    await _seed_space(client, _SID, "private")
    # Hosted HERE, so the owner's ban runs locally (not forwarded).
    await client._db.enqueue(
        "UPDATE spaces SET owner_instance_id ="
        " (SELECT instance_id FROM instance_identity) WHERE id=?",
        (_SID,),
    )
    await _seat(client, _SID, "uid-olive", "owner")
    await _seat(client, _SID, "uid-bob", "member")
    return client, hdr


_READS = ("", "/feed", "/members", "/theme", "/calendar/events", "/bazaar", "/zones")


async def test_a_ban_ends_the_seat_and_every_read(seeded):
    client, hdr = seeded
    r = await client.get(f"/api/spaces/{_SID}/feed", headers=hdr["bob"])
    assert r.status == 200, await r.text()
    r = await client.post(
        f"/api/spaces/{_SID}/ban", json={"user_id": "uid-bob"}, headers=hdr["owner"]
    )
    assert r.status in (200, 204), await r.text()
    rows = await client._db.fetchall(
        "SELECT 1 FROM space_members WHERE space_id=? AND user_id='uid-bob'", (_SID,)
    )
    assert rows == []
    for path in _READS:
        await _forbidden(
            await client.get(f"/api/spaces/{_SID}{path}", headers=hdr["bob"])
        )


@pytest.mark.parametrize("path", _READS)
async def test_a_leftover_seat_of_a_banned_user_reads_nothing(seeded, path):
    """A half-applied ban (ban row + seat row) — the old two-write ban
    could leave one — still answers the outsider's 403."""
    client, hdr = seeded
    await client._db.enqueue(
        "INSERT INTO space_bans(space_id, user_id, banned_by) VALUES(?, 'uid-bob', 'uid-olive')",
        (_SID,),
    )
    await _forbidden(await client.get(f"/api/spaces/{_SID}{path}", headers=hdr["bob"]))
