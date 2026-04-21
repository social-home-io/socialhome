"""Tests for SqliteGalleryRepo."""

from __future__ import annotations

import pytest

from social_home.crypto import (
    derive_instance_id,
    generate_identity_keypair,
)
from social_home.db.database import AsyncDatabase
from social_home.domain.gallery import GalleryAlbum, GalleryItem
from social_home.repositories.gallery_repo import SqliteGalleryRepo


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
    assert got.url.startswith("/api/media/")


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
    assert url.startswith("/api/media/")


async def test_get_first_item_thumbnail_empty_album(env):
    _, repo = env
    await repo.create_album(_album())
    assert await repo.get_first_item_thumbnail("alb-1") is None


# ─── Domain helpers ──────────────────────────────────────────────────────


def test_to_thumbnail_dict_excludes_full_url():
    """S-9: thumbnail-only projection must NOT carry the full ``url``."""
    item = _item()
    d = item.to_thumbnail_dict()
    assert "thumbnail_url" in d
    assert "url" not in d
