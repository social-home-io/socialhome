"""Notification repository — per-user notification centre (§17.2).

Rows are stored in the ``notifications`` table. Each user is capped at
``MAX_PER_USER`` entries (default 500, §17.2) — excess rows are pruned on
insert so the table can never grow without bound. The prune drops read
rows first, then the oldest unread ones, and never an unread action item
(:data:`~socialhome.domain.notification.ACTION_ITEM_TYPES`); the 90-day
purge spares unread action items too.

A row names what it is about (``object_key``, ``space_id``, 0129) and
carries ``params`` (JSON, :class:`NotificationParams`). The service finds a
row's group with :meth:`SqliteNotificationRepo.latest_in_group` and
re-opens it with :meth:`SqliteNotificationRepo.update_group` (D7); visiting,
deleting or leaving the thing reads / removes rows by key or space.

The service layer produces notification rows; this repo only persists and
queries them. Push delivery, bell-badge counts, mark-read flows all sit on
top of the methods here.
"""

from __future__ import annotations

import builtins
import sqlite3
import uuid
from collections.abc import Iterable, Sequence
from datetime import UTC, datetime, timedelta
from typing import Protocol, runtime_checkable

import orjson

from ..db import AsyncDatabase
from ..domain.notification import (
    ACTION_ITEM_TYPES,
    Notification,
    NotificationHandled,
    NotificationParams,
)
from ..domain.notification_kinds import SpaceNotifPref
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
        "object_key",
        "space_id",
    }
)


#: Per-user cap (§17.2). Rows beyond it are pruned when new ones land —
#: read rows first, unread action items never.
MAX_PER_USER: int = 500

#: The most "Needs you" items one list returns — far more than anyone
#: works through at once; the section shows 3 and a "Show N more".
MAX_PENDING_ACTIONS: int = 100

#: ``type IN (...)`` placeholders + values for the action-item types,
#: in a fixed order so the statements stay cacheable.
_ACTION_TYPES: tuple[str, ...] = tuple(sorted(ACTION_ITEM_TYPES))
_ACTION_IN = "(" + ",".join("?" * len(_ACTION_TYPES)) + ")"

#: ``(user_id, type, object_key)`` — one notification group (D7).
GroupKey = tuple[str, str, str]

#: Recipients per :meth:`SqliteNotificationRepo.latest_in_groups` /
#: :meth:`SqliteNotificationRepo.count_unread_many` statement — far under
#: SQLite's bound-parameter limit, so a fan-out stays one read per chunk.
_IN_CHUNK: int = 400

#: The per-user cap prune (§17.2): delete as many rows as the user is over,
#: read ones before unread ones, oldest first, unread action items never.
#: ``max(0, …)`` because a negative LIMIT means "no limit" in SQLite.
_PRUNE_SQL = f"""
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
"""

_INSERT_SQL = """
    INSERT INTO notifications(
        id, user_id, type, title, body, link_url, read_at, created_at,
        object_key, space_id, params
    ) VALUES(?,?,?,?,?,?,?, COALESCE(?, datetime('now')), ?,?,?)
"""

_UPDATE_GROUP_SQL = """
    UPDATE notifications
       SET title = ?, body = ?, link_url = ?, created_at = ?,
           read_at = ?, space_id = ?, params = ?
     WHERE id = ?
"""


def _insert_args(note: Notification) -> tuple:
    return (
        note.id,
        note.user_id,
        note.type,
        note.title,
        note.body,
        note.link_url,
        note.read_at,
        note.created_at,
        note.object_key,
        note.space_id,
        _params_json(note.params),
    )


def _update_args(note: Notification) -> tuple:
    return (
        note.title,
        note.body,
        note.link_url,
        note.created_at,
        note.read_at,
        note.space_id,
        _params_json(note.params),
        note.id,
    )


def _chunks(items: Sequence, size: int = _IN_CHUNK) -> Iterable[Sequence]:
    for start in range(0, len(items), size):
        yield items[start : start + size]


