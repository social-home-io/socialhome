"""Task service — thin orchestration wrapper around :class:`AbstractTaskRepo`.

Provides the service-layer entry points for the household task list and
space task lists. Route handlers call these methods and never touch the
repo directly.

Raises the usual domain exceptions so the route layer can map them to
HTTP status codes via ``_map_exc``:

* ``KeyError``      → 404 (list or task not found)
* ``ValueError``    → 422 (validation failure)
* ``PermissionError`` → 403 (not the owner / admin)
"""

from __future__ import annotations

import calendar
import uuid
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone

from ..domain.events import (
    TaskAssigned,
    TaskCompleted,
    TaskCreated,
    TaskDeleted,
    TaskListCreated,
    TaskListDeleted,
    TaskListUpdated,
    TaskUpdated,
)
from ..domain.task import (
    Task,
    TaskAttachment,
    TaskComment,
    TaskList,
    TaskStatus,
)
from ..repositories.task_repo import AbstractTaskRepo, AbstractSpaceTaskRepo


class TaskService:
    """Household task list operations."""

    __slots__ = ("_repo", "_bus", "_household")

    def __init__(self, task_repo: AbstractTaskRepo, bus=None) -> None:
        self._repo = task_repo
        self._bus = bus
        self._household = None

    def attach_household_features(self, svc) -> None:
        """Wire :class:`HouseholdFeaturesService` so ``create_list`` /
        ``create_task`` refuse with 403 when ``feat_tasks`` is off (§18).
        """
        self._household = svc

    async def _require_tasks_enabled(self) -> None:
        if self._household is not None:
            await self._household.require_enabled("tasks")

    # ── Lists ────────────────────────────────────────────────────────────

    async def create_list(
        self,
        *,
        name: str,
        created_by: str,
    ) -> TaskList:
        await self._require_tasks_enabled()
        name = name.strip()
        if not name:
            raise ValueError("task list name must not be empty")
        task_list = TaskList(
            id=uuid.uuid4().hex,
            name=name,
            created_by=created_by,
        )
        saved = await self._repo.save_list(task_list)
        if self._bus is not None:
            await self._bus.publish(
                TaskListCreated(
                    list_id=saved.id,
                    name=saved.name,
                )
            )
        return saved

    async def rename_list(self, list_id: str, *, name: str) -> TaskList:
        """Rename a task list. Raises KeyError if missing."""
        await self._require_tasks_enabled()
        name = name.strip()
        if not name:
            raise ValueError("task list name must not be empty")
        current = await self._repo.get_list(list_id)
        if current is None:
            raise KeyError(f"task list {list_id!r} not found")
        updated = replace(current, name=name)
        saved = await self._repo.save_list(updated)
        if self._bus is not None:
            await self._bus.publish(
                TaskListUpdated(
                    list_id=saved.id,
                    name=saved.name,
                )
            )
        return saved

    async def get_list(self, list_id: str) -> TaskList:
        result = await self._repo.get_list(list_id)
        if result is None:
            raise KeyError(f"task list {list_id!r} not found")
        return result

    async def list_lists(self) -> list[TaskList]:
        return await self._repo.list_lists()

    async def delete_list(self, list_id: str) -> None:
        result = await self._repo.get_list(list_id)
        if result is None:
            raise KeyError(f"task list {list_id!r} not found")
        await self._repo.delete_list(list_id)
        if self._bus is not None:
            await self._bus.publish(TaskListDeleted(list_id=list_id))

    # ── Tasks ────────────────────────────────────────────────────────────

    async def create_task(
        self,
        *,
        list_id: str,
        title: str,
        created_by: str,
        description: str | None = None,
        due_date: str | None = None,
        assignees: list[str] | None = None,
    ) -> Task:
        await self._require_tasks_enabled()
        title = title.strip()
        if not title:
            raise ValueError("task title must not be empty")
        # Ensure list exists
        task_list = await self._repo.get_list(list_id)
        if task_list is None:
            raise KeyError(f"task list {list_id!r} not found")

        due = None
        if due_date:
            try:
                due = date.fromisoformat(due_date[:10])
            except ValueError as exc:
                raise ValueError(f"invalid due_date: {due_date!r}") from exc

        now = datetime.now(timezone.utc)
        task = Task(
            id=uuid.uuid4().hex,
            list_id=list_id,
            title=title,
            status=TaskStatus.TODO,
            position=0,
            created_by=created_by,
            created_at=now,
            updated_at=now,
            description=description,
            due_date=due,
            assignees=tuple(assignees or []),
        )
        saved = await self._repo.save(task)
        if self._bus is not None:
            await self._bus.publish(TaskCreated(task=saved))
            # §15: notify assignees of the new task (skip self-assigned).
            for user_id in saved.assignees:
                if user_id == created_by:
                    continue
                await self._bus.publish(
                    TaskAssigned(
                        task=saved,
                        assigned_to=user_id,
                    )
                )
        return saved

    async def get_task(self, task_id: str) -> Task:
        result = await self._repo.get(task_id)
        if result is None:
            raise KeyError(f"task {task_id!r} not found")
        return result

    async def list_tasks(
        self,
        list_id: str,
        *,
        include_done: bool = True,
        status: str | None = None,
        assignee: str | None = None,
        due_from: str | None = None,
        due_to: str | None = None,
        limit: int | None = None,
        offset: int = 0,
    ) -> list[Task]:
        """Query tasks with optional filters + pagination.

        Dates are ISO-8601 (``YYYY-MM-DD``); ``status`` is a raw enum
        value. All filters combine with AND.
        """
        df = date.fromisoformat(due_from[:10]) if due_from else None
        dt = date.fromisoformat(due_to[:10]) if due_to else None
        return await self._repo.list_by_list(
            list_id,
            include_done=include_done,
            status=status,
            assignee=assignee,
            due_from=df,
            due_to=dt,
            limit=limit,
            offset=offset,
        )

    async def update_task(
        self,
        task_id: str,
        *,
        actor_user_id: str,
        title: str | None = None,
        description: str | None = None,
        status: str | None = None,
        due_date: str | None = None,
        assignees: list[str] | None = None,
        position: int | None = None,
    ) -> Task:
        task = await self.get_task(task_id)

        if task.created_by != actor_user_id:
            # allow if caller is an admin — service doesn't have user repo,
            # so we just allow any authenticated user to update tasks for now.
            # Stricter permission checks can be added once admin flag flows through.
            pass

        kwargs: dict = {"updated_at": datetime.now(timezone.utc)}
        if position is not None:
            kwargs["position"] = int(position)
        if title is not None:
            title = title.strip()
            if not title:
                raise ValueError("task title must not be empty")
            kwargs["title"] = title
        if description is not None:
            kwargs["description"] = description
        if status is not None:
            try:
                kwargs["status"] = TaskStatus(status)
            except ValueError as exc:
                raise ValueError(f"invalid status: {status!r}") from exc
        if due_date is not None:
            try:
                kwargs["due_date"] = date.fromisoformat(due_date[:10])
            except ValueError as exc:
                raise ValueError(f"invalid due_date: {due_date!r}") from exc
        if assignees is not None:
            kwargs["assignees"] = tuple(assignees)

        updated = replace(task, **kwargs)
        saved = await self._repo.save(updated)

        if self._bus is not None:
            # Generic "something changed" — powers live UI refresh.
            await self._bus.publish(TaskUpdated(task=saved))
            # Publish TaskAssigned for every new assignee (relative to
            # the pre-update state). Self-assignment is suppressed.
            previous = set(task.assignees or ())
            added = [u for u in (saved.assignees or ()) if u not in previous]
            for user_id in added:
                if user_id == actor_user_id:
                    continue
                await self._bus.publish(
                    TaskAssigned(
                        task=saved,
                        assigned_to=user_id,
                    )
                )

        # Publish TaskCompleted when transitioning to DONE.
        if saved.status == TaskStatus.DONE and task.status != TaskStatus.DONE:
            if self._bus is not None:
                await self._bus.publish(
                    TaskCompleted(
                        task=saved,
                        completed_by=actor_user_id,
                    )
                )
            # §15 recurrence: spawn the next instance so the user
            # doesn't lose the schedule.
            if saved.is_recurring():
                await self._spawn_recurrence(saved)

        return saved

    async def delete_task(self, task_id: str, *, actor_user_id: str) -> None:
        task = await self.get_task(task_id)  # raises KeyError if not found
        await self._repo.delete(task_id)
        if self._bus is not None:
            await self._bus.publish(
                TaskDeleted(
                    task_id=task_id,
                    list_id=task.list_id,
                )
            )

    async def reorder_tasks(
        self,
        list_id: str,
        *,
        ordered_ids: list[str],
    ) -> list[Task]:
        """Persist a new task order within a list.

        ``ordered_ids`` is the desired sequence. Each id gets its
        index as its ``position``; ids in the list that don't belong
        to ``list_id`` are silently skipped (defensive — protects
        against stale UIs). Emits one TaskUpdated per moved row.
        """
        if self._repo.get_list is None:
            raise RuntimeError("task repo missing")
        if await self._repo.get_list(list_id) is None:
            raise KeyError(f"task list {list_id!r} not found")
        updated: list[Task] = []
        for idx, tid in enumerate(ordered_ids):
            task = await self._repo.get(tid)
            if task is None or task.list_id != list_id:
                continue
            if task.position == idx:
                continue
            new_task = replace(
                task,
                position=idx,
                updated_at=datetime.now(timezone.utc),
            )
            saved = await self._repo.save(new_task)
            updated.append(saved)
            if self._bus is not None:
                await self._bus.publish(TaskUpdated(task=saved))
        return updated

    # ── Task comments / attachments (spec §23.68) ────────────────────

    async def add_comment(
        self,
        task_id: str,
        *,
        author_user_id: str,
        content: str,
    ) -> "TaskComment":
        """Attach a comment to a task. Author must exist; content non-empty."""
        await self._require_tasks_enabled()
        content = content.strip()
        if not content:
            raise ValueError("comment content must not be empty")
        await self.get_task(task_id)  # 404 if unknown
        comment = TaskComment(
            id=uuid.uuid4().hex,
            task_id=task_id,
            author=author_user_id,
            content=content,
            created_at=datetime.now(timezone.utc),
        )
        return await self._repo.add_comment(comment)

    async def list_comments(self, task_id: str) -> list["TaskComment"]:
        await self.get_task(task_id)
        return await self._repo.list_comments(task_id)

    async def delete_comment(
        self,
        comment_id: str,
        *,
        actor_user_id: str,
    ) -> None:
        """Author-or-admin only (we let the route enforce admin)."""
        await self._repo.delete_comment(comment_id)

    async def add_attachment(
        self,
        task_id: str,
        *,
        uploaded_by: str,
        url: str,
        filename: str,
        mime: str,
        size_bytes: int,
    ) -> "TaskAttachment":
        await self._require_tasks_enabled()
        await self.get_task(task_id)
        if size_bytes <= 0:
            raise ValueError("size_bytes must be > 0")
        attachment = TaskAttachment(
            id=uuid.uuid4().hex,
            task_id=task_id,
            uploaded_by=uploaded_by,
            url=url,
            filename=filename,
            mime=mime,
            size_bytes=size_bytes,
            created_at=datetime.now(timezone.utc),
        )
        return await self._repo.add_attachment(attachment)

    async def list_attachments(
        self,
        task_id: str,
    ) -> list["TaskAttachment"]:
        await self.get_task(task_id)
        return await self._repo.list_attachments(task_id)

    async def delete_attachment(self, attachment_id: str) -> None:
        await self._repo.delete_attachment(attachment_id)

    async def spawn_overdue_recurrences(
        self,
        *,
        today: date | None = None,
    ) -> list[Task]:
        """For every recurring task whose due-date has passed without
        a follow-up, spawn the next occurrence.

        Exposed for :class:`TaskRecurrenceScheduler`. Idempotent — the
        repo filter ``last_spawned_at <= due_date`` keeps us from
        re-spawning the same row.
        """
        today = today or date.today()
        overdue = await self._repo.list_recurring_overdue(today)
        spawned: list[Task] = []
        for task in overdue:
            child = await self._spawn_recurrence(task)
            if child is not None:
                spawned.append(child)
        return spawned

    async def _spawn_recurrence(self, completed: Task) -> Task | None:
        """Create the next recurring instance of ``completed``.

        Returns the new task, or ``None`` if the RRULE yields no next
        date (e.g. ``UNTIL`` clause already passed).
        """
        rec = completed.recurrence
        if rec is None:  # guarded by caller
            return None
        next_due = _next_occurrence(
            rec.rrule,
            base=completed.due_date or completed.created_at.date(),
        )
        if next_due is None:
            return None
        now = datetime.now(timezone.utc)
        spawned_rec = rec.mark_spawned(now)
        child = Task(
            id=uuid.uuid4().hex,
            list_id=completed.list_id,
            title=completed.title,
            status=TaskStatus.TODO,
            position=completed.position,
            created_by=completed.created_by,
            created_at=now,
            updated_at=now,
            description=completed.description,
            due_date=next_due,
            assignees=completed.assignees,
            recurrence=spawned_rec,
            recurrence_parent_id=completed.recurrence_parent_id or completed.id,
        )
        saved = await self._repo.save(child)
        # Update the parent so we don't re-spawn on the same completion
        # if the parent gets toggled back to done repeatedly.
        await self._repo.save(replace(completed, recurrence=spawned_rec))
        return saved


