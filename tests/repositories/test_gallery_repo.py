"""Tests for SqliteGalleryRepo."""

from __future__ import annotations

import dataclasses
from dataclasses import replace

import pytest

from socialhome.crypto import (
    derive_instance_id,
    generate_identity_keypair,
)
from socialhome.db.database import AsyncDatabase
from socialhome.domain.gallery import GalleryAlbum, GalleryItem, ItemCursor, ItemSort
from socialhome.repositories.gallery_repo import SqliteGalleryRepo


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
    await db.enqueue(
        "INSERT INTO users(username, user_id, display_name) VALUES('alice', 'a-id', 'Alice')",
    )
    await db.enqueue(
        "INSERT INTO spaces(id, name, owner_instance_id, owner_username,"
        " identity_public_key) VALUES('sp-1', 'X', ?, 'alice', ?)",
        (iid, "ab" * 32),
    )
    yield db, SqliteGalleryRepo(db)
    await db.shutdown()


def _album(album_id: str = "alb-1", *, space_id: str | None = "sp-1") -> GalleryAlbum:
    return GalleryAlbum(
        id=album_id,
        space_id=space_id,
        owner_user_id="a-id",
        name=f"Album {album_id}",
        description="d",
    )


def _item(item_id: str = "it-1", *, album_id: str = "alb-1") -> GalleryItem:
    return GalleryItem(
        id=item_id,
        album_id=album_id,
        uploaded_by="a-id",
        item_type="photo",
        url=f"/api/media/{item_id}.webp",
        thumbnail_url=f"/api/media/{item_id}-thumb.jpg",
        width=1920,
        height=1080,
    )


# ─── Albums ──────────────────────────────────────────────────────────────


async def test_create_then_get_album(env):
    _, repo = env
    await repo.create_album(_album())
    got = await repo.get_album("alb-1")
    assert got is not None
    assert got.name == "Album alb-1"
    assert got.space_id == "sp-1"


async def test_create_household_album_with_null_space(env):
    _, repo = env
    await repo.create_album(_album("alb-h", space_id=None))
    got = await repo.get_album("alb-h")
    assert got is not None
    assert got.space_id is None


async def test_list_albums_filters_by_space(env):
    _, repo = env
    await repo.create_album(_album("a1", space_id="sp-1"))
    await repo.create_album(_album("a2", space_id=None))
    sp = await repo.list_albums("sp-1")
    hh = await repo.list_albums(None)
    assert {a.id for a in sp} == {"a1"}
    assert {a.id for a in hh} == {"a2"}


async def test_list_albums_orders_by_created_desc(env):
    _, repo = env
    for i in range(3):
        await repo.create_album(_album(f"a{i}"))
    out = await repo.list_albums("sp-1")
    # Most-recent first.
    assert out[0].id == "a2"


async def test_update_album_only_allowed_keys(env):
    _, repo = env
    await repo.create_album(_album())
    await repo.update_album(
        "alb-1",
        {
            "name": "Renamed",
            "description": "new",
            "owner_user_id": "hijack",  # NOT allowed
        },
    )
    got = await repo.get_album("alb-1")
    assert got.name == "Renamed"
    assert got.description == "new"
    assert got.owner_user_id == "a-id"  # unchanged


async def test_delete_album_cascades_to_items(env):
    _, repo = env
    await repo.create_album(_album())
    await repo.create_item(_item())
    await repo.delete_album("alb-1")
    assert await repo.get_album("alb-1") is None
    assert await repo.get_item("it-1") is None


async def test_set_retention_exempt(env):
    _, repo = env
    await repo.create_album(_album())
    await repo.set_retention_exempt("alb-1", True, space_id="sp-1")
    got = await repo.get_album("alb-1")
    assert got.retention_exempt is True


async def test_set_retention_exempt_wrong_space_no_op(env):
    _, repo = env
    await repo.create_album(_album())
    await repo.set_retention_exempt("alb-1", True, space_id="sp-other")
    got = await repo.get_album("alb-1")
    assert got.retention_exempt is False


# ─── Items ───────────────────────────────────────────────────────────────


async def test_create_then_get_item(env):
    _, repo = env
    await repo.create_album(_album())
    await repo.create_item(_item())
    got = await repo.get_item("it-1")
    assert got is not None
    assert got.album_id == "alb-1"
    assert got.url.startswith("api/media/")


def _at(n: int) -> str:
    return f"2026-10-{n:02d}T12:00:00+00:00"


async def test_list_items_newest_first(env):
    """UX review #1: an album listed oldest first and capped at a page hid
    every upload after the first page."""
    _, repo = env
    await repo.create_album(_album())
    for i in range(1, 6):
        await repo.create_item(replace(_item(f"it-{i}"), created_at=_at(i)))
    out = await repo.list_items("alb-1")
    assert [i.id for i in out] == ["it-5", "it-4", "it-3", "it-2", "it-1"]


async def test_list_items_pages_reach_every_item_once_even_on_ties(env):
    """Paging with the ``(created_at, id)`` cursor of the last row walks the
    whole album: no item skipped, none repeated — even when a burst of
    uploads shares one timestamp across a page boundary."""
    _, repo = env
    await repo.create_album(_album())
    ids = []
    for i in range(1, 4):
        await repo.create_item(replace(_item(f"old-{i}"), created_at=_at(i)))
        ids.append(f"old-{i}")
    for i in range(7):  # one burst, one timestamp
        await repo.create_item(replace(_item(f"burst-{i}"), created_at=_at(9)))
        ids.append(f"burst-{i}")
    seen: list[str] = []
    before = before_id = None
    while True:
        page = await repo.list_items(
            "alb-1", limit=3, before=before, before_id=before_id
        )
        seen += [i.id for i in page]
        if len(page) < 3:
            break
        before, before_id = page[-1].created_at, page[-1].id
    assert sorted(seen) == sorted(ids)
    assert len(seen) == len(ids)
    assert seen[:7] == [f"burst-{i}" for i in reversed(range(7))]
    assert seen[7:] == ["old-3", "old-2", "old-1"]


async def test_list_items_before_alone_keeps_its_created_at_meaning(env):
    _, repo = env
    await repo.create_album(_album())
    for i in range(1, 4):
        await repo.create_item(replace(_item(f"it-{i}"), created_at=_at(i)))
    out = await repo.list_items("alb-1", before=_at(3))
    assert [i.id for i in out] == ["it-2", "it-1"]


