"""Task-related domain types (§5.2).

A :class:`TaskList` groups :class:`Task` records. Tasks have a life-cycle
(:class:`TaskStatus`), can be assigned to one or more users, and may be
recurring (via an RFC 5545 ``RRULE`` in :class:`RecurrenceRule`).

The domain layer stores the rrule string only — computing the next
occurrence is delegated to the service layer so this module stays free of
external dependencies.

Tasks also carry an optional :class:`TaskPriority` and a short tuple of
free-text labels (:func:`normalize_labels`). The one federation wire shape
for a space task — live ``SPACE_TASK_*`` events, the sync exporters and the
resume replay — is :func:`task_to_wire_dict` / :func:`task_from_wire_dict`,
so the serialisers can't drift apart again.
"""

from __future__ import annotations

import copy
import unicodedata
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import date, datetime, timezone
from enum import StrEnum
from typing import Any, Final


class TaskStatus(StrEnum):
    TODO = "todo"
    IN_PROGRESS = "in_progress"
    DONE = "done"


class TaskPriority(StrEnum):
    """How urgent a task is. ``None`` on a task means "no priority"."""

    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    URGENT = "urgent"


#: Upper bound on labels per task.
MAX_TASK_LABELS: Final = 10
#: Upper bound on one label's length, in characters.
MAX_TASK_LABEL_LENGTH: Final = 32

#: Upper bounds on a task's text, in characters (REST refuses longer
#: input with 422; a federated value is cut to fit).
MAX_TASK_TITLE_LENGTH: Final = 200
MAX_TASK_DESCRIPTION_LENGTH: Final = 5000
MAX_TASK_LIST_NAME_LENGTH: Final = 100
#: Upper bound on assignees per task (household and space alike).
MAX_TASK_ASSIGNEES: Final = 10
#: SQLite ``INTEGER`` is a signed 64-bit value.
POSITION_MIN: Final = -(2**63)
POSITION_MAX: Final = 2**63 - 1

#: Characters that only serve to spoof how a title / name / label renders:
#: bidi embeddings, overrides and isolates, the LRM / RLM / ALM marks, and
#: the Unicode line / paragraph separators. They are format (``Cf``) or
#: separator characters, so the control-character filter alone misses them.
_SPOOF_CHARS: Final = frozenset(
    "\u202a\u202b\u202c\u202d\u202e\u2066\u2067\u2068\u2069"
    "\u200e\u200f\u061c\u2028\u2029"
)
#: Invisible characters that are legitimate inside text (the ZWJ glues an
#: emoji sequence) but make a string visibly empty on their own.
_ZERO_WIDTH: Final = frozenset("\u200b\u200c\u200d\u2060\ufeff")


def _visible(text: str) -> bool:
    return any(not ch.isspace() and ch not in _ZERO_WIDTH for ch in text)


def _scrub(raw: str, *, keep: frozenset[str] = frozenset()) -> str:
    return "".join(
        ch
        for ch in raw
        if ch in keep or (unicodedata.category(ch) != "Cc" and ch not in _SPOOF_CHARS)
    )


def sanitize_line(raw: str) -> str:
    """One line of user text (a title, list name or label): control and
    spoofing characters removed, trimmed; ``""`` when nothing visible is
    left (whitespace / zero-width only)."""
    text = _scrub(raw).strip()
    return text if _visible(text) else ""


def sanitize_block(raw: str) -> str:
    """Multi-line user text (a description): like :func:`sanitize_line`
    but newlines and tabs are kept."""
    text = _scrub(raw, keep=frozenset("\n\t")).strip()
    return text if _visible(text) else ""


def _capped_line(raw: object, limit: int) -> str:
    """:func:`sanitize_line`, cut to ``limit`` (``""`` for a non-string)."""
    if not isinstance(raw, str):
        return ""
    return sanitize_line(sanitize_line(raw)[:limit])


def normalize_labels(value: Iterable[object]) -> tuple[str, ...]:
    """Canonical label tuple for a task.

    Lenient by design — it is applied to federated payloads as well as to
    validated API input: non-string items and labels that are empty after
    removing control / spoofing characters (:func:`sanitize_line`) are
    dropped, each label is cut to
    :data:`MAX_TASK_LABEL_LENGTH` characters, duplicates collapse
    case-insensitively (the first spelling wins) and at most
    :data:`MAX_TASK_LABELS` are kept.
    """
    out: list[str] = []
    seen: set[str] = set()
    for item in value:
        if not isinstance(item, str):
            continue
        label = _capped_line(item, MAX_TASK_LABEL_LENGTH)
        if not label:
            continue
        key = label.casefold()
        if key in seen:
            continue
        seen.add(key)
        out.append(label)
        if len(out) >= MAX_TASK_LABELS:
            break
    return tuple(out)


class _Unset:
    """Sentinel type for "leave this field unchanged" on a task edit."""

    __slots__ = ()

    def __repr__(self) -> str:
        return "UNSET"


