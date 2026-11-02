"""Tests for GalleryFavouritesService — each person's private ♡ (D12)."""

from __future__ import annotations

import pytest

from socialhome.config import Config
from socialhome.crypto import derive_instance_id, generate_identity_keypair
from socialhome.db.database import AsyncDatabase
from socialhome.domain.gallery import GalleryItem
from socialhome.domain.preferences import FeatureDisabledError
from socialhome.infrastructure.event_bus import EventBus
from socialhome.repositories.gallery_repo import SqliteGalleryRepo
from socialhome.repositories.instance_config_repo import SqliteInstanceConfigRepo
from socialhome.repositories.space_repo import SqliteSpaceRepo
from socialhome.repositories.user_repo import SqliteUserRepo
from socialhome.services.gallery_favourites_service import (
    FAVOURITE_BULK_MAX,
    GalleryFavouritesService,
)
from socialhome.services.gallery_service import GalleryNotFoundError, GalleryService

SID = "sp-1"


class Env:
    def __init__(self, db, repo, gallery, svc, bus) -> None:
        self.db = db
        self.repo = repo
        self.gallery = gallery
        self.svc = svc
        self.bus = bus

    async def photo(self, item_id: str, album_id: str) -> None:
        await self.repo.create_item(
            GalleryItem(
                id=item_id,
                album_id=album_id,
                uploaded_by="a-id",
                item_type="photo",
                url=f"api/media/{item_id}.webp",
                thumbnail_url=f"api/media/{item_id}-t.webp",
                width=10,
                height=10,
            )
        )

    async def ids(self, user_id: str) -> list[str]:
        favourites, _albums = await self.svc.page(user_id=user_id)
        return [f.item.id for f in favourites]


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
    for username, uid in (("alice", "a-id"), ("bob", "b-id"), ("cleo", "c-id")):
        await db.enqueue(
            "INSERT INTO users(username, user_id, display_name) VALUES(?,?,?)",
            (username, uid, username.title()),
        )
    await db.enqueue(
        "INSERT INTO spaces(id, name, owner_instance_id, owner_username,"
        " identity_public_key, feature_gallery) VALUES(?, 'Oma', ?, 'alice', ?, 1)",
        (SID, iid, "ab" * 32),
    )
    for uid in ("a-id", "b-id"):
        await db.enqueue(
            "INSERT INTO space_members(space_id, user_id, role) VALUES(?, ?, 'member')",
            (SID, uid),
        )
    cfg = Config(
        data_dir=str(tmp_dir),
        db_path=str(tmp_dir / "t.db"),
        media_path=str(tmp_dir / "media"),
        mode="standalone",
    )
    repo = SqliteGalleryRepo(db)
    space_repo = SqliteSpaceRepo(db)
    bus = EventBus()
    gallery = GalleryService(
        repo,
        space_repo,
        bus,
        cfg,
        user_repo=SqliteUserRepo(db),
        instance_config=SqliteInstanceConfigRepo(db),
    )
    svc = GalleryFavouritesService(repo, space_repo, gallery)
    yield Env(db, repo, gallery, svc, bus)
    await db.shutdown()


async def _home_album(env: Env, name: str = "Summer") -> str:
    album = await env.gallery.create_album(
        space_id=None, owner_user_id="a-id", name=name
    )
    return album.id


async def _space_album(env: Env) -> str:
    album = await env.gallery.create_album(
        space_id=SID, owner_user_id="a-id", name="Grandma"
    )
    return album.id


async def test_a_favourite_is_the_markers_own(env):
    aid = await _home_album(env)
    await env.photo("p1", aid)
    await env.photo("p2", aid)

    await env.svc.mark("p1", user_id="b-id")
    await env.svc.mark("p1", user_id="b-id")  # idempotent

    assert await env.ids("b-id") == ["p1"]
    assert await env.ids("a-id") == []  # nobody else sees Bob's ♡
    assert await env.svc.ids_among("b-id", ["p1", "p2"]) == {"p1"}
    assert await env.svc.ids_among("a-id", ["p1", "p2"]) == frozenset()

    await env.svc.unmark("p1", user_id="b-id")
    assert await env.ids("b-id") == []