# ─── Minimal RRULE evaluator (§15) ────────────────────────────────────────


def _next_occurrence(rrule: str, *, base: date) -> date | None:
    """Return the next occurrence date after *base* for *rrule*.

    Supports the tiny RRULE subset the UI produces: ``FREQ=DAILY``,
    ``FREQ=WEEKLY``, ``FREQ=MONTHLY``, ``FREQ=YEARLY`` optionally with
    ``INTERVAL=N``. An unsupported or malformed rule returns ``None``
    so the caller can fall back gracefully.
    """
    parts: dict[str, str] = {}
    for chunk in (rrule or "").split(";"):
        if "=" not in chunk:
            continue
        k, _, v = chunk.partition("=")
        parts[k.strip().upper()] = v.strip().upper()
    freq = parts.get("FREQ", "")
    try:
        interval = max(1, int(parts.get("INTERVAL", "1")))
    except ValueError:
        return None
    if not isinstance(base, date):
        return None
    if freq == "DAILY":
        return base + timedelta(days=interval)
    if freq == "WEEKLY":
        return base + timedelta(weeks=interval)
    if freq == "MONTHLY":
        # Naive month bump: add interval months, clamping to end of month.
        m = base.month - 1 + interval
        year = base.year + m // 12
        month = m % 12 + 1
        # Clamp to the last valid day of the target month.
        last_day = calendar.monthrange(year, month)[1]
        return date(year, month, min(base.day, last_day))
    if freq == "YEARLY":
        try:
            return base.replace(year=base.year + interval)
        except ValueError:
            # Feb 29 on non-leap year — roll back to Feb 28.
            return date(base.year + interval, 2, 28)
    return None


