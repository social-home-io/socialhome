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
  * ``space_posts.media_url`` + ``image_urls_json``
  * ``space_post_comments.media_url``
  * ``bazaar_listings.image_urls_json``
  * ``gallery_items.filename`` + ``thumbnail_filename`` (bare basenames)
  * ``highlight_frames.media_url``
  * ``moments.media_url``
  * ``calendar_events.cover_url`` / ``space_calendar_events.cover_url``
  * ``pages`` / ``space_pages`` / ``page_edit_history`` ``.cover_image_url``
  * ``task_attachments.url``
  * ``post_drafts.media_url``
  * ``feed_posts`` / ``space_posts`` ``.link_preview_json`` → ``$.thumbnail_url``
    (the re-encoded link-preview image; one file may back several posts
    that linked the same page)

  * ``space_moderation_queue.payload_json`` of PENDING items — a queued
    post / page / event / listing publishes its media only on approval
    (up to 7 days later), so until then the payload is the reference

A soft-deleted post keeps ``image_urls_json`` (only ``media_url`` is
cleared), so a post's image list counts only while the post is live.

NOT included (owned elsewhere / transient): DM ``.preview.webp`` /
``.part<NNNN>`` / ``.assembled`` intermediates and the ``.partial/``
staging dir — the sweep skips those by pattern; ``dm_gc`` owns them.
"""

from __future__ import annotations

import logging
import re
from typing import Protocol, runtime_checkable

import orjson

from ..db import AsyncDatabase
from ..media.cleanup import media_basename
from .base import rows_to_dicts

log = logging.getLogger(__name__)

#: ``(table, column, live-row filter)`` for single-reference columns.
_SINGLE_COLUMNS: tuple[tuple[str, str, str], ...] = (
    ("conversation_messages", "media_url", ""),
    ("feed_posts", "media_url", ""),
    ("post_comments", "media_url", ""),
    ("space_posts", "media_url", ""),
    ("space_post_comments", "media_url", ""),
    ("highlight_frames", "media_url", ""),
    ("moments", "media_url", ""),
    ("gallery_items", "filename", ""),
    ("gallery_items", "thumbnail_filename", ""),
    ("calendar_events", "cover_url", ""),
    ("space_calendar_events", "cover_url", ""),
    ("pages", "cover_image_url", ""),
    ("space_pages", "cover_image_url", ""),
    ("page_edit_history", "cover_image_url", ""),
    ("task_attachments", "url", ""),
    ("post_drafts", "media_url", ""),
)

#: ``(table, column, live-row filter)`` for JSON arrays of references.
_LIST_COLUMNS: tuple[tuple[str, str, str], ...] = (
    ("feed_posts", "image_urls_json", "deleted=0"),
    ("space_posts", "image_urls_json", "deleted=0"),
    ("bazaar_listings", "image_urls_json", ""),
)


#: ``(table, JSON column, JSON path)`` for a reference inside a JSON object.
#: Soft-deleting a post NULLs its ``link_preview_json``, so no live filter.
_JSON_FIELD_COLUMNS: tuple[tuple[str, str, str], ...] = (
    ("feed_posts", "link_preview_json", "$.thumbnail_url"),
    ("space_posts", "link_preview_json", "$.thumbnail_url"),
)


#: ``(table, text column, live-row filter)`` scanned for every
#: ``api/media/<file>`` inside a free-form JSON payload.
_PAYLOAD_SCANS: tuple[tuple[str, str, str], ...] = (
    ("space_moderation_queue", "payload_json", "status='pending'"),
)
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

#: The writer holds a seat in the space ``{col}`` names.
_SEAT = (
    "EXISTS (SELECT 1 FROM space_members sm WHERE sm.space_id = {col}"
    " AND sm.user_id = me.uid)"
)
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
    ("feed_posts", "media_url", "one", "1"),
    ("feed_posts", "image_urls_json", "list", "1"),
    ("feed_posts", "link_preview_json", "json:$.thumbnail_url", "1"),
    ("post_comments", "media_url", "one", "1"),
    ("space_posts", "media_url", "one", _SEAT.format(col="t.space_id")),
    ("space_posts", "image_urls_json", "list", _SEAT.format(col="t.space_id")),
    (
        "space_posts",
        "link_preview_json",
        "json:$.thumbnail_url",
        _SEAT.format(col="t.space_id"),
    ),
    (
        "space_post_comments",
        "media_url",
        "one",
        _SEAT.format(
            col="(SELECT p.space_id FROM space_posts p WHERE p.id = t.post_id)"
        ),
    ),
    ("bazaar_listings", "image_urls_json", "list", _SEAT.format(col="t.space_id")),
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
    ("calendar_events", "cover_url", "one", "1"),
    ("space_calendar_events", "cover_url", "one", _SEAT.format(col="t.space_id")),
    ("pages", "cover_image_url", "one", "1"),
    ("space_pages", "cover_image_url", "one", _SEAT.format(col="t.space_id")),
    (
        "page_edit_history",
        "cover_image_url",
        "one",
        "(t.space_id IS NULL OR " + _SEAT.format(col="t.space_id") + ")",
    ),
    ("task_attachments", "url", "one", "t.uploaded_by = me.uid"),
    ("post_drafts", "media_url", "one", "t.username = me.uname"),
    (
        "space_moderation_queue",
        "payload_json",
        "payload",
        "t.status = 'pending' AND t.submitted_by = me.uid",
    ),
)

#: A gallery item's file (bare basenames): a household album open to
#: everyone, a seat in a space album's space — and for a private album
#: (``chosen``) only its uploader (``private`` marks such a row).
_GALLERY_ACCESS_SQL = (
    "SELECT i.filename AS f, i.thumbnail_filename AS th,"
    " CASE WHEN a.space_id IS NOT NULL THEN "
    + _SEAT.format(col="a.space_id")
    + " WHEN a.audience = 'chosen' THEN i.uploaded_by = me.uid ELSE 1 END AS ok,"
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


def _like_escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


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
        for table, col, path in _JSON_FIELD_COLUMNS:
            rows = await self._db.fetchall(
                f"SELECT json_extract({col}, ?) AS v FROM {table} "
                f"WHERE {col} IS NOT NULL AND json_valid({col})",
                (path,),
            )
            for r in rows_to_dicts(rows):
                name = media_basename(r.get("v"))
                if name:
                    out.add(name)
        for table, col, live in _PAYLOAD_SCANS:
            rows = await self._db.fetchall(
                f"SELECT {col} AS v FROM {table} WHERE {col} IS NOT NULL AND {live}"
            )
            for r in rows_to_dicts(rows):
                out.update(_names_in_payload(r.get("v")))
        return out

    async def may_reference(self, basename: str, user_id: str) -> bool:
        if not basename:
            return False
        me = (user_id, user_id)
        rows = await self._db.fetchall(
            _ME_CTE + _GALLERY_ACCESS_SQL, (*me, basename, basename)
        )
        referenced = allowed = False
        for r in rows_to_dicts(rows):
            if r.get("private"):
                return False  # a private album's file: its uploader only
            referenced = True
            allowed = allowed or bool(r.get("ok"))
        pattern = f"%{_like_escape(basename)}%"
        for table, col, shape, access in _ACCESS_SOURCES:
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
                raw = r.get("v")
                if shape == "list":
                    hit = basename in _names_in_list(raw)
                elif shape == "payload":
                    hit = basename in _names_in_payload(raw)
                else:
                    hit = media_basename(raw) == basename
                if hit:
                    referenced = True
                    allowed = allowed or bool(r.get("ok"))
            if allowed:
                return True
        return allowed or not referenced

    async def is_referenced(self, basename: str) -> bool:
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
        for table, col, path in _JSON_FIELD_COLUMNS:
            rows = await self._db.fetchall(
                f"SELECT json_extract({col}, ?) AS v FROM {table} "
                f"WHERE {col} LIKE ? ESCAPE '\\' AND json_valid({col})",
                (path, pattern),
            )
            if any(media_basename(r["v"]) == basename for r in rows):
                return True
        for table, col, live in _PAYLOAD_SCANS:
            rows = await self._db.fetchall(
                f"SELECT {col} AS v FROM {table} "
                f"WHERE {col} LIKE ? ESCAPE '\\' AND {live}",
                (pattern,),
            )
            if any(basename in _names_in_payload(r["v"]) for r in rows):
                return True
        return False