async def test_list_items_hides_uploaders_the_viewer_blocked(env):
    """UX review #11: the viewer's personal and guardian blocks hide that
    uploader's items — in SQL, so a page stays full."""
    db, repo = env
    for uname, uid, protected in (("bob", "b-id", 0), ("kid", "k-id", 1)):
        await db.enqueue(
            "INSERT INTO users(username, user_id, display_name,"
            " child_protection_enabled) VALUES(?,?,?,?)",
            (uname, uid, uname, protected),
        )
    await repo.create_album(_album())
    for n, who in enumerate(("a-id", "b-id", "rex", "a-id"), start=1):
        await repo.create_item(
            replace(_item(f"it-{n}"), uploaded_by=who, created_at=_at(n))
        )
    await db.enqueue(
        "INSERT INTO user_blocks(blocker_user_id, blocked_user_id)"
        " VALUES('a-id', 'b-id')"
    )
    await db.enqueue(
        "INSERT INTO cp_minor_blocks(minor_user_id, blocked_user_id, blocked_by)"
        " VALUES('k-id', 'rex', 'a-id')"
    )
    everyone = [i.id for i in await repo.list_items("alb-1")]
    assert everyone == ["it-4", "it-3", "it-2", "it-1"]
    alice = await repo.list_items("alb-1", limit=2, viewer_user_id="a-id")
    assert [i.id for i in alice] == ["it-4", "it-3"]
    alice_all = await repo.list_items("alb-1", viewer_user_id="a-id")
    assert [i.id for i in alice_all] == ["it-4", "it-3", "it-1"]
    kid = await repo.list_items("alb-1", viewer_user_id="k-id")
    assert [i.id for i in kid] == ["it-4", "it-2", "it-1"]
    assert await repo.get_first_item_thumbnail(
        "alb-1", viewer_user_id="k-id"
    ) == await repo.get_first_item_thumbnail("alb-1")
    assert await repo.count_visible_items("alb-1", "a-id") == 3
    assert await repo.count_visible_items("alb-1", "k-id") == 3
    assert await repo.count_visible_items("alb-1", "nobody") == 4


async def test_list_albums_pages_without_repeating_the_system_album(env):
    """UX review #2: only 30 albums loaded. The cursor of the last regular
    album pages on; the pinned system album appears on the first page only."""
    _, repo = env
    await repo.create_album(
        replace(_album("sys"), is_system=True, owner_user_id=None, created_at=_at(1))
    )
    for i in range(2, 9):
        await repo.create_album(replace(_album(f"a-{i}"), created_at=_at(5)))
    seen: list[str] = []
    before = before_id = None
    while True:
        page = await repo.list_albums(
            "sp-1", limit=3, before=before, before_id=before_id
        )
        seen += [a.id for a in page]
        if len(page) < 3:
            break
        last = [a for a in page if not a.is_system][-1]
        before, before_id = last.created_at, last.id
    assert seen[0] == "sys"
    assert sorted(seen) == sorted(["sys"] + [f"a-{i}" for i in range(2, 9)])
    assert len(seen) == 8


async def test_increment_item_count(env):
    _, repo = env
    await repo.create_album(_album())
    await repo.increment_item_count("alb-1", 3)
    a = await repo.get_album("alb-1")
    assert a.item_count == 3


async def test_increment_item_count_clamps_at_zero(env):
    _, repo = env
    await repo.create_album(_album())
    await repo.increment_item_count("alb-1", -10)
    a = await repo.get_album("alb-1")
    assert a.item_count == 0


async def test_get_first_item_thumbnail(env):
    _, repo = env
    await repo.create_album(_album())
    await repo.create_item(_item("it-1"))
    url = await repo.get_first_item_thumbnail("alb-1")
    assert url is not None
    assert url.startswith("api/media/")


async def test_get_first_item_thumbnail_empty_album(env):
    _, repo = env
    await repo.create_album(_album())
    assert await repo.get_first_item_thumbnail("alb-1") is None


# ── list_items_since (resume catch-up §4.4) ───────────────────────────


async def test_list_items_since_joins_through_album(env):
    """``list_items_since`` filters by parent album's space_id (JOIN)."""
    db, repo = env
    # Two albums: one in our space, one household-level (NULL space_id).
    await repo.create_album(_album("alb-space", space_id="sp-1"))
    await repo.create_album(_album("alb-house", space_id=None))
    # One old item in each — both predate the cutoff.
    await db.enqueue(
        "INSERT INTO gallery_items"
        "(id, album_id, uploaded_by, item_type, filename, thumbnail_filename,"
        " width, height, created_at) VALUES(?,?,?,?,?,?,?,?,?)",
        (
            "old-space",
            "alb-space",
            "a-id",
            "photo",
            "f1",
            "t1",
            1,
            1,
            "2025-01-01T00:00:00Z",
        ),
    )
    await db.enqueue(
        "INSERT INTO gallery_items"
        "(id, album_id, uploaded_by, item_type, filename, thumbnail_filename,"
        " width, height, created_at) VALUES(?,?,?,?,?,?,?,?,?)",
        (
            "old-house",
            "alb-house",
            "a-id",
            "photo",
            "f2",
            "t2",
            1,
            1,
            "2025-01-01T00:00:00Z",
        ),
    )
    # Two new items — one in each album — after the cutoff.
    await db.enqueue(
        "INSERT INTO gallery_items"
        "(id, album_id, uploaded_by, item_type, filename, thumbnail_filename,"
        " width, height, created_at) VALUES(?,?,?,?,?,?,?,?,?)",
        (
            "new-space",
            "alb-space",
            "a-id",
            "photo",
            "f3",
            "t3",
            1,
            1,
            "2026-04-10T12:00:00Z",
        ),
    )
    await db.enqueue(
        "INSERT INTO gallery_items"
        "(id, album_id, uploaded_by, item_type, filename, thumbnail_filename,"
        " width, height, created_at) VALUES(?,?,?,?,?,?,?,?,?)",
        (
            "new-house",
            "alb-house",
            "a-id",
            "photo",
            "f4",
            "t4",
            1,
            1,
            "2026-04-10T12:00:00Z",
        ),
    )
    rows = await repo.list_items_since("sp-1", "2026-01-01T00:00:00Z")
    ids = [r.id for r in rows]
    # Only items in space-scoped albums after the cutoff are returned.
    assert ids == ["new-space"]


async def test_list_items_since_respects_limit(env):
    """``limit`` caps the burst size."""
    db, repo = env
    await repo.create_album(_album("alb-1", space_id="sp-1"))
    for i in range(5):
        await db.enqueue(
            "INSERT INTO gallery_items"
            "(id, album_id, uploaded_by, item_type, filename, thumbnail_filename,"
            " width, height, created_at) VALUES(?,?,?,?,?,?,?,?,?)",
            (
                f"it-{i}",
                "alb-1",
                "a-id",
                "photo",
                "f",
                "t",
                1,
                1,
                f"2026-04-{10 + i:02d}T12:00:00Z",
            ),
        )
    rows = await repo.list_items_since(
        "sp-1",
        "2026-01-01T00:00:00Z",
        limit=2,
    )
    assert len(rows) == 2


# ─── Domain helpers ──────────────────────────────────────────────────────


def test_to_thumbnail_dict_excludes_full_url():
    """S-9: thumbnail-only projection must NOT carry the full ``url``."""
    item = _item()
    d = item.to_thumbnail_dict()
    assert "thumbnail_url" in d
    assert "url" not in d


# ─── §24.11 space-scoped writes (federation inbound) ──────────────────────


@pytest.fixture
async def two_spaces(env):
    """alb-1 in sp-1, alb-2 in sp-2, alb-home in the household gallery;
    alb-2 already holds it-2."""
    db, repo = env
    await db.enqueue(
        "INSERT INTO spaces(id, name, owner_instance_id, owner_username,"
        " identity_public_key) VALUES('sp-2', 'Y', 'inst-x', 'alice', ?)",
        ("cd" * 32,),
    )
    await repo.create_album(_album("alb-1", space_id="sp-1"))
    await repo.create_album(_album("alb-2", space_id="sp-2"))
    await repo.create_album(_album("alb-home", space_id=None))
    assert await repo.create_item_in_space(
        _item("it-2", album_id="alb-2"), space_id="sp-2"
    )
    return db, repo


