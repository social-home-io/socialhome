""" "New photos" rows (household gallery D9): one grouped in-app row per
upload batch, per album and uploader; push off by default and never naming
the album; only people who see the album; space albums follow the space's
notification levels."""

from __future__ import annotations

import string
from pathlib import Path

import orjson
import pytest

import socialhome.i18n as i18n_pkg
from socialhome.crypto import derive_instance_id, generate_identity_keypair
from socialhome.db.database import AsyncDatabase
from socialhome.domain.events import (
    GalleryAlbumDeleted,
    GalleryItemsAdded,
    GalleryItemsShared,
)
from socialhome.domain.gallery import AUDIENCE_CHOSEN, GalleryAlbum
from socialhome.i18n import Catalog
from socialhome.infrastructure.event_bus import EventBus
from socialhome.infrastructure.key_manager import KeyManager
from socialhome.repositories.gallery_repo import SqliteGalleryRepo
from socialhome.repositories.notification_repo import SqliteNotificationRepo
from socialhome.repositories.space_post_repo import SqliteSpacePostRepo
from socialhome.repositories.space_repo import SqliteSpaceRepo
from socialhome.repositories.user_repo import SqliteUserRepo
from socialhome.services.notification_service import (
    _GALLERY_TITLES,
    _SHARE_TITLES,
    NotificationService,
)
from socialhome.services.space_service import SpaceService
from socialhome.services.user_service import UserService

_MESSAGES = Path(i18n_pkg.__file__).parent / "messages"


class _Push:
    def __init__(self) -> None:
        self.titles: list[tuple[str, str]] = []

    async def push_to_users(self, user_ids, payload):
        return 0

    async def push_to_user(self, user_id, payload):
        self.titles.append((user_id, payload.title))


@pytest.fixture
async def env(tmp_dir):
    kp = generate_identity_keypair()
    iid = derive_instance_id(kp.public_key)
    db = AsyncDatabase(tmp_dir / "t.db", batch_timeout_ms=10)
    await db.startup()
    await db.enqueue(
        "INSERT INTO instance_identity(instance_id, identity_private_key,"
        " identity_public_key, routing_secret) VALUES(?,?,?,?)",
        (iid, kp.private_key.hex(), kp.public_key.hex(), "aa" * 32),
    )
    bus = EventBus()
    users = SqliteUserRepo(db)
    space_repo = SqliteSpaceRepo(db, key_manager=KeyManager(b"\x0c" * 32))
    notifs = SqliteNotificationRepo(db, max_per_user=50)
    gallery = SqliteGalleryRepo(db)
    svc = NotificationService(
        notifs, users, space_repo, bus, i18n=Catalog.from_directory(_MESSAGES)
    )
    svc.attach_gallery_repo(gallery)
    push = _Push()
    svc.attach_push_service(push)
    svc.wire()
    user_svc = UserService(users, bus, own_instance_public_key=kp.public_key)

    class Env:
        pass

    e = Env()
    e.db, e.bus, e.users, e.notifs, e.gallery = db, bus, users, notifs, gallery
    e.svc, e.push, e.user_svc, e.space_repo = svc, push, user_svc, space_repo

    async def person(name: str):
        return await user_svc.provision(username=name, display_name=name.title())

    async def album(name: str, owner: str, space_id: str | None = None, **kw):
        return await gallery.create_album(
            GalleryAlbum(
                id=f"al-{name}", space_id=space_id, owner_user_id=owner, name=name, **kw
            )
        )

    async def people(*names: str):
        return [await person(n) for n in names]

    e.person, e.people, e.album = person, people, album
    yield e
    await db.shutdown()


async def _rows(env, user_id: str):
    return [
        n for n in await env.notifs.list(user_id, limit=50) if n.type == "gallery_added"
    ]


def _added(album_id: str, uploader: str, photos: int = 0, videos: int = 0, **kw):
    return GalleryItemsAdded(
        album_id=album_id, uploader=uploader, photos=photos, videos=videos, **kw
    )


