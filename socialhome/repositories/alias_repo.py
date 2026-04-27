"""Per-viewer user aliases (§4.1.6).

Each local user can rename any other user — local or remote — for
their own view only. Aliases are never federated; they live in
``user_aliases`` keyed by ``(viewer_user_id, target_user_id)``.

Resolution priority (in :class:`socialhome.domain.user.DisplayableUser`):

    space_display_name  >  personal alias  >  global display_name

This repo is the source of truth for "personal alias". The
:class:`socialhome.services.alias_resolver.AliasResolver` is the
typical caller — it batches lookups for a member-list / feed render
into a single round-trip.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Protocol, runtime_checkable

from ..db import AsyncDatabase

#: Inclusive length cap matching the DB ``CHECK(length(alias) <= 80)``.
#: Service-layer validation should reject anything longer with a
#: domain error rather than hitting the constraint.
MAX_ALIAS_LENGTH: int = 80


@runtime_checkable
class AbstractAliasRepo(Protocol):
    async def set_user_alias(
        self,
        *,
        viewer_user_id: str,
        target_user_id: str,
        alias: str,
    ) -> None: ...

    async def clear_user_alias(
        self,
        *,
        viewer_user_id: str,
        target_user_id: str,
    ) -> None: ...

    async def get_user_aliases(
        self,
        viewer_user_id: str,
        target_user_ids: Iterable[str],
    ) -> dict[str, str]: ...

    async def list_user_aliases(
        self,
        viewer_user_id: str,
    ) -> dict[str, str]: ...


class SqliteAliasRepo:
    """SQLite-backed :class:`AbstractAliasRepo`."""

    __slots__ = ("_db",)

    def __init__(self, db: AsyncDatabase) -> None:
        self._db = db

    async def set_user_alias(
        self,
        *,
        viewer_user_id: str,
        target_user_id: str,
        alias: str,
    ) -> None:
        await self._db.enqueue(
            """
            INSERT INTO user_aliases(
                viewer_user_id, target_user_id, alias, updated_at
            ) VALUES(?, ?, ?, datetime('now'))
            ON CONFLICT(viewer_user_id, target_user_id) DO UPDATE SET
                alias=excluded.alias,
                updated_at=excluded.updated_at
            """,
            (viewer_user_id, target_user_id, alias),
        )

    async def clear_user_alias(
        self,
        *,
        viewer_user_id: str,
        target_user_id: str,
    ) -> None:
        await self._db.enqueue(
            "DELETE FROM user_aliases WHERE viewer_user_id=? AND target_user_id=?",
            (viewer_user_id, target_user_id),
        )

    async def get_user_aliases(
        self,
        viewer_user_id: str,
        target_user_ids: Iterable[str],
    ) -> dict[str, str]:
        """Bulk lookup: ``{target_user_id: alias}`` for any matches.

        Empty ``target_user_ids`` short-circuits to ``{}`` so callers
        can pass any iterable (including from a comprehension that
        might produce no rows) without crafting a dynamic SQL clause.
        """
        ids = list(target_user_ids)
        if not ids:
            return {}
        placeholders = ",".join("?" * len(ids))
        rows = await self._db.fetchall(
            f"SELECT target_user_id, alias FROM user_aliases "
            f"WHERE viewer_user_id=? AND target_user_id IN ({placeholders})",
            (viewer_user_id, *ids),
        )
        return {r["target_user_id"]: r["alias"] for r in rows}

    async def list_user_aliases(
        self,
        viewer_user_id: str,
    ) -> dict[str, str]:
        """All aliases set by this viewer — for the settings UI."""
        rows = await self._db.fetchall(
            "SELECT target_user_id, alias FROM user_aliases "
            "WHERE viewer_user_id=? ORDER BY updated_at DESC",
            (viewer_user_id,),
        )
        return {r["target_user_id"]: r["alias"] for r in rows}
