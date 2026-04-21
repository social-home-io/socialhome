"""Tests for social_home.services.task_service."""

from __future__ import annotations

import uuid
from datetime import date, datetime, timezone

import pytest

from social_home.crypto import generate_identity_keypair, derive_instance_id
from social_home.db.database import AsyncDatabase
from social_home.domain.task import Task, TaskList, TaskStatus
from social_home.repositories.task_repo import SqliteSpaceTaskRepo, SqliteTaskRepo
from social_home.services.task_service import TaskService


@pytest.fixture
async def env(tmp_dir):
    """Env with task repos and service over a real SQLite database."""
    kp = generate_identity_keypair()
    iid = derive_instance_id(kp.public_key)
    db = AsyncDatabase(tmp_dir / "test.db", batch_timeout_ms=10)
    await db.startup()
    await db.enqueue(
        "INSERT INTO instance_identity(instance_id, identity_private_key,"
        " identity_public_key, routing_secret) VALUES(?,?,?,?)",
        (iid, kp.private_key.hex(), kp.public_key.hex(), "aa" * 32),
    )

    class Env:
        pass

    e = Env()
    e.db = db
    e.iid = iid
    e.task_repo = SqliteTaskRepo(db)
    e.space_task_repo = SqliteSpaceTaskRepo(db)
    e.task_svc = TaskService(e.task_repo)
    yield e
    await db.shutdown()


async def test_household_task_crud(env):
    """Create list, add task, update status, list, delete via task service."""
    tl = await env.task_svc.create_list(name="Chores", created_by="u1")
    assert tl.name == "Chores"

    got_list = await env.task_svc.get_list(tl.id)
    assert got_list.id == tl.id

    task = await env.task_svc.create_task(
        list_id=tl.id, title="Vacuum", created_by="u1"
    )
    assert task.title == "Vacuum"

    tasks = await env.task_svc.list_tasks(tl.id)
    assert any(t.id == task.id for t in tasks)

    updated = await env.task_svc.update_task(task.id, actor_user_id="u1", status="done")
    assert updated.status == TaskStatus.DONE

    await env.task_svc.delete_task(task.id, actor_user_id="u1")
    with pytest.raises(KeyError):
        await env.task_svc.get_task(task.id)

    await env.task_svc.delete_list(tl.id)
    with pytest.raises(KeyError):
        await env.task_svc.get_list(tl.id)


async def test_space_task_crud(env):
    """Space-scoped task list and task CRUD via SqliteSpaceTaskRepo."""
    kp2 = generate_identity_keypair()
    await env.db.enqueue(
        "INSERT INTO users(username, user_id, display_name) VALUES(?,?,?)",
        ("owner2", "uid-owner2", "Owner2"),
    )
    space_id = uuid.uuid4().hex
    await env.db.enqueue(
        """INSERT INTO spaces(
            id, name, owner_instance_id, owner_username, identity_public_key,
            config_sequence, space_type, join_mode
        ) VALUES(?,?,?,?,?,0,'private','invite_only')""",
        (space_id, "TaskSpace", env.iid, "owner2", kp2.public_key.hex()),
    )

    now = datetime.now(timezone.utc)
    tl = TaskList(id=uuid.uuid4().hex, name="Space Chores", created_by="u1")
    await env.space_task_repo.save_list(space_id, tl)

    lists = await env.space_task_repo.list_lists(space_id)
    assert any(lst.id == tl.id for lst in lists)

    task = Task(
        id=uuid.uuid4().hex,
        list_id=tl.id,
        title="Clean",
        status=TaskStatus.TODO,
        position=0,
        created_by="u1",
        created_at=now,
        updated_at=now,
    )
    await env.space_task_repo.save(space_id, task)

    tasks = await env.space_task_repo.list_by_list(tl.id)
    assert any(t.id == task.id for t in tasks)

    all_tasks = await env.space_task_repo.list_by_space(space_id)
    assert any(t.id == task.id for t in all_tasks)

    await env.space_task_repo.delete(task.id)
    result = await env.space_task_repo.get(task.id)
    assert result is None

    await env.space_task_repo.delete_list(tl.id)
    result2 = await env.space_task_repo.get_list(tl.id)
    assert result2 is None


