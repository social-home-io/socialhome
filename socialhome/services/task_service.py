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
from typing import TYPE_CHECKING, Any

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
    MAX_TASK_ASSIGNEES,
    MAX_TASK_DESCRIPTION_LENGTH,
    MAX_TASK_LABEL_LENGTH,
    MAX_TASK_LABELS,
    MAX_TASK_LIST_NAME_LENGTH,
    MAX_TASK_TITLE_LENGTH,
    POSITION_MAX,
    POSITION_MIN,
    UNSET,
    Task,
    TaskAttachment,
    TaskComment,
    TaskList,
    TaskPriority,
    TaskStatus,
    Unset,
    normalize_labels,
    sanitize_block,
    sanitize_line,
    task_to_wire_dict,
)
from ..domain.space import (
    AccessDecision,
    ContentAction,
    ModerationTargetGoneError,
    Space,
    SpaceModerationItem,
    SpacePermissionError,
)
from ..federation.owner_bound_id import (
    SPACE_TASK_KIND,
    SPACE_TASK_LIST_KIND,
    mint_owner_bound_id,
)
from ..repositories.space_remote_member_repo import AbstractSpaceRemoteMemberRepo
from ..repositories.space_repo import AbstractSpaceRepo
from ..repositories.task_repo import AbstractTaskRepo, AbstractSpaceTaskRepo
from ..repositories.user_repo import AbstractUserRepo
from .bus_publisher import BusPublisherMixin
from .content_access import ContentAccessMixin
from .space_moderation_service import ApplyResult, item_payload_snapshot
from .space_service import SpaceService

if TYPE_CHECKING:
    from .space_moderation_service import ModerationSubmitter


def parse_assignees(value: object) -> tuple[str, ...] | None:
    """Validate a client-supplied ``assignees`` value.

    ``None`` means "not supplied" and passes through. Anything else must
    be a list of at most :data:`MAX_TASK_ASSIGNEES` non-empty user-id
    strings — a bare string is refused rather than iterated (which would
    split it into one "assignee" per character). Duplicates collapse,
    first occurrence wins. Raises :class:`ValueError` (→ 422).
    """
    if value is None:
        return None
    if not isinstance(value, (list, tuple)):
        raise ValueError("assignees must be a list of user ids")
    if len(value) > MAX_TASK_ASSIGNEES:
        raise ValueError(f"at most {MAX_TASK_ASSIGNEES} assignees per task")
    out: list[str] = []
    for item in value:
        if not isinstance(item, str) or not item.strip():
            raise ValueError("assignees must be non-empty user id strings")
        uid = item.strip()
        if uid not in out:
            out.append(uid)
    return tuple(out)


def _parse_line(value: object, *, what: str, limit: int) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{what} must be a string")
    text = sanitize_line(value)
    if not text:
        raise ValueError(f"{what} must not be empty")
    if len(text) > limit:
        raise ValueError(f"{what} is at most {limit} characters long")
    return text


def parse_title(value: object) -> str:
    """A client-supplied task title: sanitised (:func:`sanitize_line`),
    visible and at most :data:`MAX_TASK_TITLE_LENGTH` characters (→ 422)."""
    return _parse_line(value, what="task title", limit=MAX_TASK_TITLE_LENGTH)


def parse_list_name(value: object) -> str:
    """A client-supplied task-list name (at most
    :data:`MAX_TASK_LIST_NAME_LENGTH` characters, → 422 otherwise)."""
    return _parse_line(value, what="task list name", limit=MAX_TASK_LIST_NAME_LENGTH)


def parse_description(value: object) -> str | None:
    """A client-supplied description; ``None`` or visibly empty → ``None``.
    At most :data:`MAX_TASK_DESCRIPTION_LENGTH` characters (→ 422)."""
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError("description must be a string")
    text = sanitize_block(value)
    if len(text) > MAX_TASK_DESCRIPTION_LENGTH:
        raise ValueError(
            f"description is at most {MAX_TASK_DESCRIPTION_LENGTH} characters long"
        )
    return text or None


def parse_position(value: object) -> int:
    """A client-supplied position: an integer (or integer string) within
    SQLite's signed 64-bit range. Anything else → :class:`ValueError`."""
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        raise ValueError(f"invalid position: {value!r}")
    try:
        position = int(value)
    except ValueError as exc:
        raise ValueError(f"invalid position: {value!r}") from exc
    if not POSITION_MIN <= position <= POSITION_MAX:
        raise ValueError(f"invalid position: {value!r}")
    return position


def parse_status(value: object) -> TaskStatus:
    """A client-supplied status. Raises :class:`ValueError` (→ 422)."""
    try:
        return TaskStatus(str(value))
    except ValueError as exc:
        raise ValueError(f"invalid status: {value!r}") from exc


def parse_priority(value: object) -> TaskPriority | None:
    """A client-supplied priority; ``None`` / ``""`` mean "no priority".

    Raises :class:`ValueError` (→ 422) for anything but a known value.
    """
    if value is None or value == "":
        return None
    if not isinstance(value, str):
        raise ValueError(f"invalid priority: {value!r}")
    try:
        return TaskPriority(value)
    except ValueError as exc:
        raise ValueError(f"invalid priority: {value!r}") from exc


