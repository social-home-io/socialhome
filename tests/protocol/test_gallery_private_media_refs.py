"""Release-blocker: naming a stored file never re-publishes what the writer
can't open (gallery D7, independent review of #1286).

Marked ``@pytest.mark.security``.

Every read of a post, comment, cover, DM, moment, highlight frame, listing,
page or task attachment signs its ``api/media/<file>`` references again for
that write's audience — so a reference is a capability. These tests are the
reviewer's proofs of concept, turned around: each one was an attack that
worked (an admin or a chosen member posting a private album's file name to
the feed, the per-user export listing private albums, a guardian losing
sight of their own upload …) and must now fail. Plus one check per write
sink that the one guard (``services/media_ref_guard.py``) is wired there.
"""

from __future__ import annotations

import orjson
import pytest

from socialhome.app_keys import (
    bazaar_service_key,
    calendar_service_key,
    dm_service_key,
    event_bus_key,
    feed_service_key,
    gallery_repo_key,
    highlight_service_key,
    moment_service_key,
    space_cal_service_key,
    space_page_service_key,
    space_repo_key,
    space_service_key,
    task_service_key,
    user_repo_key,
)
from socialhome.domain.errors import InvalidMediaRefError
from socialhome.services.realtime_service import RealtimeService
from tests.protocol.test_gallery_private_albums import (
    SECRET,
    _album,
    _file,
    _h,
    _jpeg,
    _upload,
    build_home,
)

pytestmark = pytest.mark.security


@pytest.fixture
async def home(aiohttp_client, tmp_dir):
    return await build_home(aiohttp_client, tmp_dir)


def _ref(home) -> str:
    return f"api/media/{_file(home.item['url'])}"


async def _feed_has(home, who: str, fname: str) -> bool:
    r = await home.get("/api/feed", headers=_h(home, who))
    assert r.status == 200, await r.text()
    return any(
        fname in u
        for p in await r.json()
        for u in [*(p.get("image_urls") or []), p.get("media_url") or ""]
    )


# ─── The reviewer's proofs of concept, now refused ───────────────────────


@pytest.mark.parametrize("who", ["admin", "tom", "lena"])
async def test_nobody_but_the_uploader_posts_a_private_file_to_the_feed(home, who):
    """PoC 1: the admin (who can't open the album), Tom (chosen, not the
    uploader — the share rule is uploader-only) and Lena (an outsider who
    learned the name) post the file: refused like a malformed reference,
    and nothing reaches anyone's feed or the household "Posts" album."""
    r = await home.post(
        "/api/feed/posts",
        json={"type": "image", "content": "x", "image_urls": [_ref(home)]},
        headers=_h(home, who),
    )
    assert r.status == 422
    assert (await r.json())["error"]["code"] == "INVALID_MEDIA_URL"
    fname = _file(home.item["url"])
    assert not await _feed_has(home, "lena", fname)
    r = await home.get("/api/gallery/albums", headers=_h(home, "lena"))
    for a in [a for a in await r.json() if a.get("is_system")]:
        r = await home.get(
            f"/api/gallery/albums/{a['id']}/items", headers=_h(home, "lena")
        )
        assert all(fname not in i["url"] for i in await r.json())


async def test_the_uploader_may_post_their_own_private_photo(home):
    """Privacy follows the upload: Anna decides about her own photo."""
    r = await home.post(
        "/api/feed/posts",
        json={"type": "image", "content": "x", "image_urls": [_ref(home)]},
        headers=_h(home, "anna"),
    )
    assert r.status == 201, await r.text()


async def test_a_feed_comment_can_not_carry_it_either(home):
    r = await home.post(
        "/api/feed/posts",
        json={"type": "text", "content": "hi"},
        headers=_h(home, "lena"),
    )
    post = await r.json()
    r = await home.post(
        f"/api/feed/posts/{post['id']}/comments",
        json={"type": "image", "content": "", "media_url": _ref(home)},
        headers=_h(home, "lena"),
    )
    assert r.status == 422