#: "Not supplied" for a partial task update — distinct from ``None``, which
#: clears a nullable field (due date, description, priority).
UNSET: Final = _Unset()
#: Public name of the sentinel's type, for callers' type hints.
Unset = _Unset


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
class TaskListTombstone:
    """A deleted space task list (migration 0069): its id, when it was
    deleted here (naive UTC, SQLite ``datetime('now')``), the list's
    creator (binds an owner-bound id to its space on the receiver) and the
    user who deleted it (empty when nobody can be named)."""

    id: str
    deleted_at: str
    created_by: str = ""
    deleted_by: str = ""


@dataclass(slots=True, frozen=True)
class TaskTombstone:
    """A deleted space task (migration 0071): its id, the list it was filed
    under, when it was deleted here (naive UTC, SQLite ``datetime('now')``),
    the task's creator (binds an owner-bound id to its space on the
    receiver) and the user who deleted it (empty when nobody can be
    named)."""

    id: str
    list_id: str
    deleted_at: str
    created_by: str = ""
    deleted_by: str = ""


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
    archived_at: datetime | None = None
    priority: TaskPriority | None = None
    labels: tuple[str, ...] = ()

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


# ─── Federation wire codec (space tasks) ─────────────────────────────────
#
# Merge rule for :func:`task_from_wire_dict`:
#
# * a key that is absent keeps the value already held (the default for a
#   new row);
# * a key that is present is authoritative — ``null`` / ``[]`` clears it;
# * a payload without the ``priority`` key comes from a v39 household.
#   Its inbound path dropped ``due_date``, ``archived_at`` and the
#   recurrence of every remote task it stored, so the values it sends back
#   in an edit are not its user's intent. For a row already held here those
#   fields (and ``priority`` / ``labels``, which it never knew) count as
#   absent; for a new row there is nothing to wipe, so they are taken.

#: Fields a v39 sender may have lost on its own copy of a remote task.
_V39_LOSSY_FIELDS: Final = (
    "due_date",
    "archived_at",
    "recurrence",
    "recurrence_parent_id",
)