def parse_labels(value: object) -> tuple[str, ...]:
    """Validate a client-supplied ``labels`` value; ``None`` clears.

    Must be a list of at most :data:`MAX_TASK_LABELS` strings of at most
    :data:`MAX_TASK_LABEL_LENGTH` characters — refused (→ 422) rather than
    silently cut, so the user sees why. Then normalised with
    :func:`normalize_labels` (trimmed, case-insensitive de-duplication).
    """
    if value is None:
        return ()
    if not isinstance(value, (list, tuple)):
        raise ValueError("labels must be a list of strings")
    if len(value) > MAX_TASK_LABELS:
        raise ValueError(f"at most {MAX_TASK_LABELS} labels per task")
    for item in value:
        if not isinstance(item, str):
            raise ValueError("labels must be a list of strings")
        if len(item.strip()) > MAX_TASK_LABEL_LENGTH:
            raise ValueError(
                f"a label is at most {MAX_TASK_LABEL_LENGTH} characters long"
            )
    return normalize_labels(value)


def parse_due_date(value: object) -> date | None:
    """A client-supplied due date; ``None`` / ``""`` clear it."""
    if value is None or value == "":
        return None
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError as exc:
        raise ValueError(f"invalid due_date: {value!r}") from exc


def _require_moved_id(moved_id: str, ordered_ids: list[str]) -> None:
    """A reorder lists each id once and names the one task the user
    dragged, which must be in the new order (→ 422 otherwise)."""
    if len(set(ordered_ids)) != len(ordered_ids):
        raise ValueError("order must not contain duplicate task ids")
    if not moved_id or moved_id not in ordered_ids:
        raise ValueError("moved_id must be one of the reordered task ids")


def _apply_edits(
    task: Task,
    *,
    title: object,
    description: object,
    status: object,
    due_date: object,
    assignees: tuple[str, ...] | None,
    position: object,
    priority: object,
    labels: object,
) -> Task:
    """``task`` with every supplied field applied (both scopes).

    :data:`UNSET` leaves a field alone. For the nullable fields
    (``description``, ``due_date``, ``priority``) ``None`` clears; for the
    others ``None`` means "no change", as it always has; ``labels: None``
    clears the labels.
    """
    kwargs: dict = {"updated_at": datetime.now(timezone.utc)}
    if not isinstance(title, Unset) and title is not None:
        kwargs["title"] = parse_title(title)
    if not isinstance(description, Unset):
        kwargs["description"] = parse_description(description)
    if not isinstance(status, Unset) and status is not None:
        kwargs["status"] = parse_status(status)
    if not isinstance(due_date, Unset):
        kwargs["due_date"] = parse_due_date(due_date)
    if assignees is not None:
        kwargs["assignees"] = assignees
    if not isinstance(position, Unset) and position is not None:
        kwargs["position"] = parse_position(position)
    if not isinstance(priority, Unset):
        kwargs["priority"] = parse_priority(priority)
    if not isinstance(labels, Unset):
        kwargs["labels"] = parse_labels(labels)
    return replace(task, **kwargs)


