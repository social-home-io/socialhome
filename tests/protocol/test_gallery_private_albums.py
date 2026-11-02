"""Release-blocker: a private album's photos reach only its people (gallery D7).

Marked ``@pytest.mark.security``.

"Privat – nur diese Personen": what is uploaded into a private household
album is seen by its owner, the people they chose and — for a protected
child — the child's guardians. Nobody else, a household admin included,
may read one of its photos by ANY route. This is the adversarial pass of
the private-albums PR: every test is one attempt by an outsider (Lena, a
member) or an admin (who can't open a private album but may delete it)
to read, list, fetch, move, restore, share, download or hear of a private
photo, against the real app, routes and SQLite.

The household: ``admin`` (household admin), Anna (owner of the private
album "Gifts", chosen: Tom), Tom, Lena (outsider) and Mia (a protected
child, guardian Tom).
"""

from __future__ import annotations

import io
import zipfile

import pytest
from PIL import Image

from socialhome.app_keys import (
    event_bus_key,
    gallery_repo_key,
    gallery_service_key,
    notification_service_key,
    space_repo_key,
    user_repo_key,
)
from socialhome.auth import sha256_token_hash
from socialhome.domain.gallery import GalleryItem
from socialhome.services.gallery_service import GalleryNotFoundError
from socialhome.services.realtime_service import RealtimeService
from tests.routes.conftest import _auth, _authed_client

pytestmark = pytest.mark.security


def _jpeg(color: tuple[int, int, int]) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (32, 24), color).save(buf, format="JPEG")
    return buf.getvalue()


SECRET = _jpeg((10, 200, 30))


@pytest.fixture
async def home(aiohttp_client, tmp_dir):
    return await build_home(aiohttp_client, tmp_dir)


async def build_home(aiohttp_client, tmp_dir):
    """The household above, as a signed-in test client (shared with
    ``test_gallery_private_media_refs``)."""
    client = await _authed_client(aiohttp_client, tmp_dir, None)
    toks: dict[str, str] = {"admin": client._tok}
    uids: dict[str, str] = {"admin": client._uid}
    for name in ("anna", "tom", "lena", "mia"):
        uid, tok = f"uid-{name}", f"tok-{name}"
        await client._db.enqueue(
            "INSERT INTO users(username, user_id, display_name, is_admin)"
            " VALUES(?,?,?,0)",
            (name, uid, name.title()),
        )
        await client._db.enqueue(
            "INSERT INTO api_tokens(token_id, user_id, label, token_hash)"
            " VALUES(?,?,?,?)",
            (f"t-{name}", uid, "t", sha256_token_hash(tok)),
        )
        toks[name], uids[name] = tok, uid
    await client._db.enqueue(
        "UPDATE users SET child_protection_enabled=1, is_minor=1 WHERE user_id=?",
        (uids["mia"],),
    )
    await client._db.enqueue(
        "INSERT INTO cp_guardians(minor_user_id, guardian_user_id, granted_by)"
        " VALUES(?,?,?)",
        (uids["mia"], uids["tom"], uids["admin"]),
    )
    client.toks, client.uids = toks, uids
    # Anna's private album with Tom, and one secret photo in it.
    r = await client.post(
        "/api/gallery/albums",
        json={"name": "Gifts", "audience": "chosen", "user_ids": [uids["tom"]]},
        headers=_auth(toks["anna"]),
    )
    assert r.status == 201, await r.text()
    client.album = (await r.json())["id"]
    r = await _upload(client, client.album, SECRET, "anna")
    assert r.status == 201, await r.text()
    client.item = await r.json()
    return client


async def _upload(client, album_id: str, data: bytes, who: str):
    return await client.post(
        f"/api/gallery/albums/{album_id}/items",
        data=data,
        headers={**_auth(client.toks[who]), "Content-Type": "image/jpeg"},
    )


async def _album(client, who: str, name: str = "Summer", **extra) -> str:
    r = await client.post(
        "/api/gallery/albums",
        json={"name": name, **extra},
        headers=_auth(client.toks[who]),
    )
    assert r.status == 201, await r.text()
    return (await r.json())["id"]


def _h(client, who: str) -> dict:
    return _auth(client.toks[who])


def _file(url: str) -> str:
    return url.split("?", 1)[0].rsplit("/", 1)[-1]


# ─── Reading by every route ──────────────────────────────────────────────