async def test_create_item_in_space_inserts_and_counts(two_spaces):
    _db, repo = two_spaces
    assert await repo.create_item_in_space(
        _item("it-1", album_id="alb-1"), space_id="sp-1"
    )
    assert (await repo.get_item("it-1")).album_id == "alb-1"
    assert (await repo.get_album("alb-1")).item_count == 1


async def test_create_item_in_space_redelivery_is_idempotent(two_spaces):
    _db, repo = two_spaces
    assert await repo.create_item_in_space(
        _item("it-2", album_id="alb-2"), space_id="sp-2"
    )
    assert (await repo.get_album("alb-2")).item_count == 1


async def test_create_item_in_space_refuses_album_of_another_space(two_spaces):
    _db, repo = two_spaces
    assert not await repo.create_item_in_space(
        _item("it-evil", album_id="alb-2"), space_id="sp-1"
    )
    assert await repo.get_item("it-evil") is None
    assert (await repo.get_album("alb-2")).item_count == 1


async def test_create_item_in_space_refuses_household_album(two_spaces):
    _db, repo = two_spaces
    assert not await repo.create_item_in_space(
        _item("it-evil", album_id="alb-home"), space_id="sp-1"
    )
    assert await repo.get_item("it-evil") is None
    assert (await repo.get_album("alb-home")).item_count == 0


async def test_delete_item_in_space_is_scoped(two_spaces):
    _db, repo = two_spaces
    assert not await repo.delete_item_in_space("it-2", space_id="sp-1")
    assert await repo.get_item("it-2") is not None
    assert (await repo.get_album("alb-2")).item_count == 1
    assert await repo.delete_item_in_space("it-2", space_id="sp-2")
    assert await repo.get_item("it-2") is None
    assert (await repo.get_album("alb-2")).item_count == 0


async def test_delete_item_in_space_cannot_reach_household_items(two_spaces):
    _db, repo = two_spaces
    await repo.create_item(_item("it-home", album_id="alb-home"))
    assert not await repo.delete_item_in_space("it-home", space_id="sp-1")
    assert await repo.get_item("it-home") is not None


# ─── Remote owners / uploaders (a member of another household) ────────────

#: A user of another household: no local ``users`` row, by definition.
REMOTE_USER = "u-on-another-household"


async def test_an_item_uploaded_by_a_remote_member_is_stored(two_spaces):
    """The uploader of a synced item lives on another household — there is
    no ``users`` row for them here, and the row must still land (#650)."""
    _db, repo = two_spaces
    item = GalleryItem(
        id="it-remote",
        album_id="alb-1",
        uploaded_by=REMOTE_USER,
        item_type="photo",
        url="/api/media/r.webp",
        thumbnail_url="/api/media/r-thumb.jpg",
        width=10,
        height=10,
    )
    assert await repo.create_item_in_space(item, space_id="sp-1")
    got = await repo.get_item("it-remote")
    assert got is not None and got.uploaded_by == REMOTE_USER


async def test_create_album_in_space_stores_a_remote_owners_album(two_spaces):
    _db, repo = two_spaces
    album = GalleryAlbum(
        id="alb-remote",
        space_id=None,  # untrusted — the gated space wins
        owner_user_id=REMOTE_USER,
        name="Theirs",
        description="from next door",
        item_count=99,  # untrusted — a new album starts empty
        is_system=True,  # untrusted — a wire album is never the system album
    )
    assert await repo.create_album_in_space(album, space_id="sp-1")
    got = await repo.get_album("alb-remote")
    assert got is not None
    assert (got.space_id, got.owner_user_id, got.name, got.description) == (
        "sp-1",
        REMOTE_USER,
        "Theirs",
        "from next door",
    )
    assert got.item_count == 0
    assert got.is_system is False


async def test_create_album_in_space_redelivery_is_idempotent(two_spaces):
    _db, repo = two_spaces
    assert await repo.create_album_in_space(
        _album("alb-new", space_id="sp-1"), space_id="sp-1"
    )
    assert await repo.create_album_in_space(
        GalleryAlbum(id="alb-new", space_id="sp-1", owner_user_id="a-id", name="Re?"),
        space_id="sp-1",
    )
    got = await repo.get_album("alb-new")
    assert (got.name, got.owner_user_id) == ("Album alb-new", "a-id")


async def test_create_album_in_space_refuses_the_same_id_for_another_owner(
    two_spaces,
):
    """An id already held for one owner is not "the same album" when a
    create names somebody else — that is a refusal, not a redelivery."""
    _db, repo = two_spaces
    assert await repo.create_album_in_space(
        _album("alb-new", space_id="sp-1"), space_id="sp-1"
    )
    assert not await repo.create_album_in_space(
        GalleryAlbum(id="alb-new", space_id="sp-1", owner_user_id="x", name="Re?"),
        space_id="sp-1",
    )
    got = await repo.get_album("alb-new")
    assert (got.name, got.owner_user_id) == ("Album alb-new", "a-id")


async def test_create_album_in_space_never_takes_over_another_spaces_album(
    two_spaces,
):
    _db, repo = two_spaces
    for victim in ("alb-2", "alb-home"):
        assert not await repo.create_album_in_space(
            GalleryAlbum(id=victim, space_id="sp-1", owner_user_id="x", name="Evil"),
            space_id="sp-1",
        )
    assert (await repo.get_album("alb-2")).space_id == "sp-2"
    assert (await repo.get_album("alb-home")).space_id is None


async def test_update_album_in_space_is_scoped(two_spaces):
    _db, repo = two_spaces
    assert not await repo.update_album_in_space(
        "alb-2", {"name": "Evil"}, space_id="sp-1"
    )
    assert not await repo.update_album_in_space(
        "alb-home", {"name": "Evil"}, space_id="sp-1"
    )
    assert (await repo.get_album("alb-2")).name == "Album alb-2"
    assert (await repo.get_album("alb-home")).name == "Album alb-home"
    assert await repo.update_album_in_space(
        "alb-2", {"name": "Renamed", "description": "new"}, space_id="sp-2"
    )
    got = await repo.get_album("alb-2")
    assert (got.name, got.description) == ("Renamed", "new")


async def test_an_album_edit_that_changes_nothing_writes_nothing(two_spaces):
    """A re-applied edit (the §25.6 sync re-applies the host's album on every
    stream) leaves ``updated_at`` alone; a real change bumps it."""
    db, repo = two_spaces
    await db.enqueue(
        "UPDATE gallery_albums SET updated_at='2000-01-01' WHERE id='alb-1'"
    )
    album = await repo.get_album("alb-1")
    assert await repo.update_album_in_space(
        "alb-1",
        {"name": album.name, "description": album.description, "cover_item_id": None},
        space_id="sp-1",
    )
    row = await db.fetchone("SELECT updated_at FROM gallery_albums WHERE id='alb-1'")
    assert row["updated_at"] == "2000-01-01"
    assert await repo.update_album_in_space("alb-1", {"name": "New"}, space_id="sp-1")
    row = await db.fetchone("SELECT updated_at FROM gallery_albums WHERE id='alb-1'")
    assert row["updated_at"] != "2000-01-01"