class TaskService(BusPublisherMixin):
    """Household task list operations."""

    __slots__ = ("_repo", "_bus", "_household", "_users")

    def __init__(
        self,
        task_repo: AbstractTaskRepo,
        bus=None,
        *,
        user_repo: AbstractUserRepo | None = None,
    ) -> None:
        self._repo = task_repo
        self._bus = bus
        self._household = None
        self._users = user_repo

    def attach_household_features(self, svc) -> None:
        """Wire :class:`PreferencesService` so ``create_list`` /
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
        name = parse_list_name(name)
        task_list = TaskList(
            id=uuid.uuid4().hex,
            name=name,
            created_by=created_by,
        )
        saved = await self._repo.save_list(task_list)
        await self._emit(
            TaskListCreated(
                list_id=saved.id,
                name=saved.name,
                created_by=saved.created_by,
            )
        )
        return saved

    async def rename_list(self, list_id: str, *, name: str) -> TaskList:
        """Rename a task list. Raises KeyError if missing."""
        await self._require_tasks_enabled()
        name = parse_list_name(name)
        current = await self._repo.get_list(list_id)
        if current is None:
            raise KeyError(f"task list {list_id!r} not found")
        updated = replace(current, name=name)
        saved = await self._repo.save_list(updated)
        await self._emit(
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

    async def open_counts(self) -> dict[str, int]:
        """Open (not done, not archived) tasks per list id; absent = 0."""
        return await self._repo.open_counts()

    async def delete_list(self, list_id: str) -> None:
        result = await self._repo.get_list(list_id)
        if result is None:
            raise KeyError(f"task list {list_id!r} not found")
        await self._repo.delete_list(list_id)
        await self._emit(TaskListDeleted(list_id=list_id))

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
        status: str | None = None,
        priority: str | None = None,
        labels: list[str] | None = None,
    ) -> Task:
        """Create a task at the bottom of its status column.

        ``status`` lets a board column's quick-add file the task straight
        into that column (default ``todo``).
        """
        await self._require_tasks_enabled()
        title = parse_title(title)
        description = parse_description(description)
        parsed_assignees = parse_assignees(assignees) or ()
        task_status = parse_status(status) if status is not None else TaskStatus.TODO
        task_priority = parse_priority(priority)
        task_labels = parse_labels(labels)
        due = parse_due_date(due_date)
        # Ensure list exists
        task_list = await self._repo.get_list(list_id)
        if task_list is None:
            raise KeyError(f"task list {list_id!r} not found")
        await self._require_local_assignees(parsed_assignees)

        now = datetime.now(timezone.utc)
        task = Task(
            id=uuid.uuid4().hex,
            list_id=list_id,
            title=title,
            status=task_status,
            position=await self._repo.next_position(list_id),
            created_by=created_by,
            created_at=now,
            updated_at=now,
            description=description,
            due_date=due,
            assignees=parsed_assignees,
            priority=task_priority,
            labels=task_labels,
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

    # ── Edit rights + assignees ──────────────────────────────────────────

    async def _require_editor(self, task: Task, actor_user_id: str) -> None:
        """The task's creator, one of its assignees, or a household admin
        may change it; anyone else → :class:`PermissionError` (403).

        Without a user repo (bare unit wiring) every actor may edit.
        """
        if self._users is None:
            return
        if actor_user_id == task.created_by or actor_user_id in task.assignees:
            return
        actor = await self._users.get_by_user_id(actor_user_id)
        if actor is None or not actor.is_admin:
            raise PermissionError(
                "only the task's creator, an assignee or an admin can change it"
            )

    async def _require_local_assignees(self, user_ids: tuple[str, ...]) -> None:
        """Every id must be an active user of this household — assigning
        anyone else would hand them the task through ``TaskAssigned``."""
        if self._users is None:
            return
        for uid in user_ids:
            user = await self._users.get_by_user_id(uid)
            if user is None or not user.is_active():
                raise ValueError(
                    "every assignee must be an active member of this household"
                )

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
        title: str | None | Unset = UNSET,
        description: str | None | Unset = UNSET,
        status: str | None | Unset = UNSET,
        due_date: str | None | Unset = UNSET,
        assignees: list[str] | None | Unset = UNSET,
        position: int | None | Unset = UNSET,
        priority: str | None | Unset = UNSET,
        labels: list[str] | None | Unset = UNSET,
    ) -> Task:
        """Apply a partial edit. :data:`UNSET` = leave alone; ``None``
        clears ``description`` / ``due_date`` / ``priority`` / ``labels``.
        """
        task = await self.get_task(task_id)
        await self._require_editor(task, actor_user_id)

        parsed_assignees = (
            None if isinstance(assignees, Unset) else parse_assignees(assignees)
        )
        if parsed_assignees is not None:
            # Only ids being ADDED are checked: an assignee who has since
            # left must not block an unrelated edit.
            previous_ids = set(task.assignees or ())
            await self._require_local_assignees(
                tuple(u for u in parsed_assignees if u not in previous_ids)
            )
        updated = _apply_edits(
            task,
            title=title,
            description=description,
            status=status,
            due_date=due_date,
            assignees=parsed_assignees,
            position=position,
            priority=priority,
            labels=labels,
        )
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
            await self._emit(
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
        await self._require_editor(task, actor_user_id)
        await self._repo.delete(task_id)
        await self._emit(
            TaskDeleted(
                task_id=task_id,
                list_id=task.list_id,
            )
        )

    async def archive_task(self, task_id: str, *, actor_user_id: str) -> Task:
        return await self._set_archived(task_id, actor_user_id, archived=True)

    async def unarchive_task(self, task_id: str, *, actor_user_id: str) -> Task:
        return await self._set_archived(task_id, actor_user_id, archived=False)

    async def _set_archived(
        self,
        task_id: str,
        actor_user_id: str,
        *,
        archived: bool,
    ) -> Task:
        task = await self.get_task(task_id)
        await self._require_editor(task, actor_user_id)
        now = datetime.now(timezone.utc)
        updated = replace(
            task,
            archived_at=now if archived else None,
            updated_at=now,
        )
        saved = await self._repo.save(updated)
        await self._emit(TaskUpdated(task=saved))
        return saved

    async def reorder_tasks(
        self,
        list_id: str,
        *,
        ordered_ids: list[str],
        moved_id: str,
        actor_user_id: str,
    ) -> list[Task]:
        """Persist a new task order within a list.

        ``ordered_ids`` is the desired sequence (typically one status
        column of the board); each id gets its index as its ``position``,
        and ids that don't belong to ``list_id`` are silently skipped
        (defensive — protects against stale UIs). ``moved_id`` is the task
        the user actually dragged: only it needs the actor's edit rights
        (:meth:`_require_editor`) — its neighbours' positions shift as a
        side effect and need none, as long as they keep their relative
        order; changing that too needs edit rights on each of them
        (→ :class:`PermissionError`, 403). Duplicate ids → 422. Emits one
        TaskUpdated per moved row.
        """
        _require_moved_id(moved_id, ordered_ids)
        if await self._repo.get_list(list_id) is None:
            raise KeyError(f"task list {list_id!r} not found")
        moved = await self._repo.get(moved_id)
        if moved is None or moved.list_id != list_id:
            raise KeyError(f"task {moved_id!r} not found in this list")
        await self._require_editor(moved, actor_user_id)
        held: dict[str, Task] = {}
        for tid in ordered_ids:
            task = await self._repo.get(tid)
            if task is not None and task.list_id == list_id:
                held[tid] = task
        # Only the dragged card may change its place relative to the rest:
        # the others must keep their current order (position, then age —
        # the order the list is shown in). Rearranging them as well needs
        # edit rights on each of them (an admin, or their creator).
        others = [tid for tid in ordered_ids if tid in held and tid != moved_id]
        current = sorted(
            others, key=lambda tid: (held[tid].position, held[tid].created_at)
        )
        if others != current:
            for tid in others:
                await self._require_editor(held[tid], actor_user_id)
        updated: list[Task] = []
        for idx, tid in enumerate(ordered_ids):
            task = held.get(tid)
            if task is None or task.position == idx:
                continue
            new_task = replace(
                task,
                position=idx,
                updated_at=datetime.now(timezone.utc),
            )
            saved = await self._repo.save(new_task)
            updated.append(saved)
            await self._emit(TaskUpdated(task=saved))
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


class SpaceTaskService(BusPublisherMixin, ContentAccessMixin):
    """Space task list operations.

    Each method publishes the corresponding domain event with
    ``space_id`` set, so realtime + notification + federation layers
    can scope fan-out correctly.

    Every write passes the space's ``tasks`` access level (§4.3,
    :class:`ContentAccessMixin`) for the acting user: under
    ``ADMIN_ONLY`` only the owner / admins create, edit, move, reorder,
    archive or delete tasks and lists. Under ``MODERATED`` a member's new
    task / list, and their edit / delete / archive of somebody else's,
    waits in the space moderation queue (:class:`TaskModerationHandler`
    replays it on approval). A task's assignees own its status: their
    status change / column move proceeds. A same-column reorder is LAYOUT
    and never queues. Task comments are never gated.

    Every write takes ``approved_by`` — the moderation queue's replay —
    which gates it as that approver instead of the actor; the row stays
    the actor's.
    """

    __slots__ = ("_repo", "_bus", "_spaces", "_remote_members", "_moderation")

    def __init__(
        self,
        space_task_repo: AbstractSpaceTaskRepo,
        bus=None,
        *,
        space_repo: AbstractSpaceRepo,
        remote_member_repo: AbstractSpaceRemoteMemberRepo | None = None,
    ) -> None:
        self._repo = space_task_repo
        self._bus = bus
        self._spaces = space_repo
        # Optional: without it only local members are valid assignees
        # (fail-closed for cross-household assignment).
        self._remote_members = remote_member_repo
        self._moderation: "ModerationSubmitter | None" = None

    # ── Writer gate ──────────────────────────────────────────────────────

    async def require_writer(self, space_id: str, user_id: str) -> None:
        """Raise unless ``user_id`` may write this space's tasks.

        * unknown / dissolved space → :class:`KeyError` (404);
        * archived space (read-only) → :class:`SpacePermissionError`;
        * not a member, or a read-only subscriber →
          :class:`SpacePermissionError` (403), via the uniform
          :meth:`SpaceService.assert_writable_member` rule.
        """
        space = await self._spaces.get(space_id)
        if space is None or space.dissolved:
            raise KeyError(f"space {space_id!r} not found")
        if space.archived:
            raise SpacePermissionError(
                "space is archived (read-only) — unarchive it to make changes",
            )
        member = await self._spaces.get_member(space_id, user_id)
        if member is None:
            raise SpacePermissionError("not a member of this space")
        SpaceService.assert_writable_member(member, action="edit tasks", space=space)

    # ── Assignees ────────────────────────────────────────────────────────

    async def _require_member_assignees(
        self, space_id: str, user_ids: tuple[str, ...]
    ) -> None:
        """Every id must be a member of ``space_id`` — local
        (``space_members``) or a live remote member. Assigning to anyone
        else would hand them the task's title + description through the
        ``TaskAssigned`` notification / WS frame."""
        if not user_ids:
            return
        remote: set[str] | None = None
        for uid in user_ids:
            if await self._spaces.get_member(space_id, uid) is not None:
                continue
            if remote is None:
                remote = (
                    {
                        m.user_id
                        for m in await self._remote_members.list_for_space(space_id)
                    }
                    if self._remote_members is not None
                    else set()
                )
            if uid not in remote:
                raise ValueError("every assignee must be a member of this space")

    # ── Scope guards ─────────────────────────────────────────────────────
    #
    # Every by-id operation takes the caller's (path) ``space_id`` and
    # refuses a row that lives in a different space. A row id says nothing
    # about its space, so without this a member of space A could act on
    # space B's rows by id. Raised as KeyError (→ 404), never 403, so the
    # response does not confirm the id exists elsewhere.

    async def get_list_in_space(self, list_id: str, space_id: str) -> TaskList:
        """The task list ``list_id`` of ``space_id`` — :class:`KeyError`
        otherwise (never another space's)."""
        return await self._list_in_space(list_id, space_id)

    async def get_task_in_space(self, task_id: str, space_id: str) -> Task:
        """The task ``task_id`` of ``space_id`` — :class:`KeyError`
        otherwise (never another space's)."""
        return await self._task_in_space(task_id, space_id)

    async def _list_in_space(self, list_id: str, space_id: str) -> TaskList:
        result = await self._repo.get_list(list_id)
        if result is None or result[0] != space_id:
            raise KeyError(f"task list {list_id!r} not found in this space")
        return result[1]

    async def _task_in_space(self, task_id: str, space_id: str) -> Task:
        result = await self._repo.get(task_id)
        if result is None or result[0] != space_id:
            raise KeyError(f"space task {task_id!r} not found in this space")
        return result[1]

    # ── Lists ────────────────────────────────────────────────────────────

    async def create_list(
        self,
        *,
        space_id: str,
        name: str,
        created_by: str,
        list_id: str | None = None,
        approved_by: str | None = None,
    ) -> TaskList:
        name = parse_list_name(name)
        decision = await self._gate(
            space_id,
            approved_by or created_by,
            "tasks",
            ContentAction.CREATE,
            approved_by is None,
        )
        if decision is AccessDecision.QUEUE:
            await self._submit_for_review(
                space_id,
                created_by,
                "tasks",
                ContentAction.CREATE,
                payload={
                    "entity": "list",
                    "target_id": _mint_list_id(space_id, created_by),
                    "name": name,
                },
            )
        lst = TaskList(
            # Owner-bound (v_40): only the creator's household can
            # announce this list id.
            id=list_id or _mint_list_id(space_id, created_by),
            name=name,
            created_by=created_by,
        )
        await self._repo.save_list(lst, space_id=space_id)
        saved = lst
        await self._emit(
            TaskListCreated(
                list_id=saved.id,
                name=saved.name,
                space_id=space_id,
                created_by=saved.created_by,
                actor_user_id=created_by,
            )
        )
        return saved

    async def rename_list(
        self,
        list_id: str,
        *,
        space_id: str,
        name: str,
        actor_user_id: str,
        approved_by: str | None = None,
    ) -> TaskList:
        name = parse_list_name(name)
        current = await self._list_in_space(list_id, space_id)
        decision = await self._gate(
            space_id,
            approved_by or actor_user_id,
            "tasks",
            ContentAction.EDIT,
            approved_by is None and current.created_by == actor_user_id,
        )
        if decision is AccessDecision.QUEUE:
            await self._submit_for_review(
                space_id,
                actor_user_id,
                "tasks",
                ContentAction.EDIT,
                payload={
                    "entity": "list",
                    "target_id": list_id,
                    "patch": {"name": name},
                },
                snapshot={"name": current.name},
            )
        updated = replace(current, name=name)
        if not await self._repo.save_list(updated, space_id=space_id):
            raise KeyError(f"task list {list_id!r} not found")
        saved = updated
        await self._emit(
            TaskListUpdated(
                list_id=saved.id,
                name=saved.name,
                space_id=space_id,
                actor_user_id=actor_user_id,
            )
        )
        return saved

    async def delete_list(
        self,
        list_id: str,
        *,
        space_id: str,
        actor_user_id: str,
        approved_by: str | None = None,
    ) -> None:
        current = await self._list_in_space(list_id, space_id)
        decision = await self._gate(
            space_id,
            approved_by or actor_user_id,
            "tasks",
            ContentAction.DELETE,
            approved_by is None and current.created_by == actor_user_id,
        )
        if decision is AccessDecision.QUEUE:
            await self._submit_for_review(
                space_id,
                actor_user_id,
                "tasks",
                ContentAction.DELETE,
                payload={"entity": "list", "target_id": list_id},
                snapshot={"name": current.name, "created_by": current.created_by},
            )
        # ``deleted_by`` is who authorised it — the approver for a
        # reviewed delete — so a replay of the tombstone passes the space's
        # level on a household that missed the live (approved) event.
        await self._repo.delete_list(
            list_id, space_id=space_id, deleted_by=approved_by or actor_user_id
        )
        await self._emit(
            TaskListDeleted(
                list_id=list_id,
                space_id=space_id,
                actor_user_id=actor_user_id,
            )
        )

    async def list_lists(self, space_id: str) -> list[TaskList]:
        return await self._repo.list_lists(space_id)

    async def open_counts(self, space_id: str) -> dict[str, int]:
        """Open (not done, not archived) tasks per list id; absent = 0."""
        return await self._repo.open_counts(space_id)

    # ── Tasks ────────────────────────────────────────────────────────────

    async def list_tasks(self, space_id: str) -> list[Task]:
        return await self._repo.list_by_space(space_id)

    async def list_tasks_by_list(self, list_id: str, *, space_id: str) -> list[Task]:
        await self._list_in_space(list_id, space_id)
        return await self._repo.list_by_list(list_id, space_id=space_id)

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
        status: str | None = None,
        priority: str | None = None,
        labels: list[str] | None = None,
        task_id: str | None = None,
        approved_by: str | None = None,
    ) -> Task:
        """Create a space task at the bottom of its status column."""
        title = parse_title(title)
        description = parse_description(description)
        parsed_assignees = parse_assignees(assignees) or ()
        task_status = parse_status(status) if status is not None else TaskStatus.TODO
        task_priority = parse_priority(priority)
        task_labels = parse_labels(labels)
        due = parse_due_date(due_date)
        decision = await self._gate(
            space_id,
            approved_by or created_by,
            "tasks",
            ContentAction.CREATE,
            approved_by is None,
        )
        await self._require_member_assignees(space_id, parsed_assignees)
        if decision is AccessDecision.QUEUE:
            await self._list_in_space(list_id, space_id)
            await self._submit_for_review(
                space_id,
                created_by,
                "tasks",
                ContentAction.CREATE,
                payload={
                    "entity": "task",
                    "target_id": _mint_task_id(space_id, created_by),
                    "list_id": list_id,
                    "title": title,
                    "description": description,
                    "status": task_status.value,
                    "due_date": due.isoformat() if due else None,
                    "assignees": list(parsed_assignees),
                    "priority": task_priority.value if task_priority else None,
                    "labels": list(task_labels),
                },
            )
        now = datetime.now(timezone.utc)
        task = Task(
            # Owner-bound (v_36): only the creator's household can
            # announce this id.
            id=task_id or _mint_task_id(space_id, created_by),
            list_id=list_id,
            title=title,
            status=task_status,
            position=await self._repo.next_position(list_id, space_id=space_id),
            created_by=created_by,
            created_at=now,
            updated_at=now,
            description=description,
            due_date=due,
            assignees=parsed_assignees,
            priority=task_priority,
            labels=task_labels,
        )
        if not await self._repo.save(task, space_id=space_id):
            raise KeyError(f"task list {list_id!r} not found in this space")
        saved = task
        if self._bus is not None:
            await self._bus.publish(
                TaskCreated(task=saved, space_id=space_id, actor_user_id=created_by)
            )
            for user_id in saved.assignees:
                if user_id == created_by:
                    continue
                await self._bus.publish(
                    TaskAssigned(
                        task=saved,
                        assigned_to=user_id,
                        space_id=space_id,
                    )
                )
        return saved

    async def update_task(
        self,
        task_id: str,
        *,
        space_id: str,
        actor_user_id: str,
        title: str | None | Unset = UNSET,
        description: str | None | Unset = UNSET,
        status: str | None | Unset = UNSET,
        due_date: str | None | Unset = UNSET,
        assignees: list[str] | None | Unset = UNSET,
        position: int | None | Unset = UNSET,
        priority: str | None | Unset = UNSET,
        labels: list[str] | None | Unset = UNSET,
        approved_by: str | None = None,
    ) -> Task:
        """Partial edit, same field rules as :meth:`TaskService.update_task`.

        Collaborative: any writable member may edit any task of the space
        (the route's writer gate already ran), subject to the ``tasks``
        access level. A change of ``position`` alone is a LAYOUT write;
        anything else — a status change / column move included — an EDIT,
        which the task's creator owns, and its assignees too as far as the
        status (and position) go.
        """
        task = await self._task_in_space(task_id, space_id)
        sent = {
            k: v
            for k, v in (
                ("title", title),
                ("description", description),
                ("status", status),
                ("due_date", due_date),
                ("assignees", assignees),
                ("position", position),
                ("priority", priority),
                ("labels", labels),
            )
            if not isinstance(v, Unset)
        }
        layout_only = set(sent) == {"position"}
        owns = task.created_by == actor_user_id or (
            actor_user_id in (task.assignees or ())
            and set(sent) <= {"status", "position"}
        )
        decision = await self._gate(
            space_id,
            approved_by or actor_user_id,
            "tasks",
            ContentAction.LAYOUT if layout_only else ContentAction.EDIT,
            approved_by is None and owns,
        )
        if decision is AccessDecision.QUEUE:
            patch = _task_patch(sent)
            if "assignees" in patch:
                previous_ids = set(task.assignees or ())
                await self._require_member_assignees(
                    space_id,
                    tuple(u for u in patch["assignees"] if u not in previous_ids),
                )
            before = _task_dict(task)
            await self._submit_for_review(
                space_id,
                actor_user_id,
                "tasks",
                ContentAction.EDIT,
                payload={"entity": "task", "target_id": task.id, "patch": patch},
                snapshot={k: before.get(k) for k in patch},
            )

        parsed_assignees = (
            None if isinstance(assignees, Unset) else parse_assignees(assignees)
        )
        if parsed_assignees is not None:
            # Only ids being ADDED are checked: an existing assignee who
            # has since left must not block an unrelated edit.
            previous_ids = set(task.assignees or ())
            await self._require_member_assignees(
                space_id,
                tuple(u for u in parsed_assignees if u not in previous_ids),
            )
        updated = _apply_edits(
            task,
            title=title,
            description=description,
            status=status,
            due_date=due_date,
            assignees=parsed_assignees,
            position=position,
            priority=priority,
            labels=labels,
        )
        if not await self._repo.save(updated, space_id=space_id):
            raise KeyError(f"space task {updated.id!r} not found in this space")
        saved = updated
        if self._bus is not None:
            await self._bus.publish(
                TaskUpdated(task=saved, space_id=space_id, actor_user_id=actor_user_id)
            )
            previous = set(task.assignees or ())
            for user_id in saved.assignees or ():
                if user_id in previous or user_id == actor_user_id:
                    continue
                await self._bus.publish(
                    TaskAssigned(
                        task=saved,
                        assigned_to=user_id,
                        space_id=space_id,
                    )
                )
            if saved.status == TaskStatus.DONE and task.status != TaskStatus.DONE:
                await self._bus.publish(
                    TaskCompleted(
                        task=saved,
                        completed_by=actor_user_id,
                        space_id=space_id,
                    )
                )
        return saved

    async def reorder_tasks(
        self,
        space_id: str,
        list_id: str,
        *,
        ordered_ids: list[str],
        moved_id: str,
        actor_user_id: str,
    ) -> list[Task]:
        """Persist a new task order within a space list.

        Mirrors :meth:`TaskService.reorder_tasks`: each id gets its index
        as its ``position``; an id that is not a task of ``list_id`` in
        ``space_id`` is skipped. The list itself must live in
        ``space_id`` (→ 404 otherwise). Every moved row emits
        :class:`TaskUpdated`, so it federates to the space's member
        households like any other edit. ``moved_id`` is the dragged task
        (it must be in the order and in this list); space tasks are
        collaborative, so the route's writer gate is the only rights
        check.
        """
        _require_moved_id(moved_id, ordered_ids)
        await self._list_in_space(list_id, space_id)
        moved = await self._task_in_space(moved_id, space_id)
        if moved.list_id != list_id:
            raise KeyError(f"space task {moved_id!r} not found in this list")
        await self._gate(
            space_id,
            actor_user_id,
            "tasks",
            ContentAction.LAYOUT,
            moved.created_by == actor_user_id,
        )
        updated: list[Task] = []
        for idx, tid in enumerate(ordered_ids):
            held = await self._repo.get(tid)
            if held is None or held[0] != space_id:
                continue
            task = held[1]
            if task.list_id != list_id or task.position == idx:
                continue
            new_task = replace(
                task, position=idx, updated_at=datetime.now(timezone.utc)
            )
            if not await self._repo.save(new_task, space_id=space_id):
                continue
            updated.append(new_task)
            await self._emit(
                TaskUpdated(
                    task=new_task, space_id=space_id, actor_user_id=actor_user_id
                )
            )
        return updated

    async def delete_task(
        self,
        task_id: str,
        *,
        space_id: str,
        actor_user_id: str,
        approved_by: str | None = None,
    ) -> None:
        task = await self._task_in_space(task_id, space_id)
        decision = await self._gate(
            space_id,
            approved_by or actor_user_id,
            "tasks",
            ContentAction.DELETE,
            approved_by is None and task.created_by == actor_user_id,
        )
        if decision is AccessDecision.QUEUE:
            await self._submit_for_review(
                space_id,
                actor_user_id,
                "tasks",
                ContentAction.DELETE,
                payload={"entity": "task", "target_id": task.id, "op": "delete"},
                snapshot=_task_dict(task),
            )
        await self._repo.delete(task_id, space_id=space_id)
        await self._emit(
            TaskDeleted(
                task_id=task_id,
                list_id=task.list_id,
                space_id=space_id,
                actor_user_id=actor_user_id,
            )
        )

    async def archive_task(
        self,
        task_id: str,
        *,
        space_id: str,
        actor_user_id: str,
        approved_by: str | None = None,
    ) -> Task:
        return await self._set_archived(
            task_id, space_id, actor_user_id, archived=True, approved_by=approved_by
        )

    async def unarchive_task(
        self,
        task_id: str,
        *,
        space_id: str,
        actor_user_id: str,
        approved_by: str | None = None,
    ) -> Task:
        return await self._set_archived(
            task_id, space_id, actor_user_id, archived=False, approved_by=approved_by
        )

    async def _set_archived(
        self,
        task_id: str,
        space_id: str,
        actor_user_id: str,
        *,
        archived: bool,
        approved_by: str | None = None,
    ) -> Task:
        task = await self._task_in_space(task_id, space_id)
        decision = await self._gate(
            space_id,
            approved_by or actor_user_id,
            "tasks",
            ContentAction.DELETE,
            approved_by is None and task.created_by == actor_user_id,
        )
        if decision is AccessDecision.QUEUE:
            await self._submit_for_review(
                space_id,
                actor_user_id,
                "tasks",
                ContentAction.DELETE,
                payload={
                    "entity": "task",
                    "target_id": task.id,
                    "op": "archive" if archived else "unarchive",
                },
                snapshot=_task_dict(task),
            )
        now = datetime.now(timezone.utc)
        updated = replace(
            task,
            archived_at=now if archived else None,
            updated_at=now,
        )
        if not await self._repo.save(updated, space_id=space_id):
            raise KeyError(f"space task {updated.id!r} not found in this space")
        saved = updated
        await self._emit(
            TaskUpdated(task=saved, space_id=space_id, actor_user_id=actor_user_id)
        )
        return saved


