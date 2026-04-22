"""Remote-member repository for cross-household private-space joins (§D1b).

When a household accepts a private-space invite from another household,
the inviting household records the accepter in ``space_remote_members``
so future space-message fan-outs include that instance + user in the
recipient list. Stored fields are the minimum needed to encrypt + route
subsequent space content.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from ..db.database import AsyncDatabase
from .base import rows_to_dicts


@dataclass(slots=True, frozen=True)
class SpaceRemoteMember:
    """A single remote-member row in a federated private space."""

    space_id: str
    instance_id: str
    user_id: str
    user_pk: str | None = None
    display_name: str | None = None
    joined_at: str | None = None


@runtime_checkable
class AbstractSpaceRemoteMemberRepo(Protocol):
    async def add(
        self,
        *,
        space_id: str,
        instance_id: str,
        user_id: str,
        user_pk: str | None,
        display_name: str | None,
    ) -> None: ...

    async def remove(
        self,
        space_id: str,
        instance_id: str,
        user_id: str,
    ) -> None: ...

    async def list_for_space(self, space_id: str) -> list[SpaceRemoteMember]: ...

    async def list_for_user(
        self,
        instance_id: str,
        user_id: str,
    ) -> list[SpaceRemoteMember]: ...


class SqliteSpaceRemoteMemberRepo:
    """SQLite-backed :class:`AbstractSpaceRemoteMemberRepo`."""

    __slots__ = ("_db",)

    def __init__(self, db: AsyncDatabase) -> None:
        self._db = db

    async def add(
        self,
        *,
        space_id: str,
        instance_id: str,
        user_id: str,
        user_pk: str | None,
        display_name: str | None,
    ) -> None:
        await self._db.enqueue(
            """
            INSERT INTO space_remote_members(
                space_id, instance_id, user_id, user_pk, display_name
            ) VALUES(?, ?, ?, ?, ?)
            ON CONFLICT(space_id, instance_id, user_id) DO UPDATE SET
                user_pk=excluded.user_pk,
                display_name=excluded.display_name
            """,
            (space_id, instance_id, user_id, user_pk, display_name),
        )

    async def remove(
        self,
        space_id: str,
        instance_id: str,
        user_id: str,
    ) -> None:
        await self._db.enqueue(
            "DELETE FROM space_remote_members"
            " WHERE space_id=? AND instance_id=? AND user_id=?",
            (space_id, instance_id, user_id),
        )

    async def list_for_space(self, space_id: str) -> list[SpaceRemoteMember]:
        rows = await self._db.fetchall(
            "SELECT * FROM space_remote_members WHERE space_id=? ORDER BY joined_at",
            (space_id,),
        )
        return [_row(r) for r in rows_to_dicts(rows)]

    async def list_for_user(
        self,
        instance_id: str,
        user_id: str,
    ) -> list[SpaceRemoteMember]:
        rows = await self._db.fetchall(
            "SELECT * FROM space_remote_members WHERE instance_id=? AND user_id=?",
            (instance_id, user_id),
        )
        return [_row(r) for r in rows_to_dicts(rows)]


def _row(row: dict) -> SpaceRemoteMember:
    return SpaceRemoteMember(
        space_id=row["space_id"],
        instance_id=row["instance_id"],
        user_id=row["user_id"],
        user_pk=row.get("user_pk"),
        display_name=row.get("display_name"),
        joined_at=row.get("joined_at"),
    )