def _iso(value: date | datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def _wire_datetime(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _wire_date(value: object) -> date | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return date.fromisoformat(value[:10])
    except ValueError:
        return None


def _wire_recurrence(value: object) -> RecurrenceRule | None:
    if not isinstance(value, Mapping) or not value.get("rrule"):
        return None
    return RecurrenceRule(
        rrule=str(value["rrule"]),
        last_spawned_at=_wire_datetime(value.get("last_spawned_at")),
    )


def task_to_wire_dict(task: Task, space_id: str) -> dict[str, Any]:
    """The federation wire form of a space task.

    Every field is always emitted — ``priority`` even when ``None`` — so a
    receiver can tell a current sender (key present) from a v39 one.
    """
    return {
        "id": task.id,
        "list_id": task.list_id,
        "space_id": space_id,
        "title": task.title,
        "status": task.status.value,
        "position": int(task.position),
        "created_by": task.created_by,
        "created_at": _iso(task.created_at),
        "updated_at": _iso(task.updated_at),
        "description": task.description,
        "due_date": _iso(task.due_date),
        "assignees": list(task.assignees),
        "recurrence": (
            {
                "rrule": task.recurrence.rrule,
                "last_spawned_at": _iso(task.recurrence.last_spawned_at),
            }
            if task.recurrence is not None
            else None
        ),
        "recurrence_parent_id": task.recurrence_parent_id,
        "archived_at": _iso(task.archived_at),
        "priority": task.priority.value if task.priority is not None else None,
        "labels": list(task.labels),
    }


def task_from_wire_dict(d: Mapping[str, Any], *, existing: Task | None) -> Task | None:
    """Build the task a wire payload describes, merged onto ``existing``.

    Returns ``None`` when the payload lacks an id, list id or visible
    title. See the merge rule above. ``existing`` must be the row already
    held for this id **in the payload's space** (``None`` when there is
    none) — every path that may upsert over a held row (live events, the
    host's sync chunks) passes it, or a v39 sender's lossy nulls would
    wipe our values.
    """
    task_id = str(d.get("id") or d.get("task_id") or "")
    list_id = str(d.get("list_id") or "")
    title = _capped_line(d.get("title"), MAX_TASK_TITLE_LENGTH)
    if not task_id or not list_id or not title:
        return None
    now = datetime.now(timezone.utc)
    legacy = "priority" not in d

    def present(key: str) -> bool:
        if key not in d:
            return False
        return not (legacy and existing is not None and key in _V39_LOSSY_FIELDS)

    status = existing.status if existing is not None else TaskStatus.TODO
    if "status" in d:
        try:
            status = TaskStatus(str(d.get("status") or "todo"))
        except ValueError:
            pass

    position = existing.position if existing is not None else 0
    if "position" in d:
        try:
            candidate = int(d.get("position") or 0)
        except TypeError, ValueError, OverflowError:
            pass  # ``Infinity`` / ``NaN`` / garbage — keep what we hold
        else:
            if POSITION_MIN <= candidate <= POSITION_MAX:
                position = candidate

    description = existing.description if existing is not None else None
    if "description" in d:
        raw = d.get("description")
        description = (
            sanitize_block(str(raw))[:MAX_TASK_DESCRIPTION_LENGTH] or None
            if raw is not None
            else None
        )

    assignees = existing.assignees if existing is not None else ()
    raw_assignees = d.get("assignees")
    if "assignees" in d and isinstance(raw_assignees, (list, tuple)):
        assignees = tuple(
            dict.fromkeys(a for a in raw_assignees if isinstance(a, str) and a)
        )[:MAX_TASK_ASSIGNEES]

    due_date = existing.due_date if existing is not None else None
    if present("due_date"):
        due_date = _wire_date(d.get("due_date"))

    archived_at = existing.archived_at if existing is not None else None
    if present("archived_at"):
        archived_at = _wire_datetime(d.get("archived_at"))

    recurrence = existing.recurrence if existing is not None else None
    if present("recurrence"):
        recurrence = _wire_recurrence(d.get("recurrence"))

    parent_id = existing.recurrence_parent_id if existing is not None else None
    if present("recurrence_parent_id"):
        raw_parent = d.get("recurrence_parent_id")
        parent_id = str(raw_parent) if raw_parent else None

    priority = existing.priority if existing is not None else None
    if not legacy:
        raw_priority = d.get("priority")
        if raw_priority is None:
            priority = None
        else:
            try:
                priority = TaskPriority(str(raw_priority))
            except ValueError:
                pass  # a value from a newer build — keep what we hold

    labels = existing.labels if existing is not None else ()
    raw_labels = d.get("labels")
    if not legacy and "labels" in d and isinstance(raw_labels, (list, tuple)):
        labels = normalize_labels(raw_labels)

    return Task(
        id=task_id,
        list_id=list_id,
        title=title,
        status=status,
        position=position,
        created_by=str(
            d.get("created_by") or (existing.created_by if existing else "")
        ),
        created_at=(
            _wire_datetime(d.get("created_at"))
            or (existing.created_at if existing is not None else now)
        ),
        updated_at=(
            _wire_datetime(d.get("updated_at"))
            or _wire_datetime(d.get("occurred_at"))
            or now
        ),
        description=description,
        due_date=due_date,
        assignees=assignees,
        recurrence=recurrence,
        recurrence_parent_id=parent_id,
        archived_at=archived_at,
        priority=priority,
        labels=labels,
    )


def task_list_to_wire_dict(task_list: TaskList, space_id: str) -> dict[str, Any]:
    """The federation wire form of a space task list (v_40)."""
    return {
        "id": task_list.id,
        "space_id": space_id,
        "name": task_list.name,
        "created_by": task_list.created_by,
    }


def task_list_tombstone_to_wire_dict(
    tombstone: TaskListTombstone, space_id: str
) -> dict[str, Any]:
    """The ``task_lists_deleted`` sync record of a deleted space list, and
    the replayed ``SPACE_TASK_LIST_DELETED`` payload: ``{id, space_id,
    created_by, actor_user_id}`` (the deleter, v_42 — the receiver judges
    the delete against the space's ``tasks`` level for that user)."""
    return {
        "id": tombstone.id,
        "space_id": space_id,
        "created_by": tombstone.created_by,
        "actor_user_id": tombstone.deleted_by,
    }


def task_tombstone_to_wire_dict(
    tombstone: TaskTombstone, space_id: str
) -> dict[str, Any]:
    """The ``tasks_deleted`` sync record of a deleted space task, and the
    replayed ``SPACE_TASK_DELETED`` payload: ``{id, space_id, list_id,
    created_by, actor_user_id}`` (the deleter, v_42 — the receiver judges
    the delete against the space's ``tasks`` level for that user). The
    live ``SPACE_TASK_DELETED`` shape plus ``created_by``."""
    return {
        "id": tombstone.id,
        "space_id": space_id,
        "list_id": tombstone.list_id,
        "created_by": tombstone.created_by,
        "actor_user_id": tombstone.deleted_by,
    }


def task_list_from_wire_dict(d: Mapping[str, Any]) -> TaskList | None:
    """The task list a wire payload describes; ``None`` without an id or
    a visible name (sanitised, cut to :data:`MAX_TASK_LIST_NAME_LENGTH`).
    ``created_by`` may be empty on a rename."""
    list_id = str(d.get("id") or d.get("list_id") or "")
    name = _capped_line(d.get("name"), MAX_TASK_LIST_NAME_LENGTH)
    if not list_id or not name:
        return None
    return TaskList(id=list_id, name=name, created_by=str(d.get("created_by") or ""))