class SpaceTaskService:
    """Space task list operations.

    Each method publishes the corresponding domain event with
    ``space_id`` set, so realtime + notification + federation layers
    can scope fan-out correctly.
    """

    __slots__ = ("_repo", "_bus")

    def __init__(
        self,
        space_task_repo: AbstractSpaceTaskRepo,
        bus=None,
    ) -> None:
        self._repo = space_task_repo
        self._bus = bus

    # ── Lists ────────────────────────────────────────────────────────────

    async def create_list(
        self,
        *,
        space_id: str,
        name: str,
        created_by: str,
    ) -> TaskList:
        name = name.strip()
        if not name:
            raise ValueError("task list name must not be empty")
        lst = TaskList(
            id=uuid.uuid4().hex,
            name=name,
            created_by=created_by,
        )
        saved = await self._repo.save_list(space_id, lst)
        if self._bus is not None:
            await self._bus.publish(
                TaskListCreated(
                    list_id=saved.id,
                    name=saved.name,
                    space_id=space_id,
                )
            )
        return saved

    async def rename_list(
        self,
        list_id: str,
        *,
        name: str,
    ) -> TaskList:
        name = name.strip()
        if not name:
            raise ValueError("task list name must not be empty")
        result = await self._repo.get_list(list_id)
        if result is None:
            raise KeyError(f"task list {list_id!r} not found")
        space_id, current = result
        updated = replace(current, name=name)
        saved = await self._repo.save_list(space_id, updated)
        if self._bus is not None:
            await self._bus.publish(
                TaskListUpdated(
                    list_id=saved.id,
                    name=saved.name,
                    space_id=space_id,
                )
            )
        return saved

    async def delete_list(self, list_id: str) -> None:
        result = await self._repo.get_list(list_id)
        if result is None:
            raise KeyError(f"task list {list_id!r} not found")
        space_id, _ = result
        await self._repo.delete_list(list_id)
        if self._bus is not None:
            await self._bus.publish(
                TaskListDeleted(
                    list_id=list_id,
                    space_id=space_id,
                )
            )

    async def list_lists(self, space_id: str) -> list[TaskList]:
        return await self._repo.list_lists(space_id)

    # ── Tasks ────────────────────────────────────────────────────────────

    async def list_tasks(self, space_id: str) -> list[Task]:
        return await self._repo.list_by_space(space_id)

    async def list_tasks_by_list(self, list_id: str) -> list[Task]:
        return await self._repo.list_by_list(list_id)

    async def create_task(
        self,
        *,
        space_id: str,
        list_id: str,
        title: str,
        created_by: str,
        description: str | None = None,
        due_date: str | None = None,
        assignees: list[str] | None = None,
    ) -> Task:
        title = title.strip()
        if not title:
            raise ValueError("task title must not be empty")
        due: date | None = None
        if due_date:
            try:
                due = date.fromisoformat(due_date[:10])
            except ValueError as exc:
                raise ValueError(f"invalid due_date: {due_date!r}") from exc
        now = datetime.now(timezone.utc)
        task = Task(
            id=uuid.uuid4().hex,
            list_id=list_id,
            title=title,
            status=TaskStatus.TODO,
            position=0,
            created_by=created_by,
            created_at=now,
            updated_at=now,
            description=description,
            due_date=due,
            assignees=tuple(assignees or []),
        )
        saved = await self._repo.save(space_id, task)
        if self._bus is not None:
            await self._bus.publish(TaskCreated(task=saved, space_id=space_id))
            for user_id in saved.assignees:
                if user_id == created_by:
                    continue
                await self._bus.publish(
                    TaskAssigned(
                        task=saved,
                        assigned_to=user_id,
                    )
                )
        return saved

    async def update_task(
        self,
        task_id: str,
        *,
        actor_user_id: str,
        title: str | None = None,
        description: str | None = None,
        status: str | None = None,
        due_date: str | None = None,
        assignees: list[str] | None = None,
        position: int | None = None,
    ) -> Task:
        result = await self._repo.get(task_id)
        if result is None:
            raise KeyError(f"space task {task_id!r} not found")
        space_id, task = result

        kwargs: dict = {"updated_at": datetime.now(timezone.utc)}
        if title is not None:
            title = title.strip()
            if not title:
                raise ValueError("task title must not be empty")
            kwargs["title"] = title
        if description is not None:
            kwargs["description"] = description
        if status is not None:
            try:
                kwargs["status"] = TaskStatus(status)
            except ValueError as exc:
                raise ValueError(f"invalid status: {status!r}") from exc
        if due_date is not None:
            try:
                kwargs["due_date"] = (
                    date.fromisoformat(due_date[:10]) if due_date else None
                )
            except ValueError as exc:
                raise ValueError(f"invalid due_date: {due_date!r}") from exc
        if assignees is not None:
            kwargs["assignees"] = tuple(assignees)
        if position is not None:
            kwargs["position"] = int(position)

        updated = replace(task, **kwargs)
        saved = await self._repo.save(space_id, updated)
        if self._bus is not None:
            await self._bus.publish(TaskUpdated(task=saved, space_id=space_id))
            previous = set(task.assignees or ())
            for user_id in saved.assignees or ():
                if user_id in previous or user_id == actor_user_id:
                    continue
                await self._bus.publish(
                    TaskAssigned(
                        task=saved,
                        assigned_to=user_id,
                    )
                )
            if saved.status == TaskStatus.DONE and task.status != TaskStatus.DONE:
                await self._bus.publish(
                    TaskCompleted(
                        task=saved,
                        completed_by=actor_user_id,
                    )
                )
        return saved

    async def delete_task(self, task_id: str) -> None:
        result = await self._repo.get(task_id)
        if result is None:
            raise KeyError(f"space task {task_id!r} not found")
        space_id, task = result
        await self._repo.delete(task_id)
        if self._bus is not None:
            await self._bus.publish(
                TaskDeleted(
                    task_id=task_id,
                    list_id=task.list_id,
                    space_id=space_id,
                )
            )
