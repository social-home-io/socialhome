"""A space with its gallery switched off has no gallery — on every route.

The space-scoped album routes (``/api/spaces/{id}/gallery/albums``) answer
403 ``FEATURE_DISABLED`` (section ``space:gallery``) once an admin turns the
gallery off. The by-id routes (``/api/gallery/albums/{id}``, ``…/items``,
``/api/gallery/items/{id}``) sit outside that prefix and kept reading and
writing the space's albums regardless — a client that remembered an album id
still saw its photos, uploaded into it, renamed and deleted it.

Members get the same 403 here; an outsider still gets the unknown id's 404
(the seat check comes first, so the gate names no hidden album).
"""

import pytest

from .test_space_read_gate import _add_user, _seat, _seed_space

_SID = "sp-gal-off"
_ALBUM = "al-off"
_ITEM = "it-off"


@pytest.fixture
async def seeded(client):
    hdr = {
        "owner": await _add_user(client, "olive", "uid-olive"),
        "bob": await _add_user(client, "bob", "uid-bob"),
        "guest": await _add_user(client, "guest", "uid-guest"),
    }
    await _seed_space(client, _SID, "public")
    await client._db.enqueue(
        "UPDATE spaces SET feature_gallery = 0 WHERE id=?", (_SID,)
    )
    await _seat(client, _SID, "uid-olive", "owner")
    await _seat(client, _SID, "uid-bob", "member")
    await client._db.enqueue(
        "INSERT INTO gallery_albums(id, space_id, owner_user_id, name, item_count)"
        " VALUES(?, ?, 'uid-bob', 'Summer', 1)",
        (_ALBUM, _SID),
    )
    await client._db.enqueue(
        "INSERT INTO gallery_items(id, album_id, uploaded_by, item_type, filename,"
        " thumbnail_filename, width, height)"
        " VALUES(?, ?, 'uid-bob', 'photo', 'x.webp',"
        " 'x_t.webp', 10, 10)",
        (_ITEM, _ALBUM),
    )
    return client, hdr


async def _feature_disabled(resp) -> None:
    assert resp.status == 403, await resp.text()
    body = await resp.json()
    assert body["error"]["code"] == "FEATURE_DISABLED"


async def test_the_space_scoped_route_is_the_reference(seeded):
    client, hdr = seeded
    await _feature_disabled(
        await client.get(f"/api/spaces/{_SID}/gallery/albums", headers=hdr["bob"])
    )


@pytest.mark.parametrize("suffix", ["", "/items"])
async def test_a_member_reads_nothing_by_id(seeded, suffix):
    client, hdr = seeded
    await _feature_disabled(
        await client.get(f"/api/gallery/albums/{_ALBUM}{suffix}", headers=hdr["bob"])
    )


async def test_a_member_uploads_nothing_by_id(seeded):
    client, hdr = seeded
    await _feature_disabled(
        await client.post(
            f"/api/gallery/albums/{_ALBUM}/items",
            data=b"\x89PNG not really",
            headers={**hdr["bob"], "Content-Type": "image/png"},
        )
    )
    rows = await client._db.fetchall(
        "SELECT 1 FROM gallery_items WHERE album_id=?", (_ALBUM,)
    )
    assert len(rows) == 1


async def test_the_owner_cannot_rename_retain_or_delete_by_id(seeded):
    client, hdr = seeded
    await _feature_disabled(
        await client.patch(
            f"/api/gallery/albums/{_ALBUM}", json={"name": "x"}, headers=hdr["owner"]
        )
    )
    await _feature_disabled(
        await client.post(
            f"/api/gallery/albums/{_ALBUM}/retention",
            json={"retention_exempt": True},
            headers=hdr["owner"],
        )
    )
    await _feature_disabled(
        await client.delete(f"/api/gallery/items/{_ITEM}", headers=hdr["owner"])
    )
    await _feature_disabled(
        await client.delete(f"/api/gallery/albums/{_ALBUM}", headers=hdr["owner"])
    )
    row = await client._db.fetchone(
        "SELECT name, retention_exempt, deleted_at FROM gallery_albums WHERE id=?",
        (_ALBUM,),
    )
    assert row["name"] == "Summer"
    assert not row["retention_exempt"]
    assert row["deleted_at"] is None
    item = await client._db.fetchone("SELECT 1 FROM gallery_items WHERE id=?", (_ITEM,))
    assert item is not None


@pytest.mark.parametrize("suffix", ["", "/items"])
async def test_an_outsider_still_gets_the_unknown_ids_404(seeded, suffix):
    client, hdr = seeded
    r = await client.get(f"/api/gallery/albums/{_ALBUM}{suffix}", headers=hdr["guest"])
    assert r.status == 404, await r.text()


async def test_turning_the_gallery_back_on_restores_the_album(seeded):
    client, hdr = seeded
    await client._db.enqueue(
        "UPDATE spaces SET feature_gallery = 1 WHERE id=?", (_SID,)
    )
    r = await client.get(f"/api/gallery/albums/{_ALBUM}/items", headers=hdr["bob"])
    assert r.status == 200, await r.text()
    assert [i["id"] for i in await r.json()] == [_ITEM]
