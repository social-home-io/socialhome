"""The space media-field registry (:mod:`socialhome.domain.space_media_fields`)
and its tripwires: every registered field is seen by every domain consumer,
and every domain field that looks like a file is registered or named here
as not one."""

from __future__ import annotations

import dataclasses
import pathlib
import re
import typing
from datetime import UTC, datetime

import pytest

from socialhome import media_signer
from socialhome.domain import space_media_ref
from socialhome.domain.bazaar_faq import BazaarFaqEntry
from socialhome.domain.calendar import CalendarEvent, CalendarRSVP
from socialhome.domain.conversation import ConversationMessage
from socialhome.domain.gallery import GalleryAlbum, GalleryItem
from socialhome.domain.link_preview import LinkPreview
from socialhome.domain.page import Page
from socialhome.domain.page_version import PageConflictSide
from socialhome.domain.post import (
    BAZAAR_MAX_IMAGES,
    FEED_POST_MAX_IMAGES,
    BazaarBid,
    BazaarListing,
    Comment,
    FileMeta,
    Poll,
    PollOption,
    PollVote,
    Post,
    SchedulePoll,
    ScheduleResponse,
    ScheduleSlot,
)
from socialhome.domain.space import SpaceZone
from socialhome.domain.space_media_fields import (
    FILE_HASH_RESOURCES,
    LOCAL_MEDIA_REF_RE,
    MEDIA_REF_RESOURCES,
    SPACE_MEDIA_ITEMS,
    FieldShape,
    SpaceMediaItem,
    item_media_urls,
    moderation_item,
    moderation_payload_urls,
    rename_item_media,
    rename_moderation_payload,
    replace_item_media,
    sanitize_item_media,
    sanitize_moderation_payload,
    space_media_columns,
)
from socialhome.domain.space_media_ref import (
    record_file_urls,
    record_media_urls,
)
from socialhome.domain.sticky import Sticky
from socialhome.domain.task import Task, TaskList
from socialhome.domain.timetable import (
    Timetable,
    TimetableEntry,
    TimetableOverride,
    TimetableValidity,
)
from socialhome.federation.sync.space import exporter
from socialhome.repositories import media_reference_repo
from tests.space_media_items import (
    expected_moderation_urls,
    expected_urls,
    synthetic_moderation_payload,
    synthetic_object,
    synthetic_record,
)

# ─── The registry ────────────────────────────────────────────────────────


def test_the_registry_names_every_space_item_with_files() -> None:
    assert set(SPACE_MEDIA_ITEMS) == {
        "posts",
        "comments",
        "calendar",
        "gallery",
        "bazaar",
        "pages",
    }
    for resource, item in SPACE_MEDIA_ITEMS.items():
        assert item.resource == resource
        assert item.fields
        for f in item.fields:
            assert 1 <= len(f.path) <= 2
            assert f.limit is None or f.shape is FieldShape.LIST
            assert f.json_path is None or len(f.path) == 2


def test_the_registry_is_frozen() -> None:
    item = SPACE_MEDIA_ITEMS["posts"]
    with pytest.raises(dataclasses.FrozenInstanceError):
        item.table = "x"  # type: ignore[misc]
    with pytest.raises(TypeError):
        SPACE_MEDIA_ITEMS["x"] = item  # type: ignore[index]
    assert isinstance(item, SpaceMediaItem)


def test_resource_sets_derive_from_the_registry() -> None:
    assert MEDIA_REF_RESOURCES == {"posts", "calendar", "gallery", "pages"}
    assert FILE_HASH_RESOURCES == {"posts", "calendar", "gallery", "comments", "bazaar"}
    # The descriptor module re-exports the same sets.
    assert space_media_ref.MEDIA_REF_RESOURCES is MEDIA_REF_RESOURCES
    assert space_media_ref.FILE_HASH_RESOURCES is FILE_HASH_RESOURCES