async def test_update_album_in_space_cover_must_be_an_item_of_that_album(
    two_spaces,
):
    """A cover naming another album's item would render that item's
    thumbnail in this album — possibly another space's picture."""
    _db, repo = two_spaces
    assert await repo.update_album_in_space(
        "alb-1", {"cover_item_id": "it-2", "name": "Kept"}, space_id="sp-1"
    )
    got = await repo.get_album("alb-1")
    assert (got.cover_item_id, got.name) == (None, "Kept")
    assert await repo.update_album_in_space(
        "alb-2", {"cover_item_id": "it-2"}, space_id="sp-2"
    )
    assert (await repo.get_album("alb-2")).cover_item_id == "it-2"


async def test_update_album_in_space_keeps_a_cover_that_has_not_arrived(
    two_spaces,
):
    """The edit can overtake the upload it points at. The cover is kept and
    takes effect once the item lands (the service renders it only when the
    item is in this album)."""
    _db, repo = two_spaces
    assert await repo.update_album_in_space(
        "alb-1", {"cover_item_id": "it-later"}, space_id="sp-1"
    )
    assert (await repo.get_album("alb-1")).cover_item_id == "it-later"


async def test_update_album_in_space_clears_the_cover(two_spaces):
    _db, repo = two_spaces
    assert await repo.update_album_in_space(
        "alb-2", {"cover_item_id": "it-2"}, space_id="sp-2"
    )
    assert await repo.update_album_in_space(
        "alb-2", {"cover_item_id": None}, space_id="sp-2"
    )
    assert (await repo.get_album("alb-2")).cover_item_id is None


async def test_list_album_media_names_every_file_of_the_album(two_spaces):
    _db, repo = two_spaces
    assert sorted(await repo.list_album_media("alb-2")) == [
        "api/media/it-2-thumb.jpg",
        "api/media/it-2.webp",
    ]
    assert await repo.list_album_media("alb-1") == []


async def test_item_filenames_drop_a_query_string(two_spaces):
    _db, repo = two_spaces
    item = GalleryItem(
        id="it-q",
        album_id="alb-1",
        uploaded_by="a-id",
        item_type="photo",
        url="api/media/q.webp?sig=abc",
        thumbnail_url="api/media/q-t.webp?sig=def",
        width=1,
        height=1,
    )
    assert await repo.create_item_in_space(item, space_id="sp-1")
    got = await repo.get_item("it-q")
    assert got.url.endswith("/q.webp") and got.thumbnail_url.endswith("/q-t.webp")


async def test_the_system_album_is_never_changed_from_the_wire(two_spaces):
    db, repo = two_spaces
    await db.enqueue(
        "INSERT INTO gallery_albums(id, space_id, is_system, owner_user_id, name)"
        " VALUES('alb-sys', 'sp-1', 1, NULL, 'Posts')"
    )
    assert not await repo.update_album_in_space(
        "alb-sys", {"name": "Evil"}, space_id="sp-1"
    )
    assert not await repo.delete_album_in_space("alb-sys", space_id="sp-1")
    assert (await repo.get_album("alb-sys")).name == "Posts"


async def test_delete_album_in_space_is_scoped_and_takes_its_items(two_spaces):
    _db, repo = two_spaces
    assert not await repo.delete_album_in_space("alb-2", space_id="sp-1")
    assert not await repo.delete_album_in_space("alb-home", space_id="sp-1")
    assert await repo.get_album("alb-2") is not None
    assert await repo.delete_album_in_space("alb-2", space_id="sp-2")
    assert await repo.get_album("alb-2") is None
    assert await repo.get_item("it-2") is None
    assert not await repo.delete_album_in_space("alb-2", space_id="sp-2")


async def test_sync_pages_walk_every_album_and_own_item_in_the_window(env):
    """§25.6: albums and the space's own items, page by page, no fixed
    count; an item past the retention window stays out unless its album
    is exempt; a post's mirror never streams."""
    from dataclasses import replace

    db, repo = env
    await repo.create_album(_album("alb-1"))
    await repo.create_album(replace(_album("alb-keep"), retention_exempt=True))
    await repo.create_album(_album("alb-home", space_id=None))
    for item_id, album_id in (
        ("it-new", "alb-1"),
        ("it-old", "alb-1"),
        ("it-old-kept", "alb-keep"),
        ("it-mirror", "alb-1"),
        ("it-home", "alb-home"),
    ):
        await repo.create_item(_item(item_id, album_id=album_id))
    await db.enqueue(
        "UPDATE gallery_items SET created_at='2020-01-01 00:00:00'"
        " WHERE id IN ('it-old', 'it-old-kept')"
    )
    await db.enqueue(
        "UPDATE gallery_items SET source_post_id='p-1' WHERE id='it-mirror'"
    )
    albums, cursor = await repo.list_albums_sync_page("sp-1", limit=1)
    more, cursor = await repo.list_albums_sync_page("sp-1", cursor=cursor, limit=1)
    last, cursor = await repo.list_albums_sync_page("sp-1", cursor=cursor, limit=1)
    assert [a.id for a in albums + more + last] == ["alb-1", "alb-keep"]
    assert cursor is None
    seen: list[str] = []
    cursor = None
    while True:
        page, cursor = await repo.list_items_sync_page("sp-1", cursor=cursor, limit=1)
        seen.extend(i.id for i in page)
        if cursor is None:
            break
    # No retention window: nothing prunes gallery items, so every one the
    # host shows streams, old ones included.
    assert seen == ["it-new", "it-old", "it-old-kept"]


# ─── Tombstones (migration 0085, §25.6 ``gallery_*_deleted``) ─────────


async def _item_row(db, item_id):
    row = await db.fetchone("SELECT * FROM gallery_items WHERE id=?", (item_id,))
    return dict(row) if row is not None else None


async def _album_row(db, album_id):
    row = await db.fetchone("SELECT * FROM gallery_albums WHERE id=?", (album_id,))
    return dict(row) if row is not None else None


async def test_a_space_item_delete_keeps_a_file_free_tombstone(two_spaces):
    db, repo = two_spaces
    assert await repo.delete_item_in_space("it-2", space_id="sp-2", deleted_by="u-m")
    row = await _item_row(db, "it-2")
    assert row is not None and row["deleted_at"] and row["deleted_by"] == "u-m"
    # The files are blanked: no media reference left (the blobs may go).
    assert (row["filename"], row["thumbnail_filename"], row["caption"]) == (
        "",
        "",
        None,
    )
    assert await repo.get_item("it-2") is None
    assert await repo.list_items("alb-2") == []
    assert await repo.list_items_since("sp-2", "1970-01-01") == []
    assert await repo.list_items_sync_page("sp-2") == ([], None)
    assert await repo.recount_items("alb-2") == 0
    assert await repo.get_first_item_thumbnail("alb-2") is None
    assert await repo.is_item_deleted("it-2", space_id="sp-2")
    assert not await repo.is_item_deleted("it-2", space_id="sp-1")
    # Nothing deletes it twice or brings it back.
    assert not await repo.delete_item_in_space("it-2", space_id="sp-2")
    assert await repo.create_item_in_space(
        _item("it-2", album_id="alb-2"), space_id="sp-2"
    )
    assert (await _item_row(db, "it-2"))["deleted_at"] is not None
    assert await repo.get_item("it-2") is None


