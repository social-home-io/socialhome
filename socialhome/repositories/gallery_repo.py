"""Gallery repository — albums + items (§23.119)."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Protocol, runtime_checkable

from ..db import AsyncDatabase
from ..domain.gallery import GalleryAlbum, GalleryItem
from .base import rows_to_dicts


@runtime_checkable
class AbstractGalleryRepo(Protocol):
    async def list_albums(
        self,
        space_id: str | None,
        *,
        limit: int = 30,
        before: str | None = None,
    ) -> list[GalleryAlbum]: ...
    async def get_album(self, album_id: str) -> GalleryAlbum | None: ...
    async def get_system_album(self, space_id: str | None) -> GalleryAlbum | None: ...
    async def create_album(self, album: GalleryAlbum) -> GalleryAlbum: ...
    async def update_album(self, album_id: str, patch: dict) -> None: ...
    async def delete_album(self, album_id: str) -> None: ...
    async def create_album_in_space(
        self, album: GalleryAlbum, *, space_id: str
    ) -> bool: ...
    async def update_album_in_space(
        self, album_id: str, patch: dict, *, space_id: str
    ) -> bool: ...
    async def delete_album_in_space(self, album_id: str, *, space_id: str) -> bool: ...
    async def list_album_media(self, album_id: str) -> list[str]: ...
    async def list_items(
        self,
        album_id: str,
        *,
        limit: int = 50,
        before: str | None = None,
    ) -> list[GalleryItem]: ...
    async def list_items_since(
        self,
        space_id: str,
        since: str,
        *,
        limit: int = 500,
    ) -> list[GalleryItem]: ...
    async def get_item(self, item_id: str) -> GalleryItem | None: ...
    async def list_items_by_source_post(
        self,
        post_id: str,
    ) -> list[GalleryItem]: ...
    async def create_item(self, item: GalleryItem) -> GalleryItem: ...
    async def delete_item(self, item_id: str) -> None: ...
    async def create_item_in_space(
        self,
        item: GalleryItem,
        *,
        space_id: str,
        bump_count: bool = True,
    ) -> bool: ...
    async def delete_item_in_space(self, item_id: str, *, space_id: str) -> bool: ...
    async def delete_items_by_source_post(
        self,
        post_id: str,
    ) -> tuple[str | None, int]: ...
    async def increment_item_count(self, album_id: str, delta: int) -> None: ...
    async def recount_items(self, album_id: str) -> int: ...
    async def get_first_item_thumbnail(self, album_id: str) -> str | None: ...
    async def list_space_item_filenames(self, space_id: str) -> list[str]: ...
    async def set_retention_exempt(
        self,
        album_id: str,
        exempt: bool,
        *,
        space_id: str | None = None,
    ) -> None: ...


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
        )

    async def list_albums(
        self,
        space_id: str | None,
        *,
        limit: int = 30,
        before: str | None = None,
    ) -> list[GalleryAlbum]:
        limit = max(1, min(limit, 200))
        # ``space_id IS ?`` matches both NULL (household) and a specific id.
        # System album pinned first via ``is_system DESC``.
        if before:
            rows = await self._db.fetchall(
                "SELECT * FROM gallery_albums WHERE space_id IS ? AND created_at < ? "
                "ORDER BY is_system DESC, created_at DESC LIMIT ?",
                (space_id, before, limit),
            )
        else:
            rows = await self._db.fetchall(
                "SELECT * FROM gallery_albums WHERE space_id IS ? "
                "ORDER BY is_system DESC, created_at DESC LIMIT ?",
                (space_id, limit),
            )
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
            "SELECT * FROM gallery_albums WHERE id=?",
            (album_id,),
        )
        return self._row_to_album(dict(row)) if row else None

    async def create_album(self, album: GalleryAlbum) -> GalleryAlbum:
        now = album.created_at or datetime.now(timezone.utc).isoformat()
        # ``ON CONFLICT DO NOTHING`` is the race guard for the system
        # album path: the partial unique index on (space_id, is_system)
        # prevents two concurrent ``ensure_system_album`` callers from
        # both inserting. User-created albums never collide here (they
        # have ``is_system=0``, which the partial index ignores).
        await self._db.enqueue(
            """
            INSERT INTO gallery_albums(
                id, space_id, retention_exempt, is_system, owner_user_id,
                name, description, cover_item_id, item_count, created_at, updated_at
            ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT DO NOTHING
            """,
            (
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
            ),
        )
        return album

    async def update_album(self, album_id: str, patch: dict) -> None:
        safe = {k: v for k, v in patch.items() if k in self._ALBUM_PATCH_ALLOWED}
        if not safe:
            return
        set_clause = ", ".join(f"{k}=?" for k in safe)
        await self._db.enqueue(
            f"UPDATE gallery_albums SET {set_clause}, updated_at=? WHERE id=?",
            (*safe.values(), datetime.now(timezone.utc).isoformat(), album_id),
        )

    async def delete_album(self, album_id: str) -> None:
        await self._db.enqueue(
            "DELETE FROM gallery_albums WHERE id=?",
            (album_id,),
        )

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
        now = album.created_at or datetime.now(timezone.utc).isoformat()

        def _run(conn) -> bool:
            row = conn.execute(
                "SELECT space_id, owner_user_id FROM gallery_albums WHERE id=?",
                (album.id,),
            ).fetchone()
            if row is not None:
                return row[0] == space_id and row[1] == album.owner_user_id
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
                    " WHERE id=? AND space_id=? AND is_system=0",
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
                set_clause = ", ".join(f"{k}=?" for k in safe)
                conn.execute(
                    f"UPDATE gallery_albums SET {set_clause}, updated_at=?"
                    " WHERE id=? AND space_id=?",
                    (
                        *safe.values(),
                        datetime.now(timezone.utc).isoformat(),
                        album_id,
                        space_id,
                    ),
                )
            return True

        return bool(await self._db.transact(_run))

    async def list_album_media(self, album_id: str) -> list[str]:
        """The ``api/media/`` references of every item file in the album —
        what an album delete leaves behind for the media cleanup."""
        rows = await self._db.fetchall(
            "SELECT filename, thumbnail_filename FROM gallery_items WHERE album_id=?",
            (album_id,),
        )
        out: list[str] = []
        for r in rows:
            for name in (r[0], r[1]):
                if name:
                    out.append(f"api/media/{name}")
        return out

    async def delete_album_in_space(self, album_id: str, *, space_id: str) -> bool:
        """Delete a user album of ``space_id`` and (by cascade) its items.

        ``False`` when the album is unknown, lives elsewhere, or is the
        system album.
        """

        def _run(conn) -> bool:
            cur = conn.execute(
                "DELETE FROM gallery_albums WHERE id=? AND space_id=? AND is_system=0",
                (album_id, space_id),
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
    ) -> list[GalleryItem]:
        limit = max(1, min(limit, 500))
        if before:
            rows = await self._db.fetchall(
                "SELECT * FROM gallery_items WHERE album_id=? AND created_at < ? "
                "ORDER BY sort_order, created_at LIMIT ?",
                (album_id, before, limit),
            )
        else:
            rows = await self._db.fetchall(
                "SELECT * FROM gallery_items WHERE album_id=? "
                "ORDER BY sort_order, created_at LIMIT ?",
                (album_id, limit),
            )
        return [self._row_to_item(r) for r in rows_to_dicts(rows)]

    async def get_item(self, item_id: str) -> GalleryItem | None:
        row = await self._db.fetchone(
            "SELECT * FROM gallery_items WHERE id=?",
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
            "SELECT * FROM gallery_items WHERE source_post_id=?",
            (post_id,),
        )
        return [self._row_to_item(r) for r in rows_to_dicts(rows)]

    async def create_item(self, item: GalleryItem) -> GalleryItem:
        # Strip the "/api/media/" prefix so the column stores the bare filename.
        await self._db.enqueue(
            """
            INSERT INTO gallery_items(
                id, album_id, uploaded_by, item_type,
                filename, thumbnail_filename, width, height,
                duration_s, caption, taken_at, sort_order,
                source_post_id, created_at
            ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            -- Idempotent, matching ``create_album``: a §25.6 sync can
            -- legitimately re-deliver the same item (a resumed stream, a
            -- re-issued BEGIN after a restart), and that is not an error.
            -- Without this, a redelivery raised and the receiver's blanket
            -- ``except`` hid it along with the failures that mattered.
            ON CONFLICT DO NOTHING
            """,
            self._item_params(item),
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
            item.created_at or datetime.now(timezone.utc).isoformat(),
        )

    async def delete_item(self, item_id: str) -> None:
        await self._db.enqueue(
            "DELETE FROM gallery_items WHERE id=?",
            (item_id,),
        )

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
                    "SELECT 1 FROM gallery_albums WHERE id=? AND space_id=?",
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

    async def delete_item_in_space(self, item_id: str, *, space_id: str) -> bool:
        """Delete an item whose album lives in ``space_id`` (federation).

        ``False`` when the item is unknown or sits in an album of another
        space / the household gallery. Decrements the album's
        ``item_count`` in the same transaction.
        """

        def _run(conn) -> bool:
            row = conn.execute(
                "SELECT i.album_id FROM gallery_items i"
                " JOIN gallery_albums a ON a.id = i.album_id"
                " WHERE i.id=? AND a.space_id=?",
                (item_id, space_id),
            ).fetchone()
            if row is None:
                return False
            conn.execute("DELETE FROM gallery_items WHERE id=?", (item_id,))
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
            "SELECT COUNT(*) AS n FROM gallery_items WHERE album_id=?",
            (album_id,),
        )
        n = int(row["n"]) if row else 0
        await self._db.enqueue(
            "UPDATE gallery_albums SET item_count=? WHERE id=?",
            (n, album_id),
        )
        return n

    async def get_first_item_thumbnail(self, album_id: str) -> str | None:
        row = await self._db.fetchone(
            "SELECT thumbnail_filename FROM gallery_items WHERE album_id=? "
            "ORDER BY sort_order, created_at LIMIT 1",
            (album_id,),
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


def _basename(url: str) -> str:
    """The stored file name of an ``api/media/<name>`` URL — no path, no query."""
    return url.rsplit("/", 1)[-1].split("?", 1)[0]