def test_list_limits_are_the_composer_caps() -> None:
    posts = {f.path: f for f in SPACE_MEDIA_ITEMS["posts"].fields}
    assert posts[("image_urls",)].limit == FEED_POST_MAX_IMAGES
    bazaar = SPACE_MEDIA_ITEMS["bazaar"].fields[0]
    assert bazaar.limit == BAZAAR_MAX_IMAGES


def test_space_media_columns_speak_the_repos_shapes() -> None:
    assert space_media_columns() == (
        ("space_posts", "media_url", "one"),
        ("space_posts", "image_urls_json", "list"),
        ("space_posts", "file_meta_json", "json:$.url"),
        ("space_posts", "link_preview_json", "json:$.thumbnail_url"),
        ("space_post_comments", "media_url", "one"),
        ("space_calendar_events", "cover_url", "one"),
        ("gallery_items", "thumbnail_filename", "one"),
        ("gallery_items", "filename", "one"),
        ("bazaar_listings", "image_urls_json", "list"),
        ("space_pages", "cover_image_url", "one"),
        ("space_pages", "content", "markdown"),
    )


def test_moderation_items() -> None:
    assert moderation_item("posts") is SPACE_MEDIA_ITEMS["posts"]
    assert moderation_item("calendar") is SPACE_MEDIA_ITEMS["calendar"]
    assert moderation_item("pages") is SPACE_MEDIA_ITEMS["pages"]
    assert moderation_item("tasks") is None
    assert moderation_item("stickies") is None


# ─── item_media_urls ─────────────────────────────────────────────────────


@pytest.mark.parametrize("resource", sorted(SPACE_MEDIA_ITEMS))
def test_item_media_urls_sees_every_field(resource: str) -> None:
    assert item_media_urls(resource, synthetic_record(resource)) == expected_urls(
        resource
    )


def test_item_media_urls_unknown_resource_is_empty() -> None:
    assert item_media_urls("stickies", {"media_url": "api/media/x"}) == []


def test_item_media_urls_reads_domain_objects() -> None:
    post = Post(
        id="p",
        author="u",
        type="file",  # type: ignore[arg-type]
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
        media_url="api/media/a.webp",
        image_urls=("api/media/b.webp", "", "api/media/c.webp"),
        file_meta=FileMeta(
            url="api/media/d.pdf", mime_type="x", original_name="d", size_bytes=1
        ),
        link_preview=LinkPreview(
            url="https://example.org/", thumbnail_url="api/media/e.webp"
        ),
    )
    assert item_media_urls("posts", post) == [
        "api/media/a.webp",
        "api/media/b.webp",
        "api/media/c.webp",
        "api/media/d.pdf",
        "api/media/e.webp",
    ]
    item = GalleryItem(
        id="g",
        album_id="a",
        uploaded_by="u",
        item_type="photo",  # type: ignore[arg-type]
        url="api/media/full.webp",
        thumbnail_url="api/media/thumb.webp",
        width=1,
        height=1,
    )
    assert item_media_urls("gallery", item) == [
        "api/media/thumb.webp",
        "api/media/full.webp",
    ]


def test_a_gallery_album_record_shows_no_files() -> None:
    record = synthetic_record("gallery")
    assert item_media_urls("gallery", {**record, "kind": "album"}) == []
    assert item_media_urls("gallery", {**record, "kind": "item"}) == expected_urls(
        "gallery"
    )


def test_markdown_refs_lose_their_query_and_repeat_once() -> None:
    body = "![a](api/media/x.webp?e=1) ![b](/api/media/x.webp) ![c](api/media/c.webp)"
    assert item_media_urls(
        "pages", {"cover_image_url": "api/media/c.webp", "content": body}
    ) == ["api/media/c.webp", "api/media/x.webp", "/api/media/x.webp"]


def test_one_and_list_duplicates_are_kept() -> None:
    assert item_media_urls(
        "posts", {"media_url": "api/media/a", "image_urls": ["api/media/a"]}
    ) == ["api/media/a", "api/media/a"]