def _mint_task_id(space_id: str, created_by: str) -> str:
    return mint_owner_bound_id(
        SPACE_TASK_KIND, space_id=space_id, owner_user_id=created_by
    )


def _mint_list_id(space_id: str, created_by: str) -> str:
    return mint_owner_bound_id(
        SPACE_TASK_LIST_KIND, space_id=space_id, owner_user_id=created_by
    )


def _task_dict(task: Task) -> dict[str, Any]:
    """A task's reviewable fields in their API form."""
    return {
        "list_id": task.list_id,
        "title": task.title,
        "description": task.description,
        "status": task.status.value,
        "due_date": task.due_date.isoformat() if task.due_date else None,
        "assignees": list(task.assignees or ()),
        "position": int(task.position),
        "priority": task.priority.value if task.priority is not None else None,
        "labels": list(task.labels),
        "created_by": task.created_by,
        "archived": task.archived_at is not None,
    }


def _task_patch(sent: dict[str, Any]) -> dict[str, Any]:
    """The sent task fields normalised with the REST path's parsers into
    their API form. ``None`` keeps its per-field meaning (clears the
    nullable ones, "no change" for title / status / position)."""
    patch: dict[str, Any] = {}
    for key, value in sent.items():
        match key:
            case "title":
                if value is not None:
                    patch["title"] = parse_title(value)
            case "description":
                patch["description"] = parse_description(value)
            case "status":
                if value is not None:
                    patch["status"] = parse_status(value).value
            case "due_date":
                due = parse_due_date(value)
                patch["due_date"] = due.isoformat() if due else None
            case "assignees":
                parsed = parse_assignees(value)
                if parsed is not None:
                    patch["assignees"] = list(parsed)
            case "position":
                if value is not None:
                    patch["position"] = parse_position(value)
            case "priority":
                prio = parse_priority(value)
                patch["priority"] = prio.value if prio is not None else None
            case "labels":
                patch["labels"] = list(parse_labels(value))
    if not patch:
        raise ValueError("an edit needs at least one field")
    return patch


