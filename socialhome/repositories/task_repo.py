"""Task / task-list repository (§5.2, §23.68).

Persists the household task list at ``task_lists`` + ``tasks``. Space tasks
live in the parallel ``space_task_lists`` / ``space_tasks`` pair — same
column shape, different tables — exposed as
:class:`SqliteSpaceTaskRepo` so callers don't have to carry a ``space_id``
through every query.

Also hosts task comments (``task_comments``) and task attachments
(``task_attachments``); deadline reminder state lives in the parallel
``task_deadline_notifications`` table, driven by the scheduler in
:mod:`infrastructure.task_deadline_scheduler`.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Protocol, runtime_checkable

from ..db import AsyncDatabase
from ..domain.task import (
    RecurrenceRule,
    Task,
    TaskAttachment,
    TaskComment,
    TaskList,
    TaskListTombstone,
    TaskPriority,
    TaskTombstone,
    TaskStatus,
    normalize_labels,
)
from .base import dump_json, load_json, row_to_dict, rows_to_dicts


# ─── Shared helpers ───────────────────────────────────────────────────────


def _iso(value) -> str | None:
    if value is None:
        return None
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    return str(value)


def _parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _parse_date(value: str | None) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(value[:10])
    except ValueError:
        return None


def _parse_priority(value: str | None) -> TaskPriority | None:
    if not value:
        return None
    try:
        return TaskPriority(value)
    except ValueError:
        return None


def _row_to_task(row: dict) -> Task:
    recurrence: RecurrenceRule | None = None
    if row.get("rrule"):
        recurrence = RecurrenceRule(
            rrule=row["rrule"],
            last_spawned_at=_parse_dt(row.get("last_spawned_at")),
        )
    return Task(
        id=row["id"],
        list_id=row["list_id"],
        title=row["title"],
        status=TaskStatus(row.get("status", "todo")),
        position=int(row.get("position") or 0),
        created_by=row["created_by"],
        created_at=_parse_dt(row["created_at"]) or datetime.now(timezone.utc),
        updated_at=_parse_dt(row["updated_at"]) or datetime.now(timezone.utc),
        description=row.get("description"),
        due_date=_parse_date(row.get("due_date")),
        assignees=tuple(load_json(row.get("assignees_json"), [])),
        recurrence=recurrence,
        recurrence_parent_id=row.get("recurrence_parent_id"),
        archived_at=_parse_dt(row.get("archived_at")),
        priority=_parse_priority(row.get("priority")),
        labels=normalize_labels(load_json(row.get("labels_json"), [])),
    )


def _priority_value(task: Task) -> str | None:
    return task.priority.value if task.priority is not None else None


def _row_to_list(row: dict) -> TaskList:
    return TaskList(
        id=row["id"],
        name=row["name"],
        created_by=row["created_by"],
    )


# ─── Household tasks ──────────────────────────────────────────────────────


@runtime_checkable
class AbstractTaskRepo(Protocol):
    async def save_list(self, list_: TaskList) -> TaskList: ...
    async def get_list(self, list_id: str) -> TaskList | None: ...
    async def list_lists(self) -> list[TaskList]: ...
    async def open_counts(self) -> dict[str, int]: ...
    async def delete_list(self, list_id: str) -> None: ...

    async def save(self, task: Task) -> Task: ...
    async def get(self, task_id: str) -> Task | None: ...
    async def next_position(self, list_id: str) -> int: ...
    async def list_by_list(
        self,
        list_id: str,
        *,
        include_done: bool = True,
        status: str | None = None,
        assignee: str | None = None,
        due_from: date | None = None,
        due_to: date | None = None,
        limit: int | None = None,
        offset: int = 0,
    ) -> list[Task]: ...
    async def list_by_status(self, status: TaskStatus) -> list[Task]: ...
    async def list_by_assignee(self, user_id: str) -> list[Task]: ...
    async def list_due_on(self, due: date) -> list[Task]: ...
    async def list_recurring_overdue(
        self,
        today: date,
    ) -> list[Task]: ...
    async def delete(self, task_id: str) -> None: ...

    # ── Comments (§23.68) ────────────────────────────────────────────
    async def add_comment(self, comment: TaskComment) -> TaskComment: ...
    async def list_comments(self, task_id: str) -> list[TaskComment]: ...
    async def delete_comment(self, comment_id: str) -> None: ...

    # ── Attachments (§23.68) ─────────────────────────────────────────
    async def add_attachment(
        self,
        attachment: TaskAttachment,
    ) -> TaskAttachment: ...
    async def list_attachments(
        self,
        task_id: str,
    ) -> list[TaskAttachment]: ...
    async def delete_attachment(self, attachment_id: str) -> None: ...


class SqliteTaskRepo:
    """SQLite-backed :class:`AbstractTaskRepo`."""

    _SELECT_TASKS = "SELECT * FROM tasks"

    def __init__(self, db: AsyncDatabase) -> None:
        self._db = db

    # ── Lists ──────────────────────────────────────────────────────────

    async def save_list(self, list_: TaskList) -> TaskList:
        await self._db.enqueue(
            """
            INSERT INTO task_lists(id, name, created_by)
            VALUES(?,?,?)
            ON CONFLICT(id) DO UPDATE SET name=excluded.name
            """,
            (list_.id, list_.name, list_.created_by),
        )
        return list_

    async def get_list(self, list_id: str) -> TaskList | None:
        row = await self._db.fetchone(
            "SELECT * FROM task_lists WHERE id=?",
            (list_id,),
        )
        d = row_to_dict(row)
        return _row_to_list(d) if d else None

    async def list_lists(self) -> list[TaskList]:
        rows = await self._db.fetchall(
            "SELECT * FROM task_lists ORDER BY created_at",
        )
        return [_row_to_list(d) for d in rows_to_dicts(rows)]

    async def open_counts(self) -> dict[str, int]:
        """Open tasks (not done, not archived) per list, in one query.

        A list with no open task is absent from the map.
        """
        rows = await self._db.fetchall(
            "SELECT list_id, COUNT(*) AS n FROM tasks"
            " WHERE status != ? AND archived_at IS NULL GROUP BY list_id",
            (TaskStatus.DONE.value,),
        )
        return {d["list_id"]: int(d["n"]) for d in rows_to_dicts(rows)}

    async def delete_list(self, list_id: str) -> None:
        await self._db.enqueue(
            "DELETE FROM task_lists WHERE id=?",
            (list_id,),
        )

    # ── Tasks ──────────────────────────────────────────────────────────

    async def save(self, task: Task) -> Task:
        await self._db.enqueue(
            """
            INSERT INTO tasks(
                id, list_id, title, description, due_date, assignees_json,
                status, position, created_by, rrule, last_spawned_at,
                recurrence_parent_id, archived_at, priority, labels_json,
                created_at, updated_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,
                     COALESCE(?, datetime('now')),
                     COALESCE(?, datetime('now')))
            ON CONFLICT(id) DO UPDATE SET
                title=excluded.title,
                description=excluded.description,
                due_date=excluded.due_date,
                assignees_json=excluded.assignees_json,
                status=excluded.status,
                position=excluded.position,
                rrule=excluded.rrule,
                last_spawned_at=excluded.last_spawned_at,
                recurrence_parent_id=excluded.recurrence_parent_id,
                archived_at=excluded.archived_at,
                priority=excluded.priority,
                labels_json=excluded.labels_json,
                updated_at=excluded.updated_at
            """,
            (
                task.id,
                task.list_id,
                task.title,
                task.description,
                _iso(task.due_date),
                dump_json(list(task.assignees)),
                task.status.value,
                int(task.position),
                task.created_by,
                task.recurrence.rrule if task.recurrence else None,
                _iso(task.recurrence.last_spawned_at) if task.recurrence else None,
                task.recurrence_parent_id,
                _iso(task.archived_at),
                _priority_value(task),
                dump_json(list(task.labels)),
                _iso(task.created_at),
                _iso(task.updated_at),
            ),
        )
        return task

    async def get(self, task_id: str) -> Task | None:
        row = await self._db.fetchone(
            "SELECT * FROM tasks WHERE id=?",
            (task_id,),
        )
        d = row_to_dict(row)
        return _row_to_task(d) if d else None

    async def next_position(self, list_id: str) -> int:
        """One past the highest ``position`` in ``list_id`` (0 when empty).

        ``position`` is an ordering key per list; a status column shows
        its tasks in ``position`` order, so a new task given this value
        lands at the bottom of whichever column it is filed in.
        """
        row = await self._db.fetchone(
            "SELECT COALESCE(MAX(position) + 1, 0) AS n FROM tasks WHERE list_id=?",
            (list_id,),
        )
        return int(row["n"]) if row is not None else 0

    async def list_by_list(
        self,
        list_id: str,
        *,
        include_done: bool = True,
        status: str | None = None,
        assignee: str | None = None,
        due_from: date | None = None,
        due_to: date | None = None,
        limit: int | None = None,
        offset: int = 0,
    ) -> list[Task]:
        """List tasks in ``list_id`` with optional filters + pagination.

        ``status`` forces a specific value and overrides ``include_done``.
        ``assignee`` uses the same JSON-array LIKE probe as
        :meth:`list_by_assignee`.
        """
        clauses: list[str] = ["list_id=?"]
        params: list = [list_id]
        if status is not None:
            clauses.append("status=?")
            params.append(status)
        elif not include_done:
            clauses.append("status != ?")
            params.append(TaskStatus.DONE.value)
        if assignee:
            clauses.append("assignees_json LIKE ?")
            params.append(f'%"{assignee}"%')
        if due_from is not None:
            clauses.append("due_date >= ?")
            params.append(due_from.isoformat())
        if due_to is not None:
            clauses.append("due_date <= ?")
            params.append(due_to.isoformat())
        sql = (
            f"{self._SELECT_TASKS} WHERE "
            + " AND ".join(clauses)
            + " ORDER BY position, created_at"
        )
        if limit is not None:
            sql += " LIMIT ? OFFSET ?"
            params.extend([int(limit), int(offset)])
        rows = await self._db.fetchall(sql, tuple(params))
        return [_row_to_task(d) for d in rows_to_dicts(rows)]

    async def list_by_status(self, status: TaskStatus) -> list[Task]:
        rows = await self._db.fetchall(
            f"{self._SELECT_TASKS} WHERE status=? ORDER BY due_date, created_at",
            (status.value,),
        )
        return [_row_to_task(d) for d in rows_to_dicts(rows)]

    async def list_by_assignee(self, user_id: str) -> list[Task]:
        """Return tasks whose ``assignees_json`` includes ``user_id``.

        SQLite's ``json_each`` is unavailable without the JSON1 extension on
        some builds, so this uses a LIKE probe against the compact JSON form
        (``["a","b"]``). Because we always serialise with
        :func:`base.dump_json` (sorted, compact), the probe is stable — we
        look for the quoted id inside the array.
        """
        needle = f'%"{user_id}"%'
        rows = await self._db.fetchall(
            f"{self._SELECT_TASKS} WHERE assignees_json LIKE ? "
            "ORDER BY due_date, created_at",
            (needle,),
        )
        return [_row_to_task(d) for d in rows_to_dicts(rows)]

    async def list_due_on(self, due: date) -> list[Task]:
        rows = await self._db.fetchall(
            f"{self._SELECT_TASKS} WHERE due_date=? AND status != ? ORDER BY position",
            (due.isoformat(), TaskStatus.DONE.value),
        )
        return [_row_to_task(d) for d in rows_to_dicts(rows)]

    async def list_recurring_overdue(self, today: date) -> list[Task]:
        """Recurring tasks whose due_date has passed and we haven't
        spawned the next occurrence yet.

        Used by :class:`TaskRecurrenceScheduler` to auto-advance
        recurring tasks the user never completed.
        """
        rows = await self._db.fetchall(
            f"{self._SELECT_TASKS}"
            " WHERE rrule IS NOT NULL"
            "   AND due_date IS NOT NULL"
            "   AND due_date < ?"
            "   AND (last_spawned_at IS NULL"
            "        OR last_spawned_at <= due_date)",
            (today.isoformat(),),
        )
        return [_row_to_task(d) for d in rows_to_dicts(rows)]

    async def delete(self, task_id: str) -> None:
        await self._db.enqueue(
            "DELETE FROM tasks WHERE id=?",
            (task_id,),
        )

    # ── Task comments (§23.68) ─────────────────────────────────────

    async def add_comment(self, comment: TaskComment) -> TaskComment:
        await self._db.enqueue(
            "INSERT INTO task_comments(id, task_id, author, content, created_at)"
            " VALUES(?, ?, ?, ?, ?)",
            (
                comment.id,
                comment.task_id,
                comment.author,
                comment.content,
                _iso(comment.created_at),
            ),
        )
        return comment

    async def list_comments(self, task_id: str) -> list[TaskComment]:
        rows = await self._db.fetchall(
            "SELECT * FROM task_comments WHERE task_id=? ORDER BY created_at",
            (task_id,),
        )
        return [_row_to_task_comment(d) for d in rows_to_dicts(rows)]

    async def delete_comment(self, comment_id: str) -> None:
        await self._db.enqueue(
            "DELETE FROM task_comments WHERE id=?",
            (comment_id,),
        )

    # ── Task attachments (§23.68) ──────────────────────────────────

    async def add_attachment(
        self,
        attachment: TaskAttachment,
    ) -> TaskAttachment:
        await self._db.enqueue(
            "INSERT INTO task_attachments("
            "id, task_id, uploaded_by, url, filename, mime, size_bytes, created_at"
            ") VALUES(?, ?, ?, ?, ?, ?, ?, ?)",
            (
                attachment.id,
                attachment.task_id,
                attachment.uploaded_by,
                attachment.url,
                attachment.filename,
                attachment.mime,
                attachment.size_bytes,
                _iso(attachment.created_at),
            ),
        )
        return attachment

    async def list_attachments(
        self,
        task_id: str,
    ) -> list[TaskAttachment]:
        rows = await self._db.fetchall(
            "SELECT * FROM task_attachments WHERE task_id=? ORDER BY created_at",
            (task_id,),
        )
        return [_row_to_task_attachment(d) for d in rows_to_dicts(rows)]

    async def delete_attachment(self, attachment_id: str) -> None:
        await self._db.enqueue(
            "DELETE FROM task_attachments WHERE id=?",
            (attachment_id,),
        )


# ─── Space tasks (space_task_lists / space_tasks) ─────────────────────────


@runtime_checkable
class AbstractSpaceTaskRepo(Protocol):
    async def save_list(self, list_: TaskList, *, space_id: str) -> bool: ...
    async def get_list(self, list_id: str) -> tuple[str, TaskList] | None: ...
    async def list_lists(self, space_id: str) -> list[TaskList]: ...
    async def open_counts(self, space_id: str) -> dict[str, int]: ...
    async def list_lists_since(
        self, space_id: str, since: str, *, limit: int = 500
    ) -> list[TaskList]: ...
    async def delete_list(
        self, list_id: str, *, space_id: str, deleted_by: str = ""
    ) -> bool: ...
    async def tombstone_list(
        self, list_id: str, *, space_id: str, created_by: str, deleted_by: str = ""
    ) -> bool: ...
    async def is_list_deleted(self, list_id: str, *, space_id: str) -> bool: ...
    async def list_list_tombstones(
        self, space_id: str, *, since: str | None = None, limit: int = 500
    ) -> list[TaskListTombstone]: ...

    async def save(self, task: Task, *, space_id: str) -> bool: ...
    async def get(self, task_id: str) -> tuple[str, Task] | None: ...
    async def next_position(self, list_id: str, *, space_id: str) -> int: ...
    async def list_by_list(
        self,
        list_id: str,
        *,
        space_id: str,
        include_done: bool = True,
    ) -> list[Task]: ...
    async def list_by_space(self, space_id: str) -> list[Task]: ...
    async def list_since(
        self,
        space_id: str,
        since: str,
        *,
        limit: int = 500,
    ) -> list[Task]: ...
    async def delete(
        self, task_id: str, *, space_id: str, deleted_by: str = ""
    ) -> bool: ...
    async def tombstone(
        self,
        task_id: str,
        *,
        space_id: str,
        list_id: str,
        created_by: str,
        deleted_by: str = "",
    ) -> bool: ...
    async def is_task_deleted(self, task_id: str, *, space_id: str) -> bool: ...
    async def list_task_tombstones(
        self, space_id: str, *, since: str | None = None, limit: int = 500
    ) -> list[TaskTombstone]: ...


class SqliteSpaceTaskRepo:
    """SQLite-backed :class:`AbstractSpaceTaskRepo`."""

    def __init__(self, db: AsyncDatabase) -> None:
        self._db = db

    # ── Space lists ────────────────────────────────────────────────────

    async def save_list(self, list_: TaskList, *, space_id: str) -> bool:
        """Upsert a task list into ``space_id``.

        ``space_id`` is authoritative (§24.11): a conflict on an id that
        already belongs to another space is refused, and ``False`` says
        nothing was written. So is a conflict on a **tombstoned** id — a
        deleted list never comes back (its ids are owner-bound, never
        reused). A rename stamps ``updated_at`` for the resume replay; an
        unchanged name (every host sync re-sends it) does not.
        """
        n = await self._db.enqueue_rowcount(
            """
            INSERT INTO space_task_lists(id, space_id, name, created_by)
            VALUES(?,?,?,?)
            ON CONFLICT(id) DO UPDATE SET
                name=excluded.name,
                updated_at=CASE WHEN space_task_lists.name IS excluded.name
                    THEN space_task_lists.updated_at
                    ELSE datetime('now') END
            WHERE space_task_lists.space_id = excluded.space_id
              AND space_task_lists.deleted_at IS NULL
            """,
            (list_.id, space_id, list_.name, list_.created_by),
        )
        return n > 0

    async def get_list(
        self,
        list_id: str,
    ) -> tuple[str, TaskList] | None:
        row = await self._db.fetchone(
            "SELECT * FROM space_task_lists WHERE id=? AND deleted_at IS NULL",
            (list_id,),
        )
        d = row_to_dict(row)
        if d is None:
            return None
        return d["space_id"], _row_to_list(d)

    async def list_lists(self, space_id: str) -> list[TaskList]:
        rows = await self._db.fetchall(
            "SELECT * FROM space_task_lists WHERE space_id=? AND deleted_at IS NULL"
            " ORDER BY created_at",
            (space_id,),
        )
        return [_row_to_list(d) for d in rows_to_dicts(rows)]

    async def open_counts(self, space_id: str) -> dict[str, int]:
        """Open tasks (not done, not archived) per list of ``space_id``,
        in one query. A list with no open task is absent from the map.
        """
        rows = await self._db.fetchall(
            "SELECT list_id, COUNT(*) AS n FROM space_tasks"
            " WHERE space_id=? AND status != ? AND archived_at IS NULL"
            " AND deleted_at IS NULL"
            " GROUP BY list_id",
            (space_id, TaskStatus.DONE.value),
        )
        return {d["list_id"]: int(d["n"]) for d in rows_to_dicts(rows)}

    async def list_lists_since(
        self, space_id: str, since: str, *, limit: int = 500
    ) -> list[TaskList]:
        """Live lists created or renamed at or after ``since``, oldest
        change first (resume catch-up).

        ``created_at`` / ``updated_at`` are SQLite's naive UTC
        ``datetime('now')`` while ``since`` is an ISO 8601 string, so both
        go through ``datetime()`` rather than a string compare. That is
        second precision, so the compare is ``>=``: a list changed in the
        same second as ``since`` is replayed (an idempotent upsert) rather
        than skipped.
        """
        rows = await self._db.fetchall(
            "SELECT * FROM space_task_lists"
            " WHERE space_id=? AND deleted_at IS NULL"
            " AND datetime(COALESCE(updated_at, created_at)) >= datetime(?)"
            " ORDER BY COALESCE(updated_at, created_at) ASC LIMIT ?",
            (space_id, since, int(limit)),
        )
        return [_row_to_list(d) for d in rows_to_dicts(rows)]

    async def delete_list(
        self, list_id: str, *, space_id: str, deleted_by: str = ""
    ) -> bool:
        """Tombstone a live list of ``space_id``; ``False`` if none.

        The row stays (``deleted_at`` set, ``deleted_by`` naming who
        deleted it — empty when nobody can be named) so sync and resume
        can tell a household that missed the delete; the list-tombstone
        trigger (0069, recreated in 0071) tombstones the list's tasks in
        place, so none of their ids can be re-filed under another list.
        """
        n = await self._db.enqueue_rowcount(
            "UPDATE space_task_lists SET deleted_at=datetime('now'),"
            " deleted_by=NULLIF(?, '')"
            " WHERE id=? AND space_id=? AND deleted_at IS NULL",
            (deleted_by, list_id, space_id),
        )
        return n > 0

    async def tombstone_list(
        self, list_id: str, *, space_id: str, created_by: str, deleted_by: str = ""
    ) -> bool:
        """Record a delete of a list never held here: a content-free stub
        row, so a stale copy streamed later can't create it.

        Insert-only — an id already held (live or tombstoned, in any space)
        is never touched, and ``False`` says nothing was written (or the
        space is missing). The caller must have proven the id is this
        space's (owner-bound to ``created_by`` in ``space_id``): list ids
        are global, so a stub for another space's id would block that
        space's real list here forever.
        """
        n = await self._db.enqueue_rowcount(
            "INSERT INTO space_task_lists(id, space_id, name, created_by,"
            " deleted_at, deleted_by)"
            " SELECT ?, ?, '', ?, datetime('now'), NULLIF(?, '')"
            " WHERE EXISTS (SELECT 1 FROM spaces WHERE id=?)"
            " ON CONFLICT(id) DO NOTHING",
            (list_id, space_id, created_by, deleted_by, space_id),
        )
        return n > 0

    async def is_list_deleted(self, list_id: str, *, space_id: str) -> bool:
        """Whether ``list_id`` is a tombstone of ``space_id`` here."""
        row = await self._db.fetchone(
            "SELECT 1 FROM space_task_lists"
            " WHERE id=? AND space_id=? AND deleted_at IS NOT NULL",
            (list_id, space_id),
        )
        return row is not None

    async def list_list_tombstones(
        self, space_id: str, *, since: str | None = None, limit: int = 500
    ) -> list[TaskListTombstone]:
        """The space's deleted lists, newest delete first (so a ``limit``
        keeps the deletes a peer is likeliest to have missed) — all of
        them, or those deleted at or after ``since`` (same ``>=`` compare
        as :meth:`list_lists_since`)."""
        sql = (
            "SELECT id, created_by, deleted_at, deleted_by FROM space_task_lists"
            " WHERE space_id=? AND deleted_at IS NOT NULL"
        )
        params: tuple = (space_id,)
        if since is not None:
            sql += " AND datetime(deleted_at) >= datetime(?)"
            params += (since,)
        sql += " ORDER BY deleted_at DESC LIMIT ?"
        rows = await self._db.fetchall(sql, (*params, int(limit)))
        return [
            TaskListTombstone(
                id=d["id"],
                deleted_at=d["deleted_at"],
                created_by=d["created_by"] or "",
                deleted_by=d["deleted_by"] or "",
            )
            for d in rows_to_dicts(rows)
        ]

    # ── Space tasks ────────────────────────────────────────────────────

    async def save(self, task: Task, *, space_id: str) -> bool:
        """Upsert a task into ``space_id``.

        ``space_id`` is authoritative (§24.11) and guards three things
        in a single statement: the row's own scope, the parent list's
        scope (``task.list_id`` comes from the same untrusted payload,
        so a task must not be filed under another space's list — nor
        under a deleted one), and
        the conflict case (an id owned by another space is refused, and so
        is a **tombstoned** id — a deleted task never comes back; its ids
        are owner-bound, never reused).
        ``False`` means nothing was written.
        """
        n = await self._db.enqueue_rowcount(
            """
            INSERT INTO space_tasks(
                id, list_id, space_id, title, description, due_date,
                assignees_json, status, position, created_by,
                rrule, last_spawned_at, recurrence_parent_id,
                archived_at, priority, labels_json, created_at, updated_at
            ) SELECT ?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,
                     COALESCE(?, datetime('now')),
                     COALESCE(?, datetime('now'))
               WHERE EXISTS (
                   SELECT 1 FROM space_task_lists
                    WHERE id=? AND space_id=? AND deleted_at IS NULL
               )
            ON CONFLICT(id) DO UPDATE SET
                title=excluded.title,
                description=excluded.description,
                due_date=excluded.due_date,
                assignees_json=excluded.assignees_json,
                status=excluded.status,
                position=excluded.position,
                rrule=excluded.rrule,
                last_spawned_at=excluded.last_spawned_at,
                recurrence_parent_id=excluded.recurrence_parent_id,
                archived_at=excluded.archived_at,
                priority=excluded.priority,
                labels_json=excluded.labels_json,
                updated_at=excluded.updated_at
            WHERE space_tasks.space_id = excluded.space_id
              AND space_tasks.deleted_at IS NULL
            """,
            (
                task.id,
                task.list_id,
                space_id,
                task.title,
                task.description,
                _iso(task.due_date),
                dump_json(list(task.assignees)),
                task.status.value,
                int(task.position),
                task.created_by,
                task.recurrence.rrule if task.recurrence else None,
                _iso(task.recurrence.last_spawned_at) if task.recurrence else None,
                task.recurrence_parent_id,
                _iso(task.archived_at),
                _priority_value(task),
                dump_json(list(task.labels)),
                _iso(task.created_at),
                _iso(task.updated_at),
                task.list_id,
                space_id,
            ),
        )
        return n > 0

    async def get(self, task_id: str) -> tuple[str, Task] | None:
        row = await self._db.fetchone(
            "SELECT * FROM space_tasks WHERE id=? AND deleted_at IS NULL",
            (task_id,),
        )
        d = row_to_dict(row)
        if d is None:
            return None
        return d["space_id"], _row_to_task(d)

    async def next_position(self, list_id: str, *, space_id: str) -> int:
        """Like :meth:`SqliteTaskRepo.next_position`, within ``space_id``."""
        row = await self._db.fetchone(
            "SELECT COALESCE(MAX(position) + 1, 0) AS n FROM space_tasks"
            " WHERE list_id=? AND space_id=? AND deleted_at IS NULL",
            (list_id, space_id),
        )
        return int(row["n"]) if row is not None else 0

    async def list_by_list(
        self,
        list_id: str,
        *,
        space_id: str,
        include_done: bool = True,
    ) -> list[Task]:
        """Tasks of ``list_id`` — only those stored in ``space_id``."""
        if include_done:
            rows = await self._db.fetchall(
                "SELECT * FROM space_tasks WHERE list_id=? AND space_id=? "
                "AND deleted_at IS NULL ORDER BY position, created_at",
                (list_id, space_id),
            )
        else:
            rows = await self._db.fetchall(
                "SELECT * FROM space_tasks WHERE list_id=? AND space_id=?"
                " AND deleted_at IS NULL AND status != ?"
                " ORDER BY position, created_at",
                (list_id, space_id, TaskStatus.DONE.value),
            )
        return [_row_to_task(d) for d in rows_to_dicts(rows)]

    async def list_by_space(self, space_id: str) -> list[Task]:
        rows = await self._db.fetchall(
            "SELECT * FROM space_tasks WHERE space_id=? AND deleted_at IS NULL"
            " ORDER BY position, created_at",
            (space_id,),
        )
        return [_row_to_task(d) for d in rows_to_dicts(rows)]

    async def list_since(
        self,
        space_id: str,
        since: str,
        *,
        limit: int = 500,
    ) -> list[Task]:
        """Live tasks with ``updated_at > since``, oldest-first (resume
        catch-up). Tombstones replay as deletes instead
        (:meth:`list_task_tombstones`)."""
        rows = await self._db.fetchall(
            "SELECT * FROM space_tasks "
            "WHERE space_id=? AND deleted_at IS NULL AND updated_at > ? "
            "ORDER BY updated_at ASC LIMIT ?",
            (space_id, since, int(limit)),
        )
        return [_row_to_task(d) for d in rows_to_dicts(rows)]

    async def delete(
        self, task_id: str, *, space_id: str, deleted_by: str = ""
    ) -> bool:
        """Tombstone a live task of ``space_id``; ``False`` if none.

        The row stays (``deleted_at`` set, ``deleted_by`` naming who
        authorised the delete — empty when nobody can be named) so sync and
        resume can tell a household that missed it (migration 0071). Its
        content is blanked: the tombstone keeps the id, list, creator and
        timestamps the replay needs, never the deleted text.
        """
        n = await self._db.enqueue_rowcount(
            "UPDATE space_tasks SET deleted_at=datetime('now'),"
            " deleted_by=NULLIF(?, ''), title='', description=NULL,"
            " due_date=NULL, assignees_json='[]', labels_json='[]',"
            " priority=NULL, rrule=NULL, last_spawned_at=NULL,"
            " recurrence_parent_id=NULL"
            " WHERE id=? AND space_id=? AND deleted_at IS NULL",
            (deleted_by, task_id, space_id),
        )
        return n > 0

    async def tombstone(
        self,
        task_id: str,
        *,
        space_id: str,
        list_id: str,
        created_by: str,
        deleted_by: str = "",
    ) -> bool:
        """Record a delete of a task never held here: a content-free stub
        row, so a stale copy streamed later can't create it.

        Insert-only — an id already held (live or tombstoned, in any space)
        is never touched — and only under a list live here IN ``space_id``
        (``list_id`` is a FK, and a task is never filed under another
        space's list). ``False`` says nothing was written. The caller must
        have proven the id is this space's (owner-bound to ``created_by``
        in ``space_id``): task ids are global, so a stub for another
        space's id would block that space's real task here forever.
        """
        n = await self._db.enqueue_rowcount(
            "INSERT INTO space_tasks(id, list_id, space_id, title, created_by,"
            " deleted_at, deleted_by)"
            " SELECT ?, ?, ?, '', ?, datetime('now'), NULLIF(?, '')"
            " WHERE EXISTS (SELECT 1 FROM space_task_lists"
            "  WHERE id=? AND space_id=? AND deleted_at IS NULL)"
            " ON CONFLICT(id) DO NOTHING",
            (task_id, list_id, space_id, created_by, deleted_by, list_id, space_id),
        )
        return n > 0

    async def is_task_deleted(self, task_id: str, *, space_id: str) -> bool:
        """Whether ``task_id`` is a tombstone of ``space_id`` here."""
        row = await self._db.fetchone(
            "SELECT 1 FROM space_tasks"
            " WHERE id=? AND space_id=? AND deleted_at IS NOT NULL",
            (task_id, space_id),
        )
        return row is not None

    async def list_task_tombstones(
        self, space_id: str, *, since: str | None = None, limit: int = 500
    ) -> list[TaskTombstone]:
        """The space's deleted tasks, newest delete first (so a ``limit``
        keeps the deletes a peer is likeliest to have missed) — all of
        them, or those deleted at or after ``since`` (``deleted_at`` is
        naive UTC, ``since`` ISO 8601, so both go through ``datetime()``;
        second precision, hence ``>=``).

        Tasks of a deleted list are left out: the migration-0071 trigger
        tombstoned them with the list, and the list's own tombstone
        (``task_lists_deleted`` / the replayed list delete) does the same
        on the receiver — shipping them again would only be noise."""
        sql = (
            "SELECT id, list_id, created_by, deleted_at, deleted_by"
            " FROM space_tasks WHERE space_id=? AND deleted_at IS NOT NULL"
            " AND list_id NOT IN (SELECT id FROM space_task_lists"
            "  WHERE space_id=? AND deleted_at IS NOT NULL)"
        )
        params: tuple = (space_id, space_id)
        if since is not None:
            sql += " AND datetime(deleted_at) >= datetime(?)"
            params += (since,)
        sql += " ORDER BY deleted_at DESC LIMIT ?"
        rows = await self._db.fetchall(sql, (*params, int(limit)))
        return [
            TaskTombstone(
                id=d["id"],
                list_id=d["list_id"],
                deleted_at=d["deleted_at"],
                created_by=d["created_by"] or "",
                deleted_by=d["deleted_by"] or "",
            )
            for d in rows_to_dicts(rows)
        ]


# ─── Row → domain helpers for task_comments / task_attachments ───────────


def _row_to_task_comment(row: dict) -> TaskComment:
    return TaskComment(
        id=row["id"],
        task_id=row["task_id"],
        author=row["author"],
        content=row["content"],
        created_at=_parse_dt(row["created_at"]) or datetime.now(timezone.utc),
    )


def _row_to_task_attachment(row: dict) -> TaskAttachment:
    return TaskAttachment(
        id=row["id"],
        task_id=row["task_id"],
        uploaded_by=row["uploaded_by"],
        url=row["url"],
        filename=row["filename"],
        mime=row["mime"],
        size_bytes=int(row["size_bytes"]),
        created_at=_parse_dt(row["created_at"]) or datetime.now(timezone.utc),
    )
