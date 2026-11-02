"""Tests for GallerySearchService — find photos by caption, album name, who
added them and month / year (D11), only ever what the person may see."""

from __future__ import annotations

import pytest

from socialhome.config import Config
from socialhome.crypto import derive_instance_id, generate_identity_keypair
from socialhome.db.database import AsyncDatabase
from socialhome.domain.gallery import AUDIENCE_CHOSEN, GalleryItem
from socialhome.infrastructure.event_bus import EventBus
from socialhome.repositories.gallery_repo import SqliteGalleryRepo
from socialhome.repositories.instance_config_repo import SqliteInstanceConfigRepo
from socialhome.repositories.preferences_repo import SqlitePreferencesRepo
from socialhome.repositories.space_repo import SqliteSpaceRepo
from socialhome.repositories.user_repo import SqliteUserRepo
from socialhome.services.gallery_search_service import GallerySearchService
from socialhome.services.gallery_service import GalleryService
from socialhome.services.preferences_service import PreferencesService

SID = "sp-1"


class Env:
    def __init__(self, db, repo, gallery, svc, prefs) -> None:
        self.db = db
        self.prefs = prefs
        self.repo = repo
        self.gallery = gallery
        self.svc = svc
        self._n = 0

    async def photo(
        self,
        album_id: str,
        *,
        caption: str | None = None,
        by: str = "a-id",
        taken: str | None = None,
        created: str = "2026-01-10 12:00:00",
    ) -> str:
        self._n += 1
        item_id = f"p{self._n}"
        await self.repo.create_item(
            GalleryItem(
                id=item_id,
                album_id=album_id,
                uploaded_by=by,
                item_type="photo",
                url=f"api/media/{item_id}.webp",
                thumbnail_url=f"api/media/{item_id}-t.webp",
                width=10,
                height=10,
                caption=caption,
                taken_at=taken,
                created_at=created,
            )
        )
        return item_id

    async def album(self, name: str, *, space_id: str | None = None, owner="a-id"):
        album = await self.gallery.create_album(
            space_id=space_id, owner_user_id=owner, name=name
        )
        return album.id

    async def ids(self, q: str, user: str = "a-id", **kw) -> list[str]:
        result = await self.svc.search(q, user_id=user, **kw)
        return [h.item.id for h in result.items]


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
    for username, uid, name in (
        ("alice", "a-id", "Alice"),
        ("bob", "b-id", "Bob"),
        ("cleo", "c-id", "Cleo"),
        ("lena", "l-id", "Lena Müller"),
    ):
        await db.enqueue(
            "INSERT INTO users(username, user_id, display_name) VALUES(?,?,?)",
            (username, uid, name),
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
    users = SqliteUserRepo(db)
    gallery = GalleryService(
        repo,
        space_repo,
        EventBus(),
        cfg,
        user_repo=users,
        instance_config=SqliteInstanceConfigRepo(db),
    )
    prefs = PreferencesService(SqlitePreferencesRepo(db))
    svc = GallerySearchService(
        repo, space_repo, users, gallery, household_features=prefs
    )
    yield Env(db, repo, gallery, svc, prefs)
    await db.shutdown()


async def test_finds_a_caption_word_and_its_prefix(env):
    aid = await env.album("Misc")
    p1 = await env.photo(aid, caption="Am Strand mit Oma")
    await env.photo(aid, caption="Im Garten")
    assert await env.ids("strand") == [p1]
    assert await env.ids("stra") == [p1]
    # Diacritics fold both ways.
    p3 = await env.photo(aid, caption="Café in Paris")
    assert await env.ids("cafe") == [p3]

    result = await env.svc.search("strand", user_id="a-id")
    assert result.items[0].snippet == "Am <mark>Strand</mark> mit Oma"
    assert result.items[0].album.id == aid


async def test_album_name_finds_its_photos_and_the_album(env):
    aid = await env.album("Sommerurlaub Kroatien")
    other = await env.album("Winter")
    p1 = await env.photo(aid)
    await env.photo(other)
    result = await env.svc.search("sommer", user_id="a-id")
    assert [h.item.id for h in result.items] == [p1]
    assert [a.id for a in result.albums] == [aid]


async def test_renamed_album_is_found_by_its_new_name(env):
    aid = await env.album("Alt")
    p1 = await env.photo(aid)
    await env.gallery.update_album(aid, actor_user_id="a-id", name="Hochzeit")
    assert await env.ids("hochzeit") == [p1]
    assert await env.ids("alt") == []


async def test_who_added_it(env):
    aid = await env.album("Misc")
    by_lena = await env.photo(aid, by="l-id")
    await env.photo(aid, by="b-id", caption="Lena am See")
    # "von Lena": only what Lena added.
    assert await env.ids("von Lena") == [by_lena]
    assert await env.ids("by müller") == [by_lena]
    # A bare name: her photos, or a caption naming her.
    assert len(await env.ids("Lena")) == 2
    # Nobody called that: nothing.
    assert await env.ids("von Zoe") == []


async def test_month_and_year(env):
    aid = await env.album("Misc")
    july25 = await env.photo(aid, taken="2025-07-14")
    july24 = await env.photo(aid, taken="2024-07-02")
    await env.photo(aid, taken="2025-03-01")
    # No date taken: the upload day counts.
    uploaded = await env.photo(aid, created="2025-07-30 10:00:00")
    assert await env.ids("Juli 2025") == [uploaded, july25]
    assert set(await env.ids("July")) == {uploaded, july25, july24}
    assert len(await env.ids("2025")) == 3
    assert await env.ids("2024-07") == [july24]


async def test_everything_must_match(env):
    aid = await env.album("Urlaub")
    hit = await env.photo(aid, by="l-id", taken="2025-07-01")
    await env.photo(aid, by="l-id", taken="2024-07-01")
    await env.photo(aid, by="b-id", taken="2025-07-01")
    assert await env.ids("Urlaub von Lena Juli 2025") == [hit]


async def test_private_album_only_for_its_people(env):
    aid = await env.album("Geheim", owner="a-id")
    p1 = await env.photo(aid, caption="Überraschung")
    await env.repo.set_album_audience(aid, AUDIENCE_CHOSEN, ["b-id"])
    assert await env.ids("überraschung", "a-id") == [p1]  # the owner
    assert await env.ids("überraschung", "b-id") == [p1]  # chosen
    # An outsider: not the caption, not the album name, not by date.
    assert await env.ids("überraschung", "c-id") == []
    assert await env.ids("geheim", "c-id") == []
    result = await env.svc.search("geheim", user_id="c-id")
    assert result.albums == ()
    assert await env.svc.count("überraschung", user_id="c-id") == 0
    assert await env.svc.count("überraschung", user_id="b-id") == 1


async def test_blocked_uploader_never_matches(env):
    aid = await env.album("Misc")
    await env.photo(aid, by="b-id", caption="Schneemann")
    await env.db.enqueue(
        "INSERT INTO user_blocks(blocker_user_id, blocked_user_id) VALUES(?, ?)",
        ("c-id", "b-id"),
    )
    assert await env.ids("schneemann", "c-id") == []
    assert await env.ids("von Bob", "c-id") == []
    assert await env.svc.count("schneemann", user_id="c-id") == 0
    assert len(await env.ids("schneemann", "a-id")) == 1


async def test_trash_is_left_out_and_comes_back(env):
    aid = await env.album("Misc")
    p1 = await env.photo(aid, caption="Kuchen")
    await env.repo.trash_item(p1, deleted_by="a-id")
    assert await env.ids("kuchen") == []
    await env.gallery.restore_item(p1, actor_user_id="a-id")
    assert await env.ids("kuchen") == [p1]


async def test_space_albums_need_a_seat_and_the_gallery_on(env):
    sid_album = await env.album("Oma Fotos", space_id=SID)
    p1 = await env.photo(sid_album, caption="Kaffee bei Oma")
    assert await env.ids("kaffee", "b-id") == [p1]
    assert await env.ids("kaffee", "b-id", space_id=SID) == [p1]
    # Cleo has no seat.
    assert await env.ids("kaffee", "c-id") == []
    assert await env.ids("kaffee", "c-id", space_id=SID) == []
    # A one-space search leaves the household out.
    home = await env.album("Kaffeeklatsch")
    await env.photo(home)
    assert await env.ids("kaffee", "b-id", space_id=SID) == [p1]
    # A banned leftover seat is none.
    await env.db.enqueue(
        "INSERT INTO space_bans(space_id, user_id, banned_by) VALUES(?, ?, 'a-id')",
        (SID, "b-id"),
    )
    assert await env.ids("kaffee bei", "b-id") == []
    # The space's gallery switched off.
    await env.db.enqueue("UPDATE spaces SET feature_gallery=0 WHERE id=?", (SID,))
    assert await env.ids("kaffee bei", "a-id") == []


async def test_pages(env):
    aid = await env.album("Misc")
    for d in range(1, 6):
        await env.photo(aid, caption="Ball", taken=f"2025-01-0{d}")
    first = await env.svc.search("ball", user_id="a-id", limit=2)
    assert first.more
    second = await env.svc.search("ball", user_id="a-id", limit=2, offset=4)
    assert not second.more
    assert len(second.items) == 1
    # Albums only head the first page.
    assert second.albums == ()


async def test_too_short_or_empty_finds_nothing(env):
    aid = await env.album("Misc")
    await env.photo(aid, caption="a")
    assert (await env.svc.search("a", user_id="a-id")).items == ()
    assert (await env.svc.search("--", user_id="a-id")).items == ()
    assert await env.svc.count("a", user_id="a-id") == 0


async def test_count_is_zero_with_the_household_gallery_off(env):
    aid = await env.album("Misc")
    await env.photo(aid, caption="Drachen")
    assert await env.svc.count("drachen", user_id="a-id") == 1
    await env.prefs.update_household(
        actor_is_admin=True, toggles={"feat_gallery": False}
    )
    assert await env.svc.count("drachen", user_id="a-id") == 0


async def test_a_remote_member_is_found_by_name(env):
    await env.db.enqueue(
        "INSERT INTO remote_instances(id, display_name, remote_identity_pk,"
        " key_self_to_remote, key_remote_to_self, remote_inbox_url,"
        " local_inbox_id, status, source) VALUES('inst-x', 'X', ?, 'k1', 'k2',"
        " 'https://x/wh', 'wh-x', 'confirmed', 'manual')",
        ("00" * 32,),
    )
    await env.db.enqueue(
        "INSERT INTO remote_users(user_id, instance_id, remote_username,"
        " display_name) VALUES('r-id', 'inst-x', 'rita', 'Rita')"
    )
    await env.db.enqueue(
        "INSERT INTO space_members(space_id, user_id, role) VALUES(?, 'r-id', 'member')",
        (SID,),
    )
    aid = await env.album("Oma", space_id=SID)
    p1 = await env.photo(aid, by="r-id")
    assert await env.ids("von Rita", "b-id") == [p1]
