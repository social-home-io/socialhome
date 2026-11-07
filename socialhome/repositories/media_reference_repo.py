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
from ..domain.media_constraints import SPACE_DIRECT_MEDIA_PREFIX
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
    ("calendar_events", "cover_url", "one", _HOUSEHOLD),
    ("space_calendar_events", "cover_url", "one", _SEAT.format(col="t.space_id")),
    ("pages", "cover_image_url", "one", _HOUSEHOLD),
    ("space_pages", "cover_image_url", "one", _SEAT.format(col="t.space_id")),
    (
        "page_edit_history",
        "cover_image_url",
        "one",
        f"((t.space_id IS NULL AND {_HOUSEHOLD}) OR "
        + _SEAT.format(col="t.space_id")
        + ")",
    ),
    ("task_attachments", "url", "one", "t.uploaded_by = me.uid"),
    ("post_drafts", "media_url", "one", "t.username = me.uname"),
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
#: table, column, shape, and the condition (first ``?`` = the space id).
_LIVE_SPACE_SOURCES: tuple[tuple[str, str, str, str], ...] = (
    ("space_posts", "media_url", "one", "t.space_id = ? AND t.deleted = 0"),
    ("space_posts", "image_urls_json", "list", "t.space_id = ? AND t.deleted = 0"),
    (
        "space_posts",
        "link_preview_json",
        "json:$.thumbnail_url",
        "t.space_id = ? AND t.deleted = 0",
    ),
    (
        "space_post_comments",
        "media_url",
        "one",
        (
            "t.deleted = 0 AND (SELECT p.space_id FROM space_posts p"
            " WHERE p.id = t.post_id AND p.deleted = 0) = ?"
        ),
    ),
    (
        "bazaar_listings",
        "image_urls_json",
        "list",
        (
            "t.space_id = ? AND EXISTS (SELECT 1 FROM space_posts p"
            " WHERE p.id = t.post_id AND p.deleted = 0)"
        ),
    ),
    (
        "space_calendar_events",
        "cover_url",
        "one",
        "t.space_id = ? AND t.deleted_at IS NULL",
    ),
    (
        "space_pages",
        "cover_image_url",
        "one",
        "t.space_id = ? AND t.deleted_at IS NULL",
    ),
)

#: The space each :data:`_ACCESS_SOURCES` table's row belongs to (an SQL
#: expression over the row ``t``); a table not listed is household-level
#: (``NULL``) — never a space's content (:meth:`confined_to_space`).
_SPACE_OF: dict[str, str] = {
    "space_posts": "t.space_id",
    "space_post_comments": (
        "(SELECT p.space_id FROM space_posts p WHERE p.id = t.post_id)"
    ),
    "bazaar_listings": "t.space_id",
    "space_calendar_events": "t.space_id",
    "space_pages": "t.space_id",
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

    async def confined_to_space(self, basename: str, space_id: str) -> bool:
        """Whether *basename* is referenced only by content of *space_id*
        (what the GFS media cache may seal under that space's key)."""
        ...

    async def live_in_space(self, basename: str, space_id: str) -> bool:
        """Whether a LIVE row of *space_id* shows *basename* now (not
        deleted, not only held for review) — what a household sends on
        demand (v_83)."""
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
            rows = await self._db.fetchall(
                f"SELECT {value} AS v, {space_of} AS s FROM {table} t"
                f" WHERE t.{col} LIKE ? ESCAPE '\\'{guard}",
                (pattern,),
            )
            for r in rows_to_dicts(rows):
                raw = r.get("v")
                if shape == "list":
                    hit = basename in _names_in_list(raw)
                elif shape == "payload":
                    hit = basename in _names_in_payload(raw)
                else:
                    hit = media_basename(raw) == basename
                if not hit:
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
        so a deleted item's picture is never sent again."""
        if not basename or not space_id:
            return False
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
                raw = r.get("v")
                if shape == "list":
                    hit = basename in _names_in_list(raw)
                else:
                    hit = media_basename(raw) == basename
                if hit:
                    return True
        return False

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