@pytest.mark.parametrize("who", ["lena", "admin"])
async def test_outsiders_never_get_the_album_or_its_items(home, who):
    r = await home.get("/api/gallery/albums", headers=_h(home, who))
    for a in await r.json():
        if a["id"] == home.album:
            # Only the admin's locked tile: no name, no cover, can't open.
            assert who == "admin" and a["locked"] is True
            assert a["name"] == "" and a["cover_url"] is None
            assert a["description"] is None
    for path in (
        f"/api/gallery/albums/{home.album}",
        f"/api/gallery/albums/{home.album}/items",
        f"/api/gallery/albums/{home.album}/items?sort=taken",
        (
            f"/api/gallery/albums/{home.album}/items?sort=taken_asc"
            f"&after_day=2026-01-01&after=2026-01-01&after_id={home.item['id']}"
        ),
        f"/api/gallery/albums/{home.album}/audience",
    ):
        r = await home.get(path, headers=_h(home, who))
        assert r.status == 404, path
    r = await home.post(f"/api/gallery/albums/{home.album}/seen", headers=_h(home, who))
    assert r.status == 404


async def test_the_people_inside_see_it(home):
    for who in ("anna", "tom"):
        r = await home.get(
            f"/api/gallery/albums/{home.album}/items", headers=_h(home, who)
        )
        assert [i["id"] for i in await r.json()] == [home.item["id"]]


@pytest.mark.parametrize("who", ["lena", "admin"])
async def test_the_file_url_refuses_outsiders(home, who):
    """A bearer caller who learned the file name gets the unknown file's
    404 — for the photo and its thumbnail."""
    for url in (home.item["url"], home.item["thumbnail_url"]):
        r = await home.get(f"/api/media/{_file(url)}", headers=_h(home, who))
        assert r.status == 404
    r = await home.get(f"/api/media/{_file(home.item['url'])}", headers=_h(home, "tom"))
    assert r.status == 200


@pytest.mark.parametrize("who", ["lena", "admin"])
async def test_downloads_and_zips_leave_it_out(home, who):
    r = await home.post(
        "/api/gallery/download",
        json={"item_ids": [home.item["id"]]},
        headers=_h(home, who),
    )
    assert r.status == 404
    # The admin's "photos of Anna" (leaving the household) skips it too.
    r = await home.get("/api/gallery/people/anna/photos", headers=_h(home, "admin"))
    assert (await r.json())["count"] == 0
    r = await home.get("/api/gallery/people/anna/photos/zip", headers=_h(home, "admin"))
    if r.status == 200:
        assert zipfile.ZipFile(io.BytesIO(await r.read())).namelist() == []


@pytest.mark.parametrize("who", ["lena", "admin", "tom"])
async def test_only_the_uploader_shares_it(home, who):
    r = await home.post(
        "/api/gallery/share",
        json={"item_ids": [home.item["id"]], "target": "feed"},
        headers=_h(home, who),
    )
    assert r.status == 200
    body = await r.json()
    assert body["shared"] == 0 and len(body["skipped"]) == 1


@pytest.mark.parametrize("who", ["lena", "admin"])
async def test_favourites_refuse_it(home, who):
    r = await home.put(
        f"/api/gallery/items/{home.item['id']}/favourite", headers=_h(home, who)
    )
    assert r.status == 404
    r = await home.get("/api/gallery/favourites", headers=_h(home, who))
    assert await r.json() == []


async def test_a_favourite_marked_inside_stays_inside(home):
    """Tom marked it, then Anna took him out: his favourites drop it."""
    r = await home.put(
        f"/api/gallery/items/{home.item['id']}/favourite", headers=_h(home, "tom")
    )
    assert r.status in (200, 204)
    r = await home.put(
        f"/api/gallery/albums/{home.album}/audience",
        json={"audience": "chosen", "user_ids": []},
        headers=_h(home, "anna"),
    )
    assert r.status == 200, await r.text()
    r = await home.get("/api/gallery/favourites", headers=_h(home, "tom"))
    assert await r.json() == []


async def test_a_duplicate_upload_is_no_oracle(home):
    """Lena uploading the very same bytes isn't told it is "already here"."""
    mine = await _album(home, "lena")
    r = await _upload(home, mine, SECRET, "lena")
    assert r.status == 201
    assert "duplicate" not in await r.json()


# ─── Writing to it ───────────────────────────────────────────────────────


