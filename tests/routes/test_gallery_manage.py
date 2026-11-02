"""Integration: managing household photos over the real routes —
``PATCH /api/gallery/items/{id}``, ``POST /api/gallery/bulk``,
``POST /api/gallery/albums/{id}/restore``, ``DELETE /api/gallery/trash``,
``POST /api/gallery/download`` and the leaving-household
``GET /api/gallery/people/{username}/photos[/zip]``."""

from __future__ import annotations

import io
import zipfile
from urllib.parse import unquote

from PIL import Image

from socialhome.auth import sha256_token_hash

from .conftest import _auth


def _jpeg(color: tuple[int, int, int] = (200, 40, 40)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (32, 24), color).save(buf, format="JPEG")
    return buf.getvalue()


async def _add_user(client, username: str) -> tuple[str, str]:
    uid, tok = f"uid-{username}", f"tok-{username}"
    await client._db.enqueue(
        "INSERT INTO users(username, user_id, display_name, is_admin) VALUES(?,?,?,0)",
        (username, uid, username.title()),
    )
    await client._db.enqueue(
        "INSERT INTO api_tokens(token_id, user_id, label, token_hash) VALUES(?,?,?,?)",
        (f"t-{username}", uid, "t", sha256_token_hash(tok)),
    )
    return uid, tok


async def _album(client, name: str = "Summer", tok: str | None = None) -> str:
    r = await client.post(
        "/api/gallery/albums", json={"name": name}, headers=_auth(tok or client._tok)
    )
    assert r.status == 201
    return (await r.json())["id"]


async def _upload(client, album_id: str, color=(200, 40, 40), tok: str | None = None):
    r = await client.post(
        f"/api/gallery/albums/{album_id}/items",
        data=_jpeg(color),
        headers={**_auth(tok or client._tok), "Content-Type": "image/jpeg"},
    )
    assert r.status == 201
    return await r.json()


async def test_patch_sets_caption_and_day_and_moves(client):
    h = _auth(client._tok)
    a, b = await _album(client, "A"), await _album(client, "B")
    item = await _upload(client, a)

    r = await client.patch(
        f"/api/gallery/items/{item['id']}",
        json={"caption": "Lake", "taken_at": "2026-07-12", "album_id": b},
        headers=h,
    )

    assert r.status == 200
    body = await r.json()
    assert (body["caption"], body["taken_at"], body["album_id"]) == (
        "Lake",
        "2026-07-12",
        b,
    )
    assert "sig=" in body["url"]
    r = await client.get(f"/api/gallery/albums/{b}/items", headers=h)
    assert [i["id"] for i in await r.json()] == [item["id"]]


async def test_patch_refuses_bad_input_and_strangers(client):
    _uid, tok = await _add_user(client, "eve")
    a = await _album(client)
    item = await _upload(client, a)
    url = f"/api/gallery/items/{item['id']}"
    assert (
        await client.patch(
            url, json={"taken_at": "12.07.2026"}, headers=_auth(client._tok)
        )
    ).status == 422
    assert (
        await client.patch(url, json={"caption": 5}, headers=_auth(client._tok))
    ).status == 422
    assert (
        await client.patch(url, json={"caption": "x"}, headers=_auth(tok))
    ).status == 403
    assert (
        await client.patch(
            "/api/gallery/items/nope", json={"caption": "x"}, headers=_auth(client._tok)
        )
    ).status == 404
    assert (await client.patch(url, json={"caption": "x"})).status == 401


async def test_bulk_delete_then_restore_reports_what_was_refused(client):
    _uid, tok = await _add_user(client, "eve")
    a = await _album(client)
    admins = await _upload(client, a, (1, 1, 1))
    eves = await _upload(client, a, (2, 2, 2), tok=tok)

    r = await client.post(
        "/api/gallery/bulk",
        json={"action": "delete", "item_ids": [admins["id"], eves["id"]]},
        headers=_auth(tok),
    )
    assert r.status == 200
    assert await r.json() == {"done": [eves["id"]], "refused": [admins["id"]]}

    r = await client.post(
        "/api/gallery/bulk",
        json={"action": "restore", "item_ids": [eves["id"]]},
        headers=_auth(tok),
    )
    assert (await r.json())["done"] == [eves["id"]]

    bad = await client.post(
        "/api/gallery/bulk",
        json={"action": "delete", "item_ids": "x"},
        headers=_auth(tok),
    )
    assert bad.status == 422


