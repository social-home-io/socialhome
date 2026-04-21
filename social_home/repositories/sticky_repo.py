"""Sticky-note repository.

The ``stickies`` table is shared between the household board (rows with
``space_id IS NULL``) and per-space sticky boards (``space_id`` set).
Distinguishing the two is a scope question, not a shape difference — so a
single repo with a ``space_id: str | None`` parameter handles both.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Protocol, runtime_checkable

from ..db import AsyncDatabase
from .base import row_to_dict, rows_to_dicts


DEFAULT_COLOR = "#FFF9B1"


# Domain dataclass lives in ``social_home/domain/sticky.py``;
# re-exported here so existing repo-level imports keep working.
from ..domain.sticky import Sticky  # noqa: F401,E402


@runtime_checkable
class AbstractStickyRepo(Protocol):
    async def add(
        self,
        *,
        author: str,
        content: str,
        color: str = DEFAULT_COLOR,
        position_x: float = 0.0,
        position_y: float = 0.0,
        space_id: str | None = None,
    ) -> Sticky: ...
    async def get(self, sticky_id: str) -> Sticky | None: ...
    async def list(self, *, space_id: str | None = None) -> list[Sticky]: ...
    async def update_content(self, sticky_id: str, content: str) -> None: ...
    async def update_position(
        self,
        sticky_id: str,
        x: float,
        y: float,
    ) -> None: ...
    async def update_color(self, sticky_id: str, color: str) -> None: ...
    async def delete(self, sticky_id: str) -> None: ...
    async def save(self, sticky: Sticky) -> Sticky: ...


class SqliteStickyRepo:
    """SQLite-backed :class:`AbstractStickyRepo`."""

    def __init__(self, db: AsyncDatabase) -> None:
        self._db = db

    async def add(
        self,
        *,
        author: str,
        content: str,
        color: str = DEFAULT_COLOR,
        position_x: float = 0.0,
        position_y: float = 0.0,
        space_id: str | None = None,
    ) -> Sticky:
        content = content.strip()
        if not content:
            raise ValueError("sticky content must not be empty")
        now = datetime.now(timezone.utc).isoformat()
        sticky = Sticky(
            id=uuid.uuid4().hex,
            author=author,
            content=content,
            color=color,
            position_x=float(position_x),
            position_y=float(position_y),
            created_at=now,
            updated_at=now,
            space_id=space_id,
        )
        await self._db.enqueue(
            """
            INSERT INTO stickies(
                id, space_id, author, content, color, position_x, position_y,
                created_at, updated_at
            ) VALUES(?,?,?,?,?,?,?, COALESCE(?, datetime('now')),
                                     COALESCE(?, datetime('now')))
            """,
            (
                sticky.id,
                sticky.space_id,
                sticky.author,
                sticky.content,
                sticky.color,
                sticky.position_x,
                sticky.position_y,
                sticky.created_at,
                sticky.updated_at,
            ),
        )
        return sticky

    async def get(self, sticky_id: str) -> Sticky | None:
        row = await self._db.fetchone(
            "SELECT * FROM stickies WHERE id=?",
            (sticky_id,),
        )
        return _row_to_sticky(row_to_dict(row))

    async def list(
        self,
        *,
        space_id: str | None = None,
    ) -> list[Sticky]:
        if space_id is None:
            rows = await self._db.fetchall(
                "SELECT * FROM stickies WHERE space_id IS NULL ORDER BY created_at",
            )
        else:
            rows = await self._db.fetchall(
                "SELECT * FROM stickies WHERE space_id=? ORDER BY created_at",
                (space_id,),
            )
        return [s for s in (_row_to_sticky(d) for d in rows_to_dicts(rows)) if s]

    async def update_content(self, sticky_id: str, content: str) -> None:
        content = content.strip()
        if not content:
            raise ValueError("sticky content must not be empty")
        await self._db.enqueue(
            "UPDATE stickies SET content=?, updated_at=datetime('now') WHERE id=?",
            (content, sticky_id),
        )

    async def update_position(
        self,
        sticky_id: str,
        x: float,
        y: float,
    ) -> None:
        await self._db.enqueue(
            "UPDATE stickies SET position_x=?, position_y=?, "
            "updated_at=datetime('now') WHERE id=?",
            (float(x), float(y), sticky_id),
        )

    async def update_color(self, sticky_id: str, color: str) -> None:
        await self._db.enqueue(
            "UPDATE stickies SET color=?, updated_at=datetime('now') WHERE id=?",
            (color, sticky_id),
        )

    async def save(self, sticky: Sticky) -> Sticky:
        """Upsert a sticky with an externally-provided id.

        Used by federation mirroring (§13) where the peer's id must be
        preserved on the local row — don't call :meth:`add` in that
        path because it mints a fresh id.
        """
        await self._db.enqueue(
            """
            INSERT INTO stickies(
                id, space_id, author, content, color, position_x, position_y,
                created_at, updated_at
            ) VALUES(?,?,?,?,?,?,?, COALESCE(?, datetime('now')),
                                     COALESCE(?, datetime('now')))
            ON CONFLICT(id) DO UPDATE SET
                content=excluded.content,
                color=excluded.color,
                position_x=excluded.position_x,
                position_y=excluded.position_y,
                updated_at=excluded.updated_at
            """,
            (
                sticky.id,
                sticky.space_id,
                sticky.author,
                sticky.content,
                sticky.color,
                sticky.position_x,
                sticky.position_y,
                sticky.created_at,
                sticky.updated_at,
            ),
        )
        return sticky

    async def delete(self, sticky_id: str) -> None:
        await self._db.enqueue(
            "DELETE FROM stickies WHERE id=?",
            (sticky_id,),
        )


def _row_to_sticky(row: dict | None) -> Sticky | None:
    if row is None:
        return None
    return Sticky(
        id=row["id"],
        author=row["author"],
        content=row["content"],
        color=row.get("color", DEFAULT_COLOR),
        position_x=float(row.get("position_x") or 0.0),
        position_y=float(row.get("position_y") or 0.0),
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        space_id=row.get("space_id"),
    )