@pytest.mark.parametrize("who", ["lena", "admin"])
async def test_outsiders_cant_upload_move_or_change_it(home, who):
    assert (await _upload(home, home.album, _jpeg((1, 2, 3)), who)).status == 404
    r = await home.patch(
        f"/api/gallery/items/{home.item['id']}",
        json={"caption": "seen"},
        headers=_h(home, who),
    )
    assert r.status == 404
    mine = await _album(home, who)
    r = await home.post(
        "/api/gallery/bulk",
        json={"action": "move", "item_ids": [home.item["id"]], "album_id": mine},
        headers=_h(home, who),
    )
    assert (await r.json())["refused"] == [home.item["id"]]
    r = await home.put(
        f"/api/gallery/albums/{home.album}/audience",
        json={"audience": "everyone"},
        headers=_h(home, who),
    )
    assert r.status == 404
    r = await home.patch(
        f"/api/gallery/albums/{home.album}", json={"name": "x"}, headers=_h(home, who)
    )
    assert r.status == 404


async def test_a_chosen_person_cant_move_it_out_or_open_it_up(home):
    """Only the uploader moves a private photo where more people see it,
    and only the owner chooses who sees the album."""
    open_album = await _album(home, "tom", "Ours")
    r = await home.patch(
        f"/api/gallery/items/{home.item['id']}",
        json={"album_id": open_album},
        headers=_h(home, "tom"),
    )
    assert r.status in (403, 409)
    r = await home.put(
        f"/api/gallery/albums/{home.album}/audience",
        json={"audience": "chosen", "user_ids": [home.uids["lena"]]},
        headers=_h(home, "tom"),
    )
    assert r.status == 403


async def test_moving_a_photo_into_a_private_album_never_hides_it(home):
    """Privacy follows the upload: a photo everyone sees can't be moved
    into a private album (it would vanish for the others)."""
    family = await _album(home, "anna", "Family")
    item = await (await _upload(home, family, _jpeg((9, 9, 9)), "anna")).json()
    r = await home.patch(
        f"/api/gallery/items/{item['id']}",
        json={"album_id": home.album},
        headers=_h(home, "anna"),
    )
    assert r.status == 409
    assert (await r.json())["error"]["params"]["reason"] == "would_hide"
    r = await home.get(f"/api/gallery/albums/{family}/items", headers=_h(home, "lena"))
    assert [i["id"] for i in await r.json()] == [item["id"]]


async def test_an_album_with_photos_cant_turn_private(home):
    family = await _album(home, "anna", "Family")
    await _upload(home, family, _jpeg((8, 8, 8)), "lena")
    r = await home.put(
        f"/api/gallery/albums/{family}/audience",
        json={"audience": "chosen", "user_ids": [home.uids["tom"]]},
        headers=_h(home, "anna"),
    )
    assert r.status == 409
    assert (await r.json())["error"]["params"]["reason"] == "not_empty"


async def test_someone_elses_private_photos_are_never_opened_to_everyone(home):
    await _upload(home, home.album, _jpeg((7, 7, 7)), "tom")
    r = await home.put(
        f"/api/gallery/albums/{home.album}/audience",
        json={"audience": "everyone"},
        headers=_h(home, "anna"),
    )
    assert r.status == 409
    assert (await r.json())["error"]["params"]["reason"] == "others_photos"
    # …nor is Tom taken out while his photo is in it.
    r = await home.put(
        f"/api/gallery/albums/{home.album}/audience",
        json={"audience": "chosen", "user_ids": []},
        headers=_h(home, "anna"),
    )
    assert r.status == 409
    assert (await r.json())["error"]["params"]["reason"] == "has_photos"


async def test_an_upload_in_flight_never_lands_hidden_from_its_uploader(home):
    """Lena's upload passed the album check, then Anna made the (still
    empty) album private without her: the photo must not land there."""
    gallery = home.app[gallery_service_key]
    album_id = await _album(home, "anna", "Soon private")
    album = await gallery._repo.get_album(album_id)
    r = await home.put(
        f"/api/gallery/albums/{album_id}/audience",
        json={"audience": "chosen", "user_ids": [home.uids["tom"]]},
        headers=_h(home, "anna"),
    )
    assert r.status == 200
    # The upload had checked the album while it was everyone's:
    stale = GalleryItem(
        id="it-race",
        album_id=album_id,
        uploaded_by=home.uids["lena"],
        item_type="photo",
        url="/api/media/race.webp",
        thumbnail_url="/api/media/race_t.webp",
        width=1,
        height=1,
    )
    with pytest.raises(GalleryNotFoundError):
        await gallery._store_new_item(album, stale, None)
    assert await gallery._repo.get_item("it-race") is None


# ─── Trash ───────────────────────────────────────────────────────────────