async def test_bulk_move(client):
    h = _auth(client._tok)
    a, b = await _album(client, "A"), await _album(client, "B")
    item = await _upload(client, a)
    r = await client.post(
        "/api/gallery/bulk",
        json={"action": "move", "item_ids": [item["id"]], "album_id": b},
        headers=h,
    )
    assert (await r.json())["done"] == [item["id"]]


async def test_an_album_delete_is_undone(client):
    h = _auth(client._tok)
    a = await _album(client)
    await _upload(client, a)
    assert (await client.delete(f"/api/gallery/albums/{a}", headers=h)).status == 204

    r = await client.post(f"/api/gallery/albums/{a}/restore", headers=h)

    assert r.status == 200
    assert await r.json() == {"restored": 1}
    assert (await client.get(f"/api/gallery/albums/{a}", headers=h)).status == 200
    assert (
        await client.post(f"/api/gallery/albums/{a}/restore", headers=h)
    ).status == 404


async def test_empty_trash_is_for_admins(client):
    _uid, tok = await _add_user(client, "eve")
    a = await _album(client)
    item = await _upload(client, a)
    h = _auth(client._tok)
    await client.delete(f"/api/gallery/items/{item['id']}", headers=h)

    assert (await client.delete("/api/gallery/trash", headers=_auth(tok))).status == 403
    r = await client.delete("/api/gallery/trash", headers=h)
    assert await r.json() == {"removed": 1}
    assert await (await client.get("/api/gallery/trash", headers=h)).json() == []


async def test_one_photo_downloads_as_the_stored_webp_with_a_human_name(client):
    a = await _album(client, "Sommer 2026")
    item = await _upload(client, a)
    await client.patch(
        f"/api/gallery/items/{item['id']}",
        json={"taken_at": "2026-07-12"},
        headers=_auth(client._tok),
    )

    r = await client.post(
        "/api/gallery/download",
        json={"item_ids": [item["id"]]},
        headers=_auth(client._tok),
    )

    assert r.status == 200
    data = await r.read()
    assert data[:4] == b"RIFF" and data[8:12] == b"WEBP"
    disposition = r.headers["Content-Disposition"]
    assert disposition.startswith("attachment;")
    assert (
        unquote(disposition.split("UTF-8''", 1)[1]) == "Sommer 2026 – 2026-07-12.webp"
    )


async def test_a_selection_downloads_as_one_zip(client):
    a = await _album(client, "Lake")
    one = await _upload(client, a, (1, 1, 1))
    two = await _upload(client, a, (2, 2, 2))

    r = await client.post(
        "/api/gallery/download",
        json={"item_ids": [one["id"], two["id"]], "zip_name": "Lake"},
        headers=_auth(client._tok),
    )

    assert r.status == 200
    assert r.headers["Content-Type"] == "application/zip"
    data = await r.read()
    assert int(r.headers["Content-Length"]) == len(data)
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        assert zf.testzip() is None
        names = zf.namelist()
        assert len(names) == 2 and all(n.startswith("Lake – ") for n in names)
        assert all(zf.read(n)[8:12] == b"WEBP" for n in names)


async def test_download_refuses_nothing_and_too_much(client):
    h = _auth(client._tok)
    r = await client.post(
        "/api/gallery/download", json={"item_ids": ["nope"]}, headers=h
    )
    assert r.status == 404
    r = await client.post(
        "/api/gallery/download",
        json={"item_ids": [f"i{n}" for n in range(501)]},
        headers=h,
    )
    assert r.status == 413
    assert (await r.json())["error"]["code"] == "DOWNLOAD_TOO_LARGE"
    r = await client.post("/api/gallery/download", json={"item_ids": [1]}, headers=h)
    assert r.status == 422


