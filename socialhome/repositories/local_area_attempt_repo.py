"""Daily local-community budgets (``local_area_attempts``, migration 0109).

One counter per (space, subject kind, subject, UTC day). The service layer
(:class:`~socialhome.services.local_community.LocalAreaBudget`) decides
what is counted and how much is allowed; this repo only spends a budget
atomically and prunes the days that no longer matter.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol

from ..db import AsyncDatabase

__all__ = [
    "AbstractLocalAreaAttemptRepo",
    "BudgetLine",
    "SqliteLocalAreaAttemptRepo",
]

#: ``(subject_kind, subject_id, limit)`` — one counter :meth:`try_spend`
#: checks and, when every line still has room, increments.
type BudgetLine = tuple[str, str, int]


class AbstractLocalAreaAttemptRepo(Protocol):
    async def try_spend(
        self,
        space_id: str,
        day: str,
        lines: Sequence[BudgetLine],
        *,
        prune_before: str,
    ) -> bool:
        """Spend one from every line for ``space_id`` on ``day`` — all or
        none: ``False`` (nothing spent) when any line is already at its
        limit. Days before ``prune_before`` are deleted in the same step."""
        ...


class SqliteLocalAreaAttemptRepo:
    """SQLite implementation backed by ``local_area_attempts``."""

    __slots__ = ("_db",)

    def __init__(self, db: AsyncDatabase) -> None:
        self._db = db

    async def try_spend(
        self,
        space_id: str,
        day: str,
        lines: Sequence[BudgetLine],
        *,
        prune_before: str,
    ) -> bool:
        def _run(conn) -> bool:
            conn.execute(
                "DELETE FROM local_area_attempts WHERE day < ?", (prune_before,)
            )
            for kind, subject, limit in lines:
                row = conn.execute(
                    "SELECT count FROM local_area_attempts"
                    " WHERE space_id=? AND subject_kind=? AND subject_id=? AND day=?",
                    (space_id, kind, subject, day),
                ).fetchone()
                if row is not None and int(row[0]) >= limit:
                    return False
            for kind, subject, _limit in lines:
                conn.execute(
                    "INSERT INTO local_area_attempts"
                    "(space_id, subject_kind, subject_id, day, count)"
                    " VALUES(?, ?, ?, ?, 1)"
                    " ON CONFLICT(space_id, subject_kind, subject_id, day)"
                    " DO UPDATE SET count = count + 1",
                    (space_id, kind, subject, day),
                )
            return True

        # BEGIN IMMEDIATE: two checks racing for the last unit cannot both
        # see room.
        return bool(await self._db.transact(_run))
