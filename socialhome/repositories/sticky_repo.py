"""Sticky-note repository.

The ``stickies`` table is shared between the household board (rows with
``space_id IS NULL``) and per-space sticky boards (``space_id`` set).
Distinguishing the two is a scope question, not a shape difference — so a
single repo with a ``space_id: str | None`` parameter handles both.
"""

from __future__ import annotations

import builtins
import uuid
from datetime import datetime, timezone
from typing import Protocol, runtime_checkable

from ..db import AsyncDatabase
from .base import row_to_dict, rows_to_dicts


DEFAULT_COLOR = "#FFF9B1"


# Domain dataclass lives in ``socialhome/domain/sticky.py``;
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
    async def list(self, *, space_id: str | None = None) -> builtins.list[Sticky]: ...
    async def list_since(
        self,
        space_id: str,
        since: str,
        *,
        limit: int = 500,
    ) -> builtins.list[Sticky]: ...
    async def update_content(
        self,
        sticky_id: str,
        content: str,
        *,
        space_id: str | None,
    ) -> bool: ...
    async def update_position(
        self,
        sticky_id: str,
        x: float,
        y: float,
        *,
        space_id: str | None,
    ) -> bool: ...
    async def update_color(
        self,
        sticky_id: str,
        color: str,
        *,
        space_id: str | None,
    ) -> bool: ...
    async def delete(self, sticky_id: str, *, space_id: str | None) -> bool: ...
    async def save(self, sticky: Sticky, *, space_id: str | None) -> bool: ...


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
    ) -> builtins.list[Sticky]:
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

    async def list_since(
        self,
        space_id: str,
        since: str,
        *,
        limit: int = 500,
    ) -> builtins.list[Sticky]:
        """Stickies in *space_id* with ``updated_at > since``, oldest-first.

        Household-scoped (NULL space_id) stickies are never returned —
        they don't federate so a peer has no use for them.

        Shape trap: ``updated_at`` is mixed — created rows carry the
        Python tz-aware ISO shape, later edits overwrite it with SQLite's
        naive ``datetime('now')``. The only caller today passes the
        ``1970-01-01T00:00:00+00:00`` epoch cursor, whose year digits
        decide the comparison long before the separator does, so the raw
        ``>`` is safe. A real (non-epoch) ``since`` must wrap both sides
        in ``datetime()`` — "T" (0x54) sorts above " " (0x20) in a raw
        TEXT compare, so same-day rows would be skipped on resume.
        """
        rows = await self._db.fetchall(
            "SELECT * FROM stickies "
            "WHERE space_id=? AND updated_at > ? "
            "ORDER BY updated_at ASC LIMIT ?",
            (space_id, since, int(limit)),
        )
        return [s for s in (_row_to_sticky(d) for d in rows_to_dicts(rows)) if s]

    async def update_content(
        self,
        sticky_id: str,
        content: str,
        *,
        space_id: str | None,
    ) -> bool:
        content = content.strip()
        if not content:
            raise ValueError("sticky content must not be empty")
        n = await self._db.enqueue_rowcount(
            "UPDATE stickies SET content=?, updated_at=datetime('now') "
            "WHERE id=? AND space_id IS ?",
            (content, sticky_id, space_id),
        )
        return n > 0

    async def update_position(
        self,
        sticky_id: str,
        x: float,
        y: float,
        *,
        space_id: str | None,
    ) -> bool:
        n = await self._db.enqueue_rowcount(
            "UPDATE stickies SET position_x=?, position_y=?, "
            "updated_at=datetime('now') WHERE id=? AND space_id IS ?",
            (float(x), float(y), sticky_id, space_id),
        )
        return n > 0

    async def update_color(
        self,
        sticky_id: str,
        color: str,
        *,
        space_id: str | None,
    ) -> bool:
        n = await self._db.enqueue_rowcount(
            "UPDATE stickies SET color=?, updated_at=datetime('now') "
            "WHERE id=? AND space_id IS ?",
            (color, sticky_id, space_id),
        )
        return n > 0

    async def save(self, sticky: Sticky, *, space_id: str | None) -> bool:
        """Upsert a sticky with an externally-provided id.

        Used by federation mirroring (§13) where the peer's id must be
        preserved on the local row — don't call :meth:`add` in that
        path because it mints a fresh id.

        ``space_id`` is authoritative (§24.11): it is the scope the
        inbound pipeline gated the sender on, and it — not
        ``sticky.space_id`` — decides both the column value and which
        rows this upsert may touch. A conflict on an id that already
        belongs to another space (household rows included, via the
        null-safe ``IS``) is refused and reported as ``False``.
        """
        n = await self._db.enqueue_rowcount(
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
            WHERE stickies.space_id IS excluded.space_id
            """,
            (
                sticky.id,
                space_id,
                sticky.author,
                sticky.content,
                sticky.color,
                sticky.position_x,
                sticky.position_y,
                sticky.created_at,
                sticky.updated_at,
            ),
        )
        return n > 0

    async def delete(self, sticky_id: str, *, space_id: str | None) -> bool:
        n = await self._db.enqueue_rowcount(
            "DELETE FROM stickies WHERE id=? AND space_id IS ?",
            (sticky_id, space_id),
        )
        return n > 0


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
