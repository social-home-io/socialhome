"""Shopping list repository (§23.120).

Household-scoped. Single table (``shopping_list_items``) — no per-user
isolation (every HA user shares the same shopping list, same as the HA
shopping list integration).

The list is a priority queue by creation order with a completed/not
flag. The service layer is responsible for cleaning up completed items on
a schedule or on user action.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Protocol, runtime_checkable

from ..db import AsyncDatabase
from .base import bool_col, row_to_dict, rows_to_dicts


# Domain dataclass lives in ``socialhome/domain/shopping.py``;
# re-exported here so existing repo-level imports keep working.
from ..domain.shopping import ShoppingItem  # noqa: F401,E402


@runtime_checkable
class AbstractShoppingRepo(Protocol):
    async def add(self, text: str, *, created_by: str) -> ShoppingItem: ...
    async def get(self, item_id: str) -> ShoppingItem | None: ...
    async def list(self, *, include_completed: bool = False) -> list[ShoppingItem]: ...
    async def complete(self, item_id: str) -> None: ...
    async def uncomplete(self, item_id: str) -> None: ...
    async def delete(self, item_id: str) -> None: ...
    async def clear_completed(self) -> int: ...


class SqliteShoppingRepo:
    """SQLite-backed :class:`AbstractShoppingRepo`."""

    def __init__(self, db: AsyncDatabase) -> None:
        self._db = db

    async def add(self, text: str, *, created_by: str) -> ShoppingItem:
        text = text.strip()
        if not text:
            raise ValueError("shopping item text must not be empty")
        now = datetime.now(timezone.utc).isoformat()
        item = ShoppingItem(
            id=uuid.uuid4().hex,
            text=text,
            completed=False,
            created_by=created_by,
            created_at=now,
            completed_at=None,
        )
        await self._db.enqueue(
            """
            INSERT INTO shopping_list_items(
                id, text, completed, created_by, created_at, completed_at
            ) VALUES(?, ?, 0, ?, ?, NULL)
            """,
            (item.id, item.text, item.created_by, item.created_at),
        )
        return item

    async def get(self, item_id: str) -> ShoppingItem | None:
        row = await self._db.fetchone(
            "SELECT * FROM shopping_list_items WHERE id=?",
            (item_id,),
        )
        return _row_to_item(row_to_dict(row))

    async def list(
        self,
        *,
        include_completed: bool = False,
    ) -> list[ShoppingItem]:
        if include_completed:
            rows = await self._db.fetchall(
                "SELECT * FROM shopping_list_items ORDER BY completed ASC, created_at",
            )
        else:
            rows = await self._db.fetchall(
                "SELECT * FROM shopping_list_items WHERE completed=0 "
                "ORDER BY created_at",
            )
        return [i for i in (_row_to_item(d) for d in rows_to_dicts(rows)) if i]

    async def complete(self, item_id: str) -> None:
        await self._db.enqueue(
            """
            UPDATE shopping_list_items
               SET completed=1, completed_at=datetime('now')
             WHERE id=? AND completed=0
            """,
            (item_id,),
        )

    async def uncomplete(self, item_id: str) -> None:
        await self._db.enqueue(
            """
            UPDATE shopping_list_items
               SET completed=0, completed_at=NULL
             WHERE id=?
            """,
            (item_id,),
        )

    async def delete(self, item_id: str) -> None:
        await self._db.enqueue(
            "DELETE FROM shopping_list_items WHERE id=?",
            (item_id,),
        )

    async def clear_completed(self) -> int:
        """Delete every completed item. Returns the count removed."""
        count = await self._db.fetchval(
            "SELECT COUNT(*) FROM shopping_list_items WHERE completed=1",
            default=0,
        )
        await self._db.enqueue(
            "DELETE FROM shopping_list_items WHERE completed=1",
        )
        return int(count)


def _row_to_item(row: dict | None) -> ShoppingItem | None:
    if row is None:
        return None
    return ShoppingItem(
        id=row["id"],
        text=row["text"],
        completed=bool_col(row.get("completed", 0)),
        created_by=row["created_by"],
        created_at=row["created_at"],
        completed_at=row.get("completed_at"),
    )
