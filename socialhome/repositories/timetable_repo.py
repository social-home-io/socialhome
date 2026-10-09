"""Timetable repository (school *Stundenplan*).

Household timetables live in ``timetables``; space timetables in the
parallel ``space_timetables`` table (same content columns, plus
``space_id`` and a ``deleted_at`` tombstone, minus assignees). Migration
``0063_timetables.sql``.

The weekly grid, per-date overrides, defaults, days and excluded weeks are
small bounded lists stored as JSON. Their element shape is the domain wire
format — :func:`entry_to_dict` / :func:`override_to_dict` / … from
:mod:`socialhome.domain.timetable` — so the rows and the REST / federation
body can't drift.

Timestamps are stored as tz-aware ISO 8601 (like ``task_repo``), always
normalised to UTC with microseconds so the last-writer-wins comparison in
:meth:`SqliteSpaceTimetableRepo.apply_remote` can compare them as strings.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from datetime import datetime, timezone
from typing import Any, Protocol, runtime_checkable

from ..db import AsyncDatabase
from ..domain.timetable import (
    MAX_REMOTE_VERSION_JUMP,
    Timetable,
    defaults_from_dict,
    defaults_to_dict,
    entry_from_dict,
    entry_to_dict,
    override_from_dict,
    override_to_dict,
    validity_from_dict,
    validity_to_dict,
)
from .base import changed_since_sql, dump_json, load_json, row_to_dict, rows_to_dicts

log = logging.getLogger(__name__)


# ─── Shared column mapping ────────────────────────────────────────────────

#: Content columns shared by both tables, in :func:`_content_params` order.
_CONTENT_COLS: tuple[str, ...] = (
    "name",
    "color",
    "week_start",
    "tz",
    "days_json",
    "defaults_json",
    "entries_json",
    "overrides_json",
    "valid_from",
    "valid_until",
    "excluded_weeks_json",
    "version",
    "created_by",
    "updated_by",
    "created_at",
    "updated_at",
)
#: Columns an edit may change — authorship and creation time are fixed.
_MUTABLE_COLS: tuple[str, ...] = tuple(
    c for c in _CONTENT_COLS if c not in ("created_by", "created_at")
)


def _ts(value: datetime) -> str:
    """UTC, fixed-width ISO 8601 — lexicographic order == instant order."""
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat(timespec="microseconds")


def _content_params(tt: Timetable) -> dict[str, Any]:
    validity = validity_to_dict(tt.validity)
    return {
        "name": tt.name,
        "color": tt.color,
        "week_start": int(tt.week_start),
        "tz": tt.tz,
        "days_json": dump_json(list(tt.days)),
        "defaults_json": dump_json(defaults_to_dict(tt.defaults)),
        "entries_json": dump_json([entry_to_dict(e) for e in tt.entries]),
        "overrides_json": dump_json([override_to_dict(o) for o in tt.overrides]),
        "valid_from": validity["valid_from"],
        "valid_until": validity["valid_until"],
        "excluded_weeks_json": dump_json(validity["excluded_weeks"]),
        "version": int(tt.version),
        "created_by": tt.created_by,
        "updated_by": tt.updated_by,
        "created_at": _ts(tt.created_at),
        "updated_at": _ts(tt.updated_at),
    }


def _row_to_timetable(row: dict[str, Any]) -> Timetable:
    return Timetable(
        id=row["id"],
        name=row["name"],
        created_by=row["created_by"],
        created_at=datetime.fromisoformat(row["created_at"]),
        updated_at=datetime.fromisoformat(row["updated_at"]),
        week_start=int(row["week_start"]),
        tz=row["tz"],
        color=row.get("color"),
        days=tuple(int(d) for d in load_json(row.get("days_json"), [0, 1, 2, 3, 4])),
        defaults=defaults_from_dict(load_json(row.get("defaults_json"), {})),
        entries=tuple(
            entry_from_dict(e) for e in load_json(row.get("entries_json"), [])
        ),
        overrides=tuple(
            override_from_dict(o) for o in load_json(row.get("overrides_json"), [])
        ),
        validity=validity_from_dict(
            {
                "valid_from": row.get("valid_from"),
                "valid_until": row.get("valid_until"),
                "excluded_weeks": load_json(row.get("excluded_weeks_json"), []),
            }
        ),
        assignees=tuple(load_json(row.get("assignees_json"), [])),
        version=int(row["version"]),
        updated_by=row.get("updated_by"),
    )


# ─── Household timetables ─────────────────────────────────────────────────

_HH_INSERT_COLS = ("id", *_CONTENT_COLS, "assignees_json")
_HH_INSERT_SQL = (
    f"INSERT INTO timetables({', '.join(_HH_INSERT_COLS)}) "
    f"VALUES({', '.join('?' * len(_HH_INSERT_COLS))})"
)
_HH_UPDATE_COLS = (*_MUTABLE_COLS, "assignees_json")
_HH_SAVE_SQL = (
    "UPDATE timetables SET "
    + ", ".join(f"{c}=?" for c in _HH_UPDATE_COLS)
    + " WHERE id=? AND version=?"
)


def _household_params(tt: Timetable) -> dict[str, Any]:
    return {**_content_params(tt), "assignees_json": dump_json(list(tt.assignees))}


@runtime_checkable
class AbstractTimetableRepo(Protocol):
    async def get(self, timetable_id: str) -> Timetable | None: ...
    async def list_all(self) -> list[Timetable]: ...
    async def count(self) -> int: ...
    async def insert(self, tt: Timetable) -> None: ...
    async def save(self, tt: Timetable, *, expected_version: int) -> bool: ...
    async def delete(self, timetable_id: str) -> bool: ...


class SqliteTimetableRepo:
    """SQLite-backed :class:`AbstractTimetableRepo`."""

    def __init__(self, db: AsyncDatabase) -> None:
        self._db = db

    async def get(self, timetable_id: str) -> Timetable | None:
        row = await self._db.fetchone(
            "SELECT * FROM timetables WHERE id=?",
            (timetable_id,),
        )
        d = row_to_dict(row)
        return None if d is None else _row_to_timetable(d)

    async def list_all(self) -> list[Timetable]:
        rows = await self._db.fetchall(
            "SELECT * FROM timetables ORDER BY name COLLATE NOCASE, created_at, id",
        )
        return [_row_to_timetable(d) for d in rows_to_dicts(rows)]

    async def count(self) -> int:
        return int(await self._db.fetchval("SELECT COUNT(*) FROM timetables", (), 0))

    async def insert(self, tt: Timetable) -> None:
        params = {"id": tt.id, **_household_params(tt)}
        await self._db.enqueue(
            _HH_INSERT_SQL,
            tuple(params[c] for c in _HH_INSERT_COLS),
        )

    async def save(self, tt: Timetable, *, expected_version: int) -> bool:
        """Compare-and-swap: write only if the row is still at ``expected_version``."""
        params = _household_params(tt)
        n = await self._db.enqueue_rowcount(
            _HH_SAVE_SQL,
            (*(params[c] for c in _HH_UPDATE_COLS), tt.id, int(expected_version)),
        )
        return n > 0

    async def delete(self, timetable_id: str) -> bool:
        n = await self._db.enqueue_rowcount(
            "DELETE FROM timetables WHERE id=?",
            (timetable_id,),
        )
        return n > 0


# ─── Space timetables ─────────────────────────────────────────────────────

_SP_INSERT_COLS = ("id", "space_id", *_CONTENT_COLS)
_SP_SELECT_VALUES = ", ".join("?" * len(_SP_INSERT_COLS))
_SP_INSERT_SQL = (
    f"INSERT INTO space_timetables({', '.join(_SP_INSERT_COLS)}) "
    f"SELECT {_SP_SELECT_VALUES} "
    "WHERE EXISTS (SELECT 1 FROM spaces WHERE id=?) "
    "ON CONFLICT(id) DO NOTHING"
)
_SP_SAVE_SQL = (
    "UPDATE space_timetables SET "
    + ", ".join(f"{c}=?" for c in _MUTABLE_COLS)
    + " WHERE id=? AND space_id=? AND version=? AND deleted_at IS NULL"
)
# Last-writer-wins upsert for federation replicas. The DO UPDATE guard
# refuses a cross-space id (``space_id`` is authoritative, §24.11), never
# touches a tombstone, refuses a version jump past MAX_REMOTE_VERSION_JUMP
# (a hostile peer freezing the row), and only lets a strictly newer
# (version, updated_at, updated_by) through — so a replayed or reordered
# event is a no-op, and two replicas that saw concurrent edits with the
# same version and timestamp still converge on the same row (NULL
# updated_by compares as '').
_SP_APPLY_REMOTE_SQL = (
    f"INSERT INTO space_timetables({', '.join(_SP_INSERT_COLS)}) "
    f"SELECT {_SP_SELECT_VALUES} "
    "WHERE EXISTS (SELECT 1 FROM spaces WHERE id=?) "
    "ON CONFLICT(id) DO UPDATE SET "
    + ", ".join(f"{c}=excluded.{c}" for c in _MUTABLE_COLS)
    + " WHERE space_timetables.space_id = excluded.space_id"
    " AND space_timetables.deleted_at IS NULL"
    f" AND excluded.version <= space_timetables.version + {MAX_REMOTE_VERSION_JUMP}"
    " AND (excluded.version > space_timetables.version"
    " OR (excluded.version = space_timetables.version"
    " AND excluded.updated_at > space_timetables.updated_at)"
    " OR (excluded.version = space_timetables.version"
    " AND excluded.updated_at = space_timetables.updated_at"
    " AND COALESCE(excluded.updated_by, '')"
    " > COALESCE(space_timetables.updated_by, '')))"
)
# Tombstone an id whether or not we've seen it — a delete that overtakes
# its create must still win. The stub row (empty name/author/content) is
# never readable; the conflict branch tombstones a live row in the same
# space and refuses an id owned by another space.
_SP_TOMBSTONE_SQL = (
    "INSERT INTO space_timetables("
    "id, space_id, name, created_by, created_at, updated_at, deleted_at) "
    "SELECT ?, ?, '', '', ?, ?, ? "
    "WHERE EXISTS (SELECT 1 FROM spaces WHERE id=?) "
    "ON CONFLICT(id) DO UPDATE SET deleted_at=excluded.deleted_at,"
    " entries_json='[]', overrides_json='[]'"
    " WHERE space_timetables.space_id = excluded.space_id"
)


def _space_insert_params(tt: Timetable, space_id: str) -> tuple[Any, ...]:
    params = {"id": tt.id, "space_id": space_id, **_content_params(tt)}
    return (*(params[c] for c in _SP_INSERT_COLS), space_id)


@runtime_checkable
class AbstractSpaceTimetableRepo(Protocol):
    async def get(self, timetable_id: str) -> tuple[str, Timetable] | None: ...
    async def is_tombstoned(self, timetable_id: str) -> bool: ...
    async def list_by_space(
        self, space_id: str, *, since_seq: int | None = None
    ) -> list[Timetable]:
        """The space's live timetables; ``since_seq`` (§25.6 incremental,
        migration 0088): only those stamped above it."""
        ...

    async def list_by_ids(
        self,
        ids: Sequence[str],
    ) -> list[tuple[str, Timetable]]: ...
    async def count_in_space(self, space_id: str) -> int: ...
    async def insert(self, tt: Timetable, *, space_id: str) -> bool: ...
    async def save(
        self,
        tt: Timetable,
        *,
        space_id: str,
        expected_version: int,
    ) -> bool: ...
    async def apply_remote(self, tt: Timetable, *, space_id: str) -> bool: ...
    async def soft_delete(
        self,
        timetable_id: str,
        *,
        space_id: str,
        at: datetime,
    ) -> bool: ...
    async def tombstone(
        self,
        timetable_id: str,
        *,
        space_id: str,
        at: datetime,
    ) -> bool: ...


class SqliteSpaceTimetableRepo:
    """SQLite-backed :class:`AbstractSpaceTimetableRepo`.

    Space timetables carry no assignees — the domain field is always
    ``()`` on read and ignored on write. Tombstoned rows are invisible to
    every read except :meth:`is_tombstoned`.
    """

    def __init__(self, db: AsyncDatabase) -> None:
        self._db = db

    async def get(self, timetable_id: str) -> tuple[str, Timetable] | None:
        row = await self._db.fetchone(
            "SELECT * FROM space_timetables WHERE id=? AND deleted_at IS NULL",
            (timetable_id,),
        )
        d = row_to_dict(row)
        if d is None:
            return None
        return d["space_id"], _row_to_timetable(d)

    async def is_tombstoned(self, timetable_id: str) -> bool:
        row = await self._db.fetchone(
            "SELECT 1 FROM space_timetables WHERE id=? AND deleted_at IS NOT NULL",
            (timetable_id,),
        )
        return row is not None

    async def list_by_space(
        self, space_id: str, *, since_seq: int | None = None
    ) -> list[Timetable]:
        changed, changed_params = changed_since_sql("sync_seq", since_seq)
        rows = await self._db.fetchall(
            "SELECT * FROM space_timetables"
            " WHERE space_id=? AND deleted_at IS NULL"
            + changed
            + " ORDER BY name COLLATE NOCASE, created_at, id",
            (space_id, *changed_params),
        )
        return [_row_to_timetable(d) for d in rows_to_dicts(rows)]

    async def list_by_ids(
        self,
        ids: Sequence[str],
    ) -> list[tuple[str, Timetable]]:
        ids = list(dict.fromkeys(ids))
        if not ids:
            return []
        placeholders = ",".join("?" * len(ids))
        rows = await self._db.fetchall(
            "SELECT * FROM space_timetables"
            f" WHERE id IN ({placeholders}) AND deleted_at IS NULL"
            " ORDER BY name COLLATE NOCASE, created_at, id",
            tuple(ids),
        )
        return [(d["space_id"], _row_to_timetable(d)) for d in rows_to_dicts(rows)]

    async def count_in_space(self, space_id: str) -> int:
        return int(
            await self._db.fetchval(
                "SELECT COUNT(*) FROM space_timetables"
                " WHERE space_id=? AND deleted_at IS NULL",
                (space_id,),
                0,
            )
        )

    async def insert(self, tt: Timetable, *, space_id: str) -> bool:
        """Insert into ``space_id``; ``False`` on a missing space or a taken id
        (including a tombstoned one)."""
        n = await self._db.enqueue_rowcount(
            _SP_INSERT_SQL,
            _space_insert_params(tt, space_id),
        )
        return n > 0

    async def save(
        self,
        tt: Timetable,
        *,
        space_id: str,
        expected_version: int,
    ) -> bool:
        """Compare-and-swap scoped to ``space_id``; never writes a tombstone."""
        params = _content_params(tt)
        n = await self._db.enqueue_rowcount(
            _SP_SAVE_SQL,
            (
                *(params[c] for c in _MUTABLE_COLS),
                tt.id,
                space_id,
                int(expected_version),
            ),
        )
        return n > 0

    async def apply_remote(self, tt: Timetable, *, space_id: str) -> bool:
        """Last-writer-wins upsert of a replicated timetable.

        ``True`` when a row was inserted or updated; ``False`` for a stale
        copy, a missing space, an id owned by another space, or a tombstone.
        """
        n = await self._db.enqueue_rowcount(
            _SP_APPLY_REMOTE_SQL,
            _space_insert_params(tt, space_id),
        )
        return n > 0

    async def soft_delete(
        self,
        timetable_id: str,
        *,
        space_id: str,
        at: datetime,
    ) -> bool:
        """Tombstone the row and drop its content; ``False`` if nothing live."""
        n = await self._db.enqueue_rowcount(
            "UPDATE space_timetables"
            " SET deleted_at=?, entries_json='[]', overrides_json='[]'"
            " WHERE id=? AND space_id=? AND deleted_at IS NULL",
            (_ts(at), timetable_id, space_id),
        )
        return n > 0

    async def tombstone(
        self,
        timetable_id: str,
        *,
        space_id: str,
        at: datetime,
    ) -> bool:
        """Tombstone ``timetable_id`` in ``space_id``, even if never seen.

        For a replicated delete that may overtake its create: an unknown id
        gets a content-free stub row so a later :meth:`apply_remote` /
        :meth:`insert` can't resurrect it. ``False`` for a missing space or
        an id owned by another space.
        """
        stamp = _ts(at)
        n = await self._db.enqueue_rowcount(
            _SP_TOMBSTONE_SQL,
            (timetable_id, space_id, stamp, stamp, stamp, space_id),
        )
        return n > 0
