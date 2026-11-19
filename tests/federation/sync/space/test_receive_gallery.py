"""``receive_gallery``: gallery albums and items by §25.6 sync."""

from __future__ import annotations

import logging
from unittest.mock import AsyncMock

from socialhome.federation.sync.space.stream_health import HeldBack
from tests.federation.sync.space.receiver_fakes import _here, _send


async def test_persist_album_failure_is_logged_not_swallowed(receiver, caplog):
    """A failing album insert must surface, not vanish.

    ``create_album`` is already ``ON CONFLICT DO NOTHING``, so a
    redelivered album never reaches the exception path — which means the
    old blanket ``except Exception: pass`` (commented "already exists")
    could only ever hide real failures. It hid #650 for as long as gallery
    sync has existed: a remotely-owned album violated
    ``owner_user_id REFERENCES users``, so members got image bytes and no
    rows.
    """
    r, _space_repo, _ = receiver
    r._gallery_repo.create_album = AsyncMock(side_effect=RuntimeError("boom"))

    held_back = HeldBack()
    with caplog.at_level(logging.WARNING, logger="socialhome"):
        await r._persist_album(
            {"id": "al-1", "space_id": "sp-1", "name": "Holiday", "is_system": 0},
            "sp-1",
            from_host=True,
            held_back=held_back,
        )

    assert "gallery album al-1" in caplog.text
    # Nor does it count as stored: a failure, never a forgivable hold.
    assert held_back.failed and held_back.count == 0


async def test_persist_gallery_item_failure_is_logged_not_swallowed(receiver, caplog):
    """Same for items — a missing parent album must not be silent."""
    r, _space_repo, _ = receiver
    r._gallery_repo.create_item_in_space = AsyncMock(side_effect=RuntimeError("boom"))
    held_back = HeldBack()

    with caplog.at_level(logging.WARNING, logger="socialhome"):
        await r._persist_gallery_item(
            {
                "id": "it-1",
                "album_id": "al-missing",
                "uploaded_by": "u-remote",
                "item_type": "photo",
                "url": "/api/media/a.webp",
                "thumbnail_url": "/api/media/t.webp",
                "width": 10,
                "height": 10,
            },
            "sp-1",
            held_back,
        )

    assert "gallery item it-1" in caplog.text
    assert held_back.failed and held_back.count == 0


async def test_gallery_album_then_item(setup):
    r, c, kp = setup
    await _send(
        r,
        kp,
        "gallery",
        [
            {
                "kind": "album",
                "id": "a-1",
                "space_id": "sp-1",
                "owner_user_id": "u-1",
                "name": "Trip",
            },
            {
                "kind": "item",
                "id": "i-1",
                "album_id": "a-1",
                "uploaded_by": "u-1",
                "item_type": "photo",
                "url": "/m/x.jpg",
                "thumbnail_url": "/m/x-thumb.jpg",
                "width": 1024,
                "height": 768,
            },
        ],
    )
    assert len(c.gallery_albums) == 1 and c.gallery_albums[0].id == "a-1"
    assert len(c.gallery_items) == 1 and c.gallery_items[0].id == "i-1"
    assert c.item_edits == []  # a new item: nothing to edit


def _item_record(**over) -> dict:
    record = {
        "kind": "item",
        "id": "i-1",
        "album_id": "a-1",
        "uploaded_by": "u-1",
        "item_type": "photo",
        "url": "/m/x.jpg",
        "thumbnail_url": "/m/x-thumb.jpg",
    }
    return {**record, **over}


async def test_a_held_gallery_item_takes_the_hosts_caption_day_and_album(setup):
    """v_79 (sync line 79): an item held here takes the host's caption, day
    taken and album — the edits a household below v_79 was never sent live
    reach it with one full stream once it upgrades."""
    r, c, kp = setup
    albums = [
        {
            "kind": "album",
            "id": a,
            "space_id": "sp-1",
            "owner_user_id": "u-1",
            "name": a,
        }
        for a in ("a-1", "a-2")
    ]
    await _send(r, kp, "gallery", [*albums, _item_record()])
    assert c.item_edits == []

    await _send(
        r,
        kp,
        "gallery",
        [_item_record(album_id="a-2", caption="  Lake  ", taken_at="2026-07-12")],
    )
    assert c.item_edits == [
        ("i-1", {"caption": "Lake", "taken_at": "2026-07-12"}, "a-2"),
    ]


async def test_a_held_gallery_item_skips_a_value_the_local_rules_refuse(setup):
    r, c, kp = setup
    album = {"kind": "album", "id": "a-1", "space_id": "sp-1", "owner_user_id": "u-1"}
    await _send(r, kp, "gallery", [{**album, "name": "A"}, _item_record()])
    await _send(
        r, kp, "gallery", [_item_record(caption="x" * 301, taken_at="12.07.2026")]
    )
    # Neither value is stored; the item stays in its album.
    assert c.item_edits == []


async def test_gallery_album_lands_in_the_synced_space_not_the_records(setup):
    """A record's own ``space_id`` (another space, or none = household
    gallery) is untrusted: the album is filed under the stream's space."""
    r, c, kp = setup
    await _send(
        r,
        kp,
        "gallery",
        [
            {"kind": "album", "id": "a-x", "space_id": "sp-other", "name": "X"},
            {"kind": "album", "id": "a-y", "space_id": None, "name": "Y"},
        ],
    )
    assert [a.space_id for a in c.gallery_albums] == ["sp-1", "sp-1"]


async def test_a_synced_album_deleted_here_is_not_brought_back(setup):
    """A sync from a household that missed the delete re-sends the album and
    its items; neither comes back."""
    r, c, kp = setup
    c.deleted_albums.add(("sp-1", "a-gone"))
    await _send(
        r,
        kp,
        "gallery",
        [
            {"kind": "album", "id": "a-gone", "owner_user_id": "u-1", "name": "G"},
            {
                "kind": "item",
                "id": "i-gone",
                "album_id": "a-gone",
                "uploaded_by": "u-1",
                "thumbnail_url": "api/media/g.webp",
            },
            {"kind": "album", "id": "a-kept", "owner_user_id": "u-1", "name": "K"},
        ],
    )
    assert [a.id for a in c.gallery_albums] == ["a-kept"]
    assert c.gallery_items == []


async def test_synced_item_media_references_are_normalised(setup):
    r, c, kp = setup
    await _send(
        r,
        kp,
        "gallery",
        [
            {
                "kind": "item",
                "id": "i-n",
                "album_id": "a-1",
                "uploaded_by": "u-1",
                "url": "https://elsewhere.example/x.webp",
                "thumbnail_url": "/api/media/t.webp?sig=1",
            },
        ],
    )
    item = c.gallery_items[0]
    assert (item.url, item.thumbnail_url) == ("", _here("t.webp"))
