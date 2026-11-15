"""THE registry of which fields of a space item are files.

Every consumer that asks "which files does this space item show?" reads
:data:`SPACE_MEDIA_ITEMS` — never a list of its own:

* the sender (``media_refs`` descriptors, the v_85 ``files`` hash map, the
  ``SPACE_MEDIA_BLOB`` ship lists of each send hook);
* every receiver (``record_media_urls`` / ``record_file_urls``, the
  ``SPACE_MEDIA_BLOB`` scope check);
* space moderation (what a queued item proposes, its renames, the inbound
  sanitizer);
* the media reference repo (the orphan sweep's reference set, who may name
  a file, what is live in a space, a space item's files) — its space rows
  are generated from :func:`space_media_columns`.

A new file field of a space item is added HERE, once; the tripwire tests
(``tests/domain/test_space_media_fields.py``) fail when a domain field that
looks like a file is neither registered nor named as not-a-file.

One pattern reads a markdown body: :data:`LOCAL_MEDIA_REF_RE` (also the
read-time signer's, ``media_signer``).

Pure: no I/O.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from enum import StrEnum
from types import MappingProxyType
from typing import Any

from .post import BAZAAR_MAX_IMAGES, FEED_POST_MAX_IMAGES

#: THE local media reference in a markdown body: ``api/media/<file>`` — an
#: optional leading ``/`` and a ``?query`` belong to the match — never the
#: same path inside another URL or path (``https://host/api/media/x``,
#: ``./api/media/x``, ``x/api/media/x``, ``%2Fapi/media/x``). One pattern for
#: everything that reads a body: the read-time signer
#: (``media_signer.sign_media_urls_in_markdown``), the write-time guard
#: (``media_refs_in_markdown``), the media reference repo and a page's
#: renames (v_82) — a link the signer would sign is always one the page
#: maps, never a shared-store name it missed.
LOCAL_MEDIA_REF_RE = re.compile(
    r"(?<![\w.~:/%-])/?api/media/[A-Za-z0-9._-]+(?:\?[^)\s\"'<>]*)?"
)

#: The most files one page version shows (its cover and its distinct body
#: pictures, v_82): every household ships, describes and accepts at most
#: this many, so one descriptor always carries a whole version.
MAX_PAGE_FILES: int = 16


def page_body_media_refs(text: object) -> list[str]:
    """Every local file reference in a markdown body, as written (query
    included), in order — repeats included."""
    if not isinstance(text, str) or not text:
        return []
    return [m.group(0) for m in LOCAL_MEDIA_REF_RE.finditer(text)]


def rewrite_page_body_media(text: str, rename: Callable[[str], str]) -> str:
    """*text* with every :func:`page_body_media_refs` reference replaced by
    ``rename(reference)``; nothing else is touched."""
    return LOCAL_MEDIA_REF_RE.sub(lambda m: rename(m.group(0)), text)


class FieldShape(StrEnum):
    """How a field holds its files."""

    #: A single reference.
    ONE = "one"
    #: A list of references.
    LIST = "list"
    #: References inside a markdown body (:data:`LOCAL_MEDIA_REF_RE`).
    MARKDOWN = "markdown"


@dataclass(slots=True, frozen=True)
class MediaField:
    """One file field of a space item.

    *path* is the key path on a record / payload dict, which is also the
    attribute path on the domain object (``("file_meta", "url")``); at most
    two steps. *column* is the item table's column (``None``: wire-only,
    e.g. a page resolution's ``merged_content``), *json_path* the path
    inside a JSON-object column (``"$.url"``), *limit* the most entries a
    LIST field takes from another household. *parent_requires*: the object
    holding the field means nothing without it (a post's file attachment is
    its file) — the inbound sanitizer drops that object, not just the
    reference, when the reference is refused."""

    path: tuple[str, ...]
    shape: FieldShape
    column: str | None
    json_path: str | None = None
    limit: int | None = None
    parent_requires: bool = False
    #: What a ONE field of a domain object holds when it shows no
    #: file (``""`` for a ``str``-typed field, else ``None``).
    empty: str | None = None

    @property
    def sql_shape(self) -> str:
        """The media reference repo's shape word for :attr:`column`:
        ``one``, ``list``, ``json:<path>`` or ``markdown``."""
        if self.json_path is not None:
            return f"json:{self.json_path}"
        return self.shape.value


@dataclass(slots=True, frozen=True)
class SpaceMediaItem:
    """A space item type (keyed by its §25.6 resource name) and its files.

    *media_refs*: its items carry the GFS ``media_refs`` descriptor (v_80).
    *file_hashes*: its records carry the ``files`` hash map (v_85).
    *moderation_feature*: the space moderation feature whose queue payloads
    are items of this type (``None``: not reviewed). *moderation_patch*: an
    edit's queue payload carries the changed fields under ``patch`` (read
    instead of the payload when it is a dict). *moderation_mirrors*: keys
    of an edit's payload holding a reviewer's copy of the patch's values
    (a calendar edit's ``proposed``): their file fields always follow the
    patch — never a file of their own. *attached*: items nested in
    a moderation payload — ``(key path, resource)`` (a post's bazaar
    listing at ``attachments.bazaar``). *only_when*: ``(key, value)`` — only
    a record whose *key* is *value* (absent counts as *value*) shows files
    (a gallery album record shows none). *max_files*: the most distinct
    files one record describes (a page version, :data:`MAX_PAGE_FILES`)."""

    resource: str
    table: str
    fields: tuple[MediaField, ...]
    media_refs: bool
    file_hashes: bool
    moderation_feature: str | None = None
    moderation_patch: bool = False
    moderation_mirrors: tuple[str, ...] = ()
    attached: tuple[tuple[tuple[str, ...], str], ...] = ()
    only_when: tuple[str, str] | None = None
    max_files: int | None = None


_ONE = FieldShape.ONE
_LIST = FieldShape.LIST
_MARKDOWN = FieldShape.MARKDOWN

#: Every space item type that shows files, by §25.6 resource name. Field
#: order is the order an item's files are listed in (what a descriptor and
#: a ship list carry).
SPACE_MEDIA_ITEMS: Mapping[str, SpaceMediaItem] = MappingProxyType(
    {
        "posts": SpaceMediaItem(
            resource="posts",
            table="space_posts",
            fields=(
                MediaField(("media_url",), _ONE, "media_url"),
                MediaField(
                    ("image_urls",),
                    _LIST,
                    "image_urls_json",
                    limit=FEED_POST_MAX_IMAGES,
                ),
                MediaField(
                    ("file_meta", "url"),
                    _ONE,
                    "file_meta_json",
                    json_path="$.url",
                    parent_requires=True,
                ),
                MediaField(
                    ("link_preview", "thumbnail_url"),
                    _ONE,
                    "link_preview_json",
                    json_path="$.thumbnail_url",
                ),
            ),
            media_refs=True,
            file_hashes=True,
            moderation_feature="posts",
            attached=((("attachments", "bazaar"), "bazaar"),),
        ),
        "comments": SpaceMediaItem(
            resource="comments",
            table="space_post_comments",
            fields=(MediaField(("media_url",), _ONE, "media_url"),),
            media_refs=False,
            file_hashes=True,
        ),
        "calendar": SpaceMediaItem(
            resource="calendar",
            table="space_calendar_events",
            fields=(MediaField(("cover_url",), _ONE, "cover_url"),),
            media_refs=True,
            file_hashes=True,
            moderation_feature="calendar",
            moderation_patch=True,
            moderation_mirrors=("proposed",),
        ),
        "gallery": SpaceMediaItem(
            resource="gallery",
            table="gallery_items",
            fields=(
                MediaField(("thumbnail_url",), _ONE, "thumbnail_filename", empty=""),
                MediaField(("url",), _ONE, "filename", empty=""),
            ),
            media_refs=True,
            file_hashes=True,
            only_when=("kind", "item"),
        ),
        "bazaar": SpaceMediaItem(
            resource="bazaar",
            table="bazaar_listings",
            fields=(
                MediaField(
                    ("image_urls",), _LIST, "image_urls_json", limit=BAZAAR_MAX_IMAGES
                ),
            ),
            media_refs=False,
            file_hashes=True,
        ),
        # Never ``file_hashes``: a page's files keep their page-scoped
        # ``sp-`` names and only the host fills them (v_82).
        "pages": SpaceMediaItem(
            resource="pages",
            table="space_pages",
            fields=(
                MediaField(("cover_image_url",), _ONE, "cover_image_url"),
                MediaField(("content",), _MARKDOWN, "content"),
                MediaField(("merged_content",), _MARKDOWN, None),
            ),
            media_refs=True,
            file_hashes=False,
            moderation_feature="pages",
            moderation_patch=True,
            max_files=MAX_PAGE_FILES,
        ),
    }
)

#: The resources whose items carry ``media_refs`` (v_80/v_82).
MEDIA_REF_RESOURCES: frozenset[str] = frozenset(
    r for r, item in SPACE_MEDIA_ITEMS.items() if item.media_refs
)

#: The resources whose records carry the ``files`` hash map (v_85).
FILE_HASH_RESOURCES: frozenset[str] = frozenset(
    r for r, item in SPACE_MEDIA_ITEMS.items() if item.file_hashes
)


def moderation_item(feature: str) -> SpaceMediaItem | None:
    """The item type whose moderation queue payloads *feature* holds."""
    for item in SPACE_MEDIA_ITEMS.values():
        if item.moderation_feature == feature:
            return item
    return None


def space_media_columns() -> tuple[tuple[str, str, str], ...]:
    """``(table, column, shape)`` of every stored file field of a space
    item, in registry order — shape in the media reference repo's words
    (:attr:`MediaField.sql_shape`)."""
    return tuple(
        (item.table, f.column, f.sql_shape)
        for item in SPACE_MEDIA_ITEMS.values()
        for f in item.fields
        if f.column is not None
    )


def _get(obj: object, key: str) -> object:
    if isinstance(obj, Mapping):
        return obj.get(key)
    return getattr(obj, key, None)


def _at(obj: object, path: tuple[str, ...]) -> object:
    for key in path:
        if obj is None:
            return None
        obj = _get(obj, key)
    return obj


def _str(value: object) -> list[str]:
    return [value] if isinstance(value, str) and value else []


def _shows_files(item: SpaceMediaItem, obj: object) -> bool:
    if item.only_when is None:
        return True
    key, value = item.only_when
    if isinstance(obj, Mapping):
        return obj.get(key, value) == value
    return getattr(obj, key, value) == value


def item_media_urls(resource: str, obj: object) -> list[str]:
    """The local file references an item of *resource* shows — a record /
    payload dict or the domain object — in field order: each reference of
    a ONE / LIST field as written; a markdown body's pictures without their
    query, each once (and never one already listed)."""
    item = SPACE_MEDIA_ITEMS.get(resource)
    if item is None or not _shows_files(item, obj):
        return []
    urls: list[str] = []
    for f in item.fields:
        value = _at(obj, f.path)
        if f.shape is FieldShape.ONE:
            urls.extend(_str(value))
        elif f.shape is FieldShape.LIST:
            if isinstance(value, (list, tuple)):
                for url in value:
                    urls.extend(_str(url))
        else:
            for ref in page_body_media_refs(value):
                url = ref.split("?", 1)[0]
                if url not in urls:
                    urls.append(url)
    return urls


def replace_item_media[T](
    resource: str, obj: T, rename: Callable[[str], str | None]
) -> T:
    """The domain object *obj* of *resource* with every file reference in
    a registered field passed through *rename* (``dataclasses.replace``,
    nested objects included). *rename* returning ``None`` drops the
    reference: a ONE field takes its :attr:`~MediaField.empty` value, a
    LIST loses the entry, a markdown body empties it — and the object
    holding a :attr:`~MediaField.parent_requires` field is dropped whole
    (as when it shows no file at all). A wire-only field (no attribute on
    the object) is skipped."""
    item = SPACE_MEDIA_ITEMS.get(resource)
    if item is None:
        return obj

    def one(value: object) -> object:
        return rename(value) if isinstance(value, str) and value else value

    changes: dict[str, Any] = {}
    for f in item.fields:
        key = f.path[-1]
        if len(f.path) == 2:
            holder_key = f.path[0]
            holder = changes.get(holder_key, getattr(obj, holder_key, None))
            if holder is None:
                continue
            value = getattr(holder, key, None)
            new = one(value)
            if new is None and f.parent_requires:
                changes[holder_key] = None
            elif new is not value:
                changes[holder_key] = replace(
                    holder, **{key: new if new is not None else f.empty}
                )
            continue
        if not hasattr(obj, key):
            continue
        value = getattr(obj, key)
        if f.shape is FieldShape.ONE:
            new = one(value)
            changes[key] = new if new is not None else f.empty
        elif f.shape is FieldShape.LIST:
            if isinstance(value, (list, tuple)):
                kept = [n for n in (one(v) for v in value) if n]
                changes[key] = tuple(kept) if isinstance(value, tuple) else kept
        elif isinstance(value, str) and value:
            changes[key] = rewrite_page_body_media(value, lambda u: rename(u) or "")
    return replace(obj, **changes) if changes else obj  # type: ignore[type-var]


def rename_item_media(
    resource: str, fields: Mapping[str, Any], rename: Callable[[str], str | None]
) -> dict[str, Any]:
    """*fields* (a payload of *resource*, or an edit's ``patch``) with every
    file reference passed through *rename* — same shape, a new dict; only
    present keys are touched. A markdown reference renamed to nothing is
    emptied."""
    out = dict(fields)
    item = SPACE_MEDIA_ITEMS.get(resource)
    if item is None:
        return out

    def one(value: object) -> object:
        return rename(value) if isinstance(value, str) and value else value

    for f in item.fields:
        parent: dict[str, Any] = out
        if len(f.path) == 2:
            inner = out.get(f.path[0])
            if not isinstance(inner, dict):
                continue
            parent = dict(inner)
        key = f.path[-1]
        if key not in parent:
            continue
        value = parent[key]
        if f.shape is FieldShape.ONE:
            parent[key] = one(value)
        elif f.shape is FieldShape.LIST:
            if isinstance(value, (list, tuple)):
                parent[key] = [one(v) for v in value]
        elif isinstance(value, str) and value:
            parent[key] = rewrite_page_body_media(value, lambda u: rename(u) or "")
        if len(f.path) == 2:
            out[f.path[0]] = parent
    return out


def sanitize_item_media(
    resource: str,
    fields: Mapping[str, Any],
    keep: Callable[[object], str | None],
    *,
    fill_absent: bool,
) -> dict[str, Any]:
    """*fields* (another household's payload of *resource*, or an edit's
    ``patch``) with every file reference *keep* refuses removed — a new
    dict. A refused ONE reference becomes ``None`` (its holder dropped when
    the field is :attr:`~MediaField.parent_requires`), a LIST keeps the
    references *keep* takes, up to its :attr:`~MediaField.limit`, a markdown
    body empties each refused picture. *fill_absent* (a create-only payload):
    an absent ONE / LIST field is written empty too."""
    out = dict(fields)
    item = SPACE_MEDIA_ITEMS.get(resource)
    if item is None:
        return out

    def body(text: object) -> object:
        if not isinstance(text, str) or not text:
            return text
        return rewrite_page_body_media(text, lambda ref: keep(ref) or "")

    for f in item.fields:
        key = f.path[-1]
        if len(f.path) == 2:
            holder_key = f.path[0]
            holder = out.get(holder_key)
            if f.parent_requires:
                if fill_absent or holder_key in out:
                    out[holder_key] = (
                        dict(holder)
                        if isinstance(holder, dict) and keep(holder.get(key))
                        else None
                    )
            elif isinstance(holder, dict) and holder.get(key) and not keep(holder[key]):
                out[holder_key] = {**holder, key: None}
            continue
        if f.shape is FieldShape.MARKDOWN:
            if key in out:
                out[key] = body(out[key])
            continue
        if not fill_absent and key not in out:
            continue
        value = out.get(key)
        if f.shape is FieldShape.ONE:
            out[key] = keep(value)
        else:
            kept = [v for v in value if keep(v)] if isinstance(value, list) else []
            out[key] = kept[: f.limit] if f.limit is not None else kept
    return out


def _attached_payloads(
    item: SpaceMediaItem, payload: Mapping[str, Any]
) -> list[tuple[tuple[str, ...], str, dict[str, Any]]]:
    out: list[tuple[tuple[str, ...], str, dict[str, Any]]] = []
    for path, resource in item.attached:
        node: object = payload
        for key in path:
            node = node.get(key) if isinstance(node, dict) else None
        if isinstance(node, dict):
            out.append((path, resource, node))
    return out


def _with_at(
    payload: dict[str, Any], path: tuple[str, ...], value: dict[str, Any]
) -> dict[str, Any]:
    """*payload* with the dict at *path* replaced by *value* (each dict on
    the way copied)."""
    if len(path) == 1:
        return {**payload, path[0]: value}
    inner = payload.get(path[0])
    assert isinstance(inner, dict)  # _attached_payloads walked it
    return {**payload, path[0]: _with_at(inner, path[1:], value)}


def _mirrored(item: SpaceMediaItem, payload: dict[str, Any]) -> dict[str, Any]:
    """*payload* with each :attr:`~SpaceMediaItem.moderation_mirrors` dict's
    top-level file fields set to the (already sanitized / renamed) patch's
    — dropped where the patch does not name the field."""
    patch = payload.get("patch")
    if not isinstance(patch, dict):
        return payload
    keys = [f.path[0] for f in item.fields if len(f.path) == 1]
    for mirror_key in item.moderation_mirrors:
        mirror = payload.get(mirror_key)
        if not isinstance(mirror, dict):
            continue
        mirror = {k: v for k, v in mirror.items() if k not in keys}
        mirror.update({k: patch[k] for k in keys if k in patch})
        payload = {**payload, mirror_key: mirror}
    return payload


def moderation_media_fields(
    feature: str, payload: Mapping[str, Any]
) -> Mapping[str, Any]:
    """The fields of a moderation payload of *feature* that name its
    files: an edit's ``patch`` (when the item type has one and it is a
    dict), else the payload."""
    item = moderation_item(feature)
    patch = payload.get("patch")
    if item is not None and item.moderation_patch and isinstance(patch, dict):
        return patch
    return payload


def moderation_payload_urls(feature: str, payload: Mapping[str, Any]) -> list[str]:
    """Every local file a moderation queue payload of *feature* (or the
    matching wire event / held row) references: its item's files, then
    each attached item's."""
    item = moderation_item(feature)
    if item is None:
        return []
    urls = item_media_urls(
        item.resource, dict(moderation_media_fields(feature, payload))
    )
    for _path, resource, node in _attached_payloads(item, dict(payload)):
        urls.extend(item_media_urls(resource, node))
    return urls


def rename_moderation_payload(
    feature: str, payload: Mapping[str, Any], rename: Callable[[str], str | None]
) -> dict[str, Any]:
    """*payload* with every file :func:`moderation_payload_urls` lists
    passed through *rename* (same shape, a new dict)."""
    out = dict(payload)
    item = moderation_item(feature)
    if item is None:
        return out
    patch = out.get("patch")
    if item.moderation_patch and isinstance(patch, dict):
        out["patch"] = rename_item_media(item.resource, patch, rename)
        out = _mirrored(item, out)
    else:
        out = rename_item_media(item.resource, out, rename)
    for path, resource, node in _attached_payloads(item, out):
        out = _with_at(out, path, rename_item_media(resource, node, rename))
    return out


def sanitize_moderation_payload(
    feature: str, payload: Mapping[str, Any], keep: Callable[[object], str | None]
) -> dict[str, Any]:
    """*payload* (a remote submission of *feature*) with every file
    reference *keep* refuses removed — the payload's own fields, an edit's
    ``patch`` and each attached item (:func:`sanitize_item_media`). A
    payload type without ``patch`` is a create: its absent fields are
    written empty."""
    out = dict(payload)
    item = moderation_item(feature)
    if item is None:
        return out
    out = sanitize_item_media(
        item.resource, out, keep, fill_absent=not item.moderation_patch
    )
    patch = out.get("patch")
    if item.moderation_patch and isinstance(patch, dict):
        out["patch"] = sanitize_item_media(
            item.resource, patch, keep, fill_absent=False
        )
        out = _mirrored(item, out)
    for path, resource, node in _attached_payloads(item, out):
        out = _with_at(
            out, path, sanitize_item_media(resource, node, keep, fill_absent=True)
        )
    return out


__all__ = [
    "FILE_HASH_RESOURCES",
    "LOCAL_MEDIA_REF_RE",
    "MAX_PAGE_FILES",
    "MEDIA_REF_RESOURCES",
    "SPACE_MEDIA_ITEMS",
    "FieldShape",
    "MediaField",
    "SpaceMediaItem",
    "item_media_urls",
    "moderation_item",
    "moderation_media_fields",
    "moderation_payload_urls",
    "page_body_media_refs",
    "rename_item_media",
    "rename_moderation_payload",
    "replace_item_media",
    "rewrite_page_body_media",
    "sanitize_item_media",
    "sanitize_moderation_payload",
    "space_media_columns",
]
