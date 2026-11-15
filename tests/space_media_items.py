"""Synthetic space items with a unique file in EVERY field the space
media-field registry (:mod:`socialhome.domain.space_media_fields`) names —
the tripwire fixtures each consumer's test feeds through that consumer."""

from __future__ import annotations

from datetime import UTC, datetime

from socialhome.domain.calendar import CalendarEvent
from socialhome.domain.gallery import GalleryItem
from socialhome.domain.link_preview import LinkPreview
from socialhome.domain.page import Page
from socialhome.domain.post import (
    BazaarListing,
    BazaarMode,
    BazaarStatus,
    Comment,
    CommentType,
    FileMeta,
    Post,
    PostType,
)
from socialhome.domain.space_media_fields import (
    SPACE_MEDIA_ITEMS,
    FieldShape,
    MediaField,
    moderation_item,
)


def _name(resource: str, f: MediaField) -> str:
    return f"api/media/{resource}-{'-'.join(f.path)}.webp".replace("_", "")


def synthetic_record(resource: str) -> dict:
    """A record / payload of *resource* with a unique file in each of its
    registered fields (a markdown field shows its file in a body)."""
    out: dict = {}
    for f in SPACE_MEDIA_ITEMS[resource].fields:
        name = _name(resource, f)
        value: object = (
            [name]
            if f.shape is FieldShape.LIST
            else f"intro ![x]({name}?exp=1) end"
            if f.shape is FieldShape.MARKDOWN
            else name
        )
        if len(f.path) == 2:
            out.setdefault(f.path[0], {})[f.path[1]] = value
        else:
            out[f.path[0]] = value
    return out


def expected_urls(resource: str, *, stored_only: bool = False) -> list[str]:
    """Every registered field's file, in registry order."""
    return [
        _name(resource, f)
        for f in SPACE_MEDIA_ITEMS[resource].fields
        if not stored_only or f.column is not None
    ]


def synthetic_moderation_payload(feature: str, *, patch: bool = False) -> dict:
    """A queue payload of *feature* with a unique file in each field — and
    in each attached item's."""
    item = moderation_item(feature)
    assert item is not None
    payload = synthetic_record(item.resource)
    if patch:
        payload = {"target_id": "t", "patch": payload}
    for path, resource in item.attached:
        node = payload
        for key in path[:-1]:
            node = node.setdefault(key, {})
        node[path[-1]] = synthetic_record(resource)
    return payload


def expected_moderation_urls(feature: str) -> list[str]:
    item = moderation_item(feature)
    assert item is not None
    urls = expected_urls(item.resource)
    for _path, resource in item.attached:
        urls += expected_urls(resource)
    return urls


def synthetic_object(resource: str, *, item_id: str = "item-1", space_id: str = "sp"):
    """The domain object of *resource* with :func:`synthetic_record`'s file
    in each stored field (a wire-only field — a page's ``merged_content`` —
    has no attribute)."""
    r = synthetic_record(resource)
    when = datetime(2026, 1, 1, tzinfo=UTC)
    match resource:
        case "posts":
            return Post(
                id=item_id,
                author="u",
                type=PostType.FILE,
                created_at=when,
                media_url=r["media_url"],
                image_urls=tuple(r["image_urls"]),
                file_meta=FileMeta(
                    url=r["file_meta"]["url"],
                    mime_type="application/pdf",
                    original_name="f.pdf",
                    size_bytes=1,
                ),
                link_preview=LinkPreview(
                    url="https://example.org/",
                    thumbnail_url=r["link_preview"]["thumbnail_url"],
                ),
            )
        case "comments":
            return Comment(
                id=item_id,
                post_id="post-1",
                author="u",
                type=CommentType.IMAGE,
                created_at=when,
                media_url=r["media_url"],
            )
        case "calendar":
            return CalendarEvent(
                id=item_id,
                calendar_id=space_id,
                summary="S",
                start=when,
                end=when,
                created_by="u",
                cover_url=r["cover_url"],
            )
        case "gallery":
            return GalleryItem(
                id=item_id,
                album_id="album-1",
                uploaded_by="u",
                item_type="photo",
                url=r["url"],
                thumbnail_url=r["thumbnail_url"],
                width=1,
                height=1,
            )
        case "bazaar":
            return BazaarListing(
                post_id=item_id,
                space_id=space_id,
                seller_user_id="u",
                mode=BazaarMode.FIXED,
                title="T",
                end_time="2099-01-01",
                currency="EUR",
                status=BazaarStatus.ACTIVE,
                created_at="2026-01-01",
                image_urls=tuple(r["image_urls"]),
            )
        case "pages":
            return Page(
                id=item_id,
                title="P",
                content=r["content"],
                created_by="u",
                created_at="2026-01-01",
                updated_at="2026-01-01",
                space_id=space_id,
                cover_image_url=r["cover_image_url"],
            )
    raise KeyError(resource)