def test_malformed_values_show_nothing() -> None:
    assert (
        item_media_urls(
            "posts",
            {
                "media_url": 3,
                "image_urls": "api/media/x",
                "file_meta": "api/media/y",
                "link_preview": ["api/media/z"],
            },
        )
        == []
    )


# ─── record_media_urls / record_file_urls (the descriptor module) ────────


@pytest.mark.parametrize("resource", sorted(SPACE_MEDIA_ITEMS))
def test_record_media_and_file_urls_see_every_stored_field(resource: str) -> None:
    item = SPACE_MEDIA_ITEMS[resource]
    record = synthetic_record(resource)
    stored = expected_urls(resource, stored_only=True)
    # A record (§25.6 / wire item) carries only its stored fields.
    record = {k: v for k, v in record.items() if k != "merged_content"}
    assert record_media_urls(resource, record) == (stored if item.media_refs else [])
    assert record_file_urls(resource, record) == (stored if item.file_hashes else [])


# ─── Moderation payloads ─────────────────────────────────────────────────


def _moderated() -> list[str]:
    return sorted(
        item.moderation_feature
        for item in SPACE_MEDIA_ITEMS.values()
        if item.moderation_feature
    )


@pytest.mark.parametrize("feature", _moderated())
def test_moderation_payload_urls_see_every_field(feature: str) -> None:
    payload = synthetic_moderation_payload(feature)
    assert moderation_payload_urls(feature, payload) == expected_moderation_urls(
        feature
    )


@pytest.mark.parametrize(
    "feature", [f for f in _moderated() if moderation_item(f).moderation_patch]
)
def test_an_edits_patch_is_read_instead_of_the_payload(feature: str) -> None:
    payload = synthetic_moderation_payload(feature, patch=True)
    payload["cover_url"] = "api/media/ignored.webp"
    assert moderation_payload_urls(feature, payload) == expected_moderation_urls(
        feature
    )


def test_a_posts_patch_key_is_never_read() -> None:
    """Posts are reviewed as creates only: a ``patch`` key names nothing."""
    payload = {"media_url": "api/media/a", "patch": {"media_url": "api/media/b"}}
    assert moderation_payload_urls("posts", payload) == ["api/media/a"]


@pytest.mark.parametrize("feature", _moderated())
def test_rename_moderation_payload_renames_every_field(feature: str) -> None:
    payload = synthetic_moderation_payload(feature)
    renamed = rename_moderation_payload(
        feature, payload, lambda u: u.replace(".webp", "X.webp")
    )
    assert moderation_payload_urls(feature, renamed) == [
        u.replace(".webp", "X.webp") for u in expected_moderation_urls(feature)
    ]


def test_rename_leaves_absent_keys_absent_and_empties_a_dropped_body_ref() -> None:
    out = rename_item_media(
        "pages", {"content": "a ![x](api/media/x.webp) b"}, lambda u: None
    )
    assert out == {"content": "a ![x]() b"}
    assert rename_item_media("posts", {}, lambda u: "y") == {}


def test_rename_unknown_resource_copies() -> None:
    payload = {"media_url": "api/media/a"}
    out = rename_item_media("tasks", payload, lambda u: "b")
    assert out == payload and out is not payload


def _keep_none(value: object) -> str | None:
    return None


