"""Gallery repository — albums + items (§23.119)."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Protocol, runtime_checkable

from ..db import AsyncDatabase
from ..domain.gallery import (
    AUDIENCE_CHOSEN,
    AUDIENCE_EVERYONE,
    AUDIENCES,
    GalleryAlbum,
    GalleryAlbumAudience,
    GalleryDuplicate,
    GalleryFavourite,
    GalleryItem,
    GalleryTrashItem,
    ItemCursor,
    ItemSort,
    LockedTrashAlbum,
)
from ..domain.household_dashboard import DashboardPhoto, FrameAlbumChoice
from ..domain.tombstone import SpaceRowTombstone
from .base import changed_since_sql, rows_to_dicts, sync_page_cursor
from .cp_repo import GUARDIAN_BLOCK_COUNTERPARTS_SQL

#: Items whose uploader the viewer may see: not on the viewer's personal
#: block list (§Privacy) and not across a guardian block with them
#: (§CP.F2, both directions) — the rule the feed applies to the posts the
#: system "Posts" album mirrors. Binds the viewer three times.
_VIEWER_MAY_SEE_UPLOADER_SQL = f"""
    AND uploaded_by NOT IN (
        SELECT blocked_user_id FROM user_blocks WHERE blocker_user_id = ?
    )
    AND uploaded_by NOT IN ({GUARDIAN_BLOCK_COUNTERPARTS_SQL})
"""

#: What a gallery item tombstone keeps of its row (migration 0085): no
#: file, no caption, no date — the media reference scan then sees no file
#: of it, so the delete path unlinks the blobs. Shared with the 0085
#: album-tombstone trigger, which applies the same blanking.
_ITEM_TOMBSTONE_SET = (
    "deleted_at=datetime('now'), deleted_by=?, filename='',"
    " thumbnail_filename='', width=0, height=0, duration_s=NULL,"
    " caption=NULL, taken_at=NULL"
)

#: A household album the viewer may see (migration 0139, D7) — the ONE
#: rule every read of a household photo goes through (album pages, items,
#: files, trash, favourites, downloads, share, notifications, duplicates,
#: search): every album open to everyone at home, the viewer's own, the
#: private albums they were chosen for — and every private album of a
#: protected child (§CP) the viewer is a guardian of, whatever the child
#: chose (read-time, so a guardian added later sees it, and a child can't
#: take one out). Expects the album aliased ``a``; binds the viewer twice.
_ALBUM_VISIBLE_TO_SQL = (
    "(a.audience = 'everyone' OR a.owner_user_id = ?"
    " OR ? IN (SELECT aa.user_id FROM gallery_album_audience aa"
    " WHERE aa.album_id = a.id"
    " UNION ALL SELECT g.guardian_user_id FROM cp_guardians g"
    " JOIN users m ON m.user_id = g.minor_user_id"
    " WHERE g.minor_user_id = a.owner_user_id"
    " AND m.child_protection_enabled = 1))"
)
#: The public name of :data:`_ALBUM_VISIBLE_TO_SQL` for other repos.
ALBUM_VISIBLE_TO_SQL = _ALBUM_VISIBLE_TO_SQL

#: The day an album sorts an item by — the day taken, else the upload day.
#: Spelled exactly as ``idx_gallery_items_album_taken`` (migration 0139)
#: so SQLite serves the date order from that index.
_ITEM_DAY_SQL = "COALESCE(taken_at, substr(created_at, 1, 10))"

#: An album's newest activity: its newest live item's upload time, else
#: the album's own creation. Expects the album aliased ``a``.
_ALBUM_ACTIVITY_SQL = (
    "COALESCE((SELECT MAX(i.created_at) FROM gallery_items i"
    " WHERE i.album_id = a.id AND i.deleted_at IS NULL), a.created_at)"
)

#: Items whose uploader the viewer may see, for a query that aliases the
#: items ``i`` — :data:`_VIEWER_MAY_SEE_UPLOADER_SQL` qualified.
_VIEWER_MAY_SEE_I_UPLOADER_SQL = _VIEWER_MAY_SEE_UPLOADER_SQL.replace(
    "AND uploaded_by NOT IN", "AND i.uploaded_by NOT IN"
)

#: A household item in the trash (migration 0139): deleted, files kept —
#: a space tombstone (0085) blanks its files. Expects ``i`` and ``a``.
_IN_TRASH_SQL = "a.space_id IS NULL AND i.deleted_at IS NOT NULL AND i.filename <> ''"


def _utc_now_sql() -> str:
    """Now as SQLite's ``datetime('now')`` writes it (naive UTC) — the
    shape of every ``deleted_at``, so trash rows compare as strings."""
    return datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S")


#: What an album tombstone keeps of its row: no name, description or cover.
_ALBUM_TOMBSTONE_SET = (
    "deleted_at=datetime('now'), deleted_by=?, name='', description=NULL,"
    " cover_item_id=NULL, item_count=0"
)


@runtime_checkable
class AbstractGalleryRepo(Protocol):
    async def list_albums(
        self,
        space_id: str | None,
        *,
        limit: int = 30,
        before: str | None = None,
        before_id: str | None = None,
        by_activity: bool = False,
        viewer_user_id: str | None = None,
    ) -> list[GalleryAlbum]: ...
    async def get_album(self, album_id: str) -> GalleryAlbum | None: ...
    async def get_system_album(self, space_id: str | None) -> GalleryAlbum | None: ...
    async def create_album(
        self, album: GalleryAlbum, *, audience_user_ids: frozenset[str] = frozenset()
    ) -> GalleryAlbum:
        """Insert the album — a private one (``album.audience`` ``chosen``)
        together with its people in one transaction, so it is never
        listed to everyone for a moment."""
        ...

    async def update_album(self, album_id: str, patch: dict) -> None: ...
    async def delete_album(self, album_id: str, *, deleted_by: str = "") -> None: ...
    async def create_album_in_space(
        self, album: GalleryAlbum, *, space_id: str
    ) -> bool: ...
    async def update_album_in_space(
        self, album_id: str, patch: dict, *, space_id: str
    ) -> bool: ...
    async def delete_album_in_space(
        self, album_id: str, *, space_id: str, deleted_by: str = ""
    ) -> bool: ...
    async def list_album_media(self, album_id: str) -> list[str]: ...
    async def has_items_by_others(self, album_id: str, user_id: str) -> bool: ...
    async def newest_by_others(
        self, album_ids: list[str], viewer_user_id: str
    ) -> dict[str, str]:
        """Per album, when someone other than the viewer last added a live
        item the viewer may see (D9's "neu" dot); albums with none are
        left out."""
        ...

    async def list_items(
        self,
        album_id: str,
        *,
        limit: int = 50,
        before: str | None = None,
        before_id: str | None = None,
        viewer_user_id: str | None = None,
        sort: ItemSort = ItemSort.ADDED,
        after: ItemCursor | None = None,
    ) -> list[GalleryItem]: ...
    async def find_on_this_day(
        self,
        month_days: tuple[str, ...],
        *,
        before_year: str,
        viewer_user_id: str,
        excluded_album_ids: frozenset[str] = frozenset(),
    ) -> tuple[str, str, int] | None: ...
    async def list_on_this_day_items(
        self,
        album_id: str,
        *,
        year: str,
        month_days: tuple[str, ...],
        viewer_user_id: str,
        limit: int = 3,
    ) -> list[GalleryItem]: ...
    async def list_items_since(
        self,
        space_id: str,
        since: str,
        *,
        limit: int = 500,
    ) -> list[GalleryItem]: ...
    async def list_albums_sync_page(
        self,
        space_id: str,
        *,
        cursor: int | None = None,
        limit: int = 200,
        since: int | None = None,
    ) -> tuple[list[GalleryAlbum], int | None]:
        """One page of the space's albums (the system album included) for a
        §25.6 sync. ``(rows, next_cursor)`` paging, keyset on the row id."""
        ...

    async def list_items_sync_page(
        self,
        space_id: str,
        *,
        cursor: int | None = None,
        limit: int = 200,
        since: int | None = None,
    ) -> tuple[list[GalleryItem], int | None]:
        """One page of the space's own gallery items for a §25.6 sync —
        never a mirror of a post's media (``source_post_id``; the receiver
        re-creates those from the post). Every item, whatever its age:
        nothing prunes gallery items (the retention sweep touches posts and
        chat only), so a window would hide photos the space still shows.
        ``(rows, next_cursor)`` paging, keyset on the row id."""
        ...

    async def get_item(self, item_id: str) -> GalleryItem | None: ...
    async def list_items_by_source_post(
        self,
        post_id: str,
    ) -> list[GalleryItem]: ...
    async def create_item(
        self, item: GalleryItem, *, content_sha256: str | None = None
    ) -> GalleryItem: ...
    async def delete_item(self, item_id: str, *, deleted_by: str = "") -> None: ...
    async def create_item_in_space(
        self,
        item: GalleryItem,
        *,
        space_id: str,
        bump_count: bool = True,
    ) -> bool: ...
    async def delete_item_in_space(
        self, item_id: str, *, space_id: str, deleted_by: str = ""
    ) -> bool: ...
    async def delete_items_by_source_post(
        self,
        post_id: str,
    ) -> tuple[str | None, int]: ...
    async def increment_item_count(self, album_id: str, delta: int) -> None: ...
    async def recount_items(self, album_id: str) -> int: ...
    async def get_first_item_thumbnail(
        self, album_id: str, *, viewer_user_id: str | None = None
    ) -> str | None: ...
    async def count_visible_items(self, album_id: str, viewer_user_id: str) -> int: ...
    async def list_space_item_filenames(self, space_id: str) -> list[str]: ...
    async def set_retention_exempt(
        self,
        album_id: str,
        exempt: bool,
        *,
        space_id: str | None = None,
    ) -> None: ...

    # ── Library model (migration 0139) ─────────────────────────────────
    async def find_household_duplicate(
        self, content_sha256: str, *, viewer_user_id: str
    ) -> GalleryDuplicate | None:
        """A household item (live first, else in the trash) whose uploaded
        bytes hashed to ``content_sha256`` — only among the albums
        *viewer_user_id* may see, so the lookup tells nobody about a
        private album's photo."""
        ...

    async def trash_item(self, item_id: str, *, deleted_by: str) -> bool:
        """Move a live household item to the trash (files kept)."""
        ...

    async def trash_album(self, album_id: str, *, deleted_by: str) -> list[str]:
        """Move a household user album and its live items to the trash;
        the ids of the items trashed with it."""
        ...

    async def restore_item(self, item_id: str) -> str | None:
        """Bring a household item back from the trash — and its album, if
        that was trashed too. The album id, or ``None`` if not in the trash."""
        ...

    async def get_trashed_item(
        self, item_id: str, *, viewer_user_id: str | None = None
    ) -> GalleryTrashItem | None:
        """A household item in the trash — with ``viewer_user_id``, only
        if that person may see its album."""
        ...

    async def list_trash(
        self,
        *,
        viewer_user_id: str,
        limit: int = 100,
        before_id: str | None = None,
    ) -> list[GalleryTrashItem]: ...
    async def purge_trashed_item(self, item_id: str) -> list[str]:
        """Delete one trashed household item for good; the ``api/media/``
        references it held (empty when it was not in the trash)."""
        ...

    async def purge_expired_trash(self, cutoff: str, *, limit: int = 500) -> list[str]:
        """Delete up to ``limit`` household items trashed before ``cutoff``
        (naive UTC) and the trashed albums left empty; the ``api/media/``
        references the purged items held."""
        ...

    async def reassign_household_albums(
        self, from_user_id: str, to_user_id: str
    ) -> int:
        """Hand every household album *from_user_id* owns and everyone at
        home sees (live or in the trash) to *to_user_id*. Private albums
        stay theirs. How many moved."""
        ...

    async def update_item(self, item_id: str, patch: dict) -> bool:
        """Set a live household item's ``caption`` / ``taken_at`` (other
        keys are ignored). ``False`` for an unknown, trashed or space item."""
        ...

    async def move_item(self, item_id: str, to_album_id: str) -> str | None:
        """Move a live household item into another live household user
        album, recounting both and dropping it as the old album's cover —
        in one transaction. The album it left, or ``None`` when nothing
        moved (unknown item, a space or system album on either side)."""
        ...

    async def get_trashed_album(self, album_id: str) -> GalleryAlbum | None:
        """A household user album in the trash, or ``None``."""
        ...

    async def restore_album(self, album_id: str) -> list[str]:
        """Bring a trashed household album back with the photos that went
        to the trash with it (the same ``deleted_at`` stamp) — not the ones
        deleted on their own before. The restored item ids."""
        ...

    async def list_household_uploads(
        self, uploader_user_id: str, *, limit: int
    ) -> list[GalleryItem]:
        """The live household photos and videos *uploader_user_id* added to
        user albums (not the feed's "Posts" album), oldest first."""
        ...

    async def get_album_audience(
        self, album_id: str, *, include_trashed: bool = False
    ) -> GalleryAlbumAudience | None:
        """Who sees a household album: its audience, chosen people, owner
        and — for a protected owner — guardians. ``None`` for an unknown
        (or, unless *include_trashed*, a trashed) album."""
        ...

    async def album_uploaders(self, album_id: str) -> frozenset[str]:
        """Everyone who has a photo or video in the album — live or in the
        trash (a restore brings it back to the same audience)."""
        ...

    async def list_private_albums_of(self, owner_user_id: str) -> list[str]:
        """The live private (``chosen``) household albums *owner_user_id*
        owns."""
        ...

    async def list_locked_trash(self, viewer_user_id: str) -> list[LockedTrashAlbum]:
        """Per private album *viewer_user_id* can't see: how many of its
        photos wait in the trash (an admin's locked tiles, D7) — no names,
        no previews."""
        ...

    async def guardian_block_counterparts(self, user_id: str) -> frozenset[str]:
        """Everyone a guardian block separates from *user_id*, either way
        (§CP.F2) — never chosen for their private album."""
        ...

    async def media_hidden_from(self, filename: str, viewer_user_id: str) -> bool:
        """Whether the stored file *filename* belongs to a photo or video of
        a private album *viewer_user_id* may not see (and didn't upload) —
        the file route then answers like an unknown file."""
        ...

    async def set_album_audience(
        self, album_id: str, audience: str, user_ids: frozenset[str] = frozenset()
    ) -> bool:
        """Set a live household album's audience (``chosen`` + people, or
        ``everyone``, which clears the people). ``False`` for an unknown,
        space or system album."""
        ...

    async def album_visible_to(self, album_id: str, user_id: str) -> bool: ...
    async def add_favourite(self, user_id: str, item_id: str) -> bool: ...
    async def remove_favourite(self, user_id: str, item_id: str) -> None: ...
    async def favourite_ids_among(
        self, user_id: str, item_ids: list[str]
    ) -> frozenset[str]: ...
    async def list_favourite_items(
        self,
        user_id: str,
        *,
        space_ids: frozenset[str] = frozenset(),
        limit: int = 100,
        before: str | None = None,
        before_id: str | None = None,
    ) -> list[GalleryFavourite]:
        """One page of *user_id*'s live favourites they may still see, last
        marked first: household albums open to them
        (:data:`_ALBUM_VISIBLE_TO_SQL`) and the albums of *space_ids* (the
        spaces they hold a seat in — the caller decides). The next page is
        ``before`` / ``before_id`` = the last row's ``marked_at`` / item id.
        """
        ...

    # ── Tombstones (migration 0085, §25.6 ``gallery_*_deleted``) ──────
    async def is_album_deleted(self, album_id: str, *, space_id: str) -> bool: ...
    async def is_item_deleted(self, item_id: str, *, space_id: str) -> bool: ...
    async def tombstone_album(
        self,
        album_id: str,
        *,
        space_id: str,
        owner_user_id: str,
        created_at: str = "",
        deleted_by: str = "",
    ) -> bool: ...
    async def tombstone_item(
        self,
        item_id: str,
        *,
        space_id: str,
        album_id: str,
        uploaded_by: str,
        created_at: str = "",
        deleted_by: str = "",
    ) -> bool: ...
    async def list_album_tombstones_page(
        self,
        space_id: str,
        *,
        cursor: int | None = None,
        limit: int = 200,
        since: int | None = None,
    ) -> tuple[list[SpaceRowTombstone], int | None]: ...
    async def list_item_tombstones_page(
        self,
        space_id: str,
        *,
        cursor: int | None = None,
        limit: int = 200,
        since: int | None = None,
    ) -> tuple[list[SpaceRowTombstone], int | None]: ...
    async def list_album_tombstones_since(
        self, space_id: str, since: str
    ) -> list[SpaceRowTombstone]: ...
    async def list_frame_albums(self) -> list[FrameAlbumChoice]:
        """The albums the wall's photo screensaver may show: live
        household albums everyone at home sees, with their live photos
        counted (``default_name`` is the caller's to set)."""
        ...

    async def list_frame_photos(
        self, album_ids: Sequence[str], *, limit: int
    ) -> list[DashboardPhoto]:
        """The newest *limit* live photos (by day taken) of those of
        *album_ids* that are household albums everyone at home sees."""
        ...

    async def get_frame_photo(
        self, item_id: str, album_ids: Sequence[str]
    ) -> DashboardPhoto | None:
        """The live photo *item_id* if it sits in one of *album_ids* and
        that album is a household album everyone at home sees."""
        ...