async def test_the_trash_shows_outsiders_nothing_and_an_admin_one_locked_tile(home):
    r = await home.delete(
        f"/api/gallery/items/{home.item['id']}", headers=_h(home, "anna")
    )
    assert r.status == 204
    r = await home.get("/api/gallery/trash", headers=_h(home, "lena"))
    assert await r.json() == []
    r = await home.get("/api/gallery/trash/locked", headers=_h(home, "lena"))
    assert await r.json() == []
    r = await home.get("/api/gallery/trash", headers=_h(home, "admin"))
    assert await r.json() == []
    r = await home.get("/api/gallery/trash/locked", headers=_h(home, "admin"))
    (tile,) = await r.json()
    assert tile == {
        "album_id": home.album,
        "owner_user_id": home.uids["anna"],
        "count": 1,
        "deleted_at": tile["deleted_at"],
        "purge_after": tile["purge_after"],
        "album_in_trash": False,
    }
    for who in ("lena", "admin"):
        r = await home.post(
            f"/api/gallery/items/{home.item['id']}/restore", headers=_h(home, who)
        )
        assert r.status == 404
        r = await home.delete(
            f"/api/gallery/trash/{home.item['id']}", headers=_h(home, who)
        )
        assert r.status == 404
    # Emptying the trash leaves what the admin can't see.
    await home.delete("/api/gallery/trash", headers=_h(home, "admin"))
    r = await home.get("/api/gallery/trash", headers=_h(home, "anna"))
    assert [t["id"] for t in await r.json()] == [home.item["id"]]


async def test_an_admin_deletes_it_without_opening_it_and_the_owner_is_told(home):
    r = await home.delete(
        f"/api/gallery/albums/{home.album}", headers=_h(home, "admin")
    )
    assert r.status == 204
    r = await home.get("/api/notifications", headers=_h(home, "anna"))
    body = await r.json()
    rows = body["notifications"] if isinstance(body, dict) else body
    trashed = [n for n in rows if n["type"] == "gallery_album_trashed"]
    assert len(trashed) == 1 and "Gifts" in trashed[0]["title"]
    # The admin learned nothing more, and Anna brings it back.
    r = await home.post(
        f"/api/gallery/albums/{home.album}/restore", headers=_h(home, "anna")
    )
    assert r.status == 200
    r = await home.get(
        f"/api/gallery/albums/{home.album}/items", headers=_h(home, "admin")
    )
    assert r.status == 404


async def test_a_member_cant_delete_it(home):
    r = await home.delete(f"/api/gallery/albums/{home.album}", headers=_h(home, "lena"))
    assert r.status == 204  # idempotent, like an unknown id …
    r = await home.get(f"/api/gallery/albums/{home.album}", headers=_h(home, "anna"))
    assert r.status == 200  # … and nothing happened


# ─── Notifications and live frames ───────────────────────────────────────


async def test_new_photos_tell_only_the_people_inside(home):
    """The "new photos" row's audience is the album's (the batcher hands
    it every upload batch) — never Lena, the admin or Mia."""
    notifs = home.app[notification_service_key]
    album = await home.app[gallery_repo_key].get_album(home.album)
    audience = await notifs._gallery_audience(album, None)
    assert {u.user_id for u in audience} == {home.uids["anna"], home.uids["tom"]}


async def test_live_frames_skip_outsiders(home):
    rt = RealtimeService(
        home.app[event_bus_key],
        None,
        user_repo=home.app[user_repo_key],
        space_repo=home.app[space_repo_key],
        gallery_repo=home.app[gallery_repo_key],
    )
    out = await rt._gallery_outsiders(home.album)
    assert out == {home.uids["admin"], home.uids["lena"], home.uids["mia"]}


# ─── Children (D6) and guardians ─────────────────────────────────────────


async def test_a_childs_private_album_always_shows_to_her_guardian(home):
    mine = await _album(home, "mia", "Diary", audience="chosen", user_ids=[])
    await _upload(home, mine, _jpeg((4, 4, 4)), "mia")
    r = await home.get(f"/api/gallery/albums/{mine}/items", headers=_h(home, "tom"))
    assert len(await r.json()) == 1
    r = await home.get(f"/api/gallery/albums/{mine}/audience", headers=_h(home, "mia"))
    assert (await r.json())["guardian_ids"] == [home.uids["tom"]]
    # Mia "removing" him changes nothing: guardians are outside the list.
    r = await home.put(
        f"/api/gallery/albums/{mine}/audience",
        json={"audience": "chosen", "user_ids": []},
        headers=_h(home, "mia"),
    )
    assert r.status == 200
    r = await home.get(f"/api/gallery/albums/{mine}/items", headers=_h(home, "tom"))
    assert len(await r.json()) == 1
    r = await home.get(f"/api/gallery/albums/{mine}/items", headers=_h(home, "lena"))
    assert r.status == 404


