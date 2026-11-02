"""Integration: the household gallery's trash and duplicate check (migration
0139) over the real routes — ``GET /api/gallery/trash``, ``POST
/api/gallery/items/{id}/restore``, ``DELETE /api/gallery/trash/{id}`` and
the ``duplicate`` answer of ``POST /api/gallery/albums/{id}/items``."""

from __future__ import annotations

import io
from datetime import datetime, timedelta

from PIL import Image

from socialhome.domain.gallery import TRASH_RETENTION_DAYS

from .conftest import _auth


def _jpeg(color: tuple[int, int, int] = (200, 40, 40)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (32, 24), color).save(buf, format="JPEG")
    return buf.getvalue()


async def _album(client, name: str = "Summer") -> str:
    r = await client.post(
        "/api/gallery/albums", json={"name": name}, headers=_auth(client._tok)
    )
    assert r.status == 201
    return (await r.json())["id"]


async def _upload(client, album_id: str, data: bytes):
    return await client.post(
        f"/api/gallery/albums/{album_id}/items",
        data=data,
        headers={**_auth(client._tok), "Content-Type": "image/jpeg"},
    )


async def test_the_same_photo_twice_answers_already_here(client):
    aid = await _album(client)
    first = await _upload(client, aid, _jpeg())
    assert first.status == 201
    assert "duplicate" not in await first.json()

    again = await _upload(client, aid, _jpeg())

    assert again.status == 200
    body = await again.json()
    assert body["duplicate"] == "already_here"
    assert body["id"] == (await first.json())["id"]
    assert "content_sha256" not in body  # the digest never leaves the server
    r = await client.get(f"/api/gallery/albums/{aid}/items", headers=_auth(client._tok))
    assert len(await r.json()) == 1


async def test_delete_restore_round_trip_through_the_trash(client):
    aid = await _album(client)
    item = await (await _upload(client, aid, _jpeg())).json()
    h = _auth(client._tok)

    assert (
        await client.delete(f"/api/gallery/items/{item['id']}", headers=h)
    ).status == 204
    r = await client.get(f"/api/gallery/albums/{aid}/items", headers=h)
    assert await r.json() == []

    r = await client.get("/api/gallery/trash", headers=h)
    assert r.status == 200
    (row,) = await r.json()
    assert row["id"] == item["id"]
    assert row["album_name"] == "Summer"
    assert row["album_in_trash"] is False
    assert "sig=" in row["thumbnail_url"]
    deleted = datetime.fromisoformat(row["deleted_at"])
    assert deleted.tzinfo is not None
    assert datetime.fromisoformat(row["purge_after"]) - deleted == timedelta(
        days=TRASH_RETENTION_DAYS
    )

    r = await client.post(f"/api/gallery/items/{item['id']}/restore", headers=h)
    assert r.status == 200
    assert (await r.json())["id"] == item["id"]
    r = await client.get(f"/api/gallery/albums/{aid}/items", headers=h)
    assert [i["id"] for i in await r.json()] == [item["id"]]
    assert await (await client.get("/api/gallery/trash", headers=h)).json() == []


async def test_uploading_a_trashed_photo_again_restores_it(client):
    aid = await _album(client)
    item = await (await _upload(client, aid, _jpeg())).json()
    await client.delete(f"/api/gallery/items/{item['id']}", headers=_auth(client._tok))

    again = await _upload(client, aid, _jpeg())

    assert again.status == 200
    body = await again.json()
    assert body["duplicate"] == "restored" and body["id"] == item["id"]


async def test_deleting_an_album_puts_its_photos_in_the_trash(client):
    aid = await _album(client)
    item = await (await _upload(client, aid, _jpeg())).json()
    h = _auth(client._tok)

    assert (await client.delete(f"/api/gallery/albums/{aid}", headers=h)).status == 204
    assert (await client.get(f"/api/gallery/albums/{aid}", headers=h)).status == 404
    (row,) = await (await client.get("/api/gallery/trash", headers=h)).json()
    assert row["id"] == item["id"] and row["album_in_trash"] is True

    await client.post(f"/api/gallery/items/{item['id']}/restore", headers=h)
    assert (await client.get(f"/api/gallery/albums/{aid}", headers=h)).status == 200


async def test_delete_forever_from_the_trash(client):
    aid = await _album(client)
    item = await (await _upload(client, aid, _jpeg())).json()
    h = _auth(client._tok)
    # Only an item in the trash can be deleted for good.
    assert (
        await client.delete(f"/api/gallery/trash/{item['id']}", headers=h)
    ).status == 404
    await client.delete(f"/api/gallery/items/{item['id']}", headers=h)

    assert (
        await client.delete(f"/api/gallery/trash/{item['id']}", headers=h)
    ).status == 204

    assert await (await client.get("/api/gallery/trash", headers=h)).json() == []
    r = await client.post(f"/api/gallery/items/{item['id']}/restore", headers=h)
    assert r.status == 404


async def test_restoring_an_unknown_item_is_404(client):
    r = await client.post("/api/gallery/items/nope/restore", headers=_auth(client._tok))
    assert r.status == 404


async def test_the_trash_pages_with_before_id(client):
    aid = await _album(client)
    h = _auth(client._tok)
    for shade in range(3):
        item = await (await _upload(client, aid, _jpeg((shade * 70, 9, 9)))).json()
        await client.delete(f"/api/gallery/items/{item['id']}", headers=h)
    first = await (await client.get("/api/gallery/trash?limit=2", headers=h)).json()
    assert len(first) == 2
    rest = await (
        await client.get(
            f"/api/gallery/trash?limit=2&before_id={first[-1]['id']}", headers=h
        )
    ).json()
    assert len(rest) == 1
    assert rest[0]["id"] not in {r["id"] for r in first}
    bad = await client.get("/api/gallery/trash?limit=x", headers=h)
    assert bad.status == 200


async def test_the_trash_needs_auth(client):
    assert (await client.get("/api/gallery/trash")).status == 401
    assert (await client.post("/api/gallery/items/x/restore")).status == 401
    assert (await client.delete("/api/gallery/trash/x")).status == 401