async def test_a_household_page_can_not_carry_it(home):
    r = await home.post(
        "/api/pages",
        json={"title": "Wiki", "content": f"![x]({_ref(home)})"},
        headers=_h(home, "lena"),
    )
    assert r.status == 422
    r = await home.post(
        "/api/pages",
        json={"title": "Wiki", "content": "plain"},
        headers=_h(home, "lena"),
    )
    assert r.status == 201
    page = await r.json()
    r = await home.patch(
        f"/api/pages/{page['id']}",
        json={"cover_image_url": _ref(home)},
        headers=_h(home, "lena"),
    )
    assert r.status == 422


async def test_someone_elses_chat_attachment_can_not_be_reposted(home):
    """Beyond the gallery: a file only a DM's members can open."""
    dm = home.app[dm_service_key]
    conv = await dm.create_dm(creator_username="anna", other_username="tom")
    await dm.send_message(
        conv.id,
        sender_username="anna",
        content="",
        type="image",
        media_url="api/media/chat-only-photo.webp",
    )
    feed = home.app[feed_service_key]
    with pytest.raises(InvalidMediaRefError):
        await feed.create_post(
            author_user_id=home.uids["lena"],
            type="image",
            image_urls=["api/media/chat-only-photo.webp"],
        )
    # Tom is in the chat: he may.
    await feed.create_post(
        author_user_id=home.uids["tom"],
        type="image",
        image_urls=["api/media/chat-only-photo.webp"],
    )


async def test_a_dm_can_not_attach_a_private_file(home):
    dm = home.app[dm_service_key]
    conv = await dm.create_dm(creator_username="lena", other_username="mia")
    with pytest.raises(InvalidMediaRefError):
        await dm.send_message(
            conv.id,
            sender_username="lena",
            content="",
            type="image",
            media_url=_ref(home),
        )


async def test_a_moment_can_not_carry_it(home):
    with pytest.raises(InvalidMediaRefError):
        await home.app[moment_service_key].create_moment(
            author_user_id=home.uids["tom"],
            content="look",
            media_url=_ref(home),
            media_type="image",
        )


@pytest.mark.parametrize(
    "key",
    [
        feed_service_key,
        space_service_key,
        dm_service_key,
        task_service_key,
        calendar_service_key,
        space_cal_service_key,
        space_page_service_key,
        bazaar_service_key,
        highlight_service_key,
        moment_service_key,
    ],
)
async def test_every_write_sink_has_the_guard(home, key):
    """Each service that writes media references is wired to the one guard:
    for an outsider the private file is refused; for its uploader it is
    fine; a fresh, unreferenced upload is fine for anyone."""
    svc = home.app[key]
    with pytest.raises(InvalidMediaRefError):
        await svc._require_openable_media((_ref(home),), user_id=home.uids["lena"])
    with pytest.raises(InvalidMediaRefError):
        await svc._require_openable_media((_ref(home),), user_id=home.uids["tom"])
    await svc._require_openable_media((_ref(home),), user_id=home.uids["anna"])
    await svc._require_openable_media(
        ("api/media/brand-new.webp", None), user_id=home.uids["lena"]
    )


async def test_the_admin_export_leaves_out_private_albums(home):
    """PoC 2: the per-user export an admin downloads carried the private
    album's name, the photo's caption and its stored file name."""
    await home.patch(
        f"/api/gallery/items/{home.item['id']}",
        json={"caption": "bicycle"},
        headers=_h(home, "anna"),
    )
    # Her own export keeps all of it (GDPR: her own data).
    r = await home.get("/api/me/export", headers=_h(home, "anna"))
    raw = orjson.dumps(orjson.loads(await r.read())).decode()
    assert "Gifts" in raw and _file(home.item["url"]) in raw
    # The admin's: neither the album, nor its photo, nor the notice of
    # the admin's own delete (it names the album).
    await home.delete(f"/api/gallery/albums/{home.album}", headers=_h(home, "admin"))
    r = await home.get(
        f"/api/users/{home.uids['anna']}/export", headers=_h(home, "admin")
    )
    assert r.status == 200, await r.text()
    raw = orjson.dumps(orjson.loads(await r.read())).decode()
    assert "Gifts" not in raw
    assert "bicycle" not in raw
    assert _file(home.item["url"]) not in raw