async def test_a_batch_is_one_row_for_everyone_else_at_home(env):
    anna, ben, cleo = await env.people("anna", "ben", "cleo")
    await env.album("Sommer 2026", anna.user_id)
    await env.bus.publish(_added("al-Sommer 2026", anna.user_id, photos=12))

    assert await _rows(env, anna.user_id) == []
    for member in (ben, cleo):
        [row] = await _rows(env, member.user_id)
        assert row.title == "Anna added 12 photos to “Sommer 2026”"
        assert row.link_url == "/gallery/albums/al-Sommer%202026"
        assert row.object_key == f"gallery_album:al-Sommer 2026/{anna.user_id}"
        assert row.params is not None
        assert row.params.items == 12
        assert [a.id for a in row.params.actors] == [anna.user_id]
        # What the app builds the sentence from, in the reader's language
        # (the title is only the fallback).
        assert row.params.album == "Sommer 2026"
        assert row.params.flags == ("photos",)
        assert row.params.space_name is None
        assert row.params.to_dict()["album"] == "Sommer 2026"


async def test_no_push_by_default(env):
    """Activity is In app by default: a row, never a buzz (D9)."""
    anna, ben = await env.person("anna"), await env.person("ben")
    await env.album("Trip", anna.user_id)
    await env.bus.publish(_added("al-Trip", anna.user_id, photos=3))
    assert len(await _rows(env, ben.user_id)) == 1
    assert env.push.titles == []


async def test_a_chosen_push_never_names_the_album(env):
    """An album's name can be the secret ("Geschenke für Mama")."""
    anna, ben = await env.person("anna"), await env.person("ben")
    await env.users.set_preference_key(ben.user_id, "notif_kinds", {"activity": "push"})
    await env.album("Geschenke für Mama", anna.user_id)
    await env.bus.publish(_added("al-Geschenke für Mama", anna.user_id, photos=2))
    assert env.push.titles == [(ben.user_id, "New photos from Anna")]
    [row] = await _rows(env, ben.user_id)
    assert "Geschenke" in row.title  # the app's own row keeps it


async def test_the_next_batch_adds_up_while_unread_and_reopens_once_read(env):
    anna, ben = await env.person("anna"), await env.person("ben")
    await env.album("Trip", anna.user_id)
    await env.bus.publish(_added("al-Trip", anna.user_id, photos=12))
    await env.bus.publish(_added("al-Trip", anna.user_id, videos=2))

    [row] = await _rows(env, ben.user_id)
    assert row.title == "Anna added 14 photos and videos to “Trip”"
    assert row.params is not None and row.params.items == 14

    await env.svc.mark_read(ben.user_id, row.id)
    await env.bus.publish(_added("al-Trip", anna.user_id, photos=1))
    [again] = await _rows(env, ben.user_id)
    assert again.id == row.id
    assert not again.is_read
    assert again.title == "Anna added a photo to “Trip”"
    assert again.params is not None and again.params.items == 1


async def test_each_uploader_has_their_own_row(env):
    anna, ben, cleo = await env.people("anna", "ben", "cleo")
    await env.album("Trip", anna.user_id)
    await env.bus.publish(_added("al-Trip", anna.user_id, photos=2))
    await env.bus.publish(_added("al-Trip", ben.user_id, videos=1))
    titles = sorted(r.title for r in await _rows(env, cleo.user_id))
    assert titles == ["Anna added 2 photos to “Trip”", "Ben added a video to “Trip”"]


async def test_a_blocked_uploader_tells_nobody_who_blocked_them(env):
    anna, ben = await env.person("anna"), await env.person("ben")
    await env.user_svc.block(ben.username, anna.user_id)
    await env.album("Trip", anna.user_id)
    await env.bus.publish(_added("al-Trip", anna.user_id, photos=2))
    assert await _rows(env, ben.user_id) == []


async def test_a_private_album_tells_only_its_people(env):
    """D7 hook: a private album's uploads never notify anyone outside it."""
    anna, ben, cleo, dan = await env.people("anna", "ben", "cleo", "dan")
    await env.album("Geschenke", anna.user_id)
    assert await env.gallery.set_album_audience(
        "al-Geschenke", AUDIENCE_CHOSEN, frozenset({ben.user_id})
    )
    # Ben adds to it: the owner and the chosen hear, nobody else.
    await env.bus.publish(_added("al-Geschenke", ben.user_id, photos=4))
    assert len(await _rows(env, anna.user_id)) == 1
    assert await _rows(env, cleo.user_id) == []
    assert await _rows(env, dan.user_id) == []


async def test_the_system_posts_album_never_rings(env):
    anna, ben = await env.person("anna"), await env.person("ben")
    await env.album("Posts", None, is_system=True)
    await env.bus.publish(_added("al-Posts", anna.user_id, photos=1))
    assert await _rows(env, ben.user_id) == []