def _keep_all(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


@pytest.mark.parametrize("feature", _moderated())
def test_sanitize_removes_every_field_it_refuses(feature: str) -> None:
    payload = synthetic_moderation_payload(feature)
    assert (
        moderation_payload_urls(
            feature, sanitize_moderation_payload(feature, payload, _keep_none)
        )
        == []
    )
    kept = sanitize_moderation_payload(feature, payload, _keep_all)
    assert moderation_payload_urls(feature, kept) == expected_moderation_urls(feature)


@pytest.mark.parametrize(
    "feature", [f for f in _moderated() if moderation_item(f).moderation_patch]
)
def test_sanitize_cleans_an_edits_patch(feature: str) -> None:
    payload = synthetic_moderation_payload(feature, patch=True)
    clean = sanitize_moderation_payload(feature, payload, _keep_none)
    assert moderation_payload_urls(feature, clean) == []


def test_sanitize_shapes() -> None:
    """A create-only payload's absent fields are written empty; a refused
    attachment drops the file meta, a refused card keeps the card; a list
    keeps at most its limit."""
    many = [f"api/media/{i}" for i in range(9)]
    out = sanitize_item_media(
        "posts",
        {
            "image_urls": many,
            "link_preview": {"url": "https://e/", "thumbnail_url": "bad"},
        },
        lambda v: v if isinstance(v, str) and v.startswith("api/") else None,
        fill_absent=True,
    )
    assert out["media_url"] is None
    assert out["image_urls"] == many[:FEED_POST_MAX_IMAGES]
    assert out["file_meta"] is None
    assert out["link_preview"] == {"url": "https://e/", "thumbnail_url": None}
    # Patch-style: absent stays absent.
    assert sanitize_item_media("calendar", {}, _keep_none, fill_absent=False) == {}
    assert sanitize_item_media("tasks", {"a": 1}, _keep_none, fill_absent=True) == {
        "a": 1
    }


def test_unknown_feature_payloads_pass_through() -> None:
    payload = {"media_url": "api/media/a"}
    assert moderation_payload_urls("tasks", payload) == []
    assert rename_moderation_payload("tasks", payload, lambda u: "x") == payload
    assert sanitize_moderation_payload("tasks", payload, _keep_none) == payload


# ─── Structural tripwire: every file-like field is registered ────────────

#: A field looks like a file when its name is ``content`` / ``url`` or has
#: one of these words.
_FILE_LIKE = re.compile(
    r"^(content|body)$|(^|_)(url|urls|file|files|filename|image|images|photo"
    r"|photos|picture|media|attachment|attachments|avatar|poster|banner|icon"
    r"|video|audio|voice|gif|clip|asset|assets|thumbnail|thumb|cover)(_|$)"
)

#: The domain row types every §25.6 content resource streams — keyed by
#: the exporter's resource names (roster and removal resources carry no
#: files). A new resource fails :func:`test_synced_types_cover_every_resource`
#: until it is listed here, so its fields are scanned.
SYNCED_TYPES: dict[str, tuple[type, ...]] = {
    "posts": (Post,),
    "comments": (Comment,),
    "task_lists": (TaskList,),
    "tasks": (Task,),
    "tasks_archived": (Task,),
    "pages": (Page, PageConflictSide),
    "stickies": (Sticky,),
    "calendar": (CalendarEvent, CalendarRSVP),
    "gallery": (GalleryItem, GalleryAlbum),
    "polls": (Poll, PollOption, PollVote),
    "schedules": (SchedulePoll, ScheduleSlot, ScheduleResponse),
    "space_zones": (SpaceZone,),
    "bazaar": (BazaarListing, BazaarBid, BazaarFaqEntry),
    "timetables": (Timetable, TimetableEntry, TimetableOverride, TimetableValidity),
    "chat_messages": (ConversationMessage,),
}

#: The registry item a synced row type IS (its fields are registry paths).
_ITEM_CLASS: dict[type, str] = {
    Post: "posts",
    Comment: "comments",
    CalendarEvent: "calendar",
    GalleryItem: "gallery",
    BazaarListing: "bazaar",
    Page: "pages",
}

#: Fields that look like a file but are not a space item's file — each
#: with its reason (``Class.field``).
NOT_A_SPACE_FILE: dict[str, str] = {
    "Post.content": "plain text; only a page body is signed as markdown",
    "Comment.content": "plain text; only a page body is signed as markdown",
    "LinkPreview.url": "the linked web page, never a local file",
    "Sticky.content": "plain text; stickies carry no files",
    "PageConflictSide.content": (
        "an open conflict side's body: host-only page names (v_82), read by "
        "the page paths (space_page_snapshots), never a registry item"
    ),
    "PageConflictSide.cover_image_url": (
        "an open conflict side's cover: host-only page names (v_82), read "
        "by the page paths (space_page_snapshots), never a registry item"
    ),
    "ConversationMessage.content": "chat text",
    "ConversationMessage.media_url": (
        "chat media (DMs, group and space chats) rides the conversation "
        "media path (DM_MEDIA_BLOB), not a space item's; the reference repo "
        "lists conversation_messages.media_url by hand"
    ),
    "ConversationMessage.media_blob_id": (
        "the DM_MEDIA_BLOB transfer id, not a file reference"
    ),
    "ConversationMessage.media_sync_status": "a sync state, not a file",
    "ConversationMessage.file_name": "the attachment's display label",
    "ConversationMessage.file_size_bytes": "the attachment's byte count",
    "GalleryAlbum.cover_item_id": "an item id, not a file",
    "GalleryAlbum.cover_url": (
        "not persisted — filled from the cover item, whose files are registered"
    ),
    "SpaceZone.icon": "one emoji (domain.space.validate_zone_icon), never a file",
    "TimetableEntry.icon": "one emoji (timetable._check_icon), never a file",
    "TimetableOverride.icon": "one emoji (timetable._check_icon), never a file",
}


def _nested_dataclasses(tp: object) -> list[type]:
    """The dataclasses a field annotation holds (``X | None``,
    ``tuple[X, ...]``, ``list[X]`` …)."""
    if isinstance(tp, type) and dataclasses.is_dataclass(tp):
        return [tp]
    out: list[type] = []
    for arg in typing.get_args(tp):
        out.extend(_nested_dataclasses(arg))
    return out


def _scan(
    cls: type,
    resource: str | None,
    prefix: tuple[str, ...],
    missing: list[str],
    seen: set[tuple[type, tuple[str, ...]]],
) -> None:
    if (cls, prefix) in seen:
        return
    seen.add((cls, prefix))
    hints = typing.get_type_hints(cls)
    paths = {f.path for f in SPACE_MEDIA_ITEMS[resource].fields} if resource else set()
    for f in dataclasses.fields(cls):
        nested = _nested_dataclasses(hints.get(f.name))
        # A field holding an object (a post's ``file_meta``) is scanned
        # inside, not judged by its own name.
        if _FILE_LIKE.search(f.name) and not nested:
            named = f"{cls.__name__}.{f.name}" in NOT_A_SPACE_FILE
            registered = (*prefix, f.name) in paths
            if named == registered:
                missing.append(f"{cls.__name__}.{f.name}")
        for inner in nested:
            if inner is not cls:  # a self-nesting type (a comment's replies)
                _scan(inner, resource, (*prefix, f.name), missing, seen)


def test_synced_types_cover_every_resource() -> None:
    """Derived from the §25.6 exporter: every content resource it streams
    has its row types listed (so their fields are scanned)."""
    content = (
        set(exporter.RESOURCE_ORDER)
        - exporter.ROSTER_RESOURCES
        - exporter.REMOVAL_RESOURCES
    )
    assert set(SYNCED_TYPES) == content
    assert set(_ITEM_CLASS.values()) == set(SPACE_MEDIA_ITEMS)


def test_every_file_like_field_is_registered_or_named() -> None:
    missing: list[str] = []
    seen: set[tuple[type, tuple[str, ...]]] = set()
    for types in SYNCED_TYPES.values():
        for cls in types:
            _scan(cls, _ITEM_CLASS.get(cls), (), missing, seen)
    assert not missing, (
        "a file-like field must be in SPACE_MEDIA_ITEMS or NOT_A_SPACE_FILE "
        f"(exactly one): {sorted(set(missing))}"
    )


def test_every_registered_path_exists_on_its_domain_object() -> None:
    by_resource = {r: cls for cls, r in _ITEM_CLASS.items()}
    for resource, item in SPACE_MEDIA_ITEMS.items():
        for f in item.fields:
            if f.column is None:
                continue  # wire-only (a page resolution's merged body)
            cls: object = by_resource[resource]
            for key in f.path:
                assert dataclasses.is_dataclass(cls), f.path
                hints = typing.get_type_hints(cls)
                assert key in hints, f.path
                inner = _nested_dataclasses(hints[key])
                cls = inner[0] if inner else None


# ─── One markdown pattern ────────────────────────────────────────────────


def test_one_markdown_pattern_everywhere() -> None:
    assert space_media_ref.LOCAL_MEDIA_REF_RE is LOCAL_MEDIA_REF_RE
    assert media_signer._BODY_MEDIA_RE is LOCAL_MEDIA_REF_RE
    assert media_reference_repo.page_body_media_refs is (
        space_media_ref.page_body_media_refs
    )
    # Behaviour: the repo's markdown reader skips the path inside a URL.
    body = "![a](api/media/a.webp) https://h/api/media/b.webp ./api/media/c.webp"
    assert media_reference_repo._names_in_markdown(body) == {"a.webp"}


# ─── replace_item_media: the domain-object rename ────────────────────────


@pytest.mark.parametrize("resource", sorted(SPACE_MEDIA_ITEMS))
def test_replace_item_media_renames_every_registered_field(resource: str) -> None:
    obj = synthetic_object(resource)
    out = replace_item_media(resource, obj, lambda u: u.replace(".webp", "-r.webp"))
    assert item_media_urls(resource, out) == [
        u.replace(".webp", "-r.webp") for u in expected_urls(resource, stored_only=True)
    ]


@pytest.mark.parametrize("resource", sorted(SPACE_MEDIA_ITEMS))
def test_replace_item_media_drops_every_registered_field(resource: str) -> None:
    out = replace_item_media(resource, synthetic_object(resource), lambda u: None)
    assert item_media_urls(resource, out) == []


def test_replace_item_media_drop_shapes() -> None:
    """A dropped attachment drops the file meta, a dropped card picture
    keeps the card, a str-typed field takes ``""``."""
    post = replace_item_media("posts", synthetic_object("posts"), lambda u: None)
    assert post.media_url is None and post.image_urls == ()
    assert post.file_meta is None
    assert post.link_preview is not None and post.link_preview.thumbnail_url is None
    item = replace_item_media("gallery", synthetic_object("gallery"), lambda u: None)
    assert item.url == "" and item.thumbnail_url == ""
    obj = synthetic_object("calendar")
    assert replace_item_media("tasks", obj, lambda u: None) is obj


# ─── Source tripwire: no hand-written per-type file list ─────────────────

#: The shapes a hand-written list of a space item's files took before the
#: registry (a gallery item's two files, a cover in a one-element list, a
#: comment's picture …). A consumer reads ``item_media_urls`` /
#: ``replace_item_media`` / ``SpaceMediaLocalization.item`` instead.
_HAND_LIST = re.compile(
    r"\(\w+\.thumbnail_url, \w+\.url\)|\(\w+\.url, \w+\.thumbnail_url\)"
    r"|\[\w+\.url, \w+\.thumbnail_url\]|\[\w+\.thumbnail_url, \w+\.url\]"
    r"|\[\w+\.cover_url\]|\[\w+(\[1\])?\.cover_url\] if"
    r"|\[\w+\.media_url\] if \w+\.media_url"
)

#: Hand-written lists that are NOT a space item's files — with the reason.
HAND_LIST_EXCLUSIONS: dict[str, str] = {
    "socialhome/services/gallery_share_service.py": (
        "a share's fresh file copies (``_Copy``: feed post, DM or space "
        "item alike) — not a stored space item's fields"
    ),
}


def test_no_hand_written_space_item_file_list() -> None:
    root = pathlib.Path(__file__).resolve().parents[2]
    hits = []
    for path in sorted((root / "socialhome").rglob("*.py")):
        rel = path.relative_to(root).as_posix()
        if rel in HAND_LIST_EXCLUSIONS or rel.endswith("space_media_fields.py"):
            continue
        for n, line in enumerate(path.read_text().splitlines(), 1):
            if _HAND_LIST.search(line):
                hits.append(f"{rel}:{n}: {line.strip()}")
    assert not hits, "route through the space media-field registry:\n" + "\n".join(hits)