async def test_task_by_assignee_and_due_date(env):
    """list_by_assignee and list_due_on filter tasks correctly."""
    tl = await env.task_svc.create_list(name="L", created_by="u1")
    t = await env.task_svc.create_task(list_id=tl.id, title="T", created_by="u1")
    await env.task_repo.save(
        Task(
            id=t.id,
            list_id=tl.id,
            title="T",
            status=TaskStatus.TODO,
            position=0,
            created_by="u1",
            created_at=datetime.now(timezone.utc),
            updated_at=datetime.now(timezone.utc),
            assignees=("u1",),
            due_date=date.today(),
        )
    )
    by_assignee = await env.task_repo.list_by_assignee("u1")
    assert len(by_assignee) >= 1
    due = await env.task_repo.list_due_on(date.today())
    assert len(due) >= 1


async def test_task_by_status(env):
    """list_by_status filters tasks by their status."""
    tl = await env.task_svc.create_list(name="L", created_by="u1")
    t = await env.task_svc.create_task(list_id=tl.id, title="Done", created_by="u1")
    await env.task_svc.update_task(t.id, actor_user_id="u1", status="done")
    done = await env.task_repo.list_by_status(TaskStatus.DONE)
    assert len(done) >= 1
    todo = await env.task_repo.list_by_status(TaskStatus.TODO)
    assert all(t.status is TaskStatus.TODO for t in todo)


async def test_create_list_empty_name_rejected(env):
    """Empty list name raises ValueError."""
    with pytest.raises(ValueError, match="empty"):
        await env.task_svc.create_list(name="  ", created_by="u1")


async def test_delete_nonexistent_list_rejected(env):
    """Deleting a nonexistent list raises KeyError."""
    with pytest.raises(KeyError):
        await env.task_svc.delete_list("nonexistent")


async def test_create_task_empty_title_rejected(env):
    """Empty task title raises ValueError."""
    tl = await env.task_svc.create_list(name="L", created_by="u1")
    with pytest.raises(ValueError, match="empty"):
        await env.task_svc.create_task(list_id=tl.id, title="  ", created_by="u1")


async def test_create_task_nonexistent_list_rejected(env):
    """Creating a task in a nonexistent list raises KeyError."""
    with pytest.raises(KeyError):
        await env.task_svc.create_task(
            list_id="nonexistent", title="T", created_by="u1"
        )


async def test_create_task_with_due_date(env):
    """Task with due_date string is parsed correctly."""
    tl = await env.task_svc.create_list(name="L", created_by="u1")
    t = await env.task_svc.create_task(
        list_id=tl.id,
        title="T",
        created_by="u1",
        due_date="2026-05-01",
        assignees=["u1", "u2"],
    )
    assert t.due_date == date(2026, 5, 1)
    assert t.assignees == ("u1", "u2")


async def test_create_task_invalid_due_date(env):
    """Invalid due_date string raises ValueError."""
    tl = await env.task_svc.create_list(name="L", created_by="u1")
    with pytest.raises(ValueError, match="invalid due_date"):
        await env.task_svc.create_task(
            list_id=tl.id,
            title="T",
            created_by="u1",
            due_date="not-a-date",
        )


async def test_update_task_title(env):
    """update_task with title updates it."""
    tl = await env.task_svc.create_list(name="L", created_by="u1")
    t = await env.task_svc.create_task(list_id=tl.id, title="Old", created_by="u1")
    updated = await env.task_svc.update_task(t.id, actor_user_id="u1", title="New")
    assert updated.title == "New"


async def test_update_task_empty_title_rejected(env):
    """update_task with empty title raises ValueError."""
    tl = await env.task_svc.create_list(name="L", created_by="u1")
    t = await env.task_svc.create_task(list_id=tl.id, title="T", created_by="u1")
    with pytest.raises(ValueError, match="empty"):
        await env.task_svc.update_task(t.id, actor_user_id="u1", title="  ")


async def test_update_task_invalid_status_rejected(env):
    """update_task with invalid status raises ValueError."""
    tl = await env.task_svc.create_list(name="L", created_by="u1")
    t = await env.task_svc.create_task(list_id=tl.id, title="T", created_by="u1")
    with pytest.raises(ValueError, match="invalid status"):
        await env.task_svc.update_task(t.id, actor_user_id="u1", status="bogus")


async def test_update_task_due_date_and_assignees(env):
    """update_task with due_date and assignees."""
    tl = await env.task_svc.create_list(name="L", created_by="u1")
    t = await env.task_svc.create_task(list_id=tl.id, title="T", created_by="u1")
    updated = await env.task_svc.update_task(
        t.id,
        actor_user_id="u1",
        due_date="2026-06-15",
        assignees=["u1"],
        description="Details",
    )
    assert updated.due_date == date(2026, 6, 15)
    assert updated.assignees == ("u1",)
    assert updated.description == "Details"


