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
    async def create_album(self, album: GalleryAlbum) -> GalleryAlbum: ...
    async def update_album(self, album_id: str, patch: dict) -> None: ...
    async def delete_album(self, album_id: str) -> None: ...
    async def list_items(
        self,
        album_id: str,
        *,
        limit: int = 50,
        before: str | None = None,
    ) -> list[GalleryItem]: ...
    async def get_item(self, item_id: str) -> GalleryItem | None: ...
    async def create_item(self, item: GalleryItem) -> GalleryItem: ...
    async def delete_item(self, item_id: str) -> None: ...
    async def increment_item_count(self, album_id: str, delta: int) -> None: ...
    async def get_first_item_thumbnail(self, album_id: str) -> str | None: ...
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
            owner_user_id=r["owner_user_id"],
            name=r["name"],
            description=r.get("description"),
            cover_item_id=r.get("cover_item_id"),
            item_count=int(r.get("item_count") or 0),
            retention_exempt=bool(r.get("retention_exempt")),
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
        if before:
            rows = await self._db.fetchall(
                "SELECT * FROM gallery_albums WHERE space_id IS ? AND created_at < ? "
                "ORDER BY created_at DESC LIMIT ?",
                (space_id, before, limit),
            )
        else:
            rows = await self._db.fetchall(
                "SELECT * FROM gallery_albums WHERE space_id IS ? "
                "ORDER BY created_at DESC LIMIT ?",
                (space_id, limit),
            )
        return [self._row_to_album(r) for r in rows_to_dicts(rows)]

    async def get_album(self, album_id: str) -> GalleryAlbum | None:
        row = await self._db.fetchone(
            "SELECT * FROM gallery_albums WHERE id=?",
            (album_id,),
        )
        return self._row_to_album(dict(row)) if row else None

    async def create_album(self, album: GalleryAlbum) -> GalleryAlbum:
        now = album.created_at or datetime.now(timezone.utc).isoformat()
        await self._db.enqueue(
            """
            INSERT INTO gallery_albums(
                id, space_id, retention_exempt, owner_user_id,
                name, description, cover_item_id, item_count, created_at, updated_at
            ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                album.id,
                album.space_id,
                int(album.retention_exempt),
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
            url=f"/api/media/{r['filename']}",
            thumbnail_url=f"/api/media/{r['thumbnail_filename']}",
            width=int(r["width"]),
            height=int(r["height"]),
            duration_s=r.get("duration_s"),
            caption=r.get("caption"),
            taken_at=r.get("taken_at"),
            sort_order=int(r.get("sort_order") or 0),
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

    async def create_item(self, item: GalleryItem) -> GalleryItem:
        # Strip the "/api/media/" prefix so the column stores the bare filename.
        await self._db.enqueue(
            """
            INSERT INTO gallery_items(
                id, album_id, uploaded_by, item_type,
                filename, thumbnail_filename, width, height,
                duration_s, caption, taken_at, sort_order, created_at
            ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                item.id,
                item.album_id,
                item.uploaded_by,
                item.item_type,
                item.url.rsplit("/", 1)[-1],
                item.thumbnail_url.rsplit("/", 1)[-1],
                item.width,
                item.height,
                item.duration_s,
                item.caption,
                item.taken_at,
                item.sort_order,
                item.created_at or datetime.now(timezone.utc).isoformat(),
            ),
        )
        return item

    async def delete_item(self, item_id: str) -> None:
        await self._db.enqueue(
            "DELETE FROM gallery_items WHERE id=?",
            (item_id,),
        )

    async def increment_item_count(self, album_id: str, delta: int) -> None:
        await self._db.enqueue(
            "UPDATE gallery_albums SET item_count=MAX(0, item_count + ?) WHERE id=?",
            (delta, album_id),
        )

    async def get_first_item_thumbnail(self, album_id: str) -> str | None:
        row = await self._db.fetchone(
            "SELECT thumbnail_filename FROM gallery_items WHERE album_id=? "
            "ORDER BY sort_order, created_at LIMIT 1",
            (album_id,),
        )
        return f"/api/media/{row['thumbnail_filename']}" if row else None
