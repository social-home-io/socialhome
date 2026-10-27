"""Notification repository — per-user notification centre (§17.2).

Rows are stored in the ``notifications`` table. Each user is capped at
``MAX_PER_USER`` entries (default 500, §17.2) — excess rows are pruned on
insert so the table can never grow without bound. The prune drops read
rows first, then the oldest unread ones, and never an unread action item
(:data:`~socialhome.domain.notification.ACTION_ITEM_TYPES`); the 90-day
purge spares unread action items too.

The service layer produces notification rows; this repo only persists and
queries them. Push delivery, bell-badge counts, mark-read flows all sit on
top of the methods here.
"""

from __future__ import annotations

import builtins
import uuid
from datetime import UTC, datetime, timedelta
from typing import Protocol, runtime_checkable

from ..db import AsyncDatabase
from ..domain.notification import ACTION_ITEM_TYPES, Notification
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


#: Per-user cap (§17.2). Rows beyond it are pruned when new ones land —
#: read rows first, unread action items never.
MAX_PER_USER: int = 500

#: ``type IN (...)`` placeholders + values for the action-item types,
#: in a fixed order so the statements stay cacheable.
_ACTION_TYPES: tuple[str, ...] = tuple(sorted(ACTION_ITEM_TYPES))
_ACTION_IN = "(" + ",".join("?" * len(_ACTION_TYPES)) + ")"


@runtime_checkable
class AbstractNotificationRepo(Protocol):
    async def save(self, note: Notification) -> Notification: ...
    async def save_or_bump_unread(self, note: Notification) -> Notification: ...
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
    async def mark_read_by_link(
        self,
        *,
        user_id: str,
        link_url: str,
        type: str | None = None,
    ) -> int: ...
    async def list_unread(self, user_id: str) -> builtins.list[Notification]: ...
    async def mark_read_ids(
        self, user_id: str, notification_ids: builtins.list[str]
    ) -> int: ...
    async def mark_read_for_everyone(
        self, *, link_url: str, type: str
    ) -> builtins.list[str]: ...
    async def count_unread(self, user_id: str) -> int: ...
    async def delete_old(self, older_than_days: int = 90) -> int: ...

    # ── Per-space notification preferences ─────────────────────────────
    async def get_space_notif_level(
        self,
        *,
        user_id: str,
        space_id: str,
    ) -> str: ...
    async def set_space_notif_level(
        self,
        *,
        user_id: str,
        space_id: str,
        level: str,
    ) -> None: ...


