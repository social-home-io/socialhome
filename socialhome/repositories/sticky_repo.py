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
from ..domain.sticky import DEFAULT_STICKY_COLOR, Sticky, normalize_sticky_color
from ..domain.tombstone import SpaceRowTombstone
from ..federation.owner_bound_id import SPACE_STICKY_KIND, mint_owner_bound_id
from .base import changed_since_sql, row_to_dict, rows_to_dicts, sync_page_cursor


# Domain dataclass + field rules live in ``socialhome/domain/sticky.py``;
# ``Sticky`` / ``DEFAULT_COLOR`` stay importable from here for existing
# repo-level imports.
DEFAULT_COLOR = DEFAULT_STICKY_COLOR
__all__ = ["DEFAULT_COLOR", "AbstractStickyRepo", "SqliteStickyRepo", "Sticky"]


def mint_sticky_id(*, space_id: str, author: str) -> str:
    """A space sticky's owner-bound id (v_36): only the author's household
    can announce it."""
    return mint_owner_bound_id(
        SPACE_STICKY_KIND, space_id=space_id, owner_user_id=author
    )


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
        sticky_id: str | None = None,
    ) -> Sticky: ...
    async def get(self, sticky_id: str) -> Sticky | None: ...
    async def get_scoped(
        self, sticky_id: str, *, space_id: str | None
    ) -> Sticky | None: ...
    async def list(
        self, *, space_id: str | None = None, since_seq: int | None = None
    ) -> builtins.list[Sticky]:
        """Household stickies (``space_id`` ``None``) or a space's live ones;
        ``since_seq`` (§25.6 incremental, space only): those stamped above
        it (migration 0086)."""
        ...

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
    async def delete(
        self, sticky_id: str, *, space_id: str | None, deleted_by: str = ""
    ) -> bool: ...
    async def save(self, sticky: Sticky, *, space_id: str | None) -> bool: ...
    async def is_deleted(self, sticky_id: str, *, space_id: str) -> bool: ...
    async def tombstone(
        self,
        sticky_id: str,
        *,
        space_id: str,
        author: str,
        created_at: str = "",
        deleted_by: str = "",
    ) -> bool: ...
    async def list_tombstones_page(
        self,
        space_id: str,
        *,
        cursor: int | None = None,
        limit: int = 200,
        since: int | None = None,
    ) -> tuple[builtins.list[SpaceRowTombstone], int | None]:
        """One page of the space's sticky tombstones for the §25.6
        ``stickies_deleted`` resource, keyset on the row id."""
        ...


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
        sticky_id: str | None = None,
    ) -> Sticky:
        """Insert a note. ``sticky_id`` is the id a moderation-queue item
        minted at submit time (owner-bound to the same author); otherwise
        one is minted here."""
        content = content.strip()
        if not content:
            raise ValueError("sticky content must not be empty")
        now = datetime.now(timezone.utc).isoformat()
        sticky = Sticky(
            # A space sticky federates, so its id is owner-bound (v_36):
            # only the author's household can announce it.
            id=sticky_id
            or (
                uuid.uuid4().hex
                if space_id is None
                else mint_sticky_id(space_id=space_id, author=author)
            ),
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
            "SELECT * FROM stickies WHERE id=? AND deleted_at IS NULL",
            (sticky_id,),
        )
        return _row_to_sticky(row_to_dict(row))

    async def get_scoped(
        self,
        sticky_id: str,
        *,
        space_id: str | None,
    ) -> Sticky | None:
        """The sticky only if it lives in ``space_id`` (``None`` = the
        household board, matched null-safely via ``IS``).

        Route/service read path: a household lookup must never surface a
        space row and a space lookup never another space's row, so an id
        from the wrong scope reads as "not found". The unscoped
        :meth:`get` stays for federation inbound, which needs to tell a
        cross-space id apart from a missing one.
        """
        row = await self._db.fetchone(
            "SELECT * FROM stickies WHERE id=? AND space_id IS ?"
            " AND deleted_at IS NULL",
            (sticky_id, space_id),
        )
        return _row_to_sticky(row_to_dict(row))

    async def list(
        self,
        *,
        space_id: str | None = None,
        since_seq: int | None = None,
    ) -> builtins.list[Sticky]:
        if space_id is None:
            rows = await self._db.fetchall(
                "SELECT * FROM stickies WHERE space_id IS NULL ORDER BY created_at",
            )
        else:
            changed, changed_params = changed_since_sql("sync_seq", since_seq)
            rows = await self._db.fetchall(
                "SELECT * FROM stickies WHERE space_id=? AND deleted_at IS NULL"
                + changed
                + " ORDER BY created_at",
                (space_id, *changed_params),
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
            "WHERE space_id=? AND updated_at > ? AND deleted_at IS NULL "
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
            "WHERE id=? AND space_id IS ? AND deleted_at IS NULL",
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
            "updated_at=datetime('now') WHERE id=? AND space_id IS ?"
            " AND deleted_at IS NULL",
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
            "WHERE id=? AND space_id IS ? AND deleted_at IS NULL",
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
              AND stickies.deleted_at IS NULL
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

    async def delete(
        self, sticky_id: str, *, space_id: str | None, deleted_by: str = ""
    ) -> bool:
        """Delete a sticky of ``space_id``. ``False`` = not live there.

        A space sticky keeps its row as a tombstone (migration 0085): the
        content blanked, ``deleted_at`` / ``deleted_by`` set — the §25.6
        ``stickies_deleted`` resource streams it to a household that missed
        the delete, and no upsert brings the id back. A household sticky
        never federates and is removed outright.
        """
        if space_id is None:
            n = await self._db.enqueue_rowcount(
                "DELETE FROM stickies WHERE id=? AND space_id IS NULL",
                (sticky_id,),
            )
            return n > 0
        n = await self._db.enqueue_rowcount(
            "UPDATE stickies SET deleted_at=datetime('now'), deleted_by=?,"
            " content='' WHERE id=? AND space_id=? AND deleted_at IS NULL",
            (deleted_by or None, sticky_id, space_id),
        )
        return n > 0

    async def is_deleted(self, sticky_id: str, *, space_id: str) -> bool:
        row = await self._db.fetchone(
            "SELECT 1 FROM stickies WHERE id=? AND space_id=?"
            " AND deleted_at IS NOT NULL",
            (sticky_id, space_id),
        )
        return row is not None

    async def tombstone(
        self,
        sticky_id: str,
        *,
        space_id: str,
        author: str,
        created_at: str = "",
        deleted_by: str = "",
    ) -> bool:
        """Record a delete of a sticky never held here: a content-free stub
        row, so a stale copy streamed later cannot create it.

        Insert-only — an id held already (live or tombstoned, in any scope)
        is never touched; ``False`` says nothing was written. The caller
        must have proven the id is this space's (owner-bound to ``author``
        in ``space_id``): sticky ids are global.
        """
        n = await self._db.enqueue_rowcount(
            "INSERT INTO stickies(id, space_id, author, content, created_at,"
            " updated_at, deleted_at, deleted_by)"
            " VALUES(?, ?, ?, '', COALESCE(NULLIF(?, ''), datetime('now')),"
            " datetime('now'), datetime('now'), ?)"
            " ON CONFLICT(id) DO NOTHING",
            (sticky_id, space_id, author, created_at, deleted_by or None),
        )
        return n > 0

    async def list_tombstones_page(
        self,
        space_id: str,
        *,
        cursor: int | None = None,
        limit: int = 200,
        since: int | None = None,
    ) -> tuple[builtins.list[SpaceRowTombstone], int | None]:
        changed, changed_params = changed_since_sql("sync_seq", since)
        rows = rows_to_dicts(
            await self._db.fetchall(
                "SELECT rowid AS sync_rowid, id, author, created_at, deleted_at,"
                " deleted_by FROM stickies"
                " WHERE space_id=? AND deleted_at IS NOT NULL"
                + changed
                + " AND rowid > ? ORDER BY rowid LIMIT ?",
                (space_id, *changed_params, cursor or 0, int(limit)),
            )
        )
        return [
            SpaceRowTombstone(
                id=r["id"],
                owner=r["author"],
                created_at=r["created_at"] or "",
                deleted_at=r["deleted_at"],
                deleted_by=r["deleted_by"] or "",
            )
            for r in rows
        ], sync_page_cursor(rows, limit)


def _row_to_sticky(row: dict | None) -> Sticky | None:
    if row is None:
        return None
    return Sticky(
        id=row["id"],
        author=row["author"],
        content=row["content"],
        # Read-side guard: a row stored before colours were validated
        # (e.g. ``url(...)`` or the legacy inbound default ``"yellow"``)
        # must never reach the SPA's CSS ``background``.
        color=normalize_sticky_color(row.get("color")) or DEFAULT_COLOR,
        position_x=float(row.get("position_x") or 0.0),
        position_y=float(row.get("position_y") or 0.0),
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        space_id=row.get("space_id"),
    )
