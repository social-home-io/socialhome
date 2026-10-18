"""A space gallery is the space's: only its members add to it, and the
album routes outside ``/api/spaces/{id}`` tell an outsider nothing.

Regressions found on a live instance:

* a **follower** (read-only ``subscriber`` seat) created albums in a space
  (``POST /api/spaces/{id}/gallery/albums`` → 201) and could upload into
  them — every other space write refuses a follower (403
  ``SUBSCRIBER_READ_ONLY``);
* ``GET /api/gallery/albums/{id}`` / ``…/items`` sit outside the space
  seat gate, and answered a caller with no seat ``403 "user '<their id>'
  is not a member of space '<the album's space id>'"`` — an existence
  oracle that named the hidden space — while an unknown id answered 404;
* those routes read the seat with a bare ``get_member``, so a banned
  user's leftover seat still read the album.
"""

import pytest

from .test_space_read_gate import _add_user, _seat, _seed_space

_SID = "sp-gal"
_ALBUM = "al-1"


@pytest.fixture
async def seeded(client):
    hdr = {
        "owner": await _add_user(client, "olive", "uid-olive"),
        "bob": await _add_user(client, "bob", "uid-bob"),
        "fern": await _add_user(client, "fern", "uid-fern"),
        "guest": await _add_user(client, "guest", "uid-guest"),
    }
    await _seed_space(client, _SID, "public")
    await client._db.enqueue(
        "UPDATE spaces SET feature_gallery = 1 WHERE id=?", (_SID,)
    )
    await _seat(client, _SID, "uid-olive", "owner")
    await _seat(client, _SID, "uid-bob", "member")
    await _seat(client, _SID, "uid-fern", "subscriber")
    await client._db.enqueue(
        "INSERT INTO gallery_albums(id, space_id, owner_user_id, name)"
        " VALUES(?, ?, 'uid-bob', 'Summer')",
        (_ALBUM, _SID),
    )
    return client, hdr


async def _not_found(resp, album_id: str) -> None:
    assert resp.status == 404, await resp.text()
    body = await resp.json()
    assert body["error"]["detail"] == f"album {album_id!r} not found"


async def test_a_follower_creates_no_album(seeded):
    client, hdr = seeded
    r = await client.post(
        f"/api/spaces/{_SID}/gallery/albums", json={"name": "Mine"}, headers=hdr["fern"]
    )
    assert r.status == 403, await r.text()
    assert (await r.json())["error"]["code"] == "SUBSCRIBER_READ_ONLY"
    rows = await client._db.fetchall(
        "SELECT 1 FROM gallery_albums WHERE space_id=? AND name='Mine'", (_SID,)
    )
    assert rows == []


async def test_a_follower_uploads_nothing(seeded):
    client, hdr = seeded
    r = await client.post(
        f"/api/gallery/albums/{_ALBUM}/items",
        data=b"\x89PNG not really",
        headers={**hdr["fern"], "Content-Type": "image/png"},
    )
    assert r.status == 403, await r.text()
    assert (await r.json())["error"]["code"] == "SUBSCRIBER_READ_ONLY"


async def test_a_follower_still_reads_the_gallery(seeded):
    client, hdr = seeded
    r = await client.get(f"/api/gallery/albums/{_ALBUM}", headers=hdr["fern"])
    assert r.status == 200, await r.text()
    r = await client.get(f"/api/gallery/albums/{_ALBUM}/items", headers=hdr["fern"])
    assert r.status == 200, await r.text()


@pytest.mark.parametrize("suffix", ["", "/items"])
async def test_an_outsider_gets_the_unknown_ids_answer(seeded, suffix):
    client, hdr = seeded
    await _not_found(
        await client.get(f"/api/gallery/albums/{_ALBUM}{suffix}", headers=hdr["guest"]),
        _ALBUM,
    )
    await _not_found(
        await client.get(f"/api/gallery/albums/al-nope{suffix}", headers=hdr["guest"]),
        "al-nope",
    )


@pytest.mark.parametrize("suffix", ["", "/items"])
async def test_a_banned_users_leftover_seat_reads_no_album(seeded, suffix):
    client, hdr = seeded
    await client._db.enqueue(
        "INSERT INTO space_bans(space_id, user_id, banned_by)"
        " VALUES(?, 'uid-bob', 'uid-olive')",
        (_SID,),
    )
    await _not_found(
        await client.get(f"/api/gallery/albums/{_ALBUM}{suffix}", headers=hdr["bob"]),
        _ALBUM,
    )


async def test_an_outsider_cannot_rename_or_delete_an_album(seeded):
    client, hdr = seeded
    await _not_found(
        await client.patch(
            f"/api/gallery/albums/{_ALBUM}", json={"name": "x"}, headers=hdr["guest"]
        ),
        _ALBUM,
    )
    r = await client.delete(f"/api/gallery/albums/{_ALBUM}", headers=hdr["guest"])
    assert r.status in (204, 404), await r.text()
    rows = await client._db.fetchall(
        "SELECT 1 FROM gallery_albums WHERE id=? AND deleted_at IS NULL", (_ALBUM,)
    )
    assert rows != []
