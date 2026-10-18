"""Search never reveals a space's content to someone without a seat.

Regression: ``GET /api/search`` filtered space-post / page *hits* by
membership, but

* the per-scope ``counts`` were over the whole index — a local user with
  no seat learned how many posts of a private space match a word, and
  with ``space_id=`` could probe one space's content word by word;
* DM ``message`` rows were counted for everyone though never shown;
* the hit filter failed **open**: an error reading the roster let every
  hit of that space through.

The rule: a row tied to a space is returned and counted only for a caller
with a seat there (any role, a follower included); DM rows never; a
``space_id`` the caller has no seat in answers like a query with no
matches.
"""

import pytest

from .test_space_read_gate import _add_user, _seat, _seed_space

_PRIVATE = "sp-priv"


@pytest.fixture
async def seeded(client):
    hdr = {
        "owner": await _add_user(client, "olive", "uid-olive"),
        "follower": await _add_user(client, "fern", "uid-fern"),
        "guest": await _add_user(client, "guest", "uid-guest"),
    }
    await _seed_space(client, _PRIVATE, "private")
    await _seat(client, _PRIVATE, "uid-olive", "owner")
    await _seat(client, _PRIVATE, "uid-fern", "subscriber")
    for scope, ref, space in (
        ("space_post", "p-1", _PRIVATE),
        ("page", "pg-1", _PRIVATE),
        ("message", "m-1", None),
        ("post", "hp-1", None),
    ):
        await client._db.enqueue(
            "INSERT INTO search_index(scope, ref_id, space_id, title, body)"
            " VALUES(?, ?, ?, '', 'zebra crossing secret')",
            (scope, ref, space),
        )
    return client, hdr


async def _search(client, headers, **params) -> dict:
    r = await client.get(
        "/api/search", params={"q": "zebra", **params}, headers=headers
    )
    assert r.status == 200, await r.text()
    return await r.json()


async def test_counts_hide_a_space_the_caller_has_no_seat_in(seeded):
    client, hdr = seeded
    body = await _search(client, hdr["guest"])
    assert body["counts"] == {"post": 1}
    assert [h["scope"] for h in body["hits"]] == ["post"]


async def test_space_id_without_a_seat_answers_like_no_match(seeded):
    client, hdr = seeded
    hidden = await _search(client, hdr["guest"], space_id=_PRIVATE)
    unknown = await _search(client, hdr["guest"], space_id="0" * 32)
    assert hidden == unknown == {"hits": [], "counts": {}}


@pytest.mark.parametrize("who", ["owner", "follower"])
async def test_a_seat_sees_and_counts_the_space(seeded, who):
    client, hdr = seeded
    body = await _search(client, hdr[who])
    assert body["counts"] == {"post": 1, "space_post": 1, "page": 1}
    body = await _search(client, hdr[who], space_id=_PRIVATE)
    assert body["counts"] == {"space_post": 1, "page": 1}
    assert {h["ref_id"] for h in body["hits"]} == {"p-1", "pg-1"}


async def test_dm_rows_are_never_counted(seeded):
    client, hdr = seeded
    for who in hdr:
        assert "message" not in (await _search(client, hdr[who]))["counts"]