@runtime_checkable
class AbstractNotificationRepo(Protocol):
    async def save(self, note: Notification) -> Notification: ...
    async def latest_in_group(
        self, *, user_id: str, type: str, object_key: str, since: str
    ) -> Notification | None: ...
    async def update_group(self, note: Notification) -> None: ...
    async def latest_in_groups(
        self, keys: Iterable[GroupKey], *, since: str
    ) -> dict[GroupKey, Notification]: ...
    async def save_batch(
        self,
        inserts: Sequence[Notification],
        updates: Sequence[Notification],
    ) -> frozenset[str]: ...
    async def count_unread_many(self, user_ids: Iterable[str]) -> dict[str, int]: ...
    async def list(
        self,
        user_id: str,
        *,
        before: str | None = None,
        limit: int = 50,
        unread_only: bool = False,
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
    async def resolve_object(
        self,
        object_key: str,
        *,
        types: Iterable[str],
        handled: NotificationHandled,
        user_id: str | None = None,
    ) -> builtins.list[str]: ...
    async def list_pending_actions(
        self, user_id: str, *, limit: int = ...
    ) -> builtins.list[Notification]: ...
    async def mark_read_by_object(
        self, user_id: str, object_keys: Iterable[str]
    ) -> int: ...
    async def delete_by_object(
        self, object_keys: Iterable[str], *, keep_types: Iterable[str] = ()
    ) -> builtins.list[str]: ...
    async def delete_by_space(
        self,
        space_id: str,
        *,
        user_id: str | None = None,
        keep_types: Iterable[str] = (),
    ) -> builtins.list[str]: ...
    async def count_unread(self, user_id: str) -> int: ...
    async def delete_old(self, older_than_days: int = 90) -> int: ...
    async def exists_in_space_since(
        self, *, type: str, space_id: str, since: str
    ) -> bool: ...

    # ── Per-space notification preferences ─────────────────────────────
    async def get_space_notif_level(
        self,
        *,
        user_id: str,
        space_id: str,
    ) -> str | None: ...
    async def set_space_notif_level(
        self,
        *,
        user_id: str,
        space_id: str,
        level: str,
        muted_until: str | None = None,
    ) -> None: ...
    async def get_space_notif_pref(
        self, *, user_id: str, space_id: str
    ) -> SpaceNotifPref | None: ...
    async def list_space_notif_prefs(
        self, user_id: str
    ) -> builtins.list[tuple[str, SpaceNotifPref]]: ...
    async def space_notif_prefs_in(
        self, space_id: str
    ) -> dict[str, SpaceNotifPref]: ...


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
        await self._db.enqueue(_INSERT_SQL, _insert_args(note))
        await self._db.enqueue(
            _PRUNE_SQL, (note.user_id, *_ACTION_TYPES, note.user_id, self._cap)
        )
        return note

    async def latest_in_group(
        self, *, user_id: str, type: str, object_key: str, since: str
    ) -> Notification | None:
        """The newest ``(user_id, type, object_key)`` row whose last activity
        (``created_at``) is at or after ``since`` — read or not — else
        ``None``. The group a new event joins (D7)."""
        row = await self._db.fetchone(
            """
            SELECT * FROM notifications
             WHERE object_key = ? AND user_id = ? AND type = ?
               AND created_at >= ?
             ORDER BY created_at DESC
             LIMIT 1
            """,
            (object_key, user_id, type, since),
        )
        return _row_to_notification(row_to_dict(row))

    async def update_group(self, note: Notification) -> None:
        """Write a regrouped row back by id: its fresh title, body, link,
        last activity, read state, space and params."""
        await self._db.enqueue(_UPDATE_GROUP_SQL, _update_args(note))

    async def latest_in_groups(
        self, keys: Iterable[GroupKey], *, since: str
    ) -> dict[GroupKey, Notification]:
        """:meth:`latest_in_group` for many groups at once — a fan-out's
        lookup in one read per :data:`_IN_CHUNK` recipients, not one per
        recipient. Groups with no row since ``since`` are left out."""
        wanted = set(keys)
        if not wanted:
            return {}
        types = sorted({k[1] for k in wanted})
        objects = sorted({k[2] for k in wanted})
        users = sorted({k[0] for k in wanted})
        found: dict[GroupKey, Notification] = {}
        for chunk in _chunks(users):
            rows = await self._db.fetchall(
                f"""
                SELECT * FROM notifications
                 WHERE object_key IN ({",".join("?" * len(objects))})
                   AND user_id IN ({",".join("?" * len(chunk))})
                   AND type IN ({",".join("?" * len(types))})
                   AND created_at >= ?
                 ORDER BY created_at DESC
                """,
                (*objects, *chunk, *types, since),
            )
            for d in rows_to_dicts(rows):
                note = _row_to_notification(d)
                if note is None or note.object_key is None:
                    continue
                key = (note.user_id, note.type, note.object_key)
                if key in wanted and key not in found:
                    found[key] = note
        return found

    async def save_batch(
        self,
        inserts: Sequence[Notification],
        updates: Sequence[Notification],
    ) -> frozenset[str]:
        """Write a fan-out's new rows and regrouped rows in **one**
        transaction, then prune every user who got a new row to the cap —
        the batched form of :meth:`save` + :meth:`update_group`.

        Each row gets its own SAVEPOINT, as the write queue gives each
        statement: a row that fails (its user deleted a moment ago, so the
        foreign key no longer holds) is skipped, never the rest. Returns
        the ids of the rows that did not land."""
        if not inserts and not updates:
            return frozenset()
        cap = self._cap

        def _run(conn: sqlite3.Connection) -> frozenset[str]:
            failed: set[str] = set()
            for sql, note, args in (
                *((_INSERT_SQL, n, _insert_args(n)) for n in inserts),
                *((_UPDATE_GROUP_SQL, n, _update_args(n)) for n in updates),
            ):
                conn.execute("SAVEPOINT notif_row")
                try:
                    conn.execute(sql, args)
                except sqlite3.Error:
                    conn.execute("ROLLBACK TO notif_row")
                    failed.add(note.id)
                conn.execute("RELEASE notif_row")
            for user_id in dict.fromkeys(n.user_id for n in inserts):
                conn.execute(_PRUNE_SQL, (user_id, *_ACTION_TYPES, user_id, cap))
            return frozenset(failed)

        return await self._db.transact(_run)

    async def count_unread_many(self, user_ids: Iterable[str]) -> dict[str, int]:
        """:meth:`count_unread` for many users in one read per chunk; a
        user with nothing unread reads ``0``."""
        ids = sorted(set(user_ids))
        counts = dict.fromkeys(ids, 0)
        for chunk in _chunks(ids):
            rows = await self._db.fetchall(
                "SELECT user_id, COUNT(*) AS n FROM notifications"
                f" WHERE user_id IN ({','.join('?' * len(chunk))})"
                " AND read_at IS NULL GROUP BY user_id",
                tuple(chunk),
            )
            for r in rows:
                counts[r["user_id"]] = int(r["n"])
        return counts

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
        unread_only: bool = False,
    ) -> builtins.list[Notification]:
        """Bespoke per-user listing — kept as the common-case shim.
        ``unread_only`` keeps the rows not read yet (the page's Unread tab).

        For arbitrary filter combinations use :meth:`find` with a
        :class:`Spec`. This method now composes a Spec internally so
        the SQL surface only exists in one place.
        """
        where: list[tuple[str, str, object]] = [("user_id", "=", user_id)]
        if before is not None:
            where.append(("created_at", "<", before))
        if unread_only:
            where.append(("read_at", "IS", None))
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

    async def resolve_object(
        self,
        object_key: str,
        *,
        types: Iterable[str],
        handled: NotificationHandled,
        user_id: str | None = None,
    ) -> builtins.list[str]:
        """A "Needs you" item was handled: every still-pending row of one of
        *types* about *object_key* — whoever it belongs to, or only
        ``user_id``'s — records *handled* in its params and reads as read,
        so it leaves every recipient's section at once. A row already
        handled keeps its first answer. Returns the users whose rows
        changed (to tell their devices)."""
        kinds = sorted(set(types))
        if not kinds:
            return []
        marks = ",".join("?" * len(kinds))
        where = (
            f"object_key = ? AND type IN ({marks}) "
            "AND json_extract(COALESCE(params, '{}'), '$.handled') IS NULL"
        )
        args: list[object] = [object_key, *kinds]
        if user_id is not None:
            where += " AND user_id = ?"
            args.append(user_id)
        rows = await self._db.fetchall(
            f"SELECT DISTINCT user_id FROM notifications WHERE {where}", tuple(args)
        )
        user_ids = [r["user_id"] for r in rows]
        if user_ids:
            await self._db.enqueue(
                "UPDATE notifications"
                "   SET params = json_set(COALESCE(params, '{}'), '$.handled',"
                "                         json(?)),"
                "       read_at = COALESCE(read_at, datetime('now'))"
                f" WHERE {where}",
                (orjson.dumps(handled.to_dict()).decode(), *args),
            )
        return user_ids

    async def list_pending_actions(
        self, user_id: str, *, limit: int = MAX_PENDING_ACTIONS
    ) -> builtins.list[Notification]:
        """``user_id``'s "Needs you" items no one handled yet, read or
        not, newest first."""
        rows = await self._db.fetchall(
            "SELECT * FROM notifications"
            f" WHERE user_id = ? AND type IN {_ACTION_IN}"
            "   AND json_extract(COALESCE(params, '{}'), '$.handled') IS NULL"
            " ORDER BY created_at DESC LIMIT ?",
            (user_id, *_ACTION_TYPES, int(limit)),
        )
        return [n for n in (_row_to_notification(d) for d in rows_to_dicts(rows)) if n]

    async def mark_read_by_object(
        self, user_id: str, object_keys: Iterable[str]
    ) -> int:
        """Mark every unread row of ``user_id`` about one of *object_keys*
        read (any type); returns how many flipped."""
        keys = list(dict.fromkeys(object_keys))
        if not keys:
            return 0
        marks = ",".join("?" * len(keys))
        return await self._db.enqueue_rowcount(
            "UPDATE notifications SET read_at = datetime('now') "
            f"WHERE object_key IN ({marks}) AND user_id = ? AND read_at IS NULL",
            (*keys, user_id),
        )

    async def delete_by_object(
        self, object_keys: Iterable[str], *, keep_types: Iterable[str] = ()
    ) -> builtins.list[str]:
        """The things are gone: delete every row about one of *object_keys*,
        whoever it belongs to, except rows of *keep_types*. Returns the users
        who lost an **unread** row (their badge moved)."""
        keys = list(dict.fromkeys(object_keys))
        if not keys:
            return []
        keep = sorted(set(keep_types))
        where = f"object_key IN ({','.join('?' * len(keys))})"
        if keep:
            where += f" AND type NOT IN ({','.join('?' * len(keep))})"
        return await self._delete_where(where, (*keys, *keep))

    async def delete_by_space(
        self,
        space_id: str,
        *,
        user_id: str | None = None,
        keep_types: Iterable[str] = (),
    ) -> builtins.list[str]:
        """Delete the rows about things in *space_id* — of ``user_id`` only
        (they left), or everyone's (the space ended) — except rows of
        *keep_types*. Returns the users who lost an unread row."""
        keep = sorted(set(keep_types))
        where = "space_id = ?"
        args: list[object] = [space_id]
        if user_id is not None:
            where += " AND user_id = ?"
            args.append(user_id)
        if keep:
            where += f" AND type NOT IN ({','.join('?' * len(keep))})"
            args.extend(keep)
        return await self._delete_where(where, tuple(args))

    async def _delete_where(
        self, where: str, args: tuple[object, ...]
    ) -> builtins.list[str]:
        rows = await self._db.fetchall(
            f"SELECT DISTINCT user_id FROM notifications WHERE {where} "
            "AND read_at IS NULL",
            args,
        )
        await self._db.enqueue(f"DELETE FROM notifications WHERE {where}", args)
        return [r["user_id"] for r in rows]

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

    async def exists_in_space_since(
        self, *, type: str, space_id: str, since: str
    ) -> bool:
        """Whether anyone here got a ``type`` row about ``space_id`` whose
        last activity is at or after ``since`` (an ISO UTC stamp) — the
        persisted per-space ``@here`` cooldown."""
        row = await self._db.fetchone(
            "SELECT 1 FROM notifications"
            " WHERE type = ? AND space_id = ? AND created_at >= ? LIMIT 1",
            (type, space_id, since),
        )
        return row is not None

    # ── Per-space notification preferences ─────────────────────────────

    async def get_space_notif_level(
        self,
        *,
        user_id: str,
        space_id: str,
    ) -> str | None:
        """The level that applies for ``user_id`` in ``space_id`` right now
        — ``muted`` while a timed "Nothing" runs (0130), else the level they
        chose — or ``None`` when they never chose one; the caller then
        applies the default for the space's type and their seat
        (:func:`~socialhome.domain.notification.default_space_notif_level`)."""
        pref = await self.get_space_notif_pref(user_id=user_id, space_id=space_id)
        if pref is None:
            return None
        return pref.effective(datetime.now(UTC)) or None

    async def get_space_notif_pref(
        self, *, user_id: str, space_id: str
    ) -> SpaceNotifPref | None:
        """The stored choice (level + ``muted_until``), ``None`` if never
        chosen."""
        row = await self._db.fetchone(
            "SELECT level, muted_until FROM space_notif_prefs"
            " WHERE user_id=? AND space_id=?",
            (user_id, space_id),
        )
        if row is None or not row["level"]:
            return None
        return SpaceNotifPref(row["level"], row["muted_until"])

    async def list_space_notif_prefs(
        self, user_id: str
    ) -> builtins.list[tuple[str, SpaceNotifPref]]:
        """Every space ``user_id`` chose a level for, as ``(space_id,
        pref)`` — the "Muted and changed" list."""
        rows = await self._db.fetchall(
            "SELECT space_id, level, muted_until FROM space_notif_prefs"
            " WHERE user_id=? ORDER BY space_id",
            (user_id,),
        )
        return [
            (r["space_id"], SpaceNotifPref(r["level"], r["muted_until"])) for r in rows
        ]

    async def space_notif_prefs_in(self, space_id: str) -> dict[str, SpaceNotifPref]:
        """Every choice made in ``space_id``, by user — a fan-out reads its
        members' levels in one go (:meth:`get_space_notif_pref` each)."""
        rows = await self._db.fetchall(
            "SELECT user_id, level, muted_until FROM space_notif_prefs"
            " WHERE space_id=? AND level IS NOT NULL AND level != ''",
            (space_id,),
        )
        return {
            r["user_id"]: SpaceNotifPref(r["level"], r["muted_until"]) for r in rows
        }

    async def set_space_notif_level(
        self,
        *,
        user_id: str,
        space_id: str,
        level: str,
        muted_until: str | None = None,
    ) -> None:
        """Store ``level``; ``muted_until`` (UTC ISO) mutes the space until
        then, after which ``level`` applies again (D9). Without it any
        running timed mute ends."""
        await self._db.enqueue(
            """
            INSERT INTO space_notif_prefs(user_id, space_id, level, muted_until)
            VALUES(?, ?, ?, ?)
            ON CONFLICT(user_id, space_id) DO UPDATE SET
                level=excluded.level,
                muted_until=excluded.muted_until
            """,
            (user_id, space_id, level, muted_until),
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
        object_key=row.get("object_key"),
        space_id=row.get("space_id"),
        params=_parse_params(row.get("params")),
    )


def _params_json(params: NotificationParams | None) -> str | None:
    return orjson.dumps(params.to_dict()).decode() if params is not None else None


def _parse_params(raw: str | None) -> NotificationParams | None:
    """A stored ``params`` column (JSON by the 0129 CHECK) — ``None`` for
    none or an unreadable one, never an error that breaks a list."""
    if not raw:
        return None
    try:
        return NotificationParams.from_dict(orjson.loads(raw))
    except orjson.JSONDecodeError:
        return None


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
