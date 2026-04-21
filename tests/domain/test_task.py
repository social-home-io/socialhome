"""Tests for social_home.domain.task."""

from __future__ import annotations

from datetime import datetime, timezone


from social_home.domain.task import RecurrenceRule, Task, TaskStatus, TaskUpdate


def test_task_lifecycle():
    """Task moves through todo → in_progress → done → todo via status helpers."""
    now = datetime.now(timezone.utc)
    t = Task(
        id="t1",
        list_id="l1",
        title="Buy",
        status=TaskStatus.TODO,
        position=0,
        created_by="u1",
        created_at=now,
        updated_at=now,
    )
    assert t.start().status is TaskStatus.IN_PROGRESS
    assert t.complete().status is TaskStatus.DONE
    assert t.complete().reopen().status is TaskStatus.TODO


def test_task_recurrence():
    """mark_spawned sets last_spawned_at on a RecurrenceRule."""
    r = RecurrenceRule(rrule="FREQ=DAILY")
    r2 = r.mark_spawned()
    assert r2.last_spawned_at is not None


def test_task_update_fields():
    """TaskUpdate carries optional fields for a partial task edit."""
    tu = TaskUpdate(title="New", status=TaskStatus.DONE)
    assert tu.title == "New"
    assert tu.status is TaskStatus.DONE


def test_task_with_assignees_returns_new_instance():
    """with_assignees returns a new Task without mutating the original."""
    now = datetime.now(timezone.utc)
    t = Task(
        id="t",
        list_id="l",
        title="T",
        status=TaskStatus.TODO,
        position=0,
        created_by="u",
        created_at=now,
        updated_at=now,
    )
    t2 = t.with_assignees(("a", "b"))
    assert t2.assignees == ("a", "b")
    assert t.assignees == ()


def test_recurrence_mark_spawned_does_not_mutate_original():
    """mark_spawned returns a new rule, leaving the caller unchanged."""
    r = RecurrenceRule(rrule="FREQ=DAILY")
    r2 = r.mark_spawned()
    assert r2.last_spawned_at is not None
    assert r.last_spawned_at is None
