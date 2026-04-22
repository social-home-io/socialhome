"""Task-related domain types (§5.2).

A :class:`TaskList` groups :class:`Task` records. Tasks have a life-cycle
(:class:`TaskStatus`), can be assigned to one or more users, and may be
recurring (via an RFC 5545 ``RRULE`` in :class:`RecurrenceRule`).

The domain layer stores the rrule string only — computing the next
occurrence is delegated to the service layer so this module stays free of
external dependencies.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass
from datetime import date, datetime, timezone
from enum import StrEnum


class TaskStatus(StrEnum):
    TODO = "todo"
    IN_PROGRESS = "in_progress"
    DONE = "done"


@dataclass(slots=True, frozen=True)
class RecurrenceRule:
    """RFC 5545 ``RRULE`` string plus the last time this task spawned.

    Evaluating the rule (computing the next occurrence) is done by the
    service layer — :class:`RecurrenceRule` is an immutable value carrier.
    """

    rrule: str
    last_spawned_at: datetime | None = None

    def mark_spawned(self, now: datetime | None = None) -> "RecurrenceRule":
        return copy.replace(
            self,
            last_spawned_at=now or datetime.now(timezone.utc),
        )


@dataclass(slots=True, frozen=True)
class TaskList:
    id: str
    name: str
    created_by: str  # user_id


@dataclass(slots=True, frozen=True)
class Task:
    id: str
    list_id: str
    title: str
    status: TaskStatus
    position: int
    created_by: str  # user_id
    created_at: datetime
    updated_at: datetime

    description: str | None = None
    due_date: date | None = None
    assignees: tuple[str, ...] = ()  # user_ids
    recurrence: RecurrenceRule | None = None
    recurrence_parent_id: str | None = None

    def is_recurring(self) -> bool:
        return self.recurrence is not None

    def complete(self, now: datetime | None = None) -> "Task":
        return copy.replace(
            self,
            status=TaskStatus.DONE,
            updated_at=now or datetime.now(timezone.utc),
        )

    def reopen(self, now: datetime | None = None) -> "Task":
        return copy.replace(
            self,
            status=TaskStatus.TODO,
            updated_at=now or datetime.now(timezone.utc),
        )

    def start(self, now: datetime | None = None) -> "Task":
        return copy.replace(
            self,
            status=TaskStatus.IN_PROGRESS,
            updated_at=now or datetime.now(timezone.utc),
        )

    def with_assignees(
        self, assignees: tuple[str, ...], *, now: datetime | None = None
    ) -> "Task":
        return copy.replace(
            self,
            assignees=assignees,
            updated_at=now or datetime.now(timezone.utc),
        )


@dataclass(slots=True, frozen=True)
class TaskComment:
    """A comment attached to a task (spec §23.68)."""

    id: str
    task_id: str
    author: str
    content: str
    created_at: datetime


@dataclass(slots=True, frozen=True)
class TaskAttachment:
    """A file attached to a task (spec §23.68)."""

    id: str
    task_id: str
    uploaded_by: str
    url: str
    filename: str
    mime: str
    size_bytes: int
    created_at: datetime


@dataclass(slots=True, frozen=True)
class TaskUpdate:
    """Partial update payload for ``PATCH /api/tasks/{id}``.

    ``None`` in any field means "no change".
    """

    title: str | None = None
    description: str | None = None
    due_date: date | None = None
    assignees: tuple[str, ...] | None = None
    status: TaskStatus | None = None
    position: int | None = None
