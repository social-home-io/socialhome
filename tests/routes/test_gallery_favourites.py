"""Integration: each person's private favourites (D12) over the real routes —
``PUT`` / ``DELETE /api/gallery/items/{id}/favourite``, ``GET
/api/gallery/favourites``, ``is_favourite`` on an album's items and the
``favourite`` / ``unfavourite`` bulk actions."""

from __future__ import annotations

import io
from datetime import datetime

from PIL import Image

from socialhome.auth import sha256_token_hash

from .conftest import _auth


def _jpeg(color: tuple[int, int, int]) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (32, 24), color).save(buf, format="JPEG")
    return buf.getvalue()


async def _add_user(client, username: str) -> tuple[str, dict]:
    uid, tok = f"uid-{username}", f"tok-{username}"
    await client._db.enqueue(
        "INSERT INTO users(username, user_id, display_name, is_admin) VALUES(?,?,?,0)",
        (username, uid, username.title()),
    )
    await client._db.enqueue(
        "INSERT INTO api_tokens(token_id, user_id, label, token_hash) VALUES(?,?,?,?)",
        (f"t-{username}", uid, "t", sha256_token_hash(tok)),
    )
    return uid, _auth(tok)


async def _album(client, name: str = "Summer") -> str:
    r = await client.post(
        "/api/gallery/albums", json={"name": name}, headers=_auth(client._tok)
    )
    assert r.status == 201
    return (await r.json())["id"]


async def _photo(client, album_id: str, shade: int) -> str:
    r = await client.post(
        f"/api/gallery/albums/{album_id}/items",
        data=_jpeg((shade, 40, 40)),
        headers={**_auth(client._tok), "Content-Type": "image/jpeg"},
    )
    assert r.status == 201, await r.text()
    return (await r.json())["id"]


def _fav(item_id: str) -> str:
    return f"/api/gallery/items/{item_id}/favourite"


async def _favourite_ids(client, headers: dict, query: str = "") -> list[str]:
    r = await client.get(f"/api/gallery/favourites{query}", headers=headers)
    assert r.status == 200, await r.text()
    return [row["id"] for row in await r.json()]


async def _hearts(client, album_id: str, headers: dict) -> dict[str, bool]:
    r = await client.get(f"/api/gallery/albums/{album_id}/items", headers=headers)
    assert r.status == 200
    return {row["id"]: row["is_favourite"] for row in await r.json()}


async def test_a_heart_is_only_the_markers(client):
    _uid, eve = await _add_user(client, "eve")
    me = _auth(client._tok)
    aid = await _album(client)
    p1 = await _photo(client, aid, 10)
    p2 = await _photo(client, aid, 200)

    assert (await client.put(_fav(p1), headers=eve)).status == 204
    assert (await client.put(_fav(p1), headers=eve)).status == 204  # idempotent

    assert await _hearts(client, aid, eve) == {p1: True, p2: False}
    # Nobody else learns who marked what.
    assert await _hearts(client, aid, me) == {p1: False, p2: False}
    assert await _favourite_ids(client, me) == []

    r = await client.get("/api/gallery/favourites", headers=eve)
    (row,) = await r.json()
    assert row["id"] == p1
    assert row["is_favourite"] is True
    assert row["album"] == {
        "id": aid,
        "name": "Summer",
        "space_id": None,
        "is_system": False,
        "default_name": False,
    }
    assert datetime.fromisoformat(row["favourited_at"]).tzinfo is not None
    assert "sig=" in row["thumbnail_url"]

    assert (await client.delete(_fav(p1), headers=eve)).status == 204
    assert await _favourite_ids(client, eve) == []


async def test_an_unknown_item_is_404_and_unmarking_never_fails(client):
    me = _auth(client._tok)
    assert (await client.put(_fav("nope"), headers=me)).status == 404
    assert (await client.delete(_fav("nope"), headers=me)).status == 204


async def test_a_private_album_hides_its_photos_from_who_isnt_chosen(client):
    _uid, eve = await _add_user(client, "eve")
    aid = await _album(client, "Just us")
    p1 = await _photo(client, aid, 10)
    assert (await client.put(_fav(p1), headers=eve)).status == 204
    # Private album support lands later; the audience column is the switch.
    await client._db.enqueue(
        "UPDATE gallery_albums SET audience='chosen' WHERE id=?", (aid,)
    )

    assert await _favourite_ids(client, eve) == []
    r = await client.put(_fav(p1), headers=eve)
    assert r.status == 404