async def test_update_task_invalid_due_date(env):
    """update_task with invalid due_date raises ValueError."""
    tl = await env.task_svc.create_list(name="L", created_by="u1")
    t = await env.task_svc.create_task(list_id=tl.id, title="T", created_by="u1")
    with pytest.raises(ValueError, match="invalid due_date"):
        await env.task_svc.update_task(t.id, actor_user_id="u1", due_date="nope")


# ─── Recurrence (§15) ──────────────────────────────────────────────────────


def test_next_occurrence_daily():
    from social_home.services.task_service import _next_occurrence

    assert _next_occurrence("FREQ=DAILY", base=date(2026, 4, 15)) == date(2026, 4, 16)
    assert _next_occurrence("FREQ=DAILY;INTERVAL=3", base=date(2026, 4, 15)) == date(
        2026, 4, 18
    )


def test_next_occurrence_weekly():
    from social_home.services.task_service import _next_occurrence

    assert _next_occurrence("FREQ=WEEKLY", base=date(2026, 4, 15)) == date(2026, 4, 22)


def test_next_occurrence_monthly_clamps_end_of_month():
    from social_home.services.task_service import _next_occurrence

    # Jan 31 → Feb 28 (non-leap) when INTERVAL=1.
    assert _next_occurrence("FREQ=MONTHLY", base=date(2025, 1, 31)) == date(2025, 2, 28)


def test_next_occurrence_yearly():
    from social_home.services.task_service import _next_occurrence

    assert _next_occurrence("FREQ=YEARLY", base=date(2026, 4, 15)) == date(2027, 4, 15)


def test_next_occurrence_unsupported_freq_returns_none():
    from social_home.services.task_service import _next_occurrence

    assert _next_occurrence("FREQ=HOURLY", base=date(2026, 4, 15)) is None
    assert _next_occurrence("", base=date(2026, 4, 15)) is None


async def test_complete_recurring_task_spawns_next_instance(env):
    """Transitioning a recurring task to DONE creates a child instance."""
    from dataclasses import replace
    from social_home.domain.task import RecurrenceRule

    tl = await env.task_svc.create_list(name="Chores", created_by="u1")
    parent = await env.task_svc.create_task(
        list_id=tl.id,
        title="Water plants",
        created_by="u1",
    )
    recurring = replace(
        parent,
        due_date=date(2026, 4, 15),
        recurrence=RecurrenceRule(rrule="FREQ=DAILY"),
    )
    await env.task_repo.save(recurring)

    await env.task_svc.update_task(
        recurring.id,
        actor_user_id="u1",
        status="done",
    )

    rows = await env.db.fetchall(
        "SELECT id, status, due_date, recurrence_parent_id FROM tasks WHERE list_id=?",
        (tl.id,),
    )
    statuses = {r["status"] for r in rows}
    assert TaskStatus.DONE.value in statuses
    assert TaskStatus.TODO.value in statuses
    assert any(r["recurrence_parent_id"] == recurring.id for r in rows)


async def test_complete_non_recurring_task_does_not_spawn(env):
    """Completing a one-off task does NOT create a new row."""
    tl = await env.task_svc.create_list(name="One", created_by="u1")
    t = await env.task_svc.create_task(list_id=tl.id, title="Once", created_by="u1")
    await env.task_svc.update_task(t.id, actor_user_id="u1", status="done")
    rows = await env.db.fetchall(
        "SELECT id FROM tasks WHERE list_id=?",
        (tl.id,),
    )
    assert len(rows) == 1


async def test_space_task_service_list(env):
    """SpaceTaskService.list_lists and list_tasks work."""
    from social_home.services.task_service import SpaceTaskService

    svc = SpaceTaskService(env.space_task_repo)
    # Need a space
    kp2 = generate_identity_keypair()
    import uuid as _uuid

    sid = _uuid.uuid4().hex
    await env.db.enqueue(
        "INSERT INTO users(username, user_id, display_name) VALUES(?,?,?)",
        ("sowner", "uid-so", "SO"),
    )
    await env.db.enqueue(
        """INSERT INTO spaces(id, name, owner_instance_id, owner_username,
           identity_public_key, config_sequence, space_type, join_mode)
           VALUES(?,?,?,?,?,0,'private','invite_only')""",
        (sid, "SpaceT", env.iid, "sowner", kp2.public_key.hex()),
    )
    lists = await svc.list_lists(sid)
    assert isinstance(lists, list)
    tasks = await svc.list_tasks(sid)
    assert isinstance(tasks, list)