async def test_the_service_deletes_tombstone_space_rows_and_remove_household_ones(
    two_spaces,
):
    db, repo = two_spaces
    await repo.create_item(_item("it-home", album_id="alb-home"))
    await repo.delete_item("it-home", deleted_by="a-id")
    assert await _item_row(db, "it-home") is None
    await repo.delete_item("it-2", deleted_by="a-id")
    assert (await _item_row(db, "it-2"))["deleted_by"] == "a-id"
    await repo.delete_album("alb-home", deleted_by="a-id")
    assert await _album_row(db, "alb-home") is None
    await repo.delete_album("alb-1", deleted_by="a-id")
    row = await _album_row(db, "alb-1")
    assert row["deleted_at"] and row["deleted_by"] == "a-id" and row["name"] == ""


async def test_an_album_tombstone_takes_its_items_and_is_never_revived(two_spaces):
    db, repo = two_spaces
    assert await repo.delete_album_in_space("alb-2", space_id="sp-2", deleted_by="u")
    assert (await _item_row(db, "it-2"))["deleted_at"] is not None
    assert await repo.get_album("alb-2") is None
    assert await repo.list_albums("sp-2") == []
    assert await repo.list_albums_sync_page("sp-2") == ([], None)
    assert await repo.is_album_deleted("alb-2", space_id="sp-2")
    assert not await repo.is_album_deleted("alb-2", space_id="sp-1")
    # No create brings it back, no item lands in it, no edit touches it.
    await repo.create_album(_album("alb-2", space_id="sp-2"))
    assert not await repo.create_album_in_space(
        _album("alb-2", space_id="sp-2"), space_id="sp-2"
    )
    assert not await repo.update_album_in_space(
        "alb-2", {"name": "Back"}, space_id="sp-2"
    )
    assert not await repo.create_item_in_space(
        _item("it-late", album_id="alb-2"), space_id="sp-2"
    )
    assert (await _album_row(db, "alb-2"))["name"] == ""
    # Its items are not streamed as item tombstones: the album's covers them.
    assert await repo.list_item_tombstones_page("sp-2") == ([], None)
    albums, _ = await repo.list_album_tombstones_page("sp-2")
    assert [(a.id, a.owner, a.deleted_by) for a in albums] == [("alb-2", "a-id", "u")]


async def test_item_tombstones_page_and_name_their_album(two_spaces):
    _db, repo = two_spaces
    for i in range(3):
        await repo.create_item_in_space(
            _item(f"it-{i}x", album_id="alb-1"), space_id="sp-1"
        )
        await repo.delete_item_in_space(f"it-{i}x", space_id="sp-1")
    page, cursor = await repo.list_item_tombstones_page("sp-1", limit=2)
    assert [t.id for t in page] == ["it-0x", "it-1x"] and cursor is not None
    rest, end = await repo.list_item_tombstones_page("sp-1", cursor=cursor, limit=2)
    assert [t.id for t in rest] == ["it-2x"] and end is None
    assert (rest[0].owner, rest[0].parent_id) == ("a-id", "alb-1")


async def test_stub_tombstones_are_insert_only_and_scoped(two_spaces):
    db, repo = two_spaces
    assert await repo.tombstone_album(
        "alb-new", space_id="sp-1", owner_user_id="a-id", deleted_by="u"
    )
    assert await repo.is_album_deleted("alb-new", space_id="sp-1")
    assert not await repo.tombstone_album(
        "alb-2", space_id="sp-1", owner_user_id="a-id"
    )
    assert await repo.tombstone_item(
        "it-new", space_id="sp-1", album_id="alb-1", uploaded_by="a-id"
    )
    assert await repo.is_item_deleted("it-new", space_id="sp-1")
    # Only under an album live in this space; never over a held id.
    assert not await repo.tombstone_item(
        "it-x", space_id="sp-1", album_id="alb-2", uploaded_by="a-id"
    )
    assert not await repo.tombstone_item(
        "it-y", space_id="sp-1", album_id="alb-new", uploaded_by="a-id"
    )
    assert not await repo.tombstone_item(
        "it-2", space_id="sp-2", album_id="alb-2", uploaded_by="a-id"
    )
    assert await _item_row(db, "it-x") is None
    assert (await _item_row(db, "it-2"))["deleted_at"] is None


async def test_album_tombstones_since_list_recent_deletes(two_spaces):
    _db, repo = two_spaces
    await repo.delete_album_in_space("alb-1", space_id="sp-1")
    recent = await repo.list_album_tombstones_since("sp-1", "1970-01-01")
    assert [(t.id, t.owner) for t in recent] == [("alb-1", "a-id")]
    assert await repo.list_album_tombstones_since("sp-1", "2999-01-01") == []
    # An unparsable ``since`` lists them all: replaying twice is harmless.
    every = await repo.list_album_tombstones_since("sp-1", "garbage")
    assert [t.id for t in every] == ["alb-1"]


async def test_sync_pages_since_a_stamp(env):
    """§25.6 incremental: only the albums / items / tombstones stamped after
    the mark — an album tombstone stamps the items it tombstones too."""
    db, repo = env
    await repo.create_album(_album("alb-a"))
    await repo.create_album(_album("alb-b"))
    await repo.create_item(_item("it-a", album_id="alb-a"))
    await repo.create_item(_item("it-b", album_id="alb-a"))
    await repo.create_item(_item("it-c", album_id="alb-b"))
    row = await db.fetchone("SELECT seq FROM sync_seq_counter WHERE id=1")
    mark = int(row["seq"])
    assert await repo.list_albums_sync_page("sp-1", since=mark) == ([], None)
    assert await repo.list_items_sync_page("sp-1", since=mark) == ([], None)
    await db.enqueue("UPDATE gallery_items SET caption='new' WHERE id='it-a'")
    await db.enqueue(
        "UPDATE gallery_items SET deleted_at='2026-01-01', filename='' WHERE id='it-b'"
    )
    await db.enqueue(
        "UPDATE gallery_albums SET deleted_at='2026-01-01', name='' WHERE id='alb-b'"
    )
    items, _ = await repo.list_items_sync_page("sp-1", since=mark)
    assert [i.id for i in items] == ["it-a"]
    gone_items, _ = await repo.list_item_tombstones_page("sp-1", since=mark)
    assert [t.id for t in gone_items] == ["it-b"]
    gone_albums, _ = await repo.list_album_tombstones_page("sp-1", since=mark)
    assert [t.id for t in gone_albums] == ["alb-b"]
    assert await repo.list_albums_sync_page("sp-1", since=mark) == ([], None)


async def test_has_items_by_others(env):
    _, repo = env
    await repo.create_album(_album())
    assert not await repo.has_items_by_others("alb-1", "a-id")
    await repo.create_item(_item("it-1"))
    assert not await repo.has_items_by_others("alb-1", "a-id")
    assert await repo.has_items_by_others("alb-1", "b-id")