async def test_the_trash_hides_a_favourite_until_it_comes_back(client):
    me = _auth(client._tok)
    aid = await _album(client)
    p1 = await _photo(client, aid, 10)
    await client.put(_fav(p1), headers=me)

    assert (await client.delete(f"/api/gallery/items/{p1}", headers=me)).status == 204
    assert await _favourite_ids(client, me) == []
    r = await client.post(f"/api/gallery/items/{p1}/restore", headers=me)
    assert r.status == 200
    assert await _favourite_ids(client, me) == [p1]


async def test_favourites_page_with_the_last_rows_cursor(client):
    me = _auth(client._tok)
    aid = await _album(client)
    ids = [await _photo(client, aid, shade) for shade in (10, 60, 110)]
    for iid in ids:
        await client.put(_fav(iid), headers=me)

    r = await client.get("/api/gallery/favourites?limit=2", headers=me)
    first = await r.json()
    assert len(first) == 2
    last = first[-1]
    rest = await _favourite_ids(
        client,
        me,
        f"?limit=2&before={last['favourited_at'].replace('+', '%2B')}"
        f"&before_id={last['id']}",
    )
    assert sorted([*(row["id"] for row in first), *rest]) == sorted(ids)
    assert len(rest) == 1

    bad = await client.get(
        "/api/gallery/favourites?before=yesterday&before_id=x", headers=me
    )
    assert bad.status == 422


async def test_bulk_favourite_and_unfavourite(client):
    _uid, eve = await _add_user(client, "eve")
    open_album = await _album(client)
    private = await _album(client, "Private")
    a = await _photo(client, open_album, 10)
    b = await _photo(client, open_album, 90)
    hidden = await _photo(client, private, 170)
    await client._db.enqueue(
        "UPDATE gallery_albums SET audience='chosen' WHERE id=?", (private,)
    )

    r = await client.post(
        "/api/gallery/bulk",
        json={"action": "favourite", "item_ids": [a, b, hidden]},
        headers=eve,
    )
    assert r.status == 200
    assert await r.json() == {"done": [a, b], "refused": [hidden]}

    r = await client.post(
        "/api/gallery/bulk",
        json={"action": "unfavourite", "item_ids": [a]},
        headers=eve,
    )
    assert await r.json() == {"done": [a], "refused": []}
    assert await _favourite_ids(client, eve) == [b]


async def test_space_photos_are_favourites_only_with_a_seat(client):
    olive_id, _olive = await _add_user(client, "olive")
    _guest_id, guest = await _add_user(client, "guest")
    me = _auth(client._tok)
    me_id = (await (await client.get("/api/me", headers=me)).json())["user_id"]
    await client._db.enqueue(
        "INSERT INTO spaces(id, name, owner_instance_id, owner_username,"
        " identity_public_key, feature_gallery) VALUES('sp-g', 'Oma', 'inst',"
        " 'olive', ?, 1)",
        ("ab" * 32,),
    )
    for uid in (olive_id, me_id):
        await client._db.enqueue(
            "INSERT INTO space_members(space_id, user_id, role) VALUES('sp-g', ?, 'member')",
            (uid,),
        )
    await client._db.enqueue(
        "INSERT INTO gallery_albums(id, space_id, owner_user_id, name)"
        " VALUES('al-g', 'sp-g', ?, 'Grandma')",
        (olive_id,),
    )
    await client._db.enqueue(
        "INSERT INTO gallery_items(id, album_id, uploaded_by, item_type, filename,"
        " thumbnail_filename, width, height) VALUES('it-g', 'al-g', ?, 'photo',"
        " 'g.webp', 'g-t.webp', 10, 10)",
        (olive_id,),
    )

    assert (await client.put(_fav("it-g"), headers=guest)).status == 404
    assert (await client.put(_fav("it-g"), headers=me)).status == 204
    r = await client.get("/api/gallery/favourites", headers=me)
    (row,) = await r.json()
    assert row["id"] == "it-g"
    assert row["album"]["space_id"] == "sp-g"
    assert await _hearts(client, "al-g", me) == {"it-g": True}
