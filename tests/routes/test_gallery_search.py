"""Integration: ``GET /api/gallery/search`` (D11) over the real routes, and
the gallery rows' place in the app's global ``GET /api/search`` — a count on
the "Photos" chip, never a hit or a count of a photo the caller can't see."""

from __future__ import annotations

import io

from PIL import Image

from socialhome.auth import sha256_token_hash

from .conftest import _auth


def _jpeg(color: tuple[int, int, int]) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (32, 24), color).save(buf, format="JPEG")
    return buf.getvalue()


async def _add_user(client, username: str, display: str) -> tuple[str, dict]:
    uid, tok = f"uid-{username}", f"tok-{username}"
    await client._db.enqueue(
        "INSERT INTO users(username, user_id, display_name, is_admin) VALUES(?,?,?,0)",
        (username, uid, display),
    )
    await client._db.enqueue(
        "INSERT INTO api_tokens(token_id, user_id, label, token_hash) VALUES(?,?,?,?)",
        (f"t-{username}", uid, "t", sha256_token_hash(tok)),
    )
    return uid, _auth(tok)


async def _album(client, name: str, headers: dict) -> str:
    r = await client.post("/api/gallery/albums", json={"name": name}, headers=headers)
    assert r.status == 201
    return (await r.json())["id"]


async def _photo(client, album_id: str, shade: int, headers: dict, caption: str) -> str:
    r = await client.post(
        f"/api/gallery/albums/{album_id}/items",
        data=_jpeg((shade, 40, 40)),
        headers={**headers, "Content-Type": "image/jpeg"},
    )
    assert r.status == 201, await r.text()
    item_id = (await r.json())["id"]
    r = await client.patch(
        f"/api/gallery/items/{item_id}", json={"caption": caption}, headers=headers
    )
    assert r.status == 200, await r.text()
    return item_id


async def _search(client, query: str, headers: dict) -> dict:
    r = await client.get(f"/api/gallery/search?q={query}", headers=headers)
    assert r.status == 200, await r.text()
    return await r.json()


async def test_search_requires_auth(client):
    r = await client.get("/api/gallery/search?q=strand")
    assert r.status == 401


async def test_finds_a_caption_with_its_album_and_snippet(client):
    me = _auth(client._tok)
    aid = await _album(client, "Kroatien", me)
    p1 = await _photo(client, aid, 10, me, "Am Strand mit Oma")
    await _photo(client, aid, 200, me, "Im Garten")

    body = await _search(client, "strand", me)
    (row,) = body["items"]
    assert row["id"] == p1
    assert row["snippet"] == "Am <mark>Strand</mark> mit Oma"
    assert row["album"]["id"] == aid
    assert row["album"]["name"] == "Kroatien"
    assert row["is_favourite"] is False
    # Media links are signed like an album's items.
    assert "sig=" in row["thumbnail_url"]
    assert body["more"] is False
    assert body["albums"] == []

    # The album's name finds the album and its photos.
    body = await _search(client, "kroat", me)
    assert [a["id"] for a in body["albums"]] == [aid]
    assert len(body["items"]) == 2


async def test_who_added_it_and_month(client):
    me = _auth(client._tok)
    _lena, lena = await _add_user(client, "lena", "Lena")
    aid = await _album(client, "Alltag", me)
    mine = await _photo(client, aid, 10, me, "Kaffee")
    hers = await _photo(client, aid, 90, lena, "Tee")
    body = await _search(client, "von%20Lena", me)
    assert [r["id"] for r in body["items"]] == [hers]
    # Uploaded today, no date taken: the upload month counts.
    r = await client.patch(
        f"/api/gallery/items/{mine}", json={"taken_at": "2024-07-14"}, headers=me
    )
    assert r.status == 200
    body = await _search(client, "Juli%202024", me)
    assert [r["id"] for r in body["items"]] == [mine]


async def test_a_private_album_never_matches_for_outsiders(client):
    me = _auth(client._tok)
    _eve, eve = await _add_user(client, "eve", "Eve")
    aid = await _album(client, "Geschenke", me)
    await _photo(client, aid, 10, me, "Überraschung")
    await client._db.enqueue(
        "UPDATE gallery_albums SET audience='chosen' WHERE id=?", (aid,)
    )
    assert len((await _search(client, "überraschung", me))["items"]) == 1
    body = await _search(client, "überraschung", eve)
    assert body["items"] == []
    body = await _search(client, "geschenke", eve)
    assert body == {"items": [], "albums": [], "more": False}

    # Nor through the global search: no hit, no count.
    r = await client.get("/api/search?q=überraschung", headers=eve)
    assert r.status == 200
    out = await r.json()
    assert out["hits"] == []
    assert "gallery_item" not in out["counts"]


async def test_a_blocked_uploader_never_matches(client):
    me = _auth(client._tok)
    eve_id, eve = await _add_user(client, "eve", "Eve")
    aid = await _album(client, "Winter", me)
    await _photo(client, aid, 10, eve, "Schneemann")
    r = await client.post("/api/blocks", json={"user_id": eve_id}, headers=me)
    assert r.status == 201, await r.text()
    assert (await _search(client, "schneemann", me))["items"] == []
    assert (await _search(client, "von%20Eve", me))["items"] == []
    assert len((await _search(client, "schneemann", eve))["items"]) == 1


async def test_global_search_counts_photos_but_never_lists_them(client):
    me = _auth(client._tok)
    aid = await _album(client, "Ferien", me)
    await _photo(client, aid, 10, me, "Sandburg am Meer")
    r = await client.get("/api/search?q=sandburg", headers=me)
    out = await r.json()
    assert out["hits"] == []
    assert out["counts"] == {"gallery_item": 1}
    # Even asking for the gallery's scope by name is refused.
    r = await client.get("/api/search?q=sandburg&scope=gallery_item", headers=me)
    assert r.status == 422


async def test_trash_is_left_out(client):
    me = _auth(client._tok)
    aid = await _album(client, "Kuchen", me)
    p1 = await _photo(client, aid, 10, me, "Torte")
    r = await client.delete(f"/api/gallery/items/{p1}", headers=me)
    assert r.status in (200, 204)
    assert (await _search(client, "torte", me))["items"] == []


async def test_bad_paging_is_422_and_gallery_off_is_403(client):
    me = _auth(client._tok)
    r = await client.get("/api/gallery/search?q=x&limit=abc", headers=me)
    assert r.status == 422
    r = await client.put(
        "/api/household/preferences",
        json={"toggles": {"feat_gallery": False}},
        headers=me,
    )
    assert r.status == 200
    r = await client.get("/api/gallery/search?q=strand", headers=me)
    assert r.status == 403