class SqliteNotificationRepo:
    """SQLite-backed :class:`AbstractNotificationRepo`."""

    def __init__(self, db: AsyncDatabase, *, max_per_user: int = MAX_PER_USER) -> None:
        self._db = db
        self._cap = int(max_per_user)

    async def save(self, note: Notification) -> Notification:
        """Insert a notification and prune if the user cap is exceeded.

        The prune-on-insert happens in the same write queue batch as the
        insert, so the per-user invariant holds continuously. It drops
        read rows first (oldest first), then the oldest unread ones, and
        never an unread action item — a pending request must not vanish
        under a flood of news.
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
        # Keep at most ``cap`` rows per user: delete as many rows as the
        # user is over, read ones before unread ones, oldest first, unread
        # action items never. ``max(0, …)`` because a negative LIMIT means
        # "no limit" in SQLite.
        await self._db.enqueue(
            f"""
            DELETE FROM notifications
             WHERE id IN (
                 SELECT id FROM notifications
                  WHERE user_id = ?
                    AND NOT (read_at IS NULL AND type IN {_ACTION_IN})
                  ORDER BY (read_at IS NULL) ASC, created_at ASC
                  LIMIT max(
                      0,
                      (SELECT COUNT(*) FROM notifications WHERE user_id = ?) - ?
                  )
             )
            """,
            (note.user_id, *_ACTION_TYPES, note.user_id, self._cap),
        )
        return note

    async def save_or_bump_unread(self, note: Notification) -> Notification:
        """Insert ``note``, or — if an unread row already exists with the
        same ``(user_id, type, link_url)`` — bump that existing row's
        ``created_at`` + ``title`` in place and return it.

        Caller passes a freshly-minted ``Notification`` (its own ``id``
        + timestamp); on bump the returned ``Notification`` carries the
        **existing** row's ``id`` so realtime / push handlers don't
        re-fire as a "new" notification on the SPA. The bump keeps the
        title fresh because the sender's display name can change
        between bursts; body stays ``None`` so §25.3's title-only
        redaction is preserved.

        Dedupe is **scoped to currently-unread rows only** — a read row
        with the same key does not absorb a new event. That gives the
        natural "thread cleared → next message starts a fresh row"
        behavior the bell wants.

        ``link_url`` is required for dedupe; if ``None``, this falls
        back to plain :meth:`save` so non-DM call-sites that don't
        carry a link keep the strict append-only shape.
        """
        if note.link_url is None:
            return await self.save(note)
        existing = await self._db.fetchone(
            """
            SELECT id FROM notifications
             WHERE user_id = ?
               AND type = ?
               AND link_url = ?
               AND read_at IS NULL
             ORDER BY created_at DESC
             LIMIT 1
            """,
            (note.user_id, note.type, note.link_url),
        )
        if existing is not None:
            existing_row = row_to_dict(existing)
            assert existing_row is not None  # fetchone returned a row
            existing_id = existing_row["id"]
            await self._db.enqueue(
                "UPDATE notifications SET created_at=?, title=? WHERE id=?",
                (note.created_at, note.title, existing_id),
            )
            return Notification(
                id=existing_id,
                user_id=note.user_id,
                type=note.type,
                title=note.title,
                body=note.body,
                link_url=note.link_url,
                read_at=None,
                created_at=note.created_at,
            )
        return await self.save(note)

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

        See :mod:`socialhome.repositories._spec`. Every column named
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

    async def mark_read_by_link(
        self,
        *,
        user_id: str,
        link_url: str,
        type: str | None = None,
    ) -> int:
        """Bulk-mark every unread row for ``user_id`` whose ``link_url``
        matches as read. Returns the count flipped.

        Optional ``type`` narrows the match — used by the DM thread
        auto-clear so opening a conversation only flips
        ``dm_message`` rows for that conversation, not every
        notification ever pointed there (e.g. an admin's
        ``space_post_moderated`` notification).

        Implemented as a count-then-update pair so the caller can
        decide whether to fan a UI signal — SQLite's UPDATE doesn't
        return a row count via aiosqlite at this revision.
        """
        if type is not None:
            count = int(
                await self._db.fetchval(
                    "SELECT COUNT(*) FROM notifications "
                    "WHERE user_id=? AND link_url=? AND type=? "
                    "AND read_at IS NULL",
                    (user_id, link_url, type),
                )
            )
            await self._db.enqueue(
                "UPDATE notifications "
                "SET read_at = COALESCE(read_at, datetime('now')) "
                "WHERE user_id=? AND link_url=? AND type=? "
                "AND read_at IS NULL",
                (user_id, link_url, type),
            )
        else:
            count = int(
                await self._db.fetchval(
                    "SELECT COUNT(*) FROM notifications "
                    "WHERE user_id=? AND link_url=? AND read_at IS NULL",
                    (user_id, link_url),
                )
            )
            await self._db.enqueue(
                "UPDATE notifications "
                "SET read_at = COALESCE(read_at, datetime('now')) "
                "WHERE user_id=? AND link_url=? AND read_at IS NULL",
                (user_id, link_url),
            )
        return count

    async def list_unread(self, user_id: str) -> builtins.list[Notification]:
        """Every unread row of ``user_id`` (bounded by the per-user cap)."""
        return await self.find(
            Spec(
                where=[("user_id", "=", user_id), ("read_at", "IS", None)],
                order_by=[("created_at", "DESC")],
            )
        )

    async def mark_read_ids(
        self, user_id: str, notification_ids: builtins.list[str]
    ) -> int:
        """Mark the given rows of ``user_id`` read; returns how many flipped.
        A row of another user is never touched."""
        if not notification_ids:
            return 0
        marks = ",".join("?" * len(notification_ids))
        return await self._db.enqueue_rowcount(
            "UPDATE notifications SET read_at = datetime('now') "
            f"WHERE user_id=? AND read_at IS NULL AND id IN ({marks})",
            (user_id, *notification_ids),
        )

    async def mark_read_for_everyone(
        self, *, link_url: str, type: str
    ) -> builtins.list[str]:
        """Mark every unread ``type`` row at ``link_url`` read, whoever it
        belongs to — an action item one admin handled is handled for all.
        Returns the users whose rows flipped (to tell their devices)."""
        rows = await self._db.fetchall(
            "SELECT DISTINCT user_id FROM notifications "
            "WHERE link_url=? AND type=? AND read_at IS NULL",
            (link_url, type),
        )
        user_ids = [r["user_id"] for r in rows]
        if user_ids:
            await self._db.enqueue(
                "UPDATE notifications SET read_at = datetime('now') "
                "WHERE link_url=? AND type=? AND read_at IS NULL",
                (link_url, type),
            )
        return user_ids

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
        """Delete notifications older than N days. Returns purge count.

        An unread action item stays whatever its age: it is still
        waiting for someone (it goes once handled, then ages out).

        Shape invariant: ``cutoff`` and ``notifications.created_at`` are
        both tz-aware ISO 8601 — every ``save()`` caller supplies a
        ``created_at``, so the SQL's naive ``COALESCE(?, datetime('now'))``
        fallback never fires. Keep it that way: a naive value landing in
        the column would mis-compare here, because SQLite orders TEXT
        lexicographically and "T" (0x54) sorts above " " (0x20).
        """
        cutoff = (datetime.now(UTC) - _timedelta_days(older_than_days)).isoformat()
        return await self._db.enqueue_rowcount(
            "DELETE FROM notifications WHERE created_at < ? "
            f"AND NOT (read_at IS NULL AND type IN {_ACTION_IN})",
            (cutoff, *_ACTION_TYPES),
        )

    # ── Per-space notification preferences ─────────────────────────────

    async def get_space_notif_level(
        self,
        *,
        user_id: str,
        space_id: str,
    ) -> str:
        row = await self._db.fetchone(
            "SELECT level FROM space_notif_prefs WHERE user_id=? AND space_id=?",
            (user_id, space_id),
        )
        return (row["level"] if row else "all") or "all"

    async def set_space_notif_level(
        self,
        *,
        user_id: str,
        space_id: str,
        level: str,
    ) -> None:
        await self._db.enqueue(
            """
            INSERT INTO space_notif_prefs(user_id, space_id, level)
            VALUES(?, ?, ?)
            ON CONFLICT(user_id, space_id) DO UPDATE SET
                level=excluded.level
            """,
            (user_id, space_id, level),
        )


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
        created_at=datetime.now(UTC).isoformat(),
    )
