"""Release-blocker: who may delete and manage household gallery content.

Marked ``@pytest.mark.security``.

A household album is shared by everyone at home, but that is not licence
for anyone to delete anyone's photos. Against the real application
(registry, SQLite, routes), over HTTP:

* A household photo is deleted by its uploader, the album's owner or a
  household admin — any other member gets 403 and the photo stays.
* A household album is renamed, re-flagged or deleted by its owner or a
  household admin — any other member gets 403.
* A protected account (a child) deletes only its own uploads — not even
  in an album it owns.
* A household admin has no say over a space album: the space's roles do.
"""

from __future__ import annotations

import io
from types import MappingProxyType

import pytest
from PIL import Image

from socialhome.app import create_app
from socialhome.app_keys import db_key
from socialhome.auth import sha256_token_hash
from socialhome.config import Config

pytestmark = pytest.mark.security

#: ann is the household admin; bob and carol are members; mia is a
#: protected account (a child).
PEOPLE = (("ann", 1, 0), ("bob", 0, 0), ("carol", 0, 0), ("mia", 0, 1))


def _h(name: str) -> dict:
    return {"Authorization": f"Bearer {name}-tok"}


def _config(tmp_dir) -> Config:
    return Config(
        data_dir=str(tmp_dir),
        db_path=str(tmp_dir / "gal.db"),
        media_path=str(tmp_dir / "media"),
        apps_path=str(tmp_dir / "apps"),
        mode="standalone",
        log_level="ERROR",
        db_write_batch_timeout_ms=10,
        platform_options=MappingProxyType(
            {"standalone": MappingProxyType({"external_url": "https://test.example"})},
        ),
    )


def _png() -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (8, 8), (30, 60, 90)).save(buf, "PNG")
    return buf.getvalue()


@pytest.fixture
async def tc(aiohttp_client, tmp_dir):
    app = create_app(_config(tmp_dir))
    client = await aiohttp_client(app)
    db = app[db_key]
    for name, admin, protected in PEOPLE:
        await db.enqueue(
            "INSERT INTO users(username, user_id, display_name, is_admin,"
            " child_protection_enabled) VALUES(?,?,?,?,?)",
            (name, f"u-{name}", name.title(), admin, protected),
        )
        await db.enqueue(
            "INSERT INTO api_tokens(token_id, user_id, label, token_hash)"
            " VALUES(?,?,?,?)",
            (f"t-{name}", f"u-{name}", "web", sha256_token_hash(f"{name}-tok")),
        )
    return client


async def _bobs_album_with(tc, uploader: str) -> tuple[str, str]:
    r = await tc.post("/api/gallery/albums", json={"name": "Bob's"}, headers=_h("bob"))
    assert r.status == 201
    album_id = (await r.json())["id"]
    r = await tc.post(
        f"/api/gallery/albums/{album_id}/items",
        data=_png(),
        headers={**_h(uploader), "Content-Type": "image/png"},
    )
    assert r.status == 201
    return album_id, (await r.json())["id"]


async def _item_ids(tc, album_id: str) -> list[str]:
    r = await tc.get(f"/api/gallery/albums/{album_id}/items", headers=_h("bob"))
    return [i["id"] for i in await r.json()]


async def test_a_member_cannot_delete_another_members_photo(tc):
    album_id, item_id = await _bobs_album_with(tc, "bob")
    r = await tc.delete(f"/api/gallery/items/{item_id}", headers=_h("carol"))
    assert r.status == 403
    assert await _item_ids(tc, album_id) == [item_id]


@pytest.mark.parametrize("who", ["carol", "bob", "ann"])
async def test_uploader_album_owner_and_admin_may_delete(tc, who):
    """carol uploaded it, bob owns the album, ann is the household admin."""
    album_id, item_id = await _bobs_album_with(tc, "carol")
    r = await tc.delete(f"/api/gallery/items/{item_id}", headers=_h(who))
    assert r.status == 204
    assert await _item_ids(tc, album_id) == []


async def test_only_the_owner_or_an_admin_manages_a_household_album(tc):
    album_id, _ = await _bobs_album_with(tc, "bob")
    url = f"/api/gallery/albums/{album_id}"
    assert (await tc.patch(url, json={"name": "X"}, headers=_h("carol"))).status == 403
    r = await tc.post(
        f"{url}/retention", json={"retention_exempt": True}, headers=_h("carol")
    )
    assert r.status == 403
    assert (await tc.delete(url, headers=_h("carol"))).status == 403
    assert (await tc.patch(url, json={"name": "Tidy"}, headers=_h("ann"))).status == 204
    assert (await tc.delete(url, headers=_h("ann"))).status == 204
    assert (await tc.get(url, headers=_h("bob"))).status == 404


async def test_a_protected_account_cannot_delete_others_photos_even_in_its_album(tc):
    r = await tc.post("/api/gallery/albums", json={"name": "Mia's"}, headers=_h("mia"))
    album_id = (await r.json())["id"]
    r = await tc.post(
        f"/api/gallery/albums/{album_id}/items",
        data=_png(),
        headers={**_h("ann"), "Content-Type": "image/png"},
    )
    anns = (await r.json())["id"]
    assert (
        await tc.delete(f"/api/gallery/items/{anns}", headers=_h("mia"))
    ).status == 403
    r = await tc.delete(f"/api/gallery/albums/{album_id}", headers=_h("mia"))
    assert r.status == 403
    assert await _item_ids(tc, album_id) == [anns]