async def test_marking_publishes_nothing(env, monkeypatch):
    aid = await _home_album(env)
    await env.photo("p1", aid)
    seen: list[object] = []

    async def record(_bus: EventBus, event: object) -> None:
        seen.append(event)

    # Nothing about a favourite reaches the bus — no WS frame, no
    # notification, no federation.
    monkeypatch.setattr(EventBus, "publish", record)

    await env.svc.mark("p1", user_id="b-id")
    await env.svc.unmark("p1", user_id="b-id")
    await env.svc.apply("favourite", ["p1"], user_id="b-id")

    assert seen == []


async def test_an_unknown_or_trashed_item_is_not_found(env):
    aid = await _home_album(env)
    await env.photo("p1", aid)
    with pytest.raises(GalleryNotFoundError):
        await env.svc.mark("nope", user_id="b-id")
    await env.repo.trash_item("p1", deleted_by="a-id")
    with pytest.raises(GalleryNotFoundError):
        await env.svc.mark("p1", user_id="b-id")
    # Unmarking tells nothing: it never fails.
    await env.svc.unmark("nope", user_id="b-id")


async def test_a_private_album_is_marked_only_by_its_people(env):
    aid = await _home_album(env, "Just us")
    await env.photo("p1", aid)
    await env.svc.mark("p1", user_id="b-id")
    await env.repo.set_album_audience(aid, "chosen", frozenset({"b-id"}))

    with pytest.raises(GalleryNotFoundError):
        await env.svc.mark("p1", user_id="c-id")
    await env.svc.mark("p1", user_id="a-id")  # the owner always sees it
    assert await env.ids("b-id") == ["p1"]

    # Bob is taken off the album: his favourite there is hidden too.
    await env.repo.set_album_audience(aid, "chosen")
    assert await env.ids("b-id") == []


async def test_space_photos_need_a_seat_and_the_gallery_on(env):
    aid = await _space_album(env)
    await env.photo("s1", aid)

    with pytest.raises(GalleryNotFoundError):
        await env.svc.mark("s1", user_id="c-id")  # no seat
    await env.svc.mark("s1", user_id="b-id")
    assert await env.ids("b-id") == ["s1"]

    # Banned: the leftover seat sees nothing.
    await env.db.enqueue(
        "INSERT INTO space_bans(space_id, user_id, banned_by) VALUES(?, 'b-id', 'a-id')",
        (SID,),
    )
    assert await env.ids("b-id") == []
    await env.db.enqueue("DELETE FROM space_bans")

    # The space switched its gallery off.
    await env.db.enqueue("UPDATE spaces SET feature_gallery = 0 WHERE id=?", (SID,))
    assert await env.ids("b-id") == []
    with pytest.raises(FeatureDisabledError):
        await env.svc.mark("s1", user_id="a-id")


async def test_the_page_carries_the_albums_as_the_viewer_sees_them(env):
    home = await _home_album(env)
    space = await _space_album(env)
    await env.photo("p1", home)
    await env.photo("s1", space)
    await env.svc.mark("p1", user_id="b-id")
    await env.svc.mark("s1", user_id="b-id")

    favourites, albums = await env.svc.page(user_id="b-id")

    assert {f.item.id for f in favourites} == {"p1", "s1"}
    assert albums[home].name == "Summer"
    assert albums[space].space_id == SID


async def test_bulk_marks_what_the_person_sees_and_refuses_the_rest(env):
    open_album = await _home_album(env)
    private = await _home_album(env, "Private")
    await env.repo.set_album_audience(private, "chosen")
    for iid in ("a", "b"):
        await env.photo(iid, open_album)
    await env.photo("hidden", private)

    done, refused = await env.svc.apply(
        "favourite", ["a", "b", "a", "hidden", "nope"], user_id="b-id"
    )
    assert (done, refused) == (["a", "b"], ["hidden", "nope"])

    done, refused = await env.svc.apply("unfavourite", ["a", "nope"], user_id="b-id")
    assert (done, refused) == (["a", "nope"], [])
    assert await env.ids("b-id") == ["b"]


async def test_bulk_refuses_a_bad_action_or_size(env):
    with pytest.raises(ValueError):
        await env.svc.apply("love", ["a"], user_id="b-id")
    with pytest.raises(ValueError):
        await env.svc.apply("favourite", [], user_id="b-id")
    with pytest.raises(ValueError):
        await env.svc.apply(
            "favourite",
            [f"i{n}" for n in range(FAVOURITE_BULK_MAX + 1)],
            user_id="b-id",
        )
