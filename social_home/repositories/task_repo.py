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
    TaskStatus,
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
    )


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
    async def delete_list(self, list_id: str) -> None: ...

    async def save(self, task: Task) -> Task: ...
    async def get(self, task_id: str) -> Task | None: ...
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
                recurrence_parent_id, created_at, updated_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,
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
    async def save_list(self, space_id: str, list_: TaskList) -> TaskList: ...
    async def get_list(self, list_id: str) -> tuple[str, TaskList] | None: ...
    async def list_lists(self, space_id: str) -> list[TaskList]: ...
    async def delete_list(self, list_id: str) -> None: ...

    async def save(self, space_id: str, task: Task) -> Task: ...
    async def get(self, task_id: str) -> tuple[str, Task] | None: ...
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
    async def list_by_space(self, space_id: str) -> list[Task]: ...
    async def delete(self, task_id: str) -> None: ...


class SqliteSpaceTaskRepo:
    """SQLite-backed :class:`AbstractSpaceTaskRepo`."""

    def __init__(self, db: AsyncDatabase) -> None:
        self._db = db

    # ── Space lists ────────────────────────────────────────────────────

    async def save_list(self, space_id: str, list_: TaskList) -> TaskList:
        await self._db.enqueue(
            """
            INSERT INTO space_task_lists(id, space_id, name, created_by)
            VALUES(?,?,?,?)
            ON CONFLICT(id) DO UPDATE SET name=excluded.name
            """,
            (list_.id, space_id, list_.name, list_.created_by),
        )
        return list_

    async def get_list(
        self,
        list_id: str,
    ) -> tuple[str, TaskList] | None:
        row = await self._db.fetchone(
            "SELECT * FROM space_task_lists WHERE id=?",
            (list_id,),
        )
        d = row_to_dict(row)
        if d is None:
            return None
        return d["space_id"], _row_to_list(d)

    async def list_lists(self, space_id: str) -> list[TaskList]:
        rows = await self._db.fetchall(
            "SELECT * FROM space_task_lists WHERE space_id=? ORDER BY created_at",
            (space_id,),
        )
        return [_row_to_list(d) for d in rows_to_dicts(rows)]

    async def delete_list(self, list_id: str) -> None:
        await self._db.enqueue(
            "DELETE FROM space_task_lists WHERE id=?",
            (list_id,),
        )

    # ── Space tasks ────────────────────────────────────────────────────

    async def save(self, space_id: str, task: Task) -> Task:
        await self._db.enqueue(
            """
            INSERT INTO space_tasks(
                id, list_id, space_id, title, description, due_date,
                assignees_json, status, position, created_by,
                rrule, last_spawned_at, recurrence_parent_id,
                created_at, updated_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,
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
                updated_at=excluded.updated_at
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
                _iso(task.created_at),
                _iso(task.updated_at),
            ),
        )
        return task

    async def get(self, task_id: str) -> tuple[str, Task] | None:
        row = await self._db.fetchone(
            "SELECT * FROM space_tasks WHERE id=?",
            (task_id,),
        )
        d = row_to_dict(row)
        if d is None:
            return None
        return d["space_id"], _row_to_task(d)

    async def list_by_list(
        self,
        list_id: str,
        *,
        include_done: bool = True,
    ) -> list[Task]:
        if include_done:
            rows = await self._db.fetchall(
                "SELECT * FROM space_tasks WHERE list_id=? "
                "ORDER BY position, created_at",
                (list_id,),
            )
        else:
            rows = await self._db.fetchall(
                "SELECT * FROM space_tasks WHERE list_id=? AND status != ? "
                "ORDER BY position, created_at",
                (list_id, TaskStatus.DONE.value),
            )
        return [_row_to_task(d) for d in rows_to_dicts(rows)]

    async def list_by_space(self, space_id: str) -> list[Task]:
        rows = await self._db.fetchall(
            "SELECT * FROM space_tasks WHERE space_id=? ORDER BY position, created_at",
            (space_id,),
        )
        return [_row_to_task(d) for d in rows_to_dicts(rows)]

    async def delete(self, task_id: str) -> None:
        await self._db.enqueue(
            "DELETE FROM space_tasks WHERE id=?",
            (task_id,),
        )


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