async def test_children_upload_unless_an_admin_turns_it_off(home):
    mine = await _album(home, "mia", "Mine")
    assert (await _upload(home, mine, _jpeg((3, 3, 3)), "mia")).status == 201
    r = await home.patch(
        "/api/gallery/settings",
        json={"children_may_upload": False},
        headers=_h(home, "lena"),
    )
    assert r.status == 403
    r = await home.patch(
        "/api/gallery/settings",
        json={"children_may_upload": False},
        headers=_h(home, "admin"),
    )
    assert r.status == 200
    r = await _upload(home, mine, _jpeg((2, 2, 2)), "mia")
    assert r.status == 403
    assert (await r.json())["error"]["code"] == "CHILD_UPLOAD_OFF"
    r = await home.post(
        "/api/gallery/albums", json={"name": "x"}, headers=_h(home, "mia")
    )
    assert r.status == 403
    r = await home.get("/api/gallery/settings", headers=_h(home, "mia"))
    assert (await r.json())["may_add"] is False
    assert (await _upload(home, mine, _jpeg((1, 1, 1)), "lena")).status == 201


async def test_a_child_deletes_only_her_own(home):
    shared = await _album(home, "lena", "Shared")
    theirs = await (await _upload(home, shared, _jpeg((6, 1, 1)), "lena")).json()
    r = await home.delete(f"/api/gallery/items/{theirs['id']}", headers=_h(home, "mia"))
    assert r.status == 403


# ─── Wall, On this day, sync ─────────────────────────────────────────────


async def test_the_wall_never_shows_it(home):
    repo = home.app[gallery_repo_key]
    assert home.album not in {c.id for c in await repo.list_frame_albums()}
    assert await repo.list_frame_photos([home.album], limit=10) == []
    assert await repo.get_frame_photo(home.item["id"], [home.album]) is None


async def test_on_this_day_never_uses_it_even_for_its_people(home):
    repo = home.app[gallery_repo_key]
    await home._db.enqueue(
        "UPDATE gallery_items SET taken_at='2020-11-02' WHERE id=?",
        (home.item["id"],),
    )
    for who in ("anna", "tom", "lena"):
        found = await repo.find_on_this_day(
            ("11-02",), before_year="2026", viewer_user_id=home.uids[who]
        )
        assert found is None, who


async def test_a_space_album_can_never_be_private(home):
    """The 0139 CHECK: only a household album is ``chosen``, so no space
    sync exporter, federation event or GFS relay can ever carry one."""
    with pytest.raises(Exception, match="CHECK"):
        await home._db.enqueue(
            "INSERT INTO gallery_albums(id, space_id, owner_user_id, name,"
            " audience) VALUES('x', 'sp-1', 'u', 'n', 'chosen')"
        )


# ─── Search (D11, #1285): one rule with every other read ────────────────


async def _search(home, who: str, q: str) -> tuple[list[str], list[str]]:
    r = await home.get(f"/api/gallery/search?q={q}", headers=_h(home, who))
    assert r.status == 200, await r.text()
    body = await r.json()
    return [i["id"] for i in body["items"]], [a["id"] for a in body["albums"]]


async def test_search_never_finds_a_private_name_or_caption_for_outsiders(home):
    r = await home.patch(
        f"/api/gallery/items/{home.item['id']}",
        json={"caption": "bicycle surprise"},
        headers=_h(home, "anna"),
    )
    assert r.status == 200
    for who in ("lena", "admin", "mia"):
        assert await _search(home, who, "bicycle") == ([], []), who
        items, albums = await _search(home, who, "Gifts")
        assert home.album not in albums and home.item["id"] not in items, who
    items, _ = await _search(home, "tom", "bicycle")
    assert items == [home.item["id"]]
    _, albums = await _search(home, "anna", "Gifts")
    assert home.album in albums
    # Anna takes Tom out: from now on his search finds nothing either.
    r = await home.put(
        f"/api/gallery/albums/{home.album}/audience",
        json={"audience": "chosen", "user_ids": []},
        headers=_h(home, "anna"),
    )
    assert r.status == 200
    assert await _search(home, "tom", "bicycle") == ([], [])
    assert await _search(home, "tom", "Gifts") == ([], [])