async def test_removing_a_person_keeps_their_albums_and_photos(env):
    """0001 had ``ON DELETE CASCADE`` from both columns to ``users``; 0046
    dropped them. Guard it on the fully migrated schema: even a hard delete
    of the ``users`` row (nothing does one today — ``deprovision`` only
    soft-deletes and no job consumes ``grace_until``) takes no album and no
    photo with it, nor the other people's photos in their albums."""
    db, repo = env
    await db.enqueue(
        "INSERT INTO users(username, user_id, display_name) VALUES('bob', 'b-id', 'Bob')"
    )
    await repo.create_album(_album("hh", space_id=None))
    await repo.create_item(_item("mine", album_id="hh"))
    await repo.create_item(replace(_item("bobs", album_id="hh"), uploaded_by="b-id"))
    for table in ("gallery_albums", "gallery_items"):
        fks = await db.fetchall(f"PRAGMA foreign_key_list({table})")
        assert all(dict(fk)["table"] != "users" for fk in fks), table
    await db.enqueue("DELETE FROM users WHERE user_id='a-id'")
    assert await repo.get_album("hh") is not None
    assert {i.id for i in await repo.list_items("hh")} == {"mine", "bobs"}


# ─── Library model (migration 0139) ──────────────────────────────────────

_SHA = "cd" * 32


async def _household(db, repo, album_id: str = "h-1", owner: str = "a-id") -> None:
    await db.enqueue(
        "INSERT INTO users(username, user_id, display_name)"
        " VALUES('bob', 'b-id', 'Bob') ON CONFLICT DO NOTHING"
    )
    album = _album(album_id, space_id=None)
    await repo.create_album(dataclasses.replace(album, owner_user_id=owner))


async def test_the_upload_digest_is_stored_with_the_item(env):
    db, repo = env
    await _household(db, repo)
    await repo.create_item(_item("it-1", album_id="h-1"), content_sha256=_SHA)
    row = await db.fetchone("SELECT content_sha256 FROM gallery_items WHERE id='it-1'")
    assert row[0] == _SHA


async def test_a_duplicate_is_a_live_one_before_one_in_the_trash(env):
    db, repo = env
    await _household(db, repo)
    await repo.create_item(_item("old", album_id="h-1"), content_sha256=_SHA)
    await repo.trash_item("old", deleted_by="a-id")
    found = await repo.find_household_duplicate(_SHA, viewer_user_id="b-id")
    assert found is not None and found.item.id == "old" and found.in_trash

    await repo.create_item(_item("new", album_id="h-1"), content_sha256=_SHA)
    found = await repo.find_household_duplicate(_SHA, viewer_user_id="b-id")
    assert found is not None and found.item.id == "new" and not found.in_trash
    assert await repo.find_household_duplicate("ef" * 32, viewer_user_id="b-id") is None


async def test_a_space_item_is_never_a_household_duplicate(env):
    _db, repo = env
    await repo.create_album(_album("sp-alb"))
    await repo.create_item(_item("sp-it", album_id="sp-alb"), content_sha256=_SHA)
    assert await repo.find_household_duplicate(_SHA, viewer_user_id="a-id") is None


async def test_trash_and_restore_touch_only_household_items(env):
    _db, repo = env
    await repo.create_album(_album("sp-alb"))
    await repo.create_item(_item("sp-it", album_id="sp-alb"))
    assert not await repo.trash_item("sp-it", deleted_by="a-id")
    assert await repo.trash_album("sp-alb", deleted_by="a-id") == []
    assert await repo.restore_item("sp-it") is None
    assert await repo.purge_trashed_item("sp-it") == []
    assert await repo.get_item("sp-it") is not None


async def test_the_system_album_never_goes_to_the_trash(env):
    _db, repo = env
    await repo.create_album(
        dataclasses.replace(_album("sys", space_id=None), is_system=True)
    )
    assert await repo.trash_album("sys", deleted_by="a-id") == []
    assert await repo.get_album("sys") is not None


async def test_trash_album_keeps_files_and_name(env):
    db, repo = env
    await _household(db, repo)
    await repo.create_item(_item("it-1", album_id="h-1"))
    assert await repo.trash_album("h-1", deleted_by="a-id") == ["it-1"]
    row = await db.fetchone(
        "SELECT a.name, a.deleted_at, i.filename, i.deleted_at FROM gallery_albums a"
        " JOIN gallery_items i ON i.album_id = a.id WHERE a.id='h-1'"
    )
    assert row[0] == "Album h-1" and row[2] == "it-1.webp"
    assert row[1] is not None and row[1] == row[3]  # one stamp for both
    # A second delete finds nothing live.
    assert await repo.trash_album("h-1", deleted_by="a-id") == []


async def test_the_trash_pages_through_photos_deleted_together(env):
    """An album delete stamps all its photos alike: paging on the id keeps
    every one of them, none twice."""
    db, repo = env
    await _household(db, repo)
    for n in range(5):
        await repo.create_item(_item(f"it-{n}", album_id="h-1"))
    await repo.trash_album("h-1", deleted_by="a-id")
    seen: list[str] = []
    before: str | None = None
    while True:
        page = await repo.list_trash(viewer_user_id="a-id", limit=2, before_id=before)
        if not page:
            break
        seen += [t.item.id for t in page]
        before = page[-1].item.id
    assert seen == [f"it-{n}" for n in (4, 3, 2, 1, 0)]


async def test_purge_releases_the_files_and_the_favourites(env):
    db, repo = env
    await _household(db, repo)
    await repo.create_item(_item("it-1", album_id="h-1"))
    assert await repo.add_favourite("b-id", "it-1")
    await repo.trash_item("it-1", deleted_by="a-id")
    assert await repo.purge_trashed_item("it-1") == [
        "api/media/it-1.webp",
        "api/media/it-1-thumb.jpg",
    ]
    assert await db.fetchone("SELECT 1 FROM gallery_favourites") is None
    assert await db.fetchone("SELECT 1 FROM gallery_items") is None


async def test_expired_trash_purge_leaves_newer_and_space_rows(env):
    db, repo = env
    await _household(db, repo)
    await repo.create_album(_album("sp-alb"))
    await repo.create_item(_item("old", album_id="h-1"))
    await repo.create_item(_item("new", album_id="h-1"))
    await repo.create_item(_item("sp-it", album_id="sp-alb"))
    await repo.trash_item("old", deleted_by="a-id")
    await repo.trash_item("new", deleted_by="a-id")
    await repo.delete_item("sp-it", deleted_by="a-id")  # a space tombstone
    await db.enqueue(
        "UPDATE gallery_items SET deleted_at='2026-01-01 00:00:00' WHERE id IN"
        " ('old', 'sp-it')"
    )
    media = await repo.purge_expired_trash("2026-02-01 00:00:00")
    assert media == ["api/media/old.webp", "api/media/old-thumb.jpg"]
    ids = {r[0] for r in await db.fetchall("SELECT id FROM gallery_items")}
    assert ids == {"new", "sp-it"}