_TASK_CREATE_FIELDS = (
    "list_id",
    "title",
    "description",
    "status",
    "due_date",
    "assignees",
    "priority",
    "labels",
)


class TaskModerationHandler:
    """Queue items of a space's task board (``tasks`` create / edit /
    delete of a task or a task list; archive / unarchive ride ``delete``).
    Applies through :class:`SpaceTaskService` gated as the approver; a
    field edit is latest-wins per field."""

    __slots__ = ("_svc",)

    def __init__(self, service: SpaceTaskService) -> None:
        self._svc = service

    def validate(self, space: Space, payload: dict) -> dict:
        entity = payload.get("entity")
        if entity not in ("task", "list") or not isinstance(
            payload.get("target_id"), str
        ):
            raise ValueError("not a task submission")
        out: dict[str, Any] = {"entity": entity, "target_id": payload["target_id"]}
        if "patch" in payload:
            raw = payload["patch"]
            if not isinstance(raw, dict) or not raw:
                raise ValueError("an edit needs at least one field")
            out["patch"] = (
                {"name": parse_list_name(raw.get("name"))}
                if entity == "list"
                else _task_patch(raw)
            )
        elif "op" in payload:
            if payload["op"] not in ("delete", "archive", "unarchive"):
                raise ValueError("unknown task operation")
            out["op"] = payload["op"]
        elif entity == "list" and "name" in payload:
            out["name"] = parse_list_name(payload["name"])
        elif entity == "task" and "title" in payload:
            out.update({k: payload.get(k) for k in _TASK_CREATE_FIELDS})
            out["title"] = parse_title(out["title"])
            out["description"] = parse_description(out["description"])
        return out

    async def snapshot(self, space_id: str, target_id: str) -> dict | None:
        try:
            task = await self._svc.get_task_in_space(target_id, space_id)
        except KeyError:
            try:
                lst = await self._svc.get_list_in_space(target_id, space_id)
            except KeyError:
                return None
            return {"name": lst.name, "created_by": lst.created_by}
        return _task_dict(task)

    async def held(self, space_id: str, target_id: str) -> dict | None:
        """:meth:`snapshot` plus the fields an edit never changes (the
        recurrence) — what a federated release is checked against (v_43)."""
        row = await self.snapshot(space_id, target_id)
        if row is None:
            return None
        try:
            task = await self._svc.get_task_in_space(target_id, space_id)
        except KeyError:
            return row  # a list
        wire = task_to_wire_dict(task, space_id)
        return {
            **row,
            "recurrence": wire.get("recurrence"),
            "recurrence_parent_id": wire.get("recurrence_parent_id"),
        }

    async def apply(
        self, item: SpaceModerationItem, *, approved_by: str, force: bool
    ) -> ApplyResult:
        p = item.payload
        target = str(p["target_id"])
        live = await self.snapshot(item.space_id, target)
        common: dict[str, Any] = {
            "space_id": item.space_id,
            "approved_by": approved_by,
        }
        match (item.action, p["entity"]):
            case (ContentAction.CREATE.value, "list"):
                if live is None:
                    await self._svc.create_list(
                        name=p["name"],
                        created_by=item.submitted_by,
                        list_id=target,
                        **common,
                    )
            case (ContentAction.CREATE.value, "task"):
                if live is None:
                    try:
                        await self._svc.create_task(
                            list_id=p["list_id"],
                            title=p["title"],
                            created_by=item.submitted_by,
                            description=p.get("description"),
                            due_date=p.get("due_date"),
                            assignees=p.get("assignees") or [],
                            status=p.get("status"),
                            priority=p.get("priority"),
                            labels=p.get("labels") or [],
                            task_id=target,
                            **common,
                        )
                    except KeyError as exc:
                        # The list it was meant for is gone.
                        raise ModerationTargetGoneError(target) from exc
            case (ContentAction.EDIT.value, entity):
                if live is None:
                    raise ModerationTargetGoneError(target)
                if entity == "list":
                    await self._svc.rename_list(
                        target,
                        name=p["patch"]["name"],
                        actor_user_id=item.submitted_by,
                        **common,
                    )
                else:
                    await self._svc.update_task(
                        target,
                        actor_user_id=item.submitted_by,
                        **common,
                        **p["patch"],
                    )
            case (ContentAction.DELETE.value, entity):
                if live is not None:
                    if entity == "list":
                        await self._svc.delete_list(
                            target, actor_user_id=item.submitted_by, **common
                        )
                    elif p.get("op") == "archive":
                        await self._svc.archive_task(
                            target, actor_user_id=item.submitted_by, **common
                        )
                    elif p.get("op") == "unarchive":
                        await self._svc.unarchive_task(
                            target, actor_user_id=item.submitted_by, **common
                        )
                    else:
                        await self._svc.delete_task(
                            target, actor_user_id=item.submitted_by, **common
                        )
        return ApplyResult(target_id=target)

    def preview(self, item: SpaceModerationItem) -> dict:
        p = item.payload
        if item.action == ContentAction.EDIT.value:
            return dict(p.get("patch") or {})
        if item.action == ContentAction.DELETE.value:
            return item_payload_snapshot(item) or {}
        if p.get("entity") == "list":
            return {"name": p.get("name")}
        return {k: p.get(k) for k in _TASK_CREATE_FIELDS}
