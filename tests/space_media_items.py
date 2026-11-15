"""Synthetic space items with a unique file in EVERY field the space
media-field registry (:mod:`socialhome.domain.space_media_fields`) names —
the tripwire fixtures each consumer's test feeds through that consumer."""

from __future__ import annotations

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
