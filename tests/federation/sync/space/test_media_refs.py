"""§25.6 records and the ``media_refs`` descriptor (v_80)
(:mod:`socialhome.federation.sync.space.media_refs`)."""

from __future__ import annotations

from socialhome.federation.sync.space.media_refs import (
    MEDIA_REF_RESOURCES,
    record_media_urls,
)


def test_resources() -> None:
    assert MEDIA_REF_RESOURCES == frozenset({"posts", "calendar", "gallery"})


def test_post_record_files() -> None:
    record = {
        "media_url": "api/media/m.webp",
        "image_urls": ["api/media/a.webp", "api/media/b.webp", 3],
        "file_meta": {"url": "api/media/doc.pdf"},
        "link_preview": {"thumbnail_url": "api/media/card.webp"},
    }
    assert record_media_urls("posts", record) == [
        "api/media/m.webp",
        "api/media/a.webp",
        "api/media/b.webp",
        "api/media/doc.pdf",
        "api/media/card.webp",
    ]
    assert record_media_urls("posts", {"image_urls": "nope", "file_meta": 1}) == []


def test_calendar_and_gallery_records() -> None:
    assert record_media_urls("calendar", {"cover_url": "api/media/c.webp"}) == [
        "api/media/c.webp"
    ]
    assert record_media_urls("calendar", {}) == []
    item = {
        "kind": "item",
        "thumbnail_url": "/api/media/t.jpg",
        "url": "/api/media/f.jpg",
    }
    assert record_media_urls("gallery", item) == [
        "/api/media/t.jpg",
        "/api/media/f.jpg",
    ]
    assert record_media_urls("gallery", {"kind": "album", "cover_url": "x"}) == []
    assert record_media_urls("tasks", {"media_url": "api/media/x.webp"}) == []
