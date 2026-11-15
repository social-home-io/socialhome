"""Which media filenames are still referenced by a DB row.

Two readers need the same, complete answer:

* the media orphan sweep (``MediaOrphanSweepService``) deletes files in the
  media dir that nothing references — a single missed source here means
  the sweep deletes a user's photo;
* every per-row delete path (a post, a moment, a gallery item, …) removes
  the row's files only when no *other* row still points at them
  (:func:`socialhome.media.cleanup.unlink_unreferenced`). A file name is
  not proof of ownership: a row received from another household may name
  a file that belongs to somebody else's row.

This repo owns that enumeration as one auditable place. Like
``backup_service`` / ``data_export_service`` it is a deliberate read across
tables, so it uses raw SQL directly rather than going through per-domain
repos.

Sources (verified against the schema), as ``api/media/<file>`` URLs unless
noted:
  * ``conversation_messages.media_url`` — DM media (final blob)
  * ``feed_posts.media_url`` + ``image_urls_json``
  * ``post_comments.media_url``
  * every file field of a SPACE item — generated from the registry
    (:func:`~socialhome.domain.space_media_fields.space_media_columns`):
    ``space_posts`` (``media_url``, ``image_urls_json``, ``file_meta_json``
    ``$.url``, ``link_preview_json`` ``$.thumbnail_url``),
    ``space_post_comments.media_url``, ``space_calendar_events.cover_url``,
    ``gallery_items.filename`` + ``thumbnail_filename`` (bare basenames),
    ``bazaar_listings.image_urls_json``, ``space_pages.cover_image_url`` +
    its markdown ``content``
  * ``highlight_frames.media_url``
  * ``moments.media_url``
  * ``calendar_events.cover_url``
  * ``pages`` / ``page_edit_history`` ``.cover_image_url``
    and every picture their markdown ``content`` shows (``api/media/<file>``,
    never the same path inside another URL); an open conflict side's
    ``space_page_snapshots.body``
  * ``task_attachments.url``
  * ``post_drafts.media_url``
  * ``feed_posts.link_preview_json`` → ``$.thumbnail_url`` and
    ``feed_posts.file_meta_json`` → ``$.url`` (a household file post, while live)
    (the re-encoded link-preview image; one file may back several posts
    that linked the same page)

  * ``space_moderation_queue.payload_json`` of PENDING items — a queued
    post / page / event / listing publishes its media only on approval
    (up to 7 days later), so until then the payload is the reference

A soft-deleted post keeps ``image_urls_json`` and ``file_meta_json`` (only
``media_url`` and ``link_preview_json`` are cleared), so a post's image
list and attachment count only while the post is live.

NOT included (owned elsewhere / transient): DM ``.preview.webp`` /
``.part<NNNN>`` / ``.assembled`` intermediates and the ``.partial/``
staging dir — the sweep skips those by pattern; ``dm_gc`` owns them.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Collection, Iterable
from typing import Protocol, runtime_checkable

import orjson

from ..db import AsyncDatabase
from ..domain.file_hashes import is_space_content_media_name
from ..domain.media_constraints import (
    MEDIA_REF_PREFIX,
    SPACE_DIRECT_MEDIA_PREFIX,
    SPACE_PAGE_MEDIA_PREFIX,
    is_gfs_space_media_name,
    local_media_ref,
    space_media_ref,
)
from ..domain.space_media_fields import (
    FieldShape,
    page_body_media_refs,
    space_media_columns,
)
from ..domain.space_media_ref import space_page_media_name
from ..media.cleanup import media_basename
from .base import rows_to_dicts

log = logging.getLogger(__name__)

#: A space page row whose raw names count here (v_82), decided from LOCAL
#: state only: its space is hosted here (the names are our own files), or
#: the row is our own draft (``pending_base_seq`` — only this household's
#: own edit sets it). Every other column of a member's copy is the host's
#: (creator, editor …), so on a member household a page's names keep only
#: their page-scoped ``sp-`` copies alive
#: (:meth:`SqliteMediaReferenceRepo._page_scoped_names`), never a shared
#: file.
_HOSTED_SPACE = (
    "space_id IN (SELECT hs.id FROM spaces hs, instance_identity ii"
    " WHERE hs.owner_instance_id = ii.instance_id)"
)
_SPACE_PAGE_HERE = f"({_HOSTED_SPACE} OR pending_base_seq IS NOT NULL)"
_HISTORY_HERE = f"(space_id IS NULL OR {_HOSTED_SPACE})"
_SNAPSHOT_HERE = _HOSTED_SPACE

# ─── The reference set (orphan sweep, ``is_referenced``) ─────────────────
#
# Household rows are listed by hand; every SPACE item's file fields come
# from the registry (:func:`space_media_columns`) with the live-row filter
# of :data:`_SPACE_REFERENCE_LIVE` — a table missing there fails at import.

#: The live-row filter of a space item table's references; a column of it
#: may override (:data:`_SPACE_REFERENCE_LIVE_COLUMN`).
_SPACE_REFERENCE_LIVE: dict[str, str] = {
    "space_posts": "",
    "space_post_comments": "",
    "space_calendar_events": "",
    "gallery_items": "",
    "bazaar_listings": "",
    "space_pages": _SPACE_PAGE_HERE,
}
#: A soft delete clears a post's ``media_url`` and ``link_preview_json``
#: but keeps its image list and file attachment: those count only while
#: the post is live.
_SPACE_REFERENCE_LIVE_COLUMN: dict[tuple[str, str], str] = {
    ("space_posts", "image_urls_json"): "deleted=0",
    ("space_posts", "file_meta_json"): "deleted=0",
}


def _space_reference_live(table: str, col: str) -> str:
    return _SPACE_REFERENCE_LIVE_COLUMN.get((table, col), _SPACE_REFERENCE_LIVE[table])


def _space_reference_rows(shape: str) -> tuple[tuple[str, str, str], ...]:
    """``(table, column, live-row filter)`` of every registry column of
    *shape* (``one`` / ``list`` / ``markdown``)."""
    return tuple(
        (table, col, _space_reference_live(table, col))
        for table, col, sql_shape in space_media_columns()
        if sql_shape == shape
    )


#: ``(table, column, live-row filter)`` for single-reference columns.
_SINGLE_COLUMNS: tuple[tuple[str, str, str], ...] = (
    ("conversation_messages", "media_url", ""),
    ("feed_posts", "media_url", ""),
    ("post_comments", "media_url", ""),
    ("highlight_frames", "media_url", ""),
    ("moments", "media_url", ""),
    ("calendar_events", "cover_url", ""),
    ("pages", "cover_image_url", ""),
    ("page_edit_history", "cover_image_url", _HISTORY_HERE),
    ("task_attachments", "url", ""),
    ("post_drafts", "media_url", ""),
    *_space_reference_rows(FieldShape.ONE),
)

#: ``(table, column, live-row filter)`` for JSON arrays of references.
_LIST_COLUMNS: tuple[tuple[str, str, str], ...] = (
    ("feed_posts", "image_urls_json", "deleted=0"),
    *_space_reference_rows(FieldShape.LIST),
)


#: ``(table, JSON column, JSON path, live-row filter)`` for a reference
#: inside a JSON object. Soft-deleting a post NULLs its
#: ``link_preview_json`` (a card needs no live filter) but keeps its
#: ``file_meta_json`` — the attachment counts only while the post is live
#: (:data:`_SPACE_REFERENCE_LIVE_COLUMN`).
_JSON_FIELD_COLUMNS: tuple[tuple[str, str, str, str], ...] = (
    ("feed_posts", "link_preview_json", "$.thumbnail_url", ""),
    # A household FILE post's attachment (a soft delete keeps it).
    ("feed_posts", "file_meta_json", "$.url", "deleted=0"),
    *(
        (table, col, shape[len("json:") :], _space_reference_live(table, col))
        for table, col, shape in space_media_columns()
        if shape.startswith("json:")
    ),
)


#: A space-album item is live while it and its album are, and it is the
#: item's own file: never a gallery mirror of a post's media
#: (``source_post_id``, the system "Posts" album) — that file is the post's,
#: which the post sources already cover, so a mirror would count it twice
#: (the catch-up's file budget, a ``SPACE_MEDIA_WANTED`` item list). The
#: §25.6 gallery stream skips the same rows (``list_items_sync_page``).
_GALLERY_ITEM_LIVE = (
    "t.deleted_at IS NULL AND a.space_id IS NOT NULL AND a.deleted_at IS NULL"
    " AND a.is_system = 0 AND t.source_post_id IS NULL"
)

#: Every file a space ITEM shows — a post (its images, its attachment, its
#: link card's thumbnail), a comment, a calendar cover, a space-album item
#: (thumbnail and file), a bazaar listing (by its wrapper post's id): the
#: registry's columns (:func:`space_media_columns`) — one list for every
#: reader by item: :meth:`SqliteMediaReferenceRepo.content_names_of_item`
#: (v_85), :meth:`~SqliteMediaReferenceRepo.space_rows_of` and
#: :meth:`~SqliteMediaReferenceRepo.live_item_files` (v_92
#: ``SPACE_MEDIA_WANTED``). ``(FROM clause over the row t, item id, space,
#: value, shape, live-row condition, owner)`` — the owner is the person the
#: item belongs to (the catch-up file gate's owner).
#:
#: :data:`_SPACE_ITEM_SOURCE` holds each item table's ``(FROM, item id,
#: space, live, owner)``; a registry table missing there fails at import.
#: Named exclusion (:data:`_NOT_SPACE_ITEM_FILES`): pages are not items
#: here — their files are the host's alone (v_82), read by the page paths.
_SPACE_ITEM_SOURCE: dict[str, tuple[str, str, str, str, str]] = {
    "space_posts": ("space_posts t", "t.id", "t.space_id", "t.deleted = 0", "t.author"),
    "space_post_comments": (
        "space_post_comments t LEFT JOIN space_posts p ON p.id = t.post_id",
        "t.id",
        "p.space_id",
        "t.deleted = 0 AND p.deleted = 0",
        "t.author",
    ),
    "space_calendar_events": (
        "space_calendar_events t",
        "t.id",
        "t.space_id",
        "t.deleted_at IS NULL",
        "t.created_by",
    ),
    "gallery_items": (
        "gallery_items t LEFT JOIN gallery_albums a ON a.id = t.album_id",
        "t.id",
        "a.space_id",
        _GALLERY_ITEM_LIVE,
        "t.uploaded_by",
    ),
    "bazaar_listings": (
        "bazaar_listings t LEFT JOIN space_posts p ON p.id = t.post_id",
        "t.post_id",
        "t.space_id",
        "p.deleted = 0",
        "t.seller_user_id",
    ),
}
_NOT_SPACE_ITEM_FILES: frozenset[str] = frozenset({"space_pages"})


def _item_value(col: str, shape: str) -> tuple[str, str]:
    """The SQL value of column *col* of the row ``t`` and its shape as an
    item file (``one`` / ``list``)."""
    if shape.startswith("json:"):
        path = shape[len("json:") :]
        return (
            f"CASE WHEN json_valid(t.{col}) THEN json_extract(t.{col}, '{path}') END",
            "one",
        )
    if shape not in ("one", "list"):
        raise ValueError(f"no item-file shape for {col} ({shape})")
    return f"t.{col}", shape


def _space_item_files() -> tuple[tuple[str, str, str, str, str, str, str], ...]:
    rows = []
    for table, col, shape in space_media_columns():
        if table in _NOT_SPACE_ITEM_FILES:
            continue
        source, key, space, live, owner = _SPACE_ITEM_SOURCE[table]
        value, item_shape = _item_value(col, shape)
        rows.append((source, key, space, value, item_shape, live, owner))
    return tuple(rows)


_SPACE_ITEM_FILES: tuple[tuple[str, str, str, str, str, str, str], ...] = (
    _space_item_files()
)

#: ``(FROM clause over the row t, space, names-are-ours filter)`` of every
#: registry table — what a space purge collects (:meth:`space_files`). A
#: page's RAW names count only on the space's host (:data:`_HOSTED_SPACE`):
#: a member's own draft still carries the HOST's names, which a hostile
#: host could point at one of our household files — the draft's own
#: uploads are left to the orphan sweep; a member's page-scoped ``sp-``
#: copies come from :meth:`SqliteMediaReferenceRepo._page_names_of`.
_SPACE_ROWS: dict[str, tuple[str, str, str]] = {
    **{
        t: (src, space, "")
        for t, (src, _k, space, _l, _o) in _SPACE_ITEM_SOURCE.items()
    },
    "space_pages": ("space_pages t", "t.space_id", f" AND {_HOSTED_SPACE}"),
}
#: Registry tables that store bare basenames, not ``api/media/<file>``.
_BARE_NAME_TABLES: frozenset[str] = frozenset({"gallery_items"})


def _space_file(
    raw: object, *, bare: bool, gfs_copies: Collection[str] = ()
) -> str | None:
    """The file name of a stored space reference *raw* when it is a
    canonical, non-reserved space file (:func:`space_media_ref` — never a
    DM's, a DM transient, a foreign URL or junk a legacy row kept
    unvalidated) or one of *gfs_copies* (this space's GFS reader copies,
    :meth:`SqliteMediaReferenceRepo.gfs_copies_in_space`), else ``None``."""
    if not isinstance(raw, str) or not raw:
        return None
    value = MEDIA_REF_PREFIX + raw if bare else raw
    ref = space_media_ref(value)
    if ref is not None:
        return ref[len(MEDIA_REF_PREFIX) :]
    local = local_media_ref(value)
    name = local[len(MEDIA_REF_PREFIX) :] if local is not None else None
    return name if name is not None and name in gfs_copies else None


#: A moderation queue item still to publish its files: pending, or
#: approved inside its review window — the approve claims the item before
#: its apply, and an apply that stopped part-way resumes only while the
#: window is open (``SpaceModerationService._approve``).
_HELD_LIVE = (
    "(status = 'pending' OR (status = 'approved'"
    " AND datetime(expires_at) > datetime('now')))"
)

#: ``(table, text column, live-row filter)`` scanned for every
#: ``api/media/<file>`` inside a free-form JSON payload.
_PAYLOAD_SCANS: tuple[tuple[str, str, str], ...] = (
    ("space_moderation_queue", "payload_json", _HELD_LIVE),
    # An open conflict side of a space page (its body, JSON-encoded).
    ("space_page_snapshots", "body", _SNAPSHOT_HERE),
)

#: ``(table, markdown column)`` whose pictures (:func:`page_body_media_refs`)
#: are references: a page's body, and each version in its history.
_MARKDOWN_COLUMNS: tuple[tuple[str, str, str], ...] = (
    ("pages", "content", ""),
    ("page_edit_history", "content", _HISTORY_HERE),
    *_space_reference_rows(FieldShape.MARKDOWN),
)
#: The loose ``api/media/<file>`` scan of a free-form JSON payload
#: (:data:`_PAYLOAD_SCANS`: a held item's whole payload). Named exclusion
#: from the one markdown pattern (``LOCAL_MEDIA_REF_RE``): it reads no
#: markdown, and over-matching here only keeps a held file alive longer.
_MEDIA_REF_RE = re.compile(r"api/media/([A-Za-z0-9._-]+)")

# ─── Who may reference a file (``reference_access``) ─────────────────────
#
# A write that names a local media file (a post's images, a cover, a DM
# attachment …) re-publishes it to that write's audience: every read signs
# the reference again. So the writer must be able to open the file today —
# through at least one row that references it — and a private gallery
# album's file only its uploader may re-use (gallery D7: privacy follows
# the upload). ``(table, column, shape, access)``: ``shape`` is ``one``
# (a single reference), ``list`` (a JSON array) or ``json:<path>`` (a
# field of a JSON object); ``access`` is an SQL boolean over the row ``t``
# and the writer ``me`` (``me.uid`` / ``me.uname``).

#: The writer is one of this household's own people (a ``users`` row): a
#: household-level row (the feed, a personal calendar, a household page or
#: album) is open to them — never to a remote space member, whose user id
#: reaches this check only through a reviewed item it submitted.
_HOUSEHOLD = "me.uname IS NOT NULL"

#: The writer holds a seat in the space ``{col}`` names.
_SEAT = (
    "EXISTS (SELECT 1 FROM space_members sm WHERE sm.space_id = {col}"
    " AND sm.user_id = me.uid)"
)
#: A space page's names are its host's choice (v_82). On the host they are
#: its own files; on a member household they never name the shared store —
#: the page's files live there under page-scoped ``sp-`` names — so a page
#: grants a seat access to a shared file only on the space's host.
_HOSTED_SEAT = (
    _SEAT.format(col="t.space_id")
    + " AND EXISTS (SELECT 1 FROM spaces hs, instance_identity ii"
    " WHERE hs.id = t.space_id AND hs.owner_instance_id = ii.instance_id)"
)

#: The space each space item table's row belongs to (an SQL expression
#: over the row ``t``) — :data:`_SPACE_OF`, and the seat
#: :data:`_ACCESS_SOURCES` checks. A registry table missing here fails at
#: import. Named exclusion (:data:`_OWN_GALLERY_QUERIES`): ``gallery_items``
#: — its album decides, in its own queries (:data:`_GALLERY_ACCESS_SQL`).
_SPACE_ITEM_SPACE: dict[str, str] = {
    "space_posts": "t.space_id",
    "space_post_comments": (
        "(SELECT p.space_id FROM space_posts p WHERE p.id = t.post_id)"
    ),
    "space_calendar_events": "t.space_id",
    "bazaar_listings": "t.space_id",
    "space_pages": "t.space_id",
}
_OWN_GALLERY_QUERIES: frozenset[str] = frozenset({"gallery_items"})
#: Space item tables whose names are their host's choice (v_82): a seat
#: opens their file only on the space's host (:data:`_HOSTED_SEAT`).
_HOST_NAMED: frozenset[str] = frozenset({"space_pages"})


def _space_access_rows() -> tuple[tuple[str, str, str, str], ...]:
    rows = []
    for table, col, shape in space_media_columns():
        if table in _OWN_GALLERY_QUERIES:
            continue
        access = (
            _HOSTED_SEAT
            if table in _HOST_NAMED
            else _SEAT.format(col=_SPACE_ITEM_SPACE[table])
        )
        rows.append((table, col, shape, access))
    return tuple(rows)


_ACCESS_SOURCES: tuple[tuple[str, str, str, str], ...] = (
    (
        "conversation_messages",
        "media_url",
        "one",
        (
            "EXISTS (SELECT 1 FROM conversation_members cm"
            " WHERE cm.conversation_id = t.conversation_id AND cm.username = me.uname)"
        ),
    ),
    ("feed_posts", "media_url", "one", _HOUSEHOLD),
    ("feed_posts", "image_urls_json", "list", _HOUSEHOLD),
    ("feed_posts", "link_preview_json", "json:$.thumbnail_url", _HOUSEHOLD),
    ("post_comments", "media_url", "one", _HOUSEHOLD),
    (
        "highlight_frames",
        "media_url",
        "one",
        (
            "EXISTS (SELECT 1 FROM highlights h WHERE h.id = t.highlight_id"
            " AND h.author_user_id = me.uid)"
        ),
    ),
    ("moments", "media_url", "one", "t.author_user_id = me.uid"),
    ("calendar_events", "cover_url", "one", _HOUSEHOLD),
    ("pages", "cover_image_url", "one", _HOUSEHOLD),
    ("pages", "content", "markdown", _HOUSEHOLD),
    (
        "page_edit_history",
        "cover_image_url",
        "one",
        f"((t.space_id IS NULL AND {_HOUSEHOLD}) OR {_HOSTED_SEAT})",
    ),
    (
        "page_edit_history",
        "content",
        "markdown",
        f"((t.space_id IS NULL AND {_HOUSEHOLD}) OR {_HOSTED_SEAT})",
    ),
    ("task_attachments", "url", "one", "t.uploaded_by = me.uid"),
    ("post_drafts", "media_url", "one", "t.username = me.uname"),
    *_space_access_rows(),
    # The submitter's own held item (:data:`_HELD_LIVE` — the approve
    # claims the item BEFORE its apply re-checks the files for the
    # submitter). A fallback with its own rules: :data:`_HELD_SOURCE`.
    (
        "space_moderation_queue",
        "payload_json",
        "payload",
        f"t.submitted_by = me.uid AND {_HELD_LIVE}",
    ),
)

#: The :data:`_ACCESS_SOURCES` table that counts only when no other row
#: references the file, and only when EVERY held item naming it is the
#: writer's own (one of them live). A held item is its submitter's
#: proposal, never a key: not to a file some other row shows, not to a
#: file another submitter's item names. A remote submission is not checked
#: on its way in — its files take the submitter household's ``sd-`` names
#: (``SpaceMediaNamer``) — so a remote submitter's item opens only such a
#: name, never an unreferenced file of ours (an orphan, or a fresh upload
#: not posted yet).
_HELD_SOURCE = "space_moderation_queue"

#: The LIVE rows of a space that show a file (v_83, :meth:`live_in_space`):
#: table, column, shape, and the condition (first ``?`` = the space id) —
#: the registry's columns with :data:`_SPACE_ITEM_LIVE_HERE`'s condition (a
#: registry table missing there fails at import). Named exclusion:
#: ``gallery_items`` (its own query, by album).
_SPACE_ITEM_LIVE_HERE: dict[str, str] = {
    "space_posts": "t.space_id = ? AND t.deleted = 0",
    "space_post_comments": (
        "t.deleted = 0 AND (SELECT p.space_id FROM space_posts p"
        " WHERE p.id = t.post_id AND p.deleted = 0) = ?"
    ),
    "bazaar_listings": (
        "t.space_id = ? AND EXISTS (SELECT 1 FROM space_posts p"
        " WHERE p.id = t.post_id AND p.deleted = 0)"
    ),
    "space_calendar_events": "t.space_id = ? AND t.deleted_at IS NULL",
    # A page's RAW names are our own files only on the space's host; on a
    # member household they are the host's, and only our page-scoped sp-
    # copies count (:meth:`SqliteMediaReferenceRepo._page_names_of`) — its
    # cover and (v_82) every picture its body shows.
    "space_pages": f"t.space_id = ? AND t.deleted_at IS NULL AND {_HOSTED_SPACE}",
}

_LIVE_SPACE_SOURCES: tuple[tuple[str, str, str, str], ...] = tuple(
    (table, col, shape, _SPACE_ITEM_LIVE_HERE[table])
    for table, col, shape in space_media_columns()
    if table not in _OWN_GALLERY_QUERIES
)

#: Page tables whose RAW names make a file this space's only where the
#: row's names are our own (the space's host; our own draft) — on a member
#: household a hostile host could otherwise name one of our shared files in
#: a page (#1317 review). Applied by :meth:`confined_to_space`.
_PAGE_ROWS_HERE: dict[str, str] = {
    "space_pages": _SPACE_PAGE_HERE,
    "page_edit_history": _HISTORY_HERE,
}

#: The space each :data:`_ACCESS_SOURCES` table's row belongs to (an SQL
#: expression over the row ``t``); a table not listed is household-level
#: (``NULL``) — never a space's content (:meth:`confined_to_space`).
_SPACE_OF: dict[str, str] = {
    **_SPACE_ITEM_SPACE,
    "page_edit_history": "t.space_id",
    "space_moderation_queue": "t.space_id",
}

#: A gallery item's file (bare basenames): a household album open to
#: everyone, a seat in a space album's space — and for a private album
#: (``chosen``) only its uploader (``private`` marks such a row).
_GALLERY_ACCESS_SQL = (
    "SELECT i.filename AS f, i.thumbnail_filename AS th,"
    " CASE WHEN a.space_id IS NOT NULL THEN "
    + _SEAT.format(col="a.space_id")
    + " WHEN a.audience = 'chosen' THEN i.uploaded_by = me.uid"
    + f" ELSE {_HOUSEHOLD} END AS ok,"
    " (a.space_id IS NULL AND a.audience = 'chosen'"
    " AND i.uploaded_by IS NOT me.uid) AS private"
    " FROM gallery_items i JOIN gallery_albums a ON a.id = i.album_id, me"
    " WHERE i.filename = ? OR i.thumbnail_filename = ?"
)
_ME_CTE = (
    "WITH me(uid, uname) AS (SELECT ?, (SELECT username FROM users WHERE user_id = ?)) "
)


def _names_in_payload(raw: object) -> set[str]:
    if not isinstance(raw, str) or not raw:
        return set()
    return {name for name in _MEDIA_REF_RE.findall(raw) if name not in (".", "..")}


def _names_in_side(raw: object) -> set[str]:
    """The files an open conflict side of a page shows: its body is the
    side's JSON (``content`` markdown, ``cover_image_url``), read with the
    page-body pattern (the shared strict :data:`LOCAL_MEDIA_REF_RE`) — never
    a loose scan of the encoded text."""
    if not isinstance(raw, str) or not raw:
        return set()
    try:
        side = orjson.loads(raw)
    except orjson.JSONDecodeError:
        return set()
    if not isinstance(side, dict):
        return set()
    names = _names_in_markdown(side.get("content"))
    cover = side.get("cover_image_url")
    name = media_basename(cover if isinstance(cover, str) else None)
    if name and name not in (".", ".."):
        names.add(name)
    return names


def _names_in_column(table: str, raw: object) -> set[str]:
    """The files a free-form payload column names (:data:`_PAYLOAD_SCANS`)."""
    if table == "space_page_snapshots":
        return _names_in_side(raw)
    return _names_in_payload(raw)


def _names_in_markdown(raw: object) -> set[str]:
    """The files a page body shows (:func:`page_body_media_refs`)."""
    return {
        name
        for name in (media_basename(ref) for ref in page_body_media_refs(raw))
        if name and name not in (".", "..")
    }


def _hit(shape: str, raw: object, basename: str) -> bool:
    """Does the value *raw* of a column of *shape* reference *basename*?"""
    if shape == "list":
        return basename in _names_in_list(raw)
    if shape == "payload":
        return basename in _names_in_payload(raw)
    if shape == "markdown":
        return basename in _names_in_markdown(raw)
    return media_basename(raw if isinstance(raw, str) else None) == basename


def _like_escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _json_list(raw: object) -> list[object]:
    """The entries of a stored JSON array (anything else: none)."""
    if not isinstance(raw, str) or not raw:
        return []
    try:
        values = orjson.loads(raw)
    except ValueError, TypeError:
        return []
    return values if isinstance(values, list) else []


def _names_in_list(raw: object) -> list[str]:
    if not isinstance(raw, str) or not raw:
        return []
    try:
        urls = orjson.loads(raw)
    except ValueError, TypeError:  # pragma: no cover — defensive
        return []
    if not isinstance(urls, list):
        return []
    out: list[str] = []
    for u in urls:
        name = media_basename(u if isinstance(u, str) else None)
        if name:
            out.append(name)
    return out


@runtime_checkable
class AbstractMediaReferenceRepo(Protocol):
    async def referenced_basenames(self) -> set[str]: ...
    async def is_referenced(self, basename: str) -> bool: ...
    async def may_reference(self, basename: str, user_id: str) -> bool:
        """Whether *user_id* may name the stored file *basename* in a write
        (a post, comment, cover, DM, moment, highlight, listing …): no row
        references it yet (their own fresh upload), or they can open at
        least one row that does — never a private gallery album's file
        unless they uploaded it (D7)."""
        ...

    async def confined_to_space(self, basename: str, space_id: str) -> bool:
        """Whether *basename* is referenced only by content of *space_id*
        (what the GFS media cache may seal under that space's key)."""
        ...

    async def live_in_space(self, basename: str, space_id: str) -> bool:
        """Whether a LIVE row of *space_id* shows *basename* now (not
        deleted, not only held for review) — what a household sends on
        demand (v_83)."""
        ...

    async def space_rows_of(self, basename: str) -> list[tuple[str, str, str]]:
        """``(space_id, item_id, created_at)`` of every LIVE space item
        showing *basename* (v_92 ``SPACE_MEDIA_WANTED``, requester side)."""
        ...

    async def live_item_files(
        self, space_id: str, item_id: str
    ) -> list[tuple[str, str | None]]:
        """``(basename, owner)`` of every file the LIVE item *item_id* of
        space *space_id* shows here (v_92 ``SPACE_MEDIA_WANTED``, provider
        side)."""
        ...

    async def recent_item_files(
        self, space_id: str, *, cutoff: str, limit: int
    ) -> list[tuple[str, str, str | None, list[str]]]:
        """``(created_at, item_id, owner, basenames)`` of the LIVE items of
        *space_id* created at or after *cutoff* that show files, newest
        first (the §25.6 bounded catch-up media)."""
        ...

    async def space_files(self, space_id: str) -> list[str]:
        """Every file a row of space *space_id* names in a registry column
        (live or not) — what a hard purge of the space may free."""
        ...

    async def gfs_copies_in_space(
        self, space_id: str, names: Iterable[str]
    ) -> set[str]:
        """Those of *names* that are GFS reader copies (``sg-``) of items
        of space *space_id* (its ``space_media_refs`` rows)."""
        ...

    async def content_names_of_item(self, item_id: str) -> list[str]:
        """The content-addressed (``sc-``, v_85) files the space item
        *item_id* (a post, comment, calendar event, gallery item or bazaar
        listing) shows here."""
        ...


class SqliteMediaReferenceRepo:
    """Answers "is this media file still referenced?" across the schema."""

    __slots__ = ("_db",)

    def __init__(self, db: AsyncDatabase) -> None:
        self._db = db

    async def referenced_basenames(self) -> set[str]:
        out: set[str] = set()
        for table, col, live in _SINGLE_COLUMNS:
            where = f"{col} IS NOT NULL" + (f" AND {live}" if live else "")
            rows = await self._db.fetchall(
                f"SELECT {col} AS v FROM {table} WHERE {where}"
            )
            for r in rows_to_dicts(rows):
                # ``media_basename`` also normalises a bare gallery basename
                # and strips a stray prefix / ``?query``.
                name = media_basename(r.get("v"))
                if name:
                    out.add(name)
        for table, col, live in _LIST_COLUMNS:
            where = f"{col} IS NOT NULL" + (f" AND {live}" if live else "")
            rows = await self._db.fetchall(
                f"SELECT {col} AS v FROM {table} WHERE {where}"
            )
            for r in rows_to_dicts(rows):
                out.update(_names_in_list(r.get("v")))
        for table, col, path, live in _JSON_FIELD_COLUMNS:
            rows = await self._db.fetchall(
                f"SELECT json_extract({col}, ?) AS v FROM {table} "
                f"WHERE {col} IS NOT NULL AND json_valid({col})"
                + (f" AND {live}" if live else ""),
                (path,),
            )
            for r in rows_to_dicts(rows):
                name = media_basename(r.get("v"))
                if name:
                    out.add(name)
        for table, col, live in _PAYLOAD_SCANS:
            where = f"{col} IS NOT NULL" + (f" AND {live}" if live else "")
            rows = await self._db.fetchall(
                f"SELECT {col} AS v FROM {table} WHERE {where}"
            )
            for r in rows_to_dicts(rows):
                out.update(_names_in_column(table, r.get("v")))
        for table, col, live in _MARKDOWN_COLUMNS:
            where = f"{col} LIKE '%api/media/%'" + (f" AND {live}" if live else "")
            rows = await self._db.fetchall(
                f"SELECT {col} AS v FROM {table} WHERE {where}"
            )
            for r in rows_to_dicts(rows):
                out.update(_names_in_markdown(r.get("v")))
        out.update(await self._page_scoped_names())
        return out

    async def _page_scoped_names(self) -> set[str]:
        """Every ``sp-`` name a space page, a version in its history or an
        open side shows here (v_82: a member household keeps a page's files
        under ``space_page_media_name(space, file)``)."""
        out: set[str] = set()
        for table, cols in (
            ("space_pages", ("cover_image_url", "content")),
            ("page_edit_history", ("cover_image_url", "content")),
            ("space_page_snapshots", ("body",)),
        ):
            rows = await self._db.fetchall(
                f"SELECT space_id AS s, {', '.join(cols)} FROM {table}"
                " WHERE space_id IS NOT NULL"
            )
            for r in rows_to_dicts(rows):
                names: set[str] = set()
                for col in cols:
                    raw = r.get(col)
                    if col == "cover_image_url":
                        name = media_basename(raw if isinstance(raw, str) else None)
                        if name:
                            names.add(name)
                    elif col == "body":
                        names |= _names_in_side(raw)
                    else:
                        names |= _names_in_markdown(raw)
                out.update(space_page_media_name(str(r["s"]), n) for n in names)
        return out

    async def may_reference(self, basename: str, user_id: str) -> bool:
        if not basename:
            return False
        me = (user_id, user_id)
        rows = await self._db.fetchall(
            _ME_CTE + _GALLERY_ACCESS_SQL, (*me, basename, basename)
        )
        referenced = allowed = False
        held = held_ok = False
        for r in rows_to_dicts(rows):
            if r.get("private"):
                return False  # a private album's file: its uploader only
            referenced = True
            allowed = allowed or bool(r.get("ok"))
        pattern = f"%{_like_escape(basename)}%"
        for table, col, shape, access in _ACCESS_SOURCES:
            if table == _HELD_SOURCE:
                held, held_ok = await self._held_access(
                    basename, me, pattern, col, access
                )
                continue
            if shape.startswith("json:"):
                value = f"json_extract(t.{col}, '{shape[5:]}')"
                guard = f" AND json_valid(t.{col})"
            else:
                value, guard = f"t.{col}", ""
            rows = await self._db.fetchall(
                _ME_CTE + f"SELECT {value} AS v, ({access}) AS ok FROM {table} t, me"
                f" WHERE t.{col} LIKE ? ESCAPE '\\'{guard}",
                (*me, pattern),
            )
            for r in rows_to_dicts(rows):
                if _hit(shape, r.get("v"), basename):
                    referenced = True
                    allowed = allowed or bool(r.get("ok"))
            if allowed:
                return True
        if referenced:
            return False
        # Only held items name it: their submitter's — or nobody's yet.
        return held_ok or not held

    async def _held_access(
        self, basename: str, me: tuple[str, str], pattern: str, col: str, access: str
    ) -> tuple[bool, bool]:
        """``(referenced, allowed)`` over the moderation queue's payloads."""
        rows = await self._db.fetchall(
            _ME_CTE + f"SELECT t.{col} AS v, ({access}) AS ok,"
            " t.submitted_by = me.uid AS mine, me.uname IS NOT NULL AS local"
            f" FROM {_HELD_SOURCE} t, me WHERE t.{col} LIKE ? ESCAPE '\\'",
            (*me, pattern),
        )
        referenced = allowed = False
        everyone_mine = local = True
        for r in rows_to_dicts(rows):
            if basename in _names_in_payload(r.get("v")):
                referenced = True
                allowed = allowed or bool(r.get("ok"))
                everyone_mine = everyone_mine and bool(r.get("mine"))
                local = bool(r.get("local"))
        own_copy = local or basename.startswith(SPACE_DIRECT_MEDIA_PREFIX)
        return referenced, allowed and everyone_mine and own_copy

    async def confined_to_space(self, basename: str, space_id: str) -> bool:
        """Whether the file *basename* is referenced, and ONLY by content of
        space *space_id* — what the GFS media cache may seal under that
        space's key for its readers. A file any household row, DM, moment,
        private or household album, or another space's row names is not
        (a member could name such a file in a post of the space) — except a
        link card's thumbnail: one cached file backs every post that linked
        the page, and those references outside the space are ignored."""
        if not basename or not space_id:
            return False
        if basename.startswith(SPACE_PAGE_MEDIA_PREFIX):
            # A member's page-scoped copy (v_82): bound to its space by its
            # name, so it is this space's iff a page row of it shows the file.
            return basename in await self._page_names_of(space_id, live=False)
        referenced = False
        rows = await self._db.fetchall(
            "SELECT a.space_id AS s FROM gallery_items i"
            " JOIN gallery_albums a ON a.id = i.album_id"
            " WHERE i.filename = ? OR i.thumbnail_filename = ?",
            (basename, basename),
        )
        for r in rows_to_dicts(rows):
            if r.get("s") != space_id:
                return False
            referenced = True
        pattern = f"%{_like_escape(basename)}%"
        for table, col, shape, _access in _ACCESS_SOURCES:
            space_of = _SPACE_OF.get(table, "NULL")
            if shape.startswith("json:"):
                value = f"json_extract(t.{col}, '{shape[5:]}')"
                guard = f" AND json_valid(t.{col})"
            else:
                value, guard = f"t.{col}", ""
            here = _PAGE_ROWS_HERE.get(table)
            if here:
                guard += f" AND {here}"
            rows = await self._db.fetchall(
                f"SELECT {value} AS v, {space_of} AS s FROM {table} t"
                f" WHERE t.{col} LIKE ? ESCAPE '\\'{guard}",
                (pattern,),
            )
            for r in rows_to_dicts(rows):
                if not _hit(shape, r.get("v"), basename):
                    continue
                if r.get("s") != space_id:
                    if col == "link_preview_json":
                        # A link card's thumbnail is cached per URL and backs
                        # every post that linked the page (feed, other
                        # spaces): public web content the household fetched
                        # itself, so it never keeps the file from this
                        # space's readers (#1309 re-review 3) — nor makes it
                        # this space's.
                        continue
                    return False
                referenced = True
        return referenced

    async def live_in_space(self, basename: str, space_id: str) -> bool:
        """Whether a LIVE row of space *space_id* shows *basename* right now
        — a post or comment not deleted, a listing, a calendar event, page
        or space-album item not deleted — never only a moderation queue
        entry or a page's edit history. On demand (v_83) a household sends
        a file only when this holds as well as :meth:`confined_to_space`,
        so a deleted item's picture is never sent again. A member's
        page-scoped ``sp-`` copy is live iff the space's live page shows the
        file (v_82)."""
        if not basename or not space_id:
            return False
        if basename.startswith(SPACE_PAGE_MEDIA_PREFIX):
            return basename in await self._page_names_of(space_id, live=True)
        rows = await self._db.fetchall(
            "SELECT 1 FROM gallery_items i JOIN gallery_albums a ON a.id = i.album_id"
            " WHERE a.space_id = ? AND i.deleted_at IS NULL"
            " AND (i.filename = ? OR i.thumbnail_filename = ?) LIMIT 1",
            (space_id, basename, basename),
        )
        if rows:
            return True
        pattern = f"%{_like_escape(basename)}%"
        for table, col, shape, live in _LIVE_SPACE_SOURCES:
            if shape.startswith("json:"):
                value = f"json_extract(t.{col}, '{shape[5:]}')"
                guard = f" AND json_valid(t.{col})"
            else:
                value, guard = f"t.{col}", ""
            rows = await self._db.fetchall(
                f"SELECT {value} AS v FROM {table} t WHERE {live}"
                f" AND t.{col} LIKE ? ESCAPE '\\'{guard}",
                (space_id, pattern),
            )
            for r in rows_to_dicts(rows):
                if _hit(shape, r.get("v"), basename):
                    return True
        return False

    async def space_rows_of(self, basename: str) -> list[tuple[str, str, str]]:
        """``(space_id, item_id, created_at)`` of every LIVE space item that
        shows *basename* (:data:`_SPACE_ITEM_FILES`: a post, a comment on a
        live post, a calendar cover, a space-album item, a bazaar listing —
        none deleted). Sorted, deduplicated. Which items a household names
        when it asks for a file it does not hold (v_92
        ``SPACE_MEDIA_WANTED``)."""
        if not basename:
            return []
        out: set[tuple[str, str, str]] = set()
        pattern = f"%{_like_escape(basename)}%"
        for source, key, space, value, shape, live, _owner in _SPACE_ITEM_FILES:
            rows = await self._db.fetchall(
                f"SELECT {space} AS s, {key} AS i, t.created_at AS c, {value} AS v"
                f" FROM {source} WHERE {live} AND {value} LIKE ? ESCAPE '\\'",
                (pattern,),
            )
            for r in rows_to_dicts(rows):
                if r.get("s") and _hit(shape, r.get("v"), basename):
                    out.add((str(r["s"]), str(r["i"]), str(r["c"])))
        return sorted(out)

    async def live_item_files(
        self, space_id: str, item_id: str
    ) -> list[tuple[str, str | None]]:
        """``(basename, owner)`` of every file the LIVE item *item_id* of
        space *space_id* shows here, each once, in source order — the space
        checked in SQL, so an item of another space shows nothing. What a
        provider may answer a ``SPACE_MEDIA_WANTED`` entry from (v_92)."""
        if not space_id or not item_id:
            return []
        out: list[tuple[str, str | None]] = []
        seen: set[str] = set()
        for source, key, space, value, shape, live, owner in _SPACE_ITEM_FILES:
            rows = await self._db.fetchall(
                f"SELECT {value} AS v, {owner} AS o FROM {source}"
                f" WHERE {key} = ? AND {space} = ? AND {live}",
                (item_id, space_id),
            )
            for r in rows_to_dicts(rows):
                raw = r.get("v")
                names = (
                    _names_in_list(raw)
                    if shape == "list"
                    else [n for n in (media_basename(raw),) if n]
                )
                who = r.get("o")
                for name in names:
                    if name not in seen and name not in (".", ".."):
                        seen.add(name)
                        out.append((name, str(who) if who else None))
        return out

    async def recent_item_files(
        self, space_id: str, *, cutoff: str, limit: int
    ) -> list[tuple[str, str, str | None, list[str]]]:
        """``(created_at, item_id, owner, basenames)`` of every LIVE item of
        *space_id* (:data:`_SPACE_ITEM_FILES`) created at or after *cutoff*
        that shows a file, newest first (ties by item id); ``created_at``
        normalised to SQLite's naive UTC form (stored values mix it with
        the ISO ``T`` form). Bounded: the newest *limit* rows of each
        source — the §25.6 catch-up media cuts them to its file budget."""
        if not space_id or limit <= 0:
            return []
        items: dict[str, tuple[str, str | None, list[str]]] = {}
        for source, key, space, value, shape, live, owner in _SPACE_ITEM_FILES:
            rows = await self._db.fetchall(
                f"SELECT {key} AS i, datetime(t.created_at) AS c, {owner} AS o,"
                f" {value} AS v FROM {source}"
                f" WHERE {space} = ? AND {live} AND {value} IS NOT NULL"
                # Raw text first (indexable: ``idx_space_posts_created``):
                # every stored form of a day at or after the cutoff's day
                # sorts at or above that day — then exact across forms.
                " AND t.created_at >= ?"
                " AND datetime(t.created_at) >= datetime(?)"
                " ORDER BY datetime(t.created_at) DESC LIMIT ?",
                (space_id, cutoff[:10], cutoff, int(limit)),
            )
            for r in rows_to_dicts(rows):
                raw = r.get("v")
                names = (
                    _names_in_list(raw)
                    if shape == "list"
                    else [n for n in (media_basename(raw),) if n]
                )
                names = [n for n in names if n not in (".", "..")]
                if not names or not r.get("c"):
                    continue
                item_id = str(r["i"])
                who = r.get("o")
                held = items.get(item_id)
                if held is None:
                    items[item_id] = (str(r["c"]), str(who) if who else None, [])
                    held = items[item_id]
                held[2].extend(n for n in names if n not in held[2])
        ordered = sorted(items.items(), key=lambda kv: kv[0])
        ordered.sort(key=lambda kv: kv[1][0], reverse=True)
        return [(c, item_id, who, names) for item_id, (c, who, names) in ordered]

    async def _page_names_of(self, space_id: str, *, live: bool) -> set[str]:
        """The page-scoped ``sp-`` names of the files *space_id*'s pages
        show — the live pages only (*live*), else every page row of it (its
        pages, their history, open sides)."""
        sources: tuple[tuple[str, tuple[str, ...], str], ...] = (
            ("space_pages", ("cover_image_url", "content"), " AND deleted_at IS NULL"),
        )
        if not live:
            sources = (
                ("space_pages", ("cover_image_url", "content"), ""),
                ("page_edit_history", ("cover_image_url", "content"), ""),
                ("space_page_snapshots", ("body",), ""),
            )
        names: set[str] = set()
        for table, cols, extra in sources:
            rows = await self._db.fetchall(
                f"SELECT {', '.join(cols)} FROM {table} WHERE space_id = ?{extra}",
                (space_id,),
            )
            for r in rows_to_dicts(rows):
                for col in cols:
                    raw = r.get(col)
                    if col == "cover_image_url":
                        name = media_basename(raw if isinstance(raw, str) else None)
                        if name:
                            names.add(name)
                    elif col == "body":
                        names |= _names_in_side(raw)
                    else:
                        names |= _names_in_markdown(raw)
        return {space_page_media_name(space_id, n) for n in names}

    async def space_files(self, space_id: str) -> list[str]:
        """Every file a row of space *space_id* names in a registry column
        (:func:`space_media_columns`, live or not), each once — what a hard
        purge of the space may free (``unlink_unreferenced`` still keeps a
        file another row names). Only a canonical, non-reserved space file
        (:func:`_space_file`); a page's raw names only on the space's host.
        Not covered: a page's history (``page_edit_history``) and open
        conflict sides on the host — the orphan sweep reclaims those once
        the purge drops their rows."""
        if not space_id:
            return []
        out: list[str] = []
        seen: set[str] = set()
        gfs_copies = {
            str(r["n"])
            for r in await self._db.fetchall(
                "SELECT local_name AS n FROM space_media_refs WHERE space_id = ?",
                (space_id,),
            )
        }
        for table, col, shape in space_media_columns():
            source, space, here = _SPACE_ROWS[table]
            bare = table in _BARE_NAME_TABLES
            value = f"t.{col}"
            if shape.startswith("json:"):
                value = (
                    f"CASE WHEN json_valid(t.{col})"
                    f" THEN json_extract(t.{col}, '{shape[len('json:') :]}') END"
                )
            rows = await self._db.fetchall(
                f"SELECT {value} AS v FROM {source} WHERE {space} = ?{here}",
                (space_id,),
            )
            for r in rows_to_dicts(rows):
                raw = r.get("v")
                refs: list[object]
                if shape == "list":
                    refs = list(_json_list(raw))
                elif shape == "markdown":
                    refs = list(page_body_media_refs(raw))
                else:
                    refs = [raw]
                for ref in refs:
                    name = _space_file(ref, bare=bare, gfs_copies=gfs_copies)
                    if name is not None and name not in seen:
                        seen.add(name)
                        out.append(name)
        for name in sorted(await self._page_names_of(space_id, live=False)):
            if name not in seen:
                seen.add(name)
                out.append(name)
        return out

    async def gfs_copies_in_space(
        self, space_id: str, names: Iterable[str]
    ) -> set[str]:
        """Those of *names* that are GFS reader copies (``sg-``) of items
        of space *space_id*: a ``space_media_refs`` row of the space names
        them. What a post delete may free besides its space files."""
        wanted = sorted({n for n in names if n and is_gfs_space_media_name(n)})
        if not space_id or not wanted:
            return set()
        marks = ", ".join("?" for _ in wanted)
        rows = await self._db.fetchall(
            "SELECT local_name AS n FROM space_media_refs"
            f" WHERE space_id = ? AND local_name IN ({marks})",
            (space_id, *wanted),
        )
        return {str(r["n"]) for r in rows}

    async def content_names_of_item(self, item_id: str) -> list[str]:
        """The content-addressed (``sc-``, v_85) files item *item_id* shows
        here, each once, in source order."""
        out: list[str] = []
        for source, key, _space, value, shape, _live, _owner in _SPACE_ITEM_FILES:
            rows = await self._db.fetchall(
                f"SELECT {value} AS v FROM {source} WHERE {key} = ?", (item_id,)
            )
            for r in rows:
                raw = r["v"]
                names = (
                    _names_in_list(raw)
                    if shape == "list"
                    else [n for n in (media_basename(raw),) if n]
                )
                for name in names:
                    if is_space_content_media_name(name) and name not in out:
                        out.append(name)
        return out

    async def is_referenced(self, basename: str) -> bool:
        """``True`` when any row still points at the file ``basename``
        (a page-scoped ``sp-`` name: when a space page here shows its file)."""
        if basename.startswith(SPACE_PAGE_MEDIA_PREFIX):
            return basename in await self._page_scoped_names()
        return await self._is_referenced(basename)

    async def _is_referenced(self, basename: str) -> bool:
        """``True`` when any row still points at the file ``basename``.

        A ``LIKE`` pre-filter narrows each column to candidate rows; the
        exact basename is then compared, so a file whose name is a suffix
        of another's never counts as referenced by it.
        """
        if not basename:
            return False
        pattern = f"%{_like_escape(basename)}%"
        for table, col, live in _SINGLE_COLUMNS:
            where = f"{col} LIKE ? ESCAPE '\\'" + (f" AND {live}" if live else "")
            rows = await self._db.fetchall(
                f"SELECT {col} AS v FROM {table} WHERE {where}", (pattern,)
            )
            if any(media_basename(r["v"]) == basename for r in rows):
                return True
        for table, col, live in _LIST_COLUMNS:
            where = f"{col} LIKE ? ESCAPE '\\'" + (f" AND {live}" if live else "")
            rows = await self._db.fetchall(
                f"SELECT {col} AS v FROM {table} WHERE {where}", (pattern,)
            )
            if any(basename in _names_in_list(r["v"]) for r in rows):
                return True
        for table, col, path, live in _JSON_FIELD_COLUMNS:
            rows = await self._db.fetchall(
                f"SELECT json_extract({col}, ?) AS v FROM {table} "
                f"WHERE {col} LIKE ? ESCAPE '\\' AND json_valid({col})"
                + (f" AND {live}" if live else ""),
                (path, pattern),
            )
            if any(media_basename(r["v"]) == basename for r in rows):
                return True
        for table, col, live in _PAYLOAD_SCANS:
            where = f"{col} LIKE ? ESCAPE '\\'" + (f" AND {live}" if live else "")
            rows = await self._db.fetchall(
                f"SELECT {col} AS v FROM {table} WHERE {where}", (pattern,)
            )
            if any(basename in _names_in_column(table, r["v"]) for r in rows):
                return True
        for table, col, live in _MARKDOWN_COLUMNS:
            where = f"{col} LIKE ? ESCAPE '\\'" + (f" AND {live}" if live else "")
            rows = await self._db.fetchall(
                f"SELECT {col} AS v FROM {table} WHERE {where}", (pattern,)
            )
            if any(basename in _names_in_markdown(r["v"]) for r in rows):
                return True
        return False
