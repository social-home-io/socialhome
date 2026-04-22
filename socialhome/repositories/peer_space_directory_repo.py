"""Peer-public-space directory repository (§D1a).

Mirrors each paired peer household's published list of ``type=public``
spaces. The local poll / inbound handler writes via :meth:`replace_snapshot`
(atomically swaps every row for that peer). Readers use :meth:`list_all`
or :meth:`list_for_instance` to render the "From friends" tab in the
space browser.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Protocol, runtime_checkable

from ..db.database import AsyncDatabase
from .base import rows_to_dicts


@dataclass(slots=True, frozen=True)
class PeerSpaceDirectoryEntry:
    """Single (peer, space) row in the peer-public-space directory."""

    instance_id: str
    space_id: str
    name: str
    description: str | None = None
    emoji: str | None = None
    member_count: int = 0
    join_mode: str = "request"
    min_age: int = 0
    target_audience: str = "all"
    updated_at: str | None = None
    cached_at: str | None = None


@runtime_checkable
class AbstractPeerSpaceDirectoryRepo(Protocol):
    async def replace_snapshot(
        self,
        instance_id: str,
        entries: list[PeerSpaceDirectoryEntry],
    ) -> None: ...

    async def list_all(
        self,
        *,
        max_min_age: int | None = None,
    ) -> list[PeerSpaceDirectoryEntry]: ...

    async def list_for_instance(
        self,
        instance_id: str,
    ) -> list[PeerSpaceDirectoryEntry]: ...

    async def clear_instance(self, instance_id: str) -> None: ...


class SqlitePeerSpaceDirectoryRepo:
    """SQLite-backed :class:`AbstractPeerSpaceDirectoryRepo`."""

    __slots__ = ("_db",)

    def __init__(self, db: AsyncDatabase) -> None:
        self._db = db

    async def replace_snapshot(
        self,
        instance_id: str,
        entries: list[PeerSpaceDirectoryEntry],
    ) -> None:
        cached = datetime.now(timezone.utc).isoformat()
        rows = [
            (
                instance_id,
                e.space_id,
                e.name,
                e.description,
                e.emoji,
                int(e.member_count or 0),
                e.join_mode or "request",
                int(e.min_age or 0),
                e.target_audience or "all",
                e.updated_at,
                cached,
            )
            for e in entries
        ]

        def _swap(conn):
            conn.execute(
                "DELETE FROM peer_space_directory WHERE instance_id=?",
                (instance_id,),
            )
            if rows:
                conn.executemany(
                    """
                    INSERT INTO peer_space_directory(
                        instance_id, space_id, name, description, emoji,
                        member_count, join_mode, min_age, target_audience,
                        updated_at, cached_at
                    ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    rows,
                )

        await self._db.transact(_swap)

    async def list_all(
        self,
        *,
        max_min_age: int | None = None,
    ) -> list[PeerSpaceDirectoryEntry]:
        if max_min_age is not None:
            rows = await self._db.fetchall(
                """
                SELECT * FROM peer_space_directory
                 WHERE min_age <= ?
                 ORDER BY member_count DESC, cached_at DESC
                """,
                (int(max_min_age),),
            )
        else:
            rows = await self._db.fetchall(
                "SELECT * FROM peer_space_directory"
                " ORDER BY member_count DESC, cached_at DESC",
            )
        return [_row(r) for r in rows_to_dicts(rows)]

    async def list_for_instance(
        self,
        instance_id: str,
    ) -> list[PeerSpaceDirectoryEntry]:
        rows = await self._db.fetchall(
            "SELECT * FROM peer_space_directory WHERE instance_id=?"
            " ORDER BY cached_at DESC",
            (instance_id,),
        )
        return [_row(r) for r in rows_to_dicts(rows)]

    async def clear_instance(self, instance_id: str) -> None:
        await self._db.enqueue(
            "DELETE FROM peer_space_directory WHERE instance_id=?",
            (instance_id,),
        )


def _row(row: dict) -> PeerSpaceDirectoryEntry:
    return PeerSpaceDirectoryEntry(
        instance_id=row["instance_id"],
        space_id=row["space_id"],
        name=row["name"],
        description=row.get("description"),
        emoji=row.get("emoji"),
        member_count=int(row.get("member_count") or 0),
        join_mode=row.get("join_mode") or "request",
        min_age=int(row.get("min_age") or 0),
        target_audience=row.get("target_audience") or "all",
        updated_at=row.get("updated_at"),
        cached_at=row.get("cached_at"),
    )