async def test_an_unknown_album_or_an_empty_batch_rings_nobody(env):
    anna, ben = await env.person("anna"), await env.person("ben")
    await env.bus.publish(_added("al-gone", anna.user_id, photos=1))
    await env.album("Trip", anna.user_id)
    await env.bus.publish(_added("al-Trip", anna.user_id))
    assert await _rows(env, ben.user_id) == []


async def test_a_deleted_album_takes_its_rows(env):
    anna, ben, cleo = await env.people("anna", "ben", "cleo")
    await env.album("Trip", anna.user_id)
    await env.album("Other", anna.user_id)
    await env.bus.publish(_added("al-Trip", anna.user_id, photos=1))
    await env.bus.publish(_added("al-Trip", ben.user_id, photos=1))
    await env.bus.publish(_added("al-Other", anna.user_id, photos=1))
    await env.bus.publish(GalleryAlbumDeleted(album_id="al-Trip", space_id=None))
    assert [r.title for r in await _rows(env, cleo.user_id)] == [
        "Anna added a photo to “Other”"
    ]


async def _space(env, *, level: str | None = None, gallery: bool = True):
    anna, ben = await env.person("anna"), await env.person("ben")
    space_svc = SpaceService(
        env.space_repo,
        SqliteSpacePostRepo(env.db),
        SqliteUserRepo(env.db),
        env.bus,
        own_instance_id="iid",
    )
    space = await space_svc.create_space(owner_username="anna", name="Großeltern")
    await space_svc.add_member(space.id, actor_username="anna", user_id=ben.user_id)
    await env.db.enqueue(
        "UPDATE spaces SET feature_gallery=? WHERE id=?", (int(gallery), space.id)
    )
    if level:
        await env.notifs.set_space_notif_level(
            user_id=ben.user_id, space_id=space.id, level=level
        )
    await env.album("Oma", anna.user_id, space_id=space.id)
    return space, anna, ben


async def test_a_space_album_tells_its_members_here(env):
    space, anna, ben = await _space(env)
    await env.users.set_preference_key(ben.user_id, "notif_kinds", {"activity": "push"})
    await env.bus.publish(_added("al-Oma", anna.user_id, photos=5, space_id=space.id))
    [row] = await _rows(env, ben.user_id)
    assert row.title == "Anna added 5 photos to “Oma” in Großeltern"
    assert row.link_url == f"/spaces/{space.id}/gallery/albums/al-Oma"
    assert row.space_id == space.id
    assert row.params is not None
    assert (row.params.album, row.params.space_name) == ("Oma", "Großeltern")
    # Who and where — never the album.
    assert env.push.titles == [(ben.user_id, "New photos from Anna in Großeltern")]


@pytest.mark.parametrize("level", ["muted", "mentions"])
async def test_a_space_album_follows_the_members_level(env, level):
    space, anna, ben = await _space(env, level=level)
    await env.bus.publish(_added("al-Oma", anna.user_id, photos=5, space_id=space.id))
    assert await _rows(env, ben.user_id) == []


async def test_a_space_without_its_gallery_rings_nobody(env):
    space, anna, ben = await _space(env, gallery=False)
    await env.bus.publish(_added("al-Oma", anna.user_id, photos=5, space_id=space.id))
    assert await _rows(env, ben.user_id) == []


async def test_rows_and_pushes_are_in_the_readers_language(env):
    anna, ben = await env.person("anna"), await env.person("ben")
    await env.users.set_preference_key(ben.user_id, "locale", "de")
    await env.users.set_preference_key(ben.user_id, "notif_kinds", {"activity": "push"})
    await env.album("Sommer 2026", anna.user_id)
    await env.bus.publish(_added("al-Sommer 2026", anna.user_id, photos=12))
    [row] = await _rows(env, ben.user_id)
    assert row.title == "Anna hat 12 Fotos zu „Sommer 2026“ hinzugefügt"
    assert env.push.titles == [(ben.user_id, "Neue Fotos von Anna")]


def test_every_title_has_a_catalog_entry_with_its_fields():
    """The table's English fallbacks match the English catalog."""
    catalogs = {
        loc: orjson.loads((_MESSAGES / f"{loc}.json").read_bytes())
        for loc in ("en", "de", "nl", "fr", "es")
    }

    def fields(text: str) -> set[str]:
        return {f for _, f, _, _ in string.Formatter().parse(text) if f}

    for key, fallback in _GALLERY_TITLES.values():
        assert catalogs["en"][key] == fallback
        for loc, cat in catalogs.items():
            assert fields(cat[key]) == fields(fallback), (loc, key)


# ─── "Your photos were shared" (D8) ──────────────────────────────────────


