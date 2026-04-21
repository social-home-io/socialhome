"""Notification repository — per-user notification centre (§17.2).

Rows are stored in the ``notifications`` table. Each user is capped at the
most recent ``MAX_PER_USER`` entries (default 200, §17.2) — excess rows are
pruned on insert so the table can never grow without bound.

The service layer produces notification rows; this repo only persists and
queries them. Push delivery, bell-badge counts, mark-read flows all sit on
top of the methods here.
"""

from __future__ import annotations

import builtins
import uuid
from datetime import datetime, timedelta, timezone
from typing import Protocol, runtime_checkable

from ..db import AsyncDatabase
from ._spec import Spec, spec_to_sql
from .base import row_to_dict, rows_to_dicts


#: Columns the Specification pattern is allowed to filter / order on.
_NOTIFICATION_COLS: frozenset[str] = frozenset(
    {
        "user_id",
        "type",
        "title",
        "body",
        "link_url",
        "read_at",
        "created_at",
    }
)


#: Per-user cap (§17.2). Rows older than this are pruned when new ones land.
MAX_PER_USER: int = 200


# Domain dataclass lives in ``social_home/domain/notification.py``;
# re-exported here so existing repo-level imports keep working.
from ..domain.notification import Notification  # noqa: F401,E402


@runtime_checkable
class AbstractNotificationRepo(Protocol):
    async def save(self, note: Notification) -> Notification: ...
    async def list(
        self,
        user_id: str,
        *,
        before: str | None = None,
        limit: int = 50,
    ) -> builtins.list[Notification]: ...
    async def find(self, spec: Spec) -> builtins.list[Notification]: ...
    async def get(self, notification_id: str) -> Notification | None: ...
    async def mark_read(self, notification_id: str, user_id: str) -> None: ...
    async def mark_all_read(self, user_id: str) -> None: ...
    async def count_unread(self, user_id: str) -> int: ...
    async def delete_old(self, older_than_days: int = 90) -> int: ...


class SqliteNotificationRepo:
    """SQLite-backed :class:`AbstractNotificationRepo`."""

    def __init__(self, db: AsyncDatabase, *, max_per_user: int = MAX_PER_USER) -> None:
        self._db = db
        self._cap = int(max_per_user)

    async def save(self, note: Notification) -> Notification:
        """Insert a notification and prune the oldest if the user cap is exceeded.

        The prune-on-insert happens in the same write queue batch as the
        insert, so the per-user invariant holds continuously.
        """
        await self._db.enqueue(
            """
            INSERT INTO notifications(
                id, user_id, type, title, body, link_url, read_at, created_at
            ) VALUES(?,?,?,?,?,?,?, COALESCE(?, datetime('now')))
            """,
            (
                note.id,
                note.user_id,
                note.type,
                note.title,
                note.body,
                note.link_url,
                note.read_at,
                note.created_at,
            ),
        )
        # Keep at most ``cap`` rows per user. Subquery picks the oldest rows
        # beyond the cap, outer DELETE removes them. This runs cheaply
        # against the per-user index.
        await self._db.enqueue(
            """
            DELETE FROM notifications
             WHERE id IN (
                 SELECT id FROM notifications
                  WHERE user_id = ?
                  ORDER BY created_at DESC
                  LIMIT -1 OFFSET ?
             )
            """,
            (note.user_id, self._cap),
        )
        return note

    async def get(self, notification_id: str) -> Notification | None:
        row = await self._db.fetchone(
            "SELECT * FROM notifications WHERE id=?",
            (notification_id,),
        )
        return _row_to_notification(row_to_dict(row))

    async def list(
        self,
        user_id: str,
        *,
        before: str | None = None,
        limit: int = 50,
    ) -> builtins.list[Notification]:
        """Bespoke per-user listing — kept as the common-case shim.

        For arbitrary filter combinations use :meth:`find` with a
        :class:`Spec`. This method now composes a Spec internally so
        the SQL surface only exists in one place.
        """
        where: list[tuple[str, str, object]] = [("user_id", "=", user_id)]
        if before is not None:
            where.append(("created_at", "<", before))
        spec = Spec(
            where=where,
            order_by=[("created_at", "DESC")],
            limit=int(limit),
        )
        return await self.find(spec)

    async def find(self, spec: Spec) -> builtins.list[Notification]:
        """Specification-pattern entry point — composable filters.

        See :mod:`social_home.repositories._spec`. Every column named
        in ``spec.where`` / ``spec.order_by`` is gated against the
        :data:`_NOTIFICATION_COLS` allow-list before being interpolated
        into the SQL.
        """
        clause, params = spec_to_sql(
            spec,
            table="notifications",
            allowed_cols=_NOTIFICATION_COLS,
        )
        sql = "SELECT * FROM notifications " + clause
        rows = await self._db.fetchall(sql, params)
        return [n for n in (_row_to_notification(d) for d in rows_to_dicts(rows)) if n]

    async def mark_read(self, notification_id: str, user_id: str) -> None:
        """Mark a single notification as read.

        The ``user_id`` check prevents a user from marking another user's
        notification — cheap defence-in-depth since the service layer is
        also expected to authorise.
        """
        await self._db.enqueue(
            """
            UPDATE notifications
               SET read_at = COALESCE(read_at, datetime('now'))
             WHERE id=? AND user_id=?
            """,
            (notification_id, user_id),
        )

    async def mark_all_read(self, user_id: str) -> None:
        await self._db.enqueue(
            """
            UPDATE notifications
               SET read_at = COALESCE(read_at, datetime('now'))
             WHERE user_id=? AND read_at IS NULL
            """,
            (user_id,),
        )

    async def count_unread(self, user_id: str) -> int:
        return int(
            await self._db.fetchval(
                "SELECT COUNT(*) FROM notifications "
                "WHERE user_id=? AND read_at IS NULL",
                (user_id,),
                default=0,
            )
        )

    async def delete_old(self, older_than_days: int = 90) -> int:
        """Delete notifications older than N days. Returns purge count."""
        cutoff = (
            datetime.now(timezone.utc) - _timedelta_days(older_than_days)
        ).isoformat()
        count = await self._db.fetchval(
            "SELECT COUNT(*) FROM notifications WHERE created_at < ?",
            (cutoff,),
            default=0,
        )
        await self._db.enqueue(
            "DELETE FROM notifications WHERE created_at < ?",
            (cutoff,),
        )
        return int(count)


def _timedelta_days(days: int):
    return timedelta(days=days)


def _row_to_notification(row: dict | None) -> Notification | None:
    if row is None:
        return None
    return Notification(
        id=row["id"],
        user_id=row["user_id"],
        type=row["type"],
        title=row["title"],
        body=row.get("body"),
        link_url=row.get("link_url"),
        read_at=row.get("read_at"),
        created_at=row["created_at"],
    )


# Convenience: build a Notification with a fresh UUID.
def new_notification(
    *,
    user_id: str,
    type: str,
    title: str,
    body: str | None = None,
    link_url: str | None = None,
) -> Notification:
    return Notification(
        id=uuid.uuid4().hex,
        user_id=user_id,
        type=type,
        title=title,
        body=body,
        link_url=link_url,
        created_at=datetime.now(timezone.utc).isoformat(),
    )