async def test_a_guardian_keeps_sight_of_their_own_upload(home):
    """PoC 3: Tom (guardian) added a photo to Mia's private album; when
    protection ends he no longer is a guardian — he still sees the album
    his photo is in (an uploader never loses their own photo), but adds
    nothing more."""
    r = await home.post(
        "/api/gallery/albums",
        json={"name": "Diary", "audience": "chosen", "user_ids": []},
        headers=_h(home, "mia"),
    )
    alb = (await r.json())["id"]
    assert (await _upload(home, alb, _jpeg((9, 9, 9)), "tom")).status == 201
    await home._db.enqueue(
        "UPDATE users SET child_protection_enabled=0 WHERE user_id=?",
        (home.uids["mia"],),
    )
    r = await home.get(f"/api/gallery/albums/{alb}/items", headers=_h(home, "tom"))
    assert r.status == 200 and len(await r.json()) == 1
    assert (await _upload(home, alb, _jpeg((9, 9, 8)), "tom")).status == 403
    r = await home.get(f"/api/gallery/albums/{alb}/items", headers=_h(home, "lena"))
    assert r.status == 404


async def test_a_private_file_is_never_cached(home):
    """LOW 5: a private album's file is ``no-store`` (someone taken out must
    not find it in a cache); an ordinary photo keeps its day of caching. A
    signed link a person already holds still works until it expires (≤ 7 h
    — documented)."""
    r = await home.get(
        f"/api/media/{_file(home.item['url'])}", headers=_h(home, "anna")
    )
    assert r.status == 200
    assert r.headers["Cache-Control"] == "private, no-store"
    family = await _album(home, "lena", "Family")
    it = await (await _upload(home, family, _jpeg((1, 99, 1)), "lena")).json()
    r = await home.get(f"/api/media/{_file(it['url'])}", headers=_h(home, "lena"))
    assert r.headers["Cache-Control"] == "private, max-age=86400"


async def test_the_same_bytes_elsewhere_are_a_different_file(home):
    """An outsider uploading the same photo gets their own stored file —
    never the private one's name."""
    mine = await _album(home, "lena")
    it = await (await _upload(home, mine, SECRET, "lena")).json()
    assert _file(it["url"]) != _file(home.item["url"])


async def test_the_owner_may_add_people_after_others_uploaded(home):
    """Documented (LOW 4, the owner's call): Anna adding Lena shows Tom's
    photo in her private album to Lena; Tom chose Anna's album."""
    toms = await (await _upload(home, home.album, _jpeg((5, 5, 250)), "tom")).json()
    r = await home.put(
        f"/api/gallery/albums/{home.album}/audience",
        json={"audience": "chosen", "user_ids": [home.uids["tom"], home.uids["lena"]]},
        headers=_h(home, "anna"),
    )
    assert r.status == 200
    r = await home.get(
        f"/api/gallery/albums/{home.album}/items", headers=_h(home, "lena")
    )
    assert toms["id"] in [i["id"] for i in await r.json()]


async def test_live_frames_fail_closed_without_an_album_row(home):
    """LOW 7: a frame for an album that is gone reaches nobody."""
    rt = RealtimeService(
        home.app[event_bus_key],
        None,
        user_repo=home.app[user_repo_key],
        space_repo=home.app[space_repo_key],
        gallery_repo=home.app[gallery_repo_key],
    )
    assert await rt._gallery_outsiders("no-such-album") == frozenset(home.uids.values())


async def test_a_persons_uploads_filter_before_the_limit(home):
    """LOW 8: hidden photos first must not push visible ones past the cap."""
    family = await _album(home, "anna", "Family")
    await _upload(home, family, _jpeg((2, 2, 99)), "anna")
    repo = home.app[gallery_repo_key]
    (first,) = await repo.list_household_uploads(
        home.uids["anna"], limit=1, viewer_user_id=home.uids["admin"]
    )
    assert first.album_id == family
    (unfiltered,) = await repo.list_household_uploads(home.uids["anna"], limit=1)
    assert unfiltered.album_id == home.album