async def test_audience_is_set_on_live_household_user_albums_only(env):
    db, repo = env
    await _household(db, repo)
    await repo.create_album(_album("sp-alb"))
    assert await repo.set_album_audience("h-1", "chosen", frozenset({"b-id"}))
    got = await repo.get_album_audience("h-1")
    assert got is not None
    assert (got.audience, got.user_ids) == ("chosen", frozenset({"b-id"}))
    assert await repo.album_visible_to("h-1", "b-id")
    assert await repo.album_visible_to("h-1", "a-id")  # the owner

    # Back to everyone clears the chosen people.
    assert await repo.set_album_audience("h-1", "everyone", frozenset({"b-id"}))
    got = await repo.get_album_audience("h-1")
    assert got is not None and got.user_ids == frozenset()

    assert not await repo.set_album_audience("sp-alb", "chosen")
    assert not await repo.set_album_audience("missing", "chosen")
    assert not await repo.album_visible_to("sp-alb", "a-id")
    assert await repo.get_album_audience("missing") is None
    with pytest.raises(ValueError):
        await repo.set_album_audience("h-1", "friends")


async def test_a_private_album_is_hidden_from_who_is_not_chosen(env):
    db, repo = env
    await _household(db, repo)
    await repo.set_album_audience("h-1", "chosen")
    assert not await repo.album_visible_to("h-1", "b-id")


async def test_favourites(env):
    db, repo = env
    await _household(db, repo)
    await repo.create_item(_item("it-1", album_id="h-1"))
    await repo.create_item(_item("it-2", album_id="h-1"))
    assert await repo.add_favourite("b-id", "it-1")
    assert await repo.add_favourite("b-id", "it-1")  # idempotent
    assert not await repo.add_favourite("b-id", "missing")
    assert await repo.favourite_ids_among("b-id", ["it-1", "it-2"]) == {"it-1"}
    assert await repo.favourite_ids_among("b-id", []) == frozenset()
    assert await repo.favourite_ids_among("a-id", ["it-1"]) == frozenset()
    assert [i.id for i in await repo.list_favourite_items("b-id")] == ["it-1"]

    # A trashed favourite is hidden, and comes back with a restore.
    await repo.trash_item("it-1", deleted_by="a-id")
    assert await repo.list_favourite_items("b-id") == []
    assert not await repo.add_favourite("b-id", "it-1")
    await repo.restore_item("it-1")
    assert [i.id for i in await repo.list_favourite_items("b-id")] == ["it-1"]

    await repo.remove_favourite("b-id", "it-1")
    assert await repo.list_favourite_items("b-id") == []


async def test_reassign_moves_shared_household_albums_only(env):
    db, repo = env
    await _household(db, repo, "shared", owner="b-id")
    await _household(db, repo, "private", owner="b-id")
    await repo.set_album_audience("private", "chosen")
    await repo.create_album(dataclasses.replace(_album("sp-alb"), owner_user_id="b-id"))
    await _household(db, repo, "trashed", owner="b-id")
    await repo.trash_album("trashed", deleted_by="b-id")

    assert await repo.reassign_household_albums("b-id", "a-id") == 2

    owners = dict(await db.fetchall("SELECT id, owner_user_id FROM gallery_albums"))
    assert owners == {
        "shared": "a-id",
        "trashed": "a-id",
        "private": "b-id",
        "sp-alb": "b-id",
    }


# ─── In-album date order (owner decision D1) ─────────────────────────────


async def _dated_album(repo) -> list[str]:
    """An album whose upload order differs from its taken order, with
    ties on the day and on the upload time. Returns the ids newest taken
    first — ``(day, created_at, id)`` descending."""
    await repo.create_album(_album())
    rows = [
        # (id, taken_at, created_at)
        ("t-2019", "2019-07-12", _at(20)),
        ("t-2024-a", "2024-08-01", _at(3)),
        ("t-2024-b", "2024-08-01", _at(3)),
        ("t-2024-c", "2024-08-01", _at(3)),
        ("t-2024-d", "2024-08-01", _at(4)),
        ("u-oct-05", None, _at(5)),  # no date taken: the upload day
        ("t-2026-09", "2026-09-30", _at(1)),
        ("t-1999", "1999-01-01", _at(2)),
    ]
    for item_id, taken, created in rows:
        await repo.create_item(
            replace(_item(item_id), taken_at=taken, created_at=created)
        )
    return [
        "u-oct-05",
        "t-2026-09",
        "t-2024-d",
        "t-2024-c",
        "t-2024-b",
        "t-2024-a",
        "t-2019",
        "t-1999",
    ]


async def test_list_items_by_date_taken_newest_and_oldest_first(env):
    _, repo = env
    expected = await _dated_album(repo)
    newest = await repo.list_items("alb-1", sort=ItemSort.TAKEN)
    assert [i.id for i in newest] == expected
    oldest = await repo.list_items("alb-1", sort=ItemSort.TAKEN_ASC)
    assert [i.id for i in oldest] == list(reversed(expected))
    # The upload order is unchanged for an older client.
    added = await repo.list_items("alb-1")
    assert added[0].id == "t-2019"


@pytest.mark.parametrize("sort", [ItemSort.TAKEN, ItemSort.TAKEN_ASC])
@pytest.mark.parametrize("limit", [1, 2, 3])
async def test_date_pages_reach_every_item_once_even_on_ties(env, sort, limit):
    """#1272's guarantee holds for the date order: the ``(day, created_at,
    id)`` cursor of the last row walks the album with no item skipped or
    repeated, across ties on the day and on the upload time."""
    _, repo = env
    expected = await _dated_album(repo)
    if sort is ItemSort.TAKEN_ASC:
        expected = list(reversed(expected))
    seen: list[str] = []
    after = None
    while True:
        page = await repo.list_items("alb-1", limit=limit, sort=sort, after=after)
        seen += [i.id for i in page]
        if len(page) < limit:
            break
        last = page[-1]
        after = ItemCursor(day=last.day, created_at=last.created_at, item_id=last.id)
    assert seen == expected


async def test_the_date_order_reads_the_taken_index(env):
    db, repo = env
    await _dated_album(repo)
    plan = await db.fetchall(
        "EXPLAIN QUERY PLAN SELECT * FROM gallery_items"
        " WHERE album_id=? AND deleted_at IS NULL"
        " ORDER BY COALESCE(taken_at, substr(created_at, 1, 10)) DESC,"
        " created_at DESC, id DESC LIMIT 50",
        ("alb-1",),
    )
    assert "idx_gallery_items_album_taken" in " ".join(str(r[-1]) for r in plan)


def test_item_day_is_taken_else_upload_day():
    assert _item().day == ""
    assert replace(_item(), created_at=_at(7)).day == "2026-10-07"
    assert replace(_item(), taken_at="2001-02-03", created_at=_at(7)).day == (
        "2001-02-03"
    )


# ─── Albums by newest activity ───────────────────────────────────────────


async def test_list_albums_by_activity_puts_the_newest_upload_first(env):
    _, repo = env
    await repo.create_album(replace(_album("old"), created_at=_at(1)))
    await repo.create_album(replace(_album("new"), created_at=_at(5)))
    await repo.create_album(replace(_album("empty"), created_at=_at(3)))
    await repo.create_item(replace(_item("x", album_id="old"), created_at=_at(9)))
    by_created = await repo.list_albums("sp-1")
    assert [a.id for a in by_created] == ["new", "empty", "old"]
    by_activity = await repo.list_albums("sp-1", by_activity=True)
    assert [a.id for a in by_activity] == ["old", "new", "empty"]
    assert by_activity[0].activity_at == _at(9)
    assert by_activity[1].activity_at == _at(5)
    # A trashed photo is no activity.
    await repo.delete_item("x")
    again = await repo.list_albums("sp-1", by_activity=True)
    assert [a.id for a in again] == ["new", "empty", "old"]