async def test_someones_photos_count_and_zip(client):
    _uid, tok = await _add_user(client, "ben")
    a = await _album(client, "Ben's", tok=tok)
    await _upload(client, a, (1, 1, 1), tok=tok)
    await _upload(client, a, (2, 2, 2), tok=tok)

    r = await client.get("/api/gallery/people/ben/photos", headers=_auth(client._tok))
    assert await r.json() == {"count": 2, "name": "Ben"}

    r = await client.get(
        "/api/gallery/people/ben/photos/zip", headers=_auth(client._tok)
    )
    assert r.status == 200
    with zipfile.ZipFile(io.BytesIO(await r.read())) as zf:
        assert len(zf.namelist()) == 2

    _eid, eve = await _add_user(client, "eve")
    assert (
        await client.get("/api/gallery/people/ben/photos", headers=_auth(eve))
    ).status == 403
    assert (
        await client.get("/api/gallery/people/eve/photos/zip", headers=_auth(eve))
    ).status == 404  # nothing of hers yet


# ─── A space album: same selection, roles of the space, no trash ─────────


async def _space_album(client, member_uid: str) -> str:
    db = client._db
    await db.enqueue(
        "INSERT INTO spaces(id, name, owner_instance_id, owner_username,"
        " identity_public_key, feature_gallery)"
        " VALUES('sp-m', 'X', 'inst', 'admin', ?, 1)",
        ("ab" * 32,),
    )
    for uid, role in ((client._uid, "owner"), (member_uid, "member")):
        await db.enqueue(
            "INSERT INTO space_members(space_id, user_id, role) VALUES('sp-m', ?, ?)",
            (uid, role),
        )
    r = await client.post(
        "/api/spaces/sp-m/gallery/albums",
        json={"name": "Trip"},
        headers=_auth(client._tok),
    )
    assert r.status == 201
    return (await r.json())["id"]


async def test_a_space_selection_follows_the_space_roles_and_has_no_trash(client):
    ben_uid, ben = await _add_user(client, "ben")
    aid = await _space_album(client, ben_uid)
    owners = await _upload(client, aid, (1, 1, 1))
    bens = await _upload(client, aid, (2, 2, 2), tok=ben)
    h = _auth(client._tok)

    # A plain member deletes only what they added.
    r = await client.post(
        "/api/gallery/bulk",
        json={"action": "delete", "item_ids": [owners["id"], bens["id"]]},
        headers=_auth(ben),
    )
    assert await r.json() == {"done": [bens["id"]], "refused": [owners["id"]]}
    # Gone for good (a tombstone that federates) — never in the trash.
    assert await (await client.get("/api/gallery/trash", headers=h)).json() == []
    r = await client.post(
        "/api/gallery/bulk",
        json={"action": "restore", "item_ids": [bens["id"]]},
        headers=h,
    )
    assert (await r.json())["refused"] == [bens["id"]]

    # A seat holder downloads; caption / date / move wait for space parity.
    r = await client.post(
        "/api/gallery/download", json={"item_ids": [owners["id"]]}, headers=_auth(ben)
    )
    assert r.status == 200
    assert (await r.read())[8:12] == b"WEBP"
    r = await client.patch(
        f"/api/gallery/items/{owners['id']}", json={"caption": "x"}, headers=h
    )
    assert r.status == 403

    # The space's owner may delete someone else's photo.
    other = await _upload(client, aid, (3, 3, 3), tok=ben)
    r = await client.post(
        "/api/gallery/bulk",
        json={"action": "delete", "item_ids": [other["id"]]},
        headers=h,
    )
    assert (await r.json())["done"] == [other["id"]]


async def test_bulk_purge_removes_chosen_trash_rows(client):
    h = _auth(client._tok)
    a = await _album(client)
    item = await _upload(client, a)
    await client.delete(f"/api/gallery/items/{item['id']}", headers=h)
    r = await client.post(
        "/api/gallery/bulk",
        json={"action": "purge", "item_ids": [item["id"]]},
        headers=h,
    )
    assert (await r.json())["done"] == [item["id"]]
    assert await (await client.get("/api/gallery/trash", headers=h)).json() == []
