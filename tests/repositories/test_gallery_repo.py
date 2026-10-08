"""Tests for SqliteGalleryRepo."""

from __future__ import annotations

import pytest

from socialhome.crypto import (
    derive_instance_id,
    generate_identity_keypair,
)
from socialhome.db.database import AsyncDatabase
from socialhome.domain.gallery import GalleryAlbum, GalleryItem
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


async def test_list_items_orders_by_sort_then_created(env):
    _, repo = env
    await repo.create_album(_album())
    for i in range(5):
        await repo.create_item(_item(f"it-{i}"))
    out = await repo.list_items("alb-1")
    assert [i.id for i in out] == [f"it-{i}" for i in range(5)]


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