async def test_activity_pages_reach_every_album_once(env):
    _, repo = env
    await repo.create_album(
        replace(_album("sys"), is_system=True, owner_user_id=None, created_at=_at(1))
    )
    ids = []
    for i in range(2, 9):
        await repo.create_album(replace(_album(f"a-{i}"), created_at=_at(2)))
        ids.append(f"a-{i}")
    for i in (3, 5):  # two albums share their newest upload's time
        await repo.create_item(
            replace(_item(f"it-{i}", album_id=f"a-{i}"), created_at=_at(8))
        )
    seen: list[str] = []
    before = before_id = None
    while True:
        page = await repo.list_albums(
            "sp-1", limit=3, before=before, before_id=before_id, by_activity=True
        )
        seen += [a.id for a in page]
        if len(page) < 3:
            break
        last = [a for a in page if not a.is_system][-1]
        before, before_id = last.activity_at, last.id
    assert seen[:3] == ["sys", "a-5", "a-3"]
    assert sorted(seen) == sorted(["sys", *ids])
    assert len(seen) == len(ids) + 1


# ─── On this day (owner decision D14) ────────────────────────────────────


async def _memories(db, repo) -> None:
    """Household albums with photos from 1 November in several years."""
    await _household(db, repo, "summer")
    await _household(db, repo, "autumn")
    await _household(db, repo, "secret")
    await repo.set_album_audience("secret", "chosen")
    await repo.create_album(_album("sp-alb"))  # a space album
    rows = [
        ("s-1", "summer", "2024-11-01", "a-id"),
        ("a-1", "autumn", "2023-11-01", "a-id"),
        ("a-2", "autumn", "2023-11-01", "b-id"),
        ("a-3", "autumn", "2022-11-01", "a-id"),
        ("x-1", "secret", "2021-11-01", "a-id"),
        ("x-2", "secret", "2021-11-01", "a-id"),
        ("x-3", "secret", "2021-11-01", "a-id"),
        ("p-1", "sp-alb", "2020-11-01", "a-id"),
        ("p-2", "sp-alb", "2020-11-01", "a-id"),
        ("p-3", "sp-alb", "2020-11-01", "a-id"),
        ("p-4", "sp-alb", "2020-11-01", "a-id"),
        ("n-1", "summer", "2026-11-01", "a-id"),  # this year
        ("o-1", "summer", "2024-11-02", "a-id"),  # another day
    ]
    for item_id, album_id, taken, who in rows:
        await repo.create_item(
            replace(
                _item(item_id, album_id=album_id),
                taken_at=taken,
                uploaded_by=who,
                created_at=_at(1),
            )
        )


async def test_on_this_day_uses_everyone_albums_of_earlier_years_only(env):
    db, repo = env
    await _memories(db, repo)
    found = await repo.find_on_this_day(
        ("11-01",), before_year="2026", viewer_user_id="a-id"
    )
    # Never the private album (3 photos) or the space album (4): the
    # household album with the most photos taken on the day — autumn 2023.
    assert found == ("autumn", "2023", 2)
    items = await repo.list_on_this_day_items(
        "autumn", year="2023", month_days=("11-01",), viewer_user_id="a-id"
    )
    assert [i.id for i in items] == ["a-1", "a-2"]


async def test_on_this_day_skips_left_out_albums_blocks_and_the_trash(env):
    db, repo = env
    await _memories(db, repo)
    left_out = await repo.find_on_this_day(
        ("11-01",),
        before_year="2026",
        viewer_user_id="a-id",
        excluded_album_ids=frozenset({"autumn"}),
    )
    assert left_out == ("summer", "2024", 1)
    # Blocking Bob hides his photo: autumn 2023 and 2022 tie with summer
    # 2024 at one photo — the most recent year wins.
    await db.enqueue(
        "INSERT INTO user_blocks(blocker_user_id, blocked_user_id)"
        " VALUES('a-id', 'b-id')"
    )
    blocked = await repo.find_on_this_day(
        ("11-01",), before_year="2026", viewer_user_id="a-id"
    )
    assert blocked == ("summer", "2024", 1)
    await repo.trash_item("s-1", deleted_by="a-id")
    await repo.trash_item("a-1", deleted_by="a-id")
    assert await repo.find_on_this_day(
        ("11-01",), before_year="2026", viewer_user_id="a-id"
    ) == ("autumn", "2022", 1)
    assert (
        await repo.find_on_this_day(
            ("12-24",), before_year="2026", viewer_user_id="a-id"
        )
        is None
    )
    assert (
        await repo.find_on_this_day((), before_year="2026", viewer_user_id="a-id")
        is None
    )
    assert (
        await repo.list_on_this_day_items(
            "autumn", year="2022", month_days=(), viewer_user_id="a-id"
        )
        == []
    )


async def test_on_this_day_counts_the_upload_day_when_nothing_was_taken(env):
    db, repo = env
    await _household(db, repo, "feed")
    await repo.create_item(
        replace(_item("f-1", album_id="feed"), created_at="2025-03-04T09:00:00+00:00")
    )
    assert await repo.find_on_this_day(
        ("03-04",), before_year="2026", viewer_user_id="a-id"
    ) == ("feed", "2025", 1)


async def test_newest_by_others_skips_the_viewer_the_trash_and_the_blocked(env):
    """D9's "neu" dot: per album, the newest live item someone else added
    that the viewer may see."""
    db, repo = env
    await db.enqueue(
        "INSERT INTO users(username, user_id, display_name) VALUES('bob', 'b-id', 'B')"
    )
    for album_id in ("alb-1", "alb-2", "alb-3"):
        await repo.create_album(_album(album_id, space_id=None))
    rows = (
        ("it-1", "alb-1", "b-id", 1),
        ("it-2", "alb-1", "rex", 3),
        ("it-3", "alb-1", "a-id", 9),  # the viewer's own
        ("it-4", "alb-2", "a-id", 4),
        ("it-5", "alb-3", "b-id", 5),
        ("it-6", "alb-3", "b-id", 7),  # trashed below
    )
    for item_id, album_id, who, day in rows:
        await repo.create_item(
            replace(
                _item(item_id, album_id=album_id), uploaded_by=who, created_at=_at(day)
            )
        )
    assert await repo.trash_item("it-6", deleted_by="b-id")
    got = await repo.newest_by_others(["alb-1", "alb-2", "alb-3", "alb-1"], "a-id")
    assert got == {"alb-1": _at(3), "alb-3": _at(5)}
    await db.enqueue(
        "INSERT INTO user_blocks(blocker_user_id, blocked_user_id) VALUES('a-id', 'rex')"
    )
    got = await repo.newest_by_others(["alb-1"], "a-id")
    assert got == {"alb-1": _at(1)}
    assert await repo.newest_by_others([], "a-id") == {}
