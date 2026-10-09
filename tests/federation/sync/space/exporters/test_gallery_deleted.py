"""Tests for socialhome.federation.sync.space.exporters.gallery_deleted."""

from __future__ import annotations

import pytest

from socialhome.domain.gallery import GalleryAlbum, GalleryItem
from socialhome.federation.sync.space.exporter import (
    REMOVAL_RESOURCES,
    RESOURCE_ORDER,
)
from socialhome.federation.sync.space.exporters import (
    GalleryAlbumsDeletedExporter,
    GalleryItemsDeletedExporter,
)
from socialhome.federation.sync.space.window import SYNC_PAGE_SIZE
from socialhome.repositories.gallery_repo import SqliteGalleryRepo


@pytest.fixture
async def repo(db):
    await db.enqueue(
        "INSERT INTO spaces(id, name, owner_instance_id, owner_username,"
        " identity_public_key) VALUES('sp','S','h','anna','ab')"
    )
    r = SqliteGalleryRepo(db)
    for aid in ("al-1", "al-2"):
        await r.create_album(
            GalleryAlbum(
                id=aid, space_id="sp", owner_user_id="u-o", name="Private trip"
            )
        )
    for iid, aid in (("it-1", "al-1"), ("it-2", "al-1"), ("it-3", "al-2")):
        await r.create_item(
            GalleryItem(
                id=iid,
                album_id=aid,
                uploaded_by="u-up",
                item_type="photo",
                url=f"api/media/{iid}.webp",
                thumbnail_url=f"api/media/{iid}-t.webp",
                width=1,
                height=1,
                caption="beach day",
            )
        )
    return r


async def test_albums_and_single_items_stream_without_content(repo):
    await repo.delete_item_in_space("it-1", space_id="sp", deleted_by="u-mod")
    await repo.delete_album_in_space("al-2", space_id="sp", deleted_by="u-o")
    albums = await GalleryAlbumsDeletedExporter(repo).list_records("sp")
    items = await GalleryItemsDeletedExporter(repo).list_records("sp")
    assert albums == [
        {
            "id": "al-2",
            "owner_user_id": "u-o",
            "created_at": albums[0]["created_at"],
            "actor_user_id": "u-o",
        }
    ]
    # it-3 went with its album: the album's tombstone covers it.
    assert [r["id"] for r in items] == ["it-1"]
    assert set(items[0]) == {
        "id",
        "uploaded_by",
        "created_at",
        "album_id",
        "actor_user_id",
    }
    assert items[0]["album_id"] == "al-1"
    text = repr(albums) + repr(items)
    assert "Private" not in text and "beach" not in text and ".webp" not in text


async def test_item_tombstones_page_past_one_page(repo):
    for i in range(SYNC_PAGE_SIZE + 2):
        await repo.tombstone_item(
            f"st-{i}", space_id="sp", album_id="al-1", uploaded_by="u-up"
        )
    batches = [b async for b in GalleryItemsDeletedExporter(repo).iter_batches("sp")]
    assert [len(b) for b in batches] == [SYNC_PAGE_SIZE, 2]


def test_album_tombstones_ship_before_the_gallery_and_item_tombstones_after():
    """An item stub needs its album held — a joiner gets it from
    ``gallery`` (the comment tombstones follow the posts the same way)."""
    order = list(RESOURCE_ORDER)
    assert (
        order.index("gallery_albums_deleted")
        < order.index("gallery")
        < order.index("gallery_items_deleted")
    )
    assert {"gallery_albums_deleted", "gallery_items_deleted"} <= REMOVAL_RESOURCES