#: The screensaver's rule for an album (aliased ``a``): household, live,
#: open to everyone at home — never a private (``chosen``) or space album.
_FRAME_ALBUM_SQL = "a.space_id IS NULL AND a.deleted_at IS NULL AND a.audience = ?"
#: …and for an item (aliased ``i``): a live photo with its file.
_FRAME_ITEM_SQL = "i.deleted_at IS NULL AND i.item_type = 'photo' AND i.filename <> ''"
_FRAME_PHOTO_COLS = (
    "i.id AS id, i.album_id AS album_id, a.name AS album_name,"
    " i.filename AS filename, i.width AS width, i.height AS height,"
    " COALESCE(i.taken_at, substr(i.created_at, 1, 10)) AS day"
)


def _frame_photo(r: dict) -> DashboardPhoto:
    return DashboardPhoto(
        id=r["id"],
        album_id=r["album_id"],
        album_name=r["album_name"],
        filename=r["filename"],
        width=int(r["width"]),
        height=int(r["height"]),
        day=str(r["day"] or "")[:10],
    )


class SqliteGalleryRepo:
    """SQLite-backed :class:`AbstractGalleryRepo`."""

    _ALBUM_PATCH_ALLOWED = frozenset({"name", "description", "cover_item_id"})

    def __init__(self, db: AsyncDatabase) -> None:
        self._db = db

    # ─── Albums ───────────────────────────────────────────────────────────

    def _row_to_album(self, r: dict) -> GalleryAlbum:
        return GalleryAlbum(
            id=r["id"],
            space_id=r.get("space_id"),
            owner_user_id=r.get("owner_user_id"),
            name=r["name"],
            description=r.get("description"),
            cover_item_id=r.get("cover_item_id"),
            item_count=int(r.get("item_count") or 0),
            retention_exempt=bool(r.get("retention_exempt")),
            is_system=bool(r.get("is_system")),
            cover_url=None,  # filled in by service
            created_at=r.get("created_at"),
            updated_at=r.get("updated_at"),
            activity_at=r.get("activity_at") or r.get("created_at"),
            audience=r.get("audience") or AUDIENCE_EVERYONE,
        )

    async def list_albums(
        self,
        space_id: str | None,
        *,
        limit: int = 30,
        before: str | None = None,
        before_id: str | None = None,
        by_activity: bool = False,
        viewer_user_id: str | None = None,
    ) -> list[GalleryAlbum]:
        """One page of a scope's albums: the system album pinned first,
        then newest first (``created_at DESC, id DESC`` — ``id`` breaks a
        tie, so no album is skipped or repeated between pages).

        ``by_activity`` orders by newest activity instead —
        :data:`_ALBUM_ACTIVITY_SQL`, the newest live item's upload time
        (else the album's creation) — and the cursor is that time
        (:attr:`GalleryAlbum.activity_at`).

        The next page is ``before`` / ``before_id`` = the last *regular*
        album's ``created_at`` (``activity_at``) / ``id``; a page after
        the first never carries the system album again. ``before`` alone
        (an older client) keeps its plain ``<`` meaning.

        ``viewer_user_id`` keeps a household page to the albums that person
        may see (:data:`_ALBUM_VISIBLE_TO_SQL`); without it every album of
        the scope is listed (a space's seats decide in the service, and a
        household admin's list shows private albums as locked tiles).
        """
        limit = max(1, min(limit, 200))
        key = _ALBUM_ACTIVITY_SQL if by_activity else "a.created_at"
        # ``space_id IS ?`` matches both NULL (household) and a specific id.
        visible = ""
        params: list = [space_id]
        if viewer_user_id is not None and space_id is None:
            visible = " AND " + _ALBUM_VISIBLE_TO_SQL
            params += [viewer_user_id, viewer_user_id]
        sql = (
            f"SELECT * FROM (SELECT a.*, {key} AS activity_at"
            " FROM gallery_albums a"
            f" WHERE a.space_id IS ? AND a.deleted_at IS NULL{visible})"
        )
        if before:
            sql += " WHERE is_system = 0"
            if before_id:
                sql += " AND (activity_at < ? OR (activity_at = ? AND id < ?))"
                params += [before, before, before_id]
            else:
                sql += " AND activity_at < ?"
                params.append(before)
        sql += " ORDER BY is_system DESC, activity_at DESC, id DESC LIMIT ?"
        params.append(limit)
        rows = await self._db.fetchall(sql, tuple(params))
        return [self._row_to_album(r) for r in rows_to_dicts(rows)]

    async def get_system_album(
        self,
        space_id: str | None,
    ) -> GalleryAlbum | None:
        """Return the per-scope system album (or ``None`` before lazy create).

        ``space_id IS ?`` matches NULL (household) just like
        :meth:`list_albums`. The partial unique index on
        ``(space_id, is_system) WHERE is_system=1`` guarantees at most
        one row per scope.
        """
        row = await self._db.fetchone(
            "SELECT * FROM gallery_albums WHERE space_id IS ? AND is_system=1",
            (space_id,),
        )
        return self._row_to_album(dict(row)) if row else None

    async def get_album(self, album_id: str) -> GalleryAlbum | None:
        row = await self._db.fetchone(
            "SELECT * FROM gallery_albums WHERE id=? AND deleted_at IS NULL",
            (album_id,),
        )
        return self._row_to_album(dict(row)) if row else None

    async def create_album(
        self, album: GalleryAlbum, *, audience_user_ids: frozenset[str] = frozenset()
    ) -> GalleryAlbum:
        now = album.created_at or datetime.now(UTC).isoformat()
        # ``ON CONFLICT DO NOTHING`` is the race guard for the system
        # album path: the partial unique index on (space_id, is_system)
        # prevents two concurrent ``ensure_system_album`` callers from
        # both inserting. User-created albums never collide here (they
        # have ``is_system=0``, which the partial index ignores).
        params = (
            album.id,
            album.space_id,
            int(album.retention_exempt),
            int(album.is_system),
            album.owner_user_id,
            album.name,
            album.description,
            album.cover_item_id,
            album.item_count,
            now,
            now,
            album.audience,
        )
        people = sorted(audience_user_ids) if album.audience == AUDIENCE_CHOSEN else []

        def _run(conn) -> None:
            cur = conn.execute(
                """
                INSERT INTO gallery_albums(
                    id, space_id, retention_exempt, is_system, owner_user_id,
                    name, description, cover_item_id, item_count, created_at,
                    updated_at, audience
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT DO NOTHING
                """,
                params,
            )
            if cur.rowcount and people:
                conn.executemany(
                    "INSERT INTO gallery_album_audience(album_id, user_id)"
                    " VALUES(?, ?)",
                    [(album.id, uid) for uid in people],
                )

        await self._db.transact(_run)
        return album

    async def update_album(self, album_id: str, patch: dict) -> None:
        safe = {k: v for k, v in patch.items() if k in self._ALBUM_PATCH_ALLOWED}
        if not safe:
            return
        set_clause = ", ".join(f"{k}=?" for k in safe)
        await self._db.enqueue(
            f"UPDATE gallery_albums SET {set_clause}, updated_at=?"
            " WHERE id=? AND deleted_at IS NULL",
            (*safe.values(), datetime.now(UTC).isoformat(), album_id),
        )

    async def delete_album(self, album_id: str, *, deleted_by: str = "") -> None:
        """Delete an album and its items. A household album (and its items,
        by cascade) is removed; a space album is tombstoned (migration 0085)
        and its items with it (the 0085 trigger) — the §25.6
        ``gallery_albums_deleted`` resource streams it."""

        def _run(conn) -> None:
            conn.execute(
                "DELETE FROM gallery_albums WHERE id=? AND space_id IS NULL",
                (album_id,),
            )
            conn.execute(
                f"UPDATE gallery_albums SET {_ALBUM_TOMBSTONE_SET}"
                " WHERE id=? AND space_id IS NOT NULL AND deleted_at IS NULL",
                (deleted_by or None, album_id),
            )

        await self._db.transact(_run)

    # ─── §24.11 space-scoped album writes (federation inbound) ────────────

    async def create_album_in_space(
        self, album: GalleryAlbum, *, space_id: str
    ) -> bool:
        """Mirror a member's album into ``space_id`` (federation).

        The album is filed under the gated ``space_id`` — never the
        payload's own — as an empty, non-system album: its item count
        grows with the items that follow, and the system "Posts" album is
        rebuilt locally from posts, never taken off the wire. A redelivered
        id in this space for the same owner is an idempotent no-op
        (``True``, row untouched); an id already used by an album of another
        space, of the household gallery, or of another owner is never taken
        over (``False``).
        """
        now = album.created_at or datetime.now(UTC).isoformat()

        def _run(conn) -> bool:
            row = conn.execute(
                "SELECT space_id, owner_user_id, deleted_at FROM gallery_albums"
                " WHERE id=?",
                (album.id,),
            ).fetchone()
            if row is not None:
                # A tombstone is never taken over: the album stays deleted.
                return (
                    row[0] == space_id
                    and row[1] == album.owner_user_id
                    and row[2] is None
                )
            conn.execute(
                """
                INSERT INTO gallery_albums(
                    id, space_id, retention_exempt, is_system, owner_user_id,
                    name, description, cover_item_id, item_count,
                    created_at, updated_at
                ) VALUES(?, ?, 0, 0, ?, ?, ?, NULL, 0, ?, ?)
                ON CONFLICT DO NOTHING
                """,
                (
                    album.id,
                    space_id,
                    album.owner_user_id,
                    album.name,
                    album.description,
                    now,
                    album.updated_at or now,
                ),
            )
            return True

        return bool(await self._db.transact(_run))

    async def update_album_in_space(
        self, album_id: str, patch: dict, *, space_id: str
    ) -> bool:
        """Apply a member's album edit — only to a user album of ``space_id``.

        ``False`` for an album of another space, the household gallery, or
        the system album (which nobody renames). A ``cover_item_id`` naming
        an item of *another* album is dropped from the patch (the cover
        would render some other album's picture) rather than failing the
        rest of the edit; one naming an item not held here yet is kept —
        the edit can overtake the upload, and the cover is rendered only
        once that item is in this album. ``None`` clears the cover.
        """
        safe = {k: v for k, v in patch.items() if k in self._ALBUM_PATCH_ALLOWED}

        def _run(conn) -> bool:
            if (
                conn.execute(
                    "SELECT 1 FROM gallery_albums"
                    " WHERE id=? AND space_id=? AND is_system=0"
                    " AND deleted_at IS NULL",
                    (album_id, space_id),
                ).fetchone()
                is None
            ):
                return False
            cover = safe.get("cover_item_id")
            if cover is not None:
                held = conn.execute(
                    "SELECT album_id FROM gallery_items WHERE id=?", (cover,)
                ).fetchone()
                if held is not None and held[0] != album_id:
                    safe.pop("cover_item_id")
            if safe:
                # Only what changes — a re-applied edit (the §25.6 sync
                # re-applies the host's album every stream) writes nothing.
                current = conn.execute(
                    f"SELECT {', '.join(safe)} FROM gallery_albums WHERE id=?",
                    (album_id,),
                ).fetchone()
                changed = {
                    k: v for i, (k, v) in enumerate(safe.items()) if current[i] != v
                }
                if not changed:
                    return True
                set_clause = ", ".join(f"{k}=?" for k in changed)
                conn.execute(
                    f"UPDATE gallery_albums SET {set_clause}, updated_at=?"
                    " WHERE id=? AND space_id=?",
                    (
                        *changed.values(),
                        datetime.now(UTC).isoformat(),
                        album_id,
                        space_id,
                    ),
                )
            return True

        return bool(await self._db.transact(_run))

    async def has_items_by_others(self, album_id: str, user_id: str) -> bool:
        """Whether the album holds a live item someone other than *user_id*
        uploaded."""
        row = await self._db.fetchone(
            "SELECT 1 FROM gallery_items WHERE album_id=? AND uploaded_by<>?"
            " AND deleted_at IS NULL LIMIT 1",
            (album_id, user_id),
        )
        return row is not None

    async def newest_by_others(
        self, album_ids: list[str], viewer_user_id: str
    ) -> dict[str, str]:
        ids = list(dict.fromkeys(album_ids))
        out: dict[str, str] = {}
        for start in range(0, len(ids), 200):
            chunk = ids[start : start + 200]
            rows = await self._db.fetchall(
                "SELECT i.album_id, MAX(i.created_at) AS newest"
                " FROM gallery_items i"
                f" WHERE i.album_id IN ({','.join('?' * len(chunk))})"
                " AND i.uploaded_by <> ? AND i.deleted_at IS NULL"
                f"{_VIEWER_MAY_SEE_I_UPLOADER_SQL}"
                " GROUP BY i.album_id",
                (*chunk, viewer_user_id, *([viewer_user_id] * 3)),
            )
            for r in rows:
                if r["newest"]:
                    out[str(r["album_id"])] = str(r["newest"])
        return out

    async def list_album_media(self, album_id: str) -> list[str]:
        """The ``api/media/`` references of every item file in the album —
        what an album delete leaves behind for the media cleanup."""
        rows = await self._db.fetchall(
            "SELECT filename, thumbnail_filename FROM gallery_items"
            " WHERE album_id=? AND deleted_at IS NULL",
            (album_id,),
        )
        out: list[str] = []
        for r in rows:
            for name in (r[0], r[1]):
                if name:
                    out.append(f"api/media/{name}")
        return out

    async def delete_album_in_space(
        self, album_id: str, *, space_id: str, deleted_by: str = ""
    ) -> bool:
        """Tombstone a user album of ``space_id`` and its items (migration
        0085: the album row stays, content blanked, and the 0085 trigger
        tombstones its items in place).

        ``False`` when the album is unknown, lives elsewhere, is the system
        album, or is a tombstone already.
        """

        def _run(conn) -> bool:
            cur = conn.execute(
                f"UPDATE gallery_albums SET {_ALBUM_TOMBSTONE_SET}"
                " WHERE id=? AND space_id=? AND is_system=0 AND deleted_at IS NULL",
                (deleted_by or None, album_id, space_id),
            )
            return bool(cur.rowcount)

        return bool(await self._db.transact(_run))

    async def set_retention_exempt(
        self,
        album_id: str,
        exempt: bool,
        *,
        space_id: str | None = None,
    ) -> None:
        if space_id is None:
            await self._db.enqueue(
                "UPDATE gallery_albums SET retention_exempt=? WHERE id=?",
                (int(exempt), album_id),
            )
        else:
            await self._db.enqueue(
                "UPDATE gallery_albums SET retention_exempt=? WHERE id=? AND space_id=?",
                (int(exempt), album_id, space_id),
            )

    # ─── Items ────────────────────────────────────────────────────────────

    def _row_to_item(self, r: dict) -> GalleryItem:
        return GalleryItem(
            id=r["id"],
            album_id=r["album_id"],
            uploaded_by=r["uploaded_by"],
            item_type=r["item_type"],
            url=f"api/media/{r['filename']}",
            thumbnail_url=f"api/media/{r['thumbnail_filename']}",
            width=int(r["width"]),
            height=int(r["height"]),
            duration_s=r.get("duration_s"),
            caption=r.get("caption"),
            taken_at=r.get("taken_at"),
            sort_order=int(r.get("sort_order") or 0),
            source_post_id=r.get("source_post_id"),
            created_at=r.get("created_at"),
        )

    async def list_items(
        self,
        album_id: str,
        *,
        limit: int = 50,
        before: str | None = None,
        before_id: str | None = None,
        viewer_user_id: str | None = None,
        sort: ItemSort = ItemSort.ADDED,
        after: ItemCursor | None = None,
    ) -> list[GalleryItem]:
        """One page of an album.

        ``ItemSort.ADDED`` (the default): newest upload first
        (``created_at DESC, id DESC`` — ``id`` breaks a tie, so no item is
        skipped or repeated between pages). The next page is ``before`` /
        ``before_id`` = the last item's ``created_at`` / ``id``; ``before``
        alone keeps its plain ``created_at <`` meaning.

        ``ItemSort.TAKEN`` / ``TAKEN_ASC``: by the day taken (the upload
        day when the photo carries none — :data:`_ITEM_DAY_SQL`, served by
        ``idx_gallery_items_album_taken``), newest / oldest day first, then
        upload time and ``id`` the same way. The next page starts
        ``after`` the last item's full key (:class:`ItemCursor`), so the
        cursor matches the order exactly.

        ``viewer_user_id`` hides the items of uploaders the viewer blocked
        or is guardian-blocked from, in SQL so a page stays full.
        """
        limit = max(1, min(limit, 500))
        sql = "SELECT * FROM gallery_items WHERE album_id=? AND deleted_at IS NULL"
        params: list = [album_id]
        if sort is ItemSort.ADDED:
            if before and before_id:
                sql += " AND (created_at < ? OR (created_at = ? AND id < ?))"
                params += [before, before, before_id]
            elif before:
                sql += " AND created_at < ?"
                params.append(before)
            order = " ORDER BY created_at DESC, id DESC"
        else:
            cmp, way = ("<", "DESC") if sort is ItemSort.TAKEN else (">", "ASC")
            if after is not None:
                sql += f" AND ({_ITEM_DAY_SQL}, created_at, id) {cmp} (?, ?, ?)"
                params += [after.day, after.created_at, after.item_id]
            order = f" ORDER BY {_ITEM_DAY_SQL} {way}, created_at {way}, id {way}"
        if viewer_user_id is not None:
            sql += _VIEWER_MAY_SEE_UPLOADER_SQL
            params += [viewer_user_id] * 3
        sql += order + " LIMIT ?"
        params.append(limit)
        rows = await self._db.fetchall(sql, tuple(params))
        return [self._row_to_item(r) for r in rows_to_dicts(rows)]

    async def find_on_this_day(
        self,
        month_days: tuple[str, ...],
        *,
        before_year: str,
        viewer_user_id: str,
        excluded_album_ids: frozenset[str] = frozenset(),
    ) -> tuple[str, str, int] | None:
        """The album and year with the most live items taken on one of
        *month_days* (``MM-DD``) in a year before *before_year* (``YYYY``)
        — ``(album_id, year, count)``, the most recent year on a tie — or
        ``None``.

        Only household albums open to everyone at home: a private album
        (``audience = 'chosen'``) and every space album never feed "On this
        day" (owner decisions D7 / D14). Items of uploaders hidden from the
        viewer stay out, and so do the viewer's *excluded_album_ids*.
        """
        if not month_days:
            return None
        day = _ITEM_DAY_SQL.replace("taken_at", "i.taken_at").replace(
            "created_at", "i.created_at"
        )
        sql = (
            f"SELECT i.album_id AS album_id, substr({day}, 1, 4) AS yr,"
            " COUNT(*) AS n"
            " FROM gallery_items i JOIN gallery_albums a ON a.id = i.album_id"
            " WHERE a.space_id IS NULL AND a.deleted_at IS NULL"
            " AND a.audience = ? AND i.deleted_at IS NULL"
            f" AND substr({day}, 6, 5) IN ({','.join('?' * len(month_days))})"
            f" AND substr({day}, 1, 4) < ?"
        )
        params: list = [AUDIENCE_EVERYONE, *month_days, before_year]
        if excluded_album_ids:
            marks = ",".join("?" * len(excluded_album_ids))
            sql += f" AND i.album_id NOT IN ({marks})"
            params += sorted(excluded_album_ids)
        sql += _VIEWER_MAY_SEE_I_UPLOADER_SQL
        params += [viewer_user_id] * 3
        sql += " GROUP BY i.album_id, yr ORDER BY n DESC, yr DESC, i.album_id LIMIT 1"
        row = await self._db.fetchone(sql, tuple(params))
        if row is None:
            return None
        return str(row["album_id"]), str(row["yr"]), int(row["n"])

    async def list_on_this_day_items(
        self,
        album_id: str,
        *,
        year: str,
        month_days: tuple[str, ...],
        viewer_user_id: str,
        limit: int = 3,
    ) -> list[GalleryItem]:
        """The first *limit* live items of the album taken on one of
        *month_days* in *year*, oldest first (the album's date order)."""
        days = [f"{year}-{md}" for md in month_days]
        if not days:
            return []
        sql = (
            "SELECT * FROM gallery_items WHERE album_id=? AND deleted_at IS NULL"
            f" AND {_ITEM_DAY_SQL} IN ({','.join('?' * len(days))})"
            + _VIEWER_MAY_SEE_UPLOADER_SQL
            + f" ORDER BY {_ITEM_DAY_SQL}, created_at, id LIMIT ?"
        )
        params = [album_id, *days, *([viewer_user_id] * 3), max(1, min(limit, 50))]
        rows = await self._db.fetchall(sql, tuple(params))
        return [self._row_to_item(r) for r in rows_to_dicts(rows)]

    async def list_albums_sync_page(
        self,
        space_id: str,
        *,
        cursor: int | None = None,
        limit: int = 200,
        since: int | None = None,
    ) -> tuple[list[GalleryAlbum], int | None]:
        changed, changed_params = changed_since_sql("sync_seq", since)
        rows = rows_to_dicts(
            await self._db.fetchall(
                "SELECT rowid AS sync_rowid, * FROM gallery_albums"
                " WHERE space_id=? AND deleted_at IS NULL"
                + changed
                + " AND rowid > ? ORDER BY rowid LIMIT ?",
                (space_id, *changed_params, cursor or 0, int(limit)),
            )
        )
        return [self._row_to_album(r) for r in rows], sync_page_cursor(rows, limit)

    async def list_items_sync_page(
        self,
        space_id: str,
        *,
        cursor: int | None = None,
        limit: int = 200,
        since: int | None = None,
    ) -> tuple[list[GalleryItem], int | None]:
        changed, changed_params = changed_since_sql("i.sync_seq", since)
        rows = rows_to_dicts(
            await self._db.fetchall(
                "SELECT i.rowid AS sync_rowid, i.* FROM gallery_items i"
                " JOIN gallery_albums a ON a.id = i.album_id"
                " WHERE a.space_id=? AND i.source_post_id IS NULL"
                " AND a.is_system = 0"
                " AND i.deleted_at IS NULL AND a.deleted_at IS NULL"
                + changed
                + " AND i.rowid > ? ORDER BY i.rowid LIMIT ?",
                (space_id, *changed_params, cursor or 0, int(limit)),
            )
        )
        return [self._row_to_item(r) for r in rows], sync_page_cursor(rows, limit)

    async def get_item(self, item_id: str) -> GalleryItem | None:
        row = await self._db.fetchone(
            "SELECT * FROM gallery_items WHERE id=? AND deleted_at IS NULL",
            (item_id,),
        )
        return self._row_to_item(dict(row)) if row else None

    async def list_items_since(
        self,
        space_id: str,
        since: str,
        *,
        limit: int = 500,
    ) -> list[GalleryItem]:
        """Items uploaded after ``since`` for any album in *space_id*.

        Joins ``gallery_items`` with ``gallery_albums`` so resume can
        scope by space — the item row itself only knows its album.
        Items in household-level albums (NULL ``space_id``) are
        intentionally excluded; they don't federate. Oldest-first by
        ``created_at`` so the receiver applies them in chronological
        order.
        """
        rows = await self._db.fetchall(
            "SELECT i.* FROM gallery_items i "
            "JOIN gallery_albums a ON a.id = i.album_id "
            "WHERE a.space_id=? AND i.created_at > ? "
            "AND i.deleted_at IS NULL AND a.deleted_at IS NULL "
            "ORDER BY i.created_at ASC LIMIT ?",
            (space_id, since, int(limit)),
        )
        return [self._row_to_item(r) for r in rows_to_dicts(rows)]

    async def list_items_by_source_post(
        self,
        post_id: str,
    ) -> list[GalleryItem]:
        """Items mirrored from a specific feed post (system-album only).

        Used by :class:`GalleryService.mirror_post` to diff existing
        rows against the post's current media — lets a text-only edit
        on an image post short-circuit without churning rows.
        """
        rows = await self._db.fetchall(
            "SELECT * FROM gallery_items WHERE source_post_id=? AND deleted_at IS NULL",
            (post_id,),
        )
        return [self._row_to_item(r) for r in rows_to_dicts(rows)]

    async def create_item(
        self, item: GalleryItem, *, content_sha256: str | None = None
    ) -> GalleryItem:
        """Insert an item. ``content_sha256``: the SHA-256 of the bytes as
        uploaded (household uploads only — the duplicate check, 0139)."""
        # Strip the "/api/media/" prefix so the column stores the bare filename.
        await self._db.enqueue(
            """
            INSERT INTO gallery_items(
                id, album_id, uploaded_by, item_type,
                filename, thumbnail_filename, width, height,
                duration_s, caption, taken_at, sort_order,
                source_post_id, created_at, content_sha256
            ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            -- Idempotent, matching ``create_album``: a §25.6 sync can
            -- legitimately re-deliver the same item (a resumed stream, a
            -- re-issued BEGIN after a restart), and that is not an error.
            -- Without this, a redelivery raised and the receiver's blanket
            -- ``except`` hid it along with the failures that mattered.
            ON CONFLICT DO NOTHING
            """,
            (*self._item_params(item), content_sha256),
        )
        return item

    @staticmethod
    def _item_params(item: GalleryItem) -> tuple:
        return (
            item.id,
            item.album_id,
            item.uploaded_by,
            item.item_type,
            _basename(item.url),
            _basename(item.thumbnail_url),
            item.width,
            item.height,
            item.duration_s,
            item.caption,
            item.taken_at,
            item.sort_order,
            item.source_post_id,
            item.created_at or datetime.now(UTC).isoformat(),
        )

    async def delete_item(self, item_id: str, *, deleted_by: str = "") -> None:
        """Delete an item. One in a household album is removed; one in a
        space album is tombstoned (migration 0085) — files blanked, so the
        caller's media cleanup unlinks the blobs, and the §25.6
        ``gallery_items_deleted`` resource streams it."""

        def _run(conn) -> None:
            conn.execute(
                "DELETE FROM gallery_items WHERE id=? AND album_id IN"
                " (SELECT id FROM gallery_albums WHERE space_id IS NULL)",
                (item_id,),
            )
            conn.execute(
                f"UPDATE gallery_items SET {_ITEM_TOMBSTONE_SET}"
                " WHERE id=? AND deleted_at IS NULL AND album_id IN"
                " (SELECT id FROM gallery_albums WHERE space_id IS NOT NULL)",
                (deleted_by or None, item_id),
            )

        await self._db.transact(_run)

    async def create_item_in_space(
        self,
        item: GalleryItem,
        *,
        space_id: str,
        bump_count: bool = True,
    ) -> bool:
        """Insert ``item`` into an album of ``space_id`` (federation, §24.11).

        ``item.album_id`` comes from the untrusted payload, so the album's
        space is checked in the same transaction as the insert: an album
        of another space — or a household album (``space_id IS NULL``) —
        is refused with ``False``. A redelivered id is an idempotent
        no-op (``True``, count untouched); an id already used by an item
        elsewhere is never overwritten. ``item_count`` is bumped only when
        a row was actually inserted — and not at all with
        ``bump_count=False`` (the §25.6 sync ships the album's count on
        the album record itself).
        """
        params = self._item_params(item)

        def _run(conn) -> bool:
            if (
                conn.execute(
                    "SELECT 1 FROM gallery_albums WHERE id=? AND space_id=?"
                    " AND deleted_at IS NULL",
                    (item.album_id, space_id),
                ).fetchone()
                is None
            ):
                return False
            cur = conn.execute(
                """
                INSERT INTO gallery_items(
                    id, album_id, uploaded_by, item_type,
                    filename, thumbnail_filename, width, height,
                    duration_s, caption, taken_at, sort_order,
                    source_post_id, created_at
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT DO NOTHING
                """,
                params,
            )
            if cur.rowcount and bump_count:
                conn.execute(
                    "UPDATE gallery_albums SET item_count = item_count + 1 "
                    "WHERE id=? AND space_id=?",
                    (item.album_id, space_id),
                )
            return True

        return bool(await self._db.transact(_run))

    async def delete_item_in_space(
        self, item_id: str, *, space_id: str, deleted_by: str = ""
    ) -> bool:
        """Tombstone an item whose album lives in ``space_id`` (migration
        0085: files blanked, ``deleted_at`` / ``deleted_by`` set).

        ``False`` when the item is unknown, a tombstone already, or sits in
        an album of another space / the household gallery. Decrements the
        album's ``item_count`` in the same transaction.
        """

        def _run(conn) -> bool:
            row = conn.execute(
                "SELECT i.album_id FROM gallery_items i"
                " JOIN gallery_albums a ON a.id = i.album_id"
                " WHERE i.id=? AND a.space_id=? AND i.deleted_at IS NULL",
                (item_id, space_id),
            ).fetchone()
            if row is None:
                return False
            conn.execute(
                f"UPDATE gallery_items SET {_ITEM_TOMBSTONE_SET} WHERE id=?",
                (deleted_by or None, item_id),
            )
            conn.execute(
                "UPDATE gallery_albums SET item_count=MAX(0, item_count - 1) "
                "WHERE id=? AND space_id=?",
                (row[0], space_id),
            )
            return True

        return bool(await self._db.transact(_run))

    async def delete_items_by_source_post(
        self,
        post_id: str,
    ) -> tuple[str | None, int]:
        """Bulk-delete every mirrored item for ``post_id``.

        Returns ``(album_id, count)`` so the service can decrement
        ``item_count`` on the affected album. ``album_id`` is ``None``
        when no items existed (no-op).
        """
        rows = await self._db.fetchall(
            "SELECT id, album_id FROM gallery_items WHERE source_post_id=?",
            (post_id,),
        )
        items = list(rows_to_dicts(rows))
        if not items:
            return (None, 0)
        album_id = items[0]["album_id"]
        await self._db.enqueue(
            "DELETE FROM gallery_items WHERE source_post_id=?",
            (post_id,),
        )
        return (album_id, len(items))

    async def increment_item_count(self, album_id: str, delta: int) -> None:
        await self._db.enqueue(
            "UPDATE gallery_albums SET item_count=MAX(0, item_count + ?) WHERE id=?",
            (delta, album_id),
        )

    async def recount_items(self, album_id: str) -> int:
        """Recompute ``item_count`` from ``COUNT(*)`` and persist it.

        Used by ``mirror_post`` after delete-then-insert to keep the
        cached count in sync without making the caller juggle deltas.
        Returns the new count.
        """
        row = await self._db.fetchone(
            "SELECT COUNT(*) AS n FROM gallery_items"
            " WHERE album_id=? AND deleted_at IS NULL",
            (album_id,),
        )
        n = int(row["n"]) if row else 0
        await self._db.enqueue(
            "UPDATE gallery_albums SET item_count=? WHERE id=?",
            (n, album_id),
        )
        return n

    async def count_visible_items(self, album_id: str, viewer_user_id: str) -> int:
        """How many live items of the album *viewer_user_id* may see — the
        system album's count, which hides what its listing hides."""
        row = await self._db.fetchone(
            "SELECT COUNT(*) AS n FROM gallery_items WHERE album_id=?"
            " AND deleted_at IS NULL" + _VIEWER_MAY_SEE_UPLOADER_SQL,
            (album_id, viewer_user_id, viewer_user_id, viewer_user_id),
        )
        return int(row["n"]) if row else 0

    async def get_first_item_thumbnail(
        self, album_id: str, *, viewer_user_id: str | None = None
    ) -> str | None:
        """The album's first item's thumbnail — the default cover.
        ``viewer_user_id`` skips uploaders hidden from that viewer."""
        sql = (
            "SELECT thumbnail_filename FROM gallery_items WHERE album_id=? "
            "AND deleted_at IS NULL"
        )
        params: list = [album_id]
        if viewer_user_id is not None:
            sql += _VIEWER_MAY_SEE_UPLOADER_SQL
            params += [viewer_user_id] * 3
        row = await self._db.fetchone(
            sql + " ORDER BY sort_order, created_at LIMIT 1", tuple(params)
        )
        return f"api/media/{row['thumbnail_filename']}" if row else None

    async def list_space_item_filenames(self, space_id: str) -> list[str]:
        """Every gallery item file + thumbnail in a space's albums, for
        on-disk cleanup when the space is hard-deleted.

        Returns bare basenames (``unlink_media`` resolves them under the
        media dir). Mirrored items reuse the source post's blob — the
        caller dedupes across post + gallery collections, and a missing
        file unlink is a no-op, so the overlap is harmless. Must be
        called *before* the rows are dropped.
        """
        rows = await self._db.fetchall(
            """
            SELECT i.filename, i.thumbnail_filename
              FROM gallery_items i
              JOIN gallery_albums a ON i.album_id = a.id
             WHERE a.space_id = ?
            """,
            (space_id,),
        )
        out: list[str] = []
        for r in rows:
            if r["filename"]:
                out.append(r["filename"])
            if r["thumbnail_filename"]:
                out.append(r["thumbnail_filename"])
        return out

    # ─── Library model (migration 0139) ───────────────────────────────────

    async def find_household_duplicate(
        self, content_sha256: str, *, viewer_user_id: str
    ) -> GalleryDuplicate | None:
        """A household item with these uploaded bytes that *viewer_user_id*
        may see — a live one first, else one in the trash (oldest first).
        Only what the viewer can see counts: a match in a private album
        they aren't in is no duplicate (else the answer would reveal it)."""
        row = await self._db.fetchone(
            "SELECT i.* FROM gallery_items i"
            " JOIN gallery_albums a ON a.id = i.album_id"
            " WHERE i.content_sha256 = ? AND a.space_id IS NULL"
            " AND a.is_system = 0 AND i.filename <> ''"
            " AND " + _ALBUM_VISIBLE_TO_SQL + " ORDER BY i.deleted_at IS NOT NULL,"
            " i.created_at, i.id LIMIT 1",
            (content_sha256, viewer_user_id, viewer_user_id),
        )
        if row is None:
            return None
        r = dict(row)
        return GalleryDuplicate(
            item=self._row_to_item(r), in_trash=r.get("deleted_at") is not None
        )

    async def trash_item(self, item_id: str, *, deleted_by: str) -> bool:
        """Move a live household item to the trash: ``deleted_at`` /
        ``deleted_by`` set, files and content kept, the album's count down
        one — in one transaction. ``False`` for an unknown or space item
        (a space item is tombstoned, see :meth:`delete_item`)."""
        now = _utc_now_sql()

        def _run(conn) -> bool:
            row = conn.execute(
                "SELECT i.album_id FROM gallery_items i"
                " JOIN gallery_albums a ON a.id = i.album_id"
                " WHERE i.id=? AND a.space_id IS NULL AND i.deleted_at IS NULL",
                (item_id,),
            ).fetchone()
            if row is None:
                return False
            conn.execute(
                "UPDATE gallery_items SET deleted_at=?, deleted_by=? WHERE id=?",
                (now, deleted_by or None, item_id),
            )
            conn.execute(
                "UPDATE gallery_albums SET item_count=MAX(0, item_count - 1)"
                " WHERE id=?",
                (row[0],),
            )
            return True

        return bool(await self._db.transact(_run))

    async def trash_album(self, album_id: str, *, deleted_by: str) -> list[str]:
        """Move a live household user album to the trash with its live items
        (one stamp for all). The items first: the 0085 album trigger then
        finds none live, so it blanks no file. The album keeps its name, so
        the trash can say where a photo came from and restore it there."""
        now = _utc_now_sql()

        def _run(conn) -> list[str]:
            if (
                conn.execute(
                    "SELECT 1 FROM gallery_albums WHERE id=? AND space_id IS NULL"
                    " AND is_system=0 AND deleted_at IS NULL",
                    (album_id,),
                ).fetchone()
                is None
            ):
                return []
            ids = [
                r[0]
                for r in conn.execute(
                    "SELECT id FROM gallery_items WHERE album_id=?"
                    " AND deleted_at IS NULL",
                    (album_id,),
                ).fetchall()
            ]
            conn.execute(
                "UPDATE gallery_items SET deleted_at=?, deleted_by=?"
                " WHERE album_id=? AND deleted_at IS NULL",
                (now, deleted_by or None, album_id),
            )
            conn.execute(
                "UPDATE gallery_albums SET deleted_at=?, deleted_by=?, item_count=0"
                " WHERE id=?",
                (now, deleted_by or None, album_id),
            )
            return ids

        return list(await self._db.transact(_run))

    async def restore_item(self, item_id: str) -> str | None:
        """Bring a household item back from the trash, and its album with it
        when the album went to the trash too; the album's count is
        recounted from its live rows."""

        def _run(conn) -> str | None:
            row = conn.execute(
                "SELECT i.album_id FROM gallery_items i"
                " JOIN gallery_albums a ON a.id = i.album_id"
                " WHERE i.id=? AND " + _IN_TRASH_SQL,
                (item_id,),
            ).fetchone()
            if row is None:
                return None
            album_id = str(row[0])
            conn.execute(
                "UPDATE gallery_items SET deleted_at=NULL, deleted_by=NULL WHERE id=?",
                (item_id,),
            )
            conn.execute(
                "UPDATE gallery_albums SET deleted_at=NULL, deleted_by=NULL"
                " WHERE id=? AND deleted_at IS NOT NULL",
                (album_id,),
            )
            conn.execute(
                "UPDATE gallery_albums SET item_count=(SELECT COUNT(*)"
                " FROM gallery_items WHERE album_id=? AND deleted_at IS NULL)"
                " WHERE id=?",
                (album_id, album_id),
            )
            return album_id

        result = await self._db.transact(_run)
        return str(result) if result is not None else None

    def _row_to_trash_item(self, r: dict) -> GalleryTrashItem:
        return GalleryTrashItem(
            item=self._row_to_item(r),
            deleted_at=r["deleted_at"],
            deleted_by=r.get("deleted_by"),
            album_name=r["album_name"],
            album_owner_user_id=r.get("album_owner_user_id"),
            album_in_trash=r.get("album_deleted_at") is not None,
        )

    _TRASH_SELECT = (
        "SELECT i.*, a.name AS album_name, a.owner_user_id AS album_owner_user_id,"
        " a.deleted_at AS album_deleted_at FROM gallery_items i"
        " JOIN gallery_albums a ON a.id = i.album_id WHERE " + _IN_TRASH_SQL
    )

    async def get_trashed_item(
        self, item_id: str, *, viewer_user_id: str | None = None
    ) -> GalleryTrashItem | None:
        sql = self._TRASH_SELECT + " AND i.id=?"
        params: tuple = (item_id,)
        if viewer_user_id is not None:
            sql += " AND " + _ALBUM_VISIBLE_TO_SQL
            params = (item_id, viewer_user_id, viewer_user_id)
        row = await self._db.fetchone(sql, params)
        return self._row_to_trash_item(dict(row)) if row else None

    async def list_trash(
        self,
        *,
        viewer_user_id: str,
        limit: int = 100,
        before_id: str | None = None,
    ) -> list[GalleryTrashItem]:
        """The household trash *viewer_user_id* may see, last deleted first
        (``deleted_at DESC, id DESC`` — an album delete stamps all its
        photos alike, so ``id`` breaks the tie). ``before_id`` = the last
        row's id for the next page."""
        limit = max(1, min(limit, 500))
        sql = self._TRASH_SELECT + " AND " + _ALBUM_VISIBLE_TO_SQL
        params: list = [viewer_user_id, viewer_user_id]
        if before_id:
            sql += (
                " AND (i.deleted_at, i.id) <"
                " (SELECT deleted_at, id FROM gallery_items WHERE id = ?)"
            )
            params.append(before_id)
        sql += " ORDER BY i.deleted_at DESC, i.id DESC LIMIT ?"
        params.append(limit)
        rows = await self._db.fetchall(sql, tuple(params))
        return [self._row_to_trash_item(r) for r in rows_to_dicts(rows)]

    @staticmethod
    def _purge_rows(conn, item_ids: list[str]) -> list[str]:
        """Hard-delete these items (favourites cascade); their media refs."""
        media: list[str] = []
        for iid in item_ids:
            row = conn.execute(
                "SELECT filename, thumbnail_filename FROM gallery_items WHERE id=?",
                (iid,),
            ).fetchone()
            if row is None:
                continue
            media.extend(f"api/media/{n}" for n in (row[0], row[1]) if n)
            conn.execute("DELETE FROM gallery_items WHERE id=?", (iid,))
        return media

    async def purge_trashed_item(self, item_id: str) -> list[str]:
        def _run(conn) -> list[str]:
            row = conn.execute(
                "SELECT i.album_id FROM gallery_items i"
                " JOIN gallery_albums a ON a.id = i.album_id"
                " WHERE i.id=? AND " + _IN_TRASH_SQL,
                (item_id,),
            ).fetchone()
            if row is None:
                return []
            media = self._purge_rows(conn, [item_id])
            # A trashed album left with no item goes with its last photo.
            conn.execute(
                "DELETE FROM gallery_albums WHERE id=? AND space_id IS NULL"
                " AND deleted_at IS NOT NULL AND NOT EXISTS"
                " (SELECT 1 FROM gallery_items WHERE album_id=?)",
                (row[0], row[0]),
            )
            return media

        return list(await self._db.transact(_run))

    async def purge_expired_trash(self, cutoff: str, *, limit: int = 500) -> list[str]:
        """Delete up to ``limit`` household items trashed before ``cutoff``
        (oldest first), then every trashed household album deleted before
        ``cutoff`` that holds no item any more."""

        def _run(conn) -> list[str]:
            ids = [
                r[0]
                for r in conn.execute(
                    "SELECT i.id FROM gallery_items i"
                    " JOIN gallery_albums a ON a.id = i.album_id"
                    " WHERE " + _IN_TRASH_SQL + " AND i.deleted_at < ?"
                    " ORDER BY i.deleted_at LIMIT ?",
                    (cutoff, int(limit)),
                ).fetchall()
            ]
            media = self._purge_rows(conn, ids)
            conn.execute(
                "DELETE FROM gallery_albums WHERE space_id IS NULL"
                " AND deleted_at IS NOT NULL AND deleted_at < ?"
                " AND NOT EXISTS (SELECT 1 FROM gallery_items i"
                " WHERE i.album_id = gallery_albums.id)",
                (cutoff,),
            )
            return media

        return list(await self._db.transact(_run))

    async def reassign_household_albums(
        self, from_user_id: str, to_user_id: str
    ) -> int:
        return await self._db.enqueue_rowcount(
            "UPDATE gallery_albums SET owner_user_id=?"
            " WHERE owner_user_id=? AND space_id IS NULL AND is_system=0"
            " AND audience=?",
            (to_user_id, from_user_id, AUDIENCE_EVERYONE),
        )

    _ITEM_PATCH_ALLOWED = frozenset({"caption", "taken_at"})

    async def update_item(self, item_id: str, patch: dict) -> bool:
        safe = {k: v for k, v in patch.items() if k in self._ITEM_PATCH_ALLOWED}
        if not safe:
            return False
        set_clause = ", ".join(f"{k}=?" for k in safe)
        changed = await self._db.enqueue_rowcount(
            f"UPDATE gallery_items SET {set_clause} WHERE id=?"
            " AND deleted_at IS NULL AND album_id IN"
            " (SELECT id FROM gallery_albums WHERE space_id IS NULL)",
            (*safe.values(), item_id),
        )
        return changed > 0

    async def move_item(self, item_id: str, to_album_id: str) -> str | None:
        def _run(conn) -> str | None:
            row = conn.execute(
                "SELECT i.album_id FROM gallery_items i"
                " JOIN gallery_albums a ON a.id = i.album_id"
                " WHERE i.id=? AND i.deleted_at IS NULL AND a.space_id IS NULL"
                " AND a.is_system = 0 AND a.deleted_at IS NULL",
                (item_id,),
            ).fetchone()
            if row is None:
                return None
            from_id = str(row[0])
            if from_id == to_album_id:
                return None
            if (
                conn.execute(
                    "SELECT 1 FROM gallery_albums WHERE id=? AND space_id IS NULL"
                    " AND is_system = 0 AND deleted_at IS NULL",
                    (to_album_id,),
                ).fetchone()
                is None
            ):
                return None
            conn.execute(
                "UPDATE gallery_items SET album_id=? WHERE id=?",
                (to_album_id, item_id),
            )
            conn.execute(
                "UPDATE gallery_albums SET cover_item_id=NULL"
                " WHERE id=? AND cover_item_id=?",
                (from_id, item_id),
            )
            for aid in (from_id, to_album_id):
                conn.execute(
                    "UPDATE gallery_albums SET item_count=(SELECT COUNT(*)"
                    " FROM gallery_items WHERE album_id=? AND deleted_at IS NULL)"
                    " WHERE id=?",
                    (aid, aid),
                )
            return from_id

        result = await self._db.transact(_run)
        return str(result) if result is not None else None

    async def get_trashed_album(self, album_id: str) -> GalleryAlbum | None:
        row = await self._db.fetchone(
            "SELECT * FROM gallery_albums WHERE id=? AND space_id IS NULL"
            " AND is_system = 0 AND deleted_at IS NOT NULL",
            (album_id,),
        )
        return self._row_to_album(dict(row)) if row else None

    async def restore_album(self, album_id: str) -> list[str]:
        def _run(conn) -> list[str]:
            row = conn.execute(
                "SELECT deleted_at FROM gallery_albums WHERE id=?"
                " AND space_id IS NULL AND is_system = 0"
                " AND deleted_at IS NOT NULL",
                (album_id,),
            ).fetchone()
            if row is None:
                return []
            stamp = row[0]
            ids = [
                r[0]
                for r in conn.execute(
                    "SELECT id FROM gallery_items WHERE album_id=?"
                    " AND deleted_at=? AND filename <> ''",
                    (album_id, stamp),
                ).fetchall()
            ]
            conn.execute(
                "UPDATE gallery_items SET deleted_at=NULL, deleted_by=NULL"
                " WHERE album_id=? AND deleted_at=? AND filename <> ''",
                (album_id, stamp),
            )
            conn.execute(
                "UPDATE gallery_albums SET deleted_at=NULL, deleted_by=NULL,"
                " item_count=(SELECT COUNT(*) FROM gallery_items"
                " WHERE album_id=? AND deleted_at IS NULL) WHERE id=?",
                (album_id, album_id),
            )
            return ids

        return list(await self._db.transact(_run))

    async def list_household_uploads(
        self, uploader_user_id: str, *, limit: int
    ) -> list[GalleryItem]:
        rows = await self._db.fetchall(
            "SELECT i.* FROM gallery_items i"
            " JOIN gallery_albums a ON a.id = i.album_id"
            " WHERE i.uploaded_by=? AND i.deleted_at IS NULL AND i.filename <> ''"
            " AND a.space_id IS NULL AND a.is_system = 0 AND a.deleted_at IS NULL"
            " ORDER BY i.created_at, i.id LIMIT ?",
            (uploader_user_id, int(limit)),
        )
        return [self._row_to_item(r) for r in rows_to_dicts(rows)]

    async def get_album_audience(
        self, album_id: str, *, include_trashed: bool = False
    ) -> GalleryAlbumAudience | None:
        live = "" if include_trashed else " AND deleted_at IS NULL"
        row = await self._db.fetchone(
            f"SELECT audience, owner_user_id FROM gallery_albums WHERE id=?{live}",
            (album_id,),
        )
        if row is None:
            return None
        people = await self._db.fetchall(
            "SELECT user_id FROM gallery_album_audience WHERE album_id=?",
            (album_id,),
        )
        guardians = await self._db.fetchall(
            "SELECT g.guardian_user_id FROM cp_guardians g"
            " JOIN users m ON m.user_id = g.minor_user_id"
            " WHERE g.minor_user_id = ? AND m.child_protection_enabled = 1",
            (row[1],),
        )
        return GalleryAlbumAudience(
            album_id=album_id,
            audience=row[0],
            user_ids=frozenset(r[0] for r in people),
            owner_user_id=row[1],
            guardian_ids=frozenset(r[0] for r in guardians),
        )

    async def album_uploaders(self, album_id: str) -> frozenset[str]:
        rows = await self._db.fetchall(
            "SELECT DISTINCT uploaded_by FROM gallery_items"
            " WHERE album_id=? AND filename <> ''",
            (album_id,),
        )
        return frozenset(r[0] for r in rows if r[0])

    async def list_private_albums_of(self, owner_user_id: str) -> list[str]:
        rows = await self._db.fetchall(
            "SELECT id FROM gallery_albums WHERE owner_user_id=?"
            " AND space_id IS NULL AND is_system=0 AND deleted_at IS NULL"
            " AND audience=? ORDER BY id",
            (owner_user_id, AUDIENCE_CHOSEN),
        )
        return [r[0] for r in rows]

    async def list_locked_trash(self, viewer_user_id: str) -> list[LockedTrashAlbum]:
        rows = await self._db.fetchall(
            "SELECT a.id AS album_id, a.owner_user_id AS owner_user_id,"
            " COUNT(*) AS n, MAX(i.deleted_at) AS deleted_at,"
            " a.deleted_at IS NOT NULL AS album_in_trash"
            " FROM gallery_items i JOIN gallery_albums a ON a.id = i.album_id"
            " WHERE "
            + _IN_TRASH_SQL
            + " AND NOT "
            + _ALBUM_VISIBLE_TO_SQL
            + " GROUP BY a.id ORDER BY MAX(i.deleted_at) DESC, a.id DESC LIMIT 200",
            (viewer_user_id, viewer_user_id),
        )
        return [
            LockedTrashAlbum(
                album_id=r["album_id"],
                owner_user_id=r["owner_user_id"],
                count=int(r["n"]),
                deleted_at=r["deleted_at"],
                album_in_trash=bool(r["album_in_trash"]),
            )
            for r in rows_to_dicts(rows)
        ]

    async def guardian_block_counterparts(self, user_id: str) -> frozenset[str]:
        rows = await self._db.fetchall(
            GUARDIAN_BLOCK_COUNTERPARTS_SQL, (user_id, user_id)
        )
        return frozenset(r[0] for r in rows if r[0])

    async def media_hidden_from(self, filename: str, viewer_user_id: str) -> bool:
        # Private albums are few: the planner walks them (audience) and
        # their items by ``idx_gallery_items_album``, not every item.
        row = await self._db.fetchone(
            "SELECT 1 FROM gallery_albums a"
            " JOIN gallery_items i ON i.album_id = a.id"
            " WHERE a.audience = ? AND a.space_id IS NULL"
            " AND (i.filename = ? OR i.thumbnail_filename = ?)"
            " AND i.uploaded_by IS NOT ?"
            " AND NOT " + _ALBUM_VISIBLE_TO_SQL + " LIMIT 1",
            (
                AUDIENCE_CHOSEN,
                filename,
                filename,
                viewer_user_id,
                viewer_user_id,
                viewer_user_id,
            ),
        )
        return row is not None

    async def set_album_audience(
        self, album_id: str, audience: str, user_ids: frozenset[str] = frozenset()
    ) -> bool:
        if audience not in AUDIENCES:
            raise ValueError(f"unknown album audience {audience!r}")
        people = sorted(user_ids) if audience == AUDIENCE_CHOSEN else []

        def _run(conn) -> bool:
            cur = conn.execute(
                "UPDATE gallery_albums SET audience=? WHERE id=?"
                " AND space_id IS NULL AND is_system=0 AND deleted_at IS NULL",
                (audience, album_id),
            )
            if not cur.rowcount:
                return False
            conn.execute(
                "DELETE FROM gallery_album_audience WHERE album_id=?", (album_id,)
            )
            conn.executemany(
                "INSERT INTO gallery_album_audience(album_id, user_id) VALUES(?, ?)",
                [(album_id, uid) for uid in people],
            )
            return True

        return bool(await self._db.transact(_run))

    async def album_visible_to(self, album_id: str, user_id: str) -> bool:
        """Whether *user_id* may see this live household album under its
        audience (a space album answers ``False``: its seats decide)."""
        row = await self._db.fetchone(
            "SELECT 1 FROM gallery_albums a WHERE a.id=? AND a.space_id IS NULL"
            " AND a.deleted_at IS NULL AND " + _ALBUM_VISIBLE_TO_SQL,
            (album_id, user_id, user_id),
        )
        return row is not None

    async def add_favourite(self, user_id: str, item_id: str) -> bool:
        """Mark a live item a favourite of *user_id*; ``False`` when the
        item is unknown or deleted. Idempotent."""
        await self._db.enqueue(
            "INSERT INTO gallery_favourites(user_id, item_id)"
            " SELECT ?, id FROM gallery_items WHERE id=? AND deleted_at IS NULL"
            " ON CONFLICT DO NOTHING",
            (user_id, item_id),
        )
        row = await self._db.fetchone(
            "SELECT 1 FROM gallery_favourites f JOIN gallery_items i"
            " ON i.id = f.item_id WHERE f.user_id=? AND f.item_id=?"
            " AND i.deleted_at IS NULL",
            (user_id, item_id),
        )
        return row is not None

    async def remove_favourite(self, user_id: str, item_id: str) -> None:
        await self._db.enqueue(
            "DELETE FROM gallery_favourites WHERE user_id=? AND item_id=?",
            (user_id, item_id),
        )

    async def favourite_ids_among(
        self, user_id: str, item_ids: list[str]
    ) -> frozenset[str]:
        if not item_ids:
            return frozenset()
        marks = ",".join("?" * len(item_ids))
        rows = await self._db.fetchall(
            f"SELECT item_id FROM gallery_favourites WHERE user_id=?"
            f" AND item_id IN ({marks})",
            (user_id, *item_ids),
        )
        return frozenset(r[0] for r in rows)

    async def list_favourite_items(
        self,
        user_id: str,
        *,
        space_ids: frozenset[str] = frozenset(),
        limit: int = 100,
        before: str | None = None,
        before_id: str | None = None,
    ) -> list[GalleryFavourite]:
        # A trashed photo (or one in a trashed album) is hidden until it is
        # restored; the system "Posts" album hides what the feed hides.
        spaces = sorted(space_ids)
        in_spaces = (
            f" OR a.space_id IN ({','.join('?' * len(spaces))})" if spaces else ""
        )
        sql = (
            "SELECT i.*, f.created_at AS marked_at FROM gallery_favourites f"
            " JOIN gallery_items i ON i.id = f.item_id"
            " JOIN gallery_albums a ON a.id = i.album_id"
            " WHERE f.user_id=? AND i.deleted_at IS NULL AND a.deleted_at IS NULL"
            " AND ((a.space_id IS NULL AND "
            + _ALBUM_VISIBLE_TO_SQL
            + ")"
            + in_spaces
            + ") AND (a.is_system = 0 OR (1=1"
            + _VIEWER_MAY_SEE_UPLOADER_SQL
            + "))"
        )
        params: list = [user_id, user_id, user_id, *spaces, *([user_id] * 3)]
        if before and before_id:
            sql += " AND (f.created_at < ? OR (f.created_at = ? AND f.item_id < ?))"
            params += [before, before, before_id]
        sql += " ORDER BY f.created_at DESC, f.item_id DESC LIMIT ?"
        params.append(max(1, min(limit, 500)))
        rows = await self._db.fetchall(sql, tuple(params))
        return [
            GalleryFavourite(item=self._row_to_item(r), marked_at=r["marked_at"])
            for r in rows_to_dicts(rows)
        ]

    # ─── Tombstones (migration 0085, §25.6 ``gallery_*_deleted``) ─────────

    async def is_album_deleted(self, album_id: str, *, space_id: str) -> bool:
        row = await self._db.fetchone(
            "SELECT 1 FROM gallery_albums WHERE id=? AND space_id=?"
            " AND deleted_at IS NOT NULL",
            (album_id, space_id),
        )
        return row is not None

    async def is_item_deleted(self, item_id: str, *, space_id: str) -> bool:
        row = await self._db.fetchone(
            "SELECT 1 FROM gallery_items i JOIN gallery_albums a ON a.id = i.album_id"
            " WHERE i.id=? AND a.space_id=? AND i.deleted_at IS NOT NULL",
            (item_id, space_id),
        )
        return row is not None

    async def tombstone_album(
        self,
        album_id: str,
        *,
        space_id: str,
        owner_user_id: str,
        created_at: str = "",
        deleted_by: str = "",
    ) -> bool:
        """Record a delete of an album never held here: a content-free stub
        row, so a create (or a stale copy streamed) later cannot bring it.

        Insert-only — an id held already (live or tombstoned, any space) is
        never touched; ``False`` says nothing was written. The caller must
        have proven the id is this space's (owner-bound to
        ``owner_user_id`` in ``space_id``): album ids are global.
        """
        n = await self._db.enqueue_rowcount(
            "INSERT INTO gallery_albums(id, space_id, owner_user_id, name,"
            " created_at, updated_at, deleted_at, deleted_by)"
            " VALUES(?, ?, ?, '', COALESCE(NULLIF(?, ''), datetime('now')),"
            " datetime('now'), datetime('now'), ?)"
            " ON CONFLICT DO NOTHING",
            (album_id, space_id, owner_user_id, created_at, deleted_by or None),
        )
        return n > 0

    async def tombstone_item(
        self,
        item_id: str,
        *,
        space_id: str,
        album_id: str,
        uploaded_by: str,
        created_at: str = "",
        deleted_by: str = "",
    ) -> bool:
        """Record a delete of an item never held here — insert-only, and only
        under an album live here IN ``space_id`` (``album_id`` is a FK, and
        an item is never filed under another space's album). An item of an
        album tombstoned here needs nothing: the album's tombstone covers
        it. ``False`` says nothing was written. The caller must have proven
        the id is this space's (owner-bound to ``uploaded_by``).
        """
        n = await self._db.enqueue_rowcount(
            "INSERT INTO gallery_items(id, album_id, uploaded_by, item_type,"
            " filename, thumbnail_filename, width, height, created_at,"
            " deleted_at, deleted_by)"
            " SELECT ?, ?, ?, 'photo', '', '', 0, 0,"
            " COALESCE(NULLIF(?, ''), datetime('now')), datetime('now'), ?"
            " WHERE EXISTS (SELECT 1 FROM gallery_albums"
            "   WHERE id=? AND space_id=? AND deleted_at IS NULL)"
            " ON CONFLICT DO NOTHING",
            (
                item_id,
                album_id,
                uploaded_by,
                created_at,
                deleted_by or None,
                album_id,
                space_id,
            ),
        )
        return n > 0

    async def list_album_tombstones_page(
        self,
        space_id: str,
        *,
        cursor: int | None = None,
        limit: int = 200,
        since: int | None = None,
    ) -> tuple[list[SpaceRowTombstone], int | None]:
        """One page of the space's album tombstones, keyset on the row id."""
        changed, changed_params = changed_since_sql("sync_seq", since)
        rows = rows_to_dicts(
            await self._db.fetchall(
                "SELECT rowid AS sync_rowid, id, owner_user_id, created_at,"
                " deleted_at, deleted_by FROM gallery_albums"
                " WHERE space_id=? AND deleted_at IS NOT NULL"
                + changed
                + " AND rowid > ? ORDER BY rowid LIMIT ?",
                (space_id, *changed_params, cursor or 0, int(limit)),
            )
        )
        return [
            SpaceRowTombstone(
                id=r["id"],
                owner=r["owner_user_id"] or "",
                created_at=r["created_at"] or "",
                deleted_at=r["deleted_at"],
                deleted_by=r["deleted_by"] or "",
            )
            for r in rows
        ], sync_page_cursor(rows, limit)

    async def list_item_tombstones_page(
        self,
        space_id: str,
        *,
        cursor: int | None = None,
        limit: int = 200,
        since: int | None = None,
    ) -> tuple[list[SpaceRowTombstone], int | None]:
        """One page of the space's single-item tombstones, keyset on the row
        id. Items of a tombstoned album are left out — the album's own
        tombstone tells a household that missed it, and its trigger
        tombstones the items there too."""
        changed, changed_params = changed_since_sql("i.sync_seq", since)
        rows = rows_to_dicts(
            await self._db.fetchall(
                "SELECT i.rowid AS sync_rowid, i.id, i.album_id, i.uploaded_by,"
                " i.created_at, i.deleted_at, i.deleted_by FROM gallery_items i"
                " JOIN gallery_albums a ON a.id = i.album_id"
                " WHERE a.space_id=? AND a.deleted_at IS NULL"
                " AND a.is_system = 0"
                " AND i.deleted_at IS NOT NULL"
                + changed
                + " AND i.rowid > ? ORDER BY i.rowid LIMIT ?",
                (space_id, *changed_params, cursor or 0, int(limit)),
            )
        )
        return [
            SpaceRowTombstone(
                id=r["id"],
                owner=r["uploaded_by"],
                created_at=r["created_at"] or "",
                deleted_at=r["deleted_at"],
                deleted_by=r["deleted_by"] or "",
                parent_id=r["album_id"],
            )
            for r in rows
        ], sync_page_cursor(rows, limit)

    async def list_album_tombstones_since(
        self, space_id: str, since: str
    ) -> list[SpaceRowTombstone]:
        """Albums of ``space_id`` tombstoned after ``since`` (ISO 8601) —
        the resume replay's ``SPACE_GALLERY_ALBUM_DELETED`` list. An
        unparsable ``since`` lists every album tombstone of the space:
        replaying a delete twice is harmless, missing one is not."""
        rows = rows_to_dicts(
            await self._db.fetchall(
                "SELECT id, owner_user_id, created_at, deleted_at, deleted_by"
                " FROM gallery_albums WHERE space_id=? AND deleted_at IS NOT NULL"
                " AND (datetime(?) IS NULL OR datetime(deleted_at) > datetime(?))"
                " ORDER BY rowid",
                (space_id, since, since),
            )
        )
        return [
            SpaceRowTombstone(
                id=r["id"],
                owner=r["owner_user_id"] or "",
                created_at=r["created_at"] or "",
                deleted_at=r["deleted_at"],
                deleted_by=r["deleted_by"] or "",
            )
            for r in rows
        ]

    # ─── Wall photo screensaver (gallery decision D10) ────────────────────

    async def list_frame_albums(self) -> list[FrameAlbumChoice]:
        rows = await self._db.fetchall(
            "SELECT a.id AS id, a.name AS name, a.is_system AS is_system,"
            " (SELECT COUNT(*) FROM gallery_items i WHERE i.album_id = a.id"
            f" AND {_FRAME_ITEM_SQL}) AS photos"
            f" FROM gallery_albums a WHERE {_FRAME_ALBUM_SQL}"
            " ORDER BY a.is_system DESC, a.name COLLATE NOCASE, a.id",
            (AUDIENCE_EVERYONE,),
        )
        return [
            FrameAlbumChoice(
                id=r["id"],
                name=r["name"],
                photo_count=int(r["photos"] or 0),
                is_system=bool(r["is_system"]),
            )
            for r in rows_to_dicts(rows)
        ]

    async def list_frame_photos(
        self, album_ids: Sequence[str], *, limit: int
    ) -> list[DashboardPhoto]:
        ids = list(dict.fromkeys(album_ids))
        if not ids:
            return []
        rows = await self._db.fetchall(
            f"SELECT {_FRAME_PHOTO_COLS}"
            " FROM gallery_items i JOIN gallery_albums a ON a.id = i.album_id"
            f" WHERE i.album_id IN ({','.join('?' * len(ids))})"
            f" AND {_FRAME_ALBUM_SQL} AND {_FRAME_ITEM_SQL}"
            " ORDER BY day DESC, i.created_at DESC, i.id DESC LIMIT ?",
            (*ids, AUDIENCE_EVERYONE, max(1, min(limit, 5000))),
        )
        return [_frame_photo(r) for r in rows_to_dicts(rows)]

    async def get_frame_photo(
        self, item_id: str, album_ids: Sequence[str]
    ) -> DashboardPhoto | None:
        ids = list(dict.fromkeys(album_ids))
        if not ids:
            return None
        row = await self._db.fetchone(
            f"SELECT {_FRAME_PHOTO_COLS}"
            " FROM gallery_items i JOIN gallery_albums a ON a.id = i.album_id"
            f" WHERE i.id = ? AND i.album_id IN ({','.join('?' * len(ids))})"
            f" AND {_FRAME_ALBUM_SQL} AND {_FRAME_ITEM_SQL}",
            (item_id, *ids, AUDIENCE_EVERYONE),
        )
        return _frame_photo(dict(row)) if row else None


def _basename(url: str) -> str:
    """The stored file name of an ``api/media/<name>`` URL — no path, no query."""
    return url.rsplit("/", 1)[-1].split("?", 1)[0]