def _shared(sharer: str, uploader: str, *, photos=0, videos=0, target="feed", **kw):
    return GalleryItemsShared(
        share_id=kw.pop("share_id", "sh-1"),
        sharer=sharer,
        uploader=uploader,
        photos=photos,
        videos=videos,
        target=target,
        album_id=kw.pop("album_id", "al-Trip"),
        **kw,
    )


async def _shared_rows(env, user_id: str):
    return [
        n
        for n in await env.notifs.list(user_id, limit=50)
        if n.type == "gallery_shared"
    ]


async def test_the_uploader_hears_once_per_share_and_push_names_who_only(env):
    anna, lena = await env.person("anna"), await env.person("lena")
    await env.users.set_preference_key(
        lena.user_id, "notif_kinds", {"activity": "push"}
    )
    await env.bus.publish(_shared(anna.user_id, lena.user_id, photos=2))
    await env.bus.publish(_shared(anna.user_id, lena.user_id, photos=1, share_id="s2"))
    rows = await _shared_rows(env, lena.user_id)
    assert sorted(r.title for r in rows) == [
        "Anna shared 2 of your photos in the feed",
        "Anna shared your photo in the feed",
    ]
    assert {r.link_url for r in rows} == {"/gallery/albums/al-Trip"}
    assert await _shared_rows(env, anna.user_id) == []
    assert env.push.titles == [
        (lena.user_id, "Anna shared your photos"),
        (lena.user_id, "Anna shared your photos"),
    ]


async def test_a_share_is_in_app_only_by_default(env):
    anna, lena = await env.person("anna"), await env.person("lena")
    await env.bus.publish(_shared(anna.user_id, lena.user_id, videos=1))
    [row] = await _shared_rows(env, lena.user_id)
    assert row.title == "Anna shared your video in the feed"
    assert env.push.titles == []


async def test_a_space_share_names_the_space_and_a_chat_never_names_the_chat(env):
    space, anna, ben = await _space(env)
    await env.bus.publish(
        _shared(
            ben.user_id,
            anna.user_id,
            photos=2,
            videos=1,
            target="space",
            space_id=space.id,
        )
    )
    await env.bus.publish(
        _shared(ben.user_id, anna.user_id, photos=3, target="chat", share_id="s9")
    )
    rows = await _shared_rows(env, anna.user_id)
    assert sorted(r.title for r in rows) == [
        "Ben sent 3 of your photos in a chat",
        "Ben shared 3 of your photos and videos in “Großeltern”",
    ]
    # Not a row of the space: its mute or level is not the uploader's.
    assert all(r.space_id is None for r in rows)


async def test_a_blocked_sharer_or_ones_own_share_tells_nobody(env):
    anna, lena = await env.person("anna"), await env.person("lena")
    await env.user_svc.block(lena.username, anna.user_id)
    await env.bus.publish(_shared(anna.user_id, lena.user_id, photos=2))
    await env.bus.publish(_shared(lena.user_id, lena.user_id, photos=2))
    cleo = await env.person("cleo")
    await env.bus.publish(
        _shared(cleo.user_id, lena.user_id, photos=1, target="space", space_id="gone")
    )
    await env.bus.publish(_shared(cleo.user_id, lena.user_id))
    assert await _shared_rows(env, lena.user_id) == []


async def test_a_share_row_is_in_the_readers_language(env):
    anna, lena = await env.person("anna"), await env.person("lena")
    await env.users.set_preference_key(lena.user_id, "locale", "de")
    await env.bus.publish(_shared(anna.user_id, lena.user_id, photos=2))
    [row] = await _shared_rows(env, lena.user_id)
    assert row.title == "Anna hat 2 deiner Fotos im Feed geteilt"


def test_every_share_title_has_a_catalog_entry_with_its_fields():
    catalogs = {
        loc: orjson.loads((_MESSAGES / f"{loc}.json").read_bytes())
        for loc in ("en", "de", "nl", "fr", "es")
    }

    def fields(text: str) -> set[str]:
        return {f for _, f, _, _ in string.Formatter().parse(text) if f}

    for key, fallback in _SHARE_TITLES.values():
        assert catalogs["en"][key] == fallback
        for loc, cat in catalogs.items():
            assert fields(cat[key]) == fields(fallback), (loc, key)
    # A push names who only — never an album or a chat.
    for loc, cat in catalogs.items():
        assert fields(cat["notification.push.gallery_shared"]) == {"name"}, loc
        assert fields(cat["notification.push.gallery_shared_anon"]) == set(), loc
