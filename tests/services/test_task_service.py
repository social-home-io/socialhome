"""Tests for socialhome.services.task_service."""

from __future__ import annotations

import uuid
from datetime import date, datetime, timezone

import pytest

from socialhome.crypto import generate_identity_keypair, derive_instance_id
from socialhome.db.database import AsyncDatabase
from socialhome.domain.task import Task, TaskList, TaskPriority, TaskStatus
from socialhome.federation.owner_bound_id import (
    SPACE_TASK_KIND,
    SPACE_TASK_LIST_KIND,
    OwnerBinding,
    check_owner_bound_id,
)
from socialhome.domain.events import TaskAssigned, TaskCompleted, TaskUpdated
from socialhome.domain.space import (
    ContentAction,
    ContentQueuedForReview,
    ModerationStatus,
    ModerationTargetGoneError,
    SpacePermissionError,
)
from socialhome.domain.user import User
from socialhome.infrastructure.event_bus import EventBus
from socialhome.repositories.space_remote_member_repo import (
    SqliteSpaceRemoteMemberRepo,
)
from socialhome.repositories.space_repo import SqliteSpaceRepo
from socialhome.repositories.task_repo import SqliteSpaceTaskRepo, SqliteTaskRepo
from socialhome.repositories.user_repo import SqliteUserRepo
from socialhome.services.space_moderation_service import SpaceModerationService
from socialhome.services.task_service import (
    SpaceTaskService,
    TaskModerationHandler,
    TaskService,
)


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
    e.space_repo = SqliteSpaceRepo(db)
    e.remote_member_repo = SqliteSpaceRemoteMemberRepo(db)
    e.user_repo = SqliteUserRepo(db)
    e.task_svc = TaskService(e.task_repo, user_repo=e.user_repo)
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
    await env.space_task_repo.save_list(tl, space_id=space_id)

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
    await env.space_task_repo.save(task, space_id=space_id)

    tasks = await env.space_task_repo.list_by_list(tl.id, space_id=space_id)
    assert any(t.id == task.id for t in tasks)

    all_tasks = await env.space_task_repo.list_by_space(space_id)
    assert any(t.id == task.id for t in all_tasks)

    await env.space_task_repo.delete(task.id, space_id=space_id)
    result = await env.space_task_repo.get(task.id)
    assert result is None

    await env.space_task_repo.delete_list(tl.id, space_id=space_id)
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


async def _seed_users(env, *user_ids: str, is_admin: bool = False) -> None:
    for uid in user_ids:
        await env.user_repo.save(
            User(user_id=uid, username=uid, display_name=uid, is_admin=is_admin)
        )


async def test_create_task_with_due_date(env):
    """Task with due_date string is parsed correctly."""
    await _seed_users(env, "u1", "u2")
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
    await _seed_users(env, "u1")
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


async def test_update_task_rejected_for_non_creator_non_admin(env):
    """Non-creator non-admin actors cannot update someone else's task."""
    await env.user_repo.save(
        User(user_id="u2", username="u2", display_name="U2", is_admin=False)
    )
    tl = await env.task_svc.create_list(name="L", created_by="u1")
    t = await env.task_svc.create_task(list_id=tl.id, title="T", created_by="u1")
    with pytest.raises(PermissionError):
        await env.task_svc.update_task(t.id, actor_user_id="u2", title="Hijack")


async def test_update_task_allowed_for_admin_non_creator(env):
    """Admins can update tasks they didn't create."""
    await env.user_repo.save(
        User(user_id="admin1", username="admin1", display_name="Admin", is_admin=True)
    )
    tl = await env.task_svc.create_list(name="L", created_by="u1")
    t = await env.task_svc.create_task(list_id=tl.id, title="T", created_by="u1")
    updated = await env.task_svc.update_task(
        t.id, actor_user_id="admin1", title="Edited by admin"
    )
    assert updated.title == "Edited by admin"


async def test_update_task_unknown_actor_rejected(env):
    """An actor_user_id that doesn't resolve to a stored user is rejected."""
    tl = await env.task_svc.create_list(name="L", created_by="u1")
    t = await env.task_svc.create_task(list_id=tl.id, title="T", created_by="u1")
    with pytest.raises(PermissionError):
        await env.task_svc.update_task(t.id, actor_user_id="ghost", title="Edit")


async def test_archive_task_round_trip(env):
    """archive_task sets archived_at; unarchive_task clears it."""
    tl = await env.task_svc.create_list(name="L", created_by="u1")
    t = await env.task_svc.create_task(list_id=tl.id, title="T", created_by="u1")
    archived = await env.task_svc.archive_task(t.id, actor_user_id="u1")
    assert archived.archived_at is not None
    fetched = await env.task_svc.get_task(t.id)
    assert fetched.archived_at is not None

    unarchived = await env.task_svc.unarchive_task(t.id, actor_user_id="u1")
    assert unarchived.archived_at is None
    fetched_again = await env.task_svc.get_task(t.id)
    assert fetched_again.archived_at is None


async def test_archive_task_rejected_for_non_creator_non_admin(env):
    """Non-creator non-admin actors cannot archive someone else's task."""
    await env.user_repo.save(
        User(user_id="u2", username="u2", display_name="U2", is_admin=False)
    )
    tl = await env.task_svc.create_list(name="L", created_by="u1")
    t = await env.task_svc.create_task(list_id=tl.id, title="T", created_by="u1")
    with pytest.raises(PermissionError):
        await env.task_svc.archive_task(t.id, actor_user_id="u2")


# ─── Recurrence (§15) ──────────────────────────────────────────────────────


def test_next_occurrence_daily():
    from socialhome.services.task_service import _next_occurrence

    assert _next_occurrence("FREQ=DAILY", base=date(2026, 4, 15)) == date(2026, 4, 16)
    assert _next_occurrence("FREQ=DAILY;INTERVAL=3", base=date(2026, 4, 15)) == date(
        2026, 4, 18
    )


def test_next_occurrence_weekly():
    from socialhome.services.task_service import _next_occurrence

    assert _next_occurrence("FREQ=WEEKLY", base=date(2026, 4, 15)) == date(2026, 4, 22)


def test_next_occurrence_monthly_clamps_end_of_month():
    from socialhome.services.task_service import _next_occurrence

    # Jan 31 → Feb 28 (non-leap) when INTERVAL=1.
    assert _next_occurrence("FREQ=MONTHLY", base=date(2025, 1, 31)) == date(2025, 2, 28)


def test_next_occurrence_yearly():
    from socialhome.services.task_service import _next_occurrence

    assert _next_occurrence("FREQ=YEARLY", base=date(2026, 4, 15)) == date(2027, 4, 15)


def test_next_occurrence_unsupported_freq_returns_none():
    from socialhome.services.task_service import _next_occurrence

    assert _next_occurrence("FREQ=HOURLY", base=date(2026, 4, 15)) is None
    assert _next_occurrence("", base=date(2026, 4, 15)) is None


async def test_complete_recurring_task_spawns_next_instance(env):
    """Transitioning a recurring task to DONE creates a child instance."""
    from dataclasses import replace
    from socialhome.domain.task import RecurrenceRule

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
    from socialhome.services.task_service import SpaceTaskService

    svc = SpaceTaskService(env.space_task_repo, space_repo=env.space_repo)
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


async def test_a_space_task_id_commits_to_its_creator(env):
    """v_36: a space task federates, so its id is owner-bound to its
    creator in its space — no other household can announce it first."""
    svc = SpaceTaskService(env.space_task_repo, space_repo=env.space_repo)
    await env.db.enqueue(
        "INSERT INTO users(username, user_id, display_name) VALUES(?,?,?)",
        ("sowner", "uid-so", "SO"),
    )
    await env.db.enqueue(
        """INSERT INTO spaces(id, name, owner_instance_id, owner_username,
           identity_public_key, config_sequence, space_type, join_mode)
           VALUES(?,?,?,?,?,0,'private','invite_only')""",
        (
            "sp-t",
            "SpaceT",
            env.iid,
            "sowner",
            generate_identity_keypair().public_key.hex(),
        ),
    )
    lst = await svc.create_list(space_id="sp-t", name="L", created_by="uid-so")
    task = await svc.create_task(
        space_id="sp-t", list_id=lst.id, title="T", created_by="uid-so"
    )
    for owner, expected in (
        ("uid-so", OwnerBinding.VALID),
        ("uid-x", OwnerBinding.MISMATCH),
    ):
        assert (
            check_owner_bound_id(
                SPACE_TASK_KIND, task.id, space_id="sp-t", owner_user_id=owner
            )
            is expected
        )


# ─── Cross-space scope (IDOR) ────────────────────────────────────────────


async def _two_spaces_with_a_task(env):
    """Seed spaces ``sp-a`` and ``sp-b``; return ``(svc, list_b, task_b)``.

    The caller acts on ``sp-a``'s path while naming ``sp-b``'s rows.
    """
    svc = SpaceTaskService(env.space_task_repo, space_repo=env.space_repo)
    await env.db.enqueue(
        "INSERT INTO users(username, user_id, display_name) VALUES(?,?,?)",
        ("sowner", "uid-so", "SO"),
    )
    for sid in ("sp-a", "sp-b"):
        await env.db.enqueue(
            """INSERT INTO spaces(id, name, owner_instance_id, owner_username,
               identity_public_key, config_sequence, space_type, join_mode)
               VALUES(?,?,?,?,?,0,'private','invite_only')""",
            (
                sid,
                sid,
                env.iid,
                "sowner",
                generate_identity_keypair().public_key.hex(),
            ),
        )
    lst_b = await svc.create_list(space_id="sp-b", name="B", created_by="uid-so")
    task_b = await svc.create_task(
        space_id="sp-b", list_id=lst_b.id, title="TB", created_by="uid-so"
    )
    return svc, lst_b, task_b


async def test_a_space_task_from_another_space_is_not_found(env):
    """Every task op scoped to ``sp-a`` refuses ``sp-b``'s task with
    KeyError (→ 404) and leaves it untouched."""
    svc, _, task_b = await _two_spaces_with_a_task(env)
    with pytest.raises(KeyError):
        await svc.update_task(
            task_b.id, space_id="sp-a", actor_user_id="uid-so", title="pwned"
        )
    with pytest.raises(KeyError):
        await svc.archive_task(task_b.id, space_id="sp-a", actor_user_id="uid-so")
    with pytest.raises(KeyError):
        await svc.unarchive_task(task_b.id, space_id="sp-a", actor_user_id="uid-so")
    with pytest.raises(KeyError):
        await svc.delete_task(task_b.id, space_id="sp-a", actor_user_id="u-test")
    got = await env.space_task_repo.get(task_b.id)
    assert got is not None
    space_id, task = got
    assert space_id == "sp-b"
    assert task.title == "TB"
    assert task.archived_at is None


async def test_a_space_task_list_from_another_space_is_not_found(env):
    """List ops scoped to ``sp-a`` refuse ``sp-b``'s list."""
    svc, lst_b, task_b = await _two_spaces_with_a_task(env)
    with pytest.raises(KeyError):
        await svc.rename_list(
            lst_b.id, space_id="sp-a", name="pwned", actor_user_id="u-test"
        )
    with pytest.raises(KeyError):
        await svc.delete_list(lst_b.id, space_id="sp-a", actor_user_id="u-test")
    with pytest.raises(KeyError):
        await svc.list_tasks_by_list(lst_b.id, space_id="sp-a")
    got = await env.space_task_repo.get_list(lst_b.id)
    assert got is not None
    assert got[1].name == "B"
    assert await env.space_task_repo.get(task_b.id) is not None
    # Defence in depth: the repo read itself is space-filtered.
    assert await env.space_task_repo.list_by_list(lst_b.id, space_id="sp-a") == []
    assert len(await env.space_task_repo.list_by_list(lst_b.id, space_id="sp-b")) == 1


async def test_space_task_ops_in_their_own_space_still_work(env):
    """The same calls scoped to the row's own space succeed."""
    svc, lst_b, task_b = await _two_spaces_with_a_task(env)
    rows = await svc.list_tasks_by_list(lst_b.id, space_id="sp-b")
    assert [t.id for t in rows] == [task_b.id]
    renamed = await svc.rename_list(
        lst_b.id, space_id="sp-b", name="B2", actor_user_id="u-test"
    )
    assert renamed.name == "B2"
    updated = await svc.update_task(
        task_b.id, space_id="sp-b", actor_user_id="uid-so", title="TB2"
    )
    assert updated.title == "TB2"
    archived = await svc.archive_task(
        task_b.id, space_id="sp-b", actor_user_id="uid-so"
    )
    assert archived.archived_at is not None
    restored = await svc.unarchive_task(
        task_b.id, space_id="sp-b", actor_user_id="uid-so"
    )
    assert restored.archived_at is None
    await svc.delete_task(task_b.id, space_id="sp-b", actor_user_id="u-test")
    assert await env.space_task_repo.get(task_b.id) is None
    await svc.delete_list(lst_b.id, space_id="sp-b", actor_user_id="u-test")
    assert await env.space_task_repo.get_list(lst_b.id) is None


async def test_list_tasks_by_list_of_a_missing_list_is_not_found(env):
    svc = SpaceTaskService(env.space_task_repo, space_repo=env.space_repo)
    with pytest.raises(KeyError):
        await svc.list_tasks_by_list("missing", space_id="sp-a")


# ─── Assignees: membership + shape ──────────────────────────────────────


async def _space_with_members(env, *, archived: bool = False):
    """``sp-m``: owner ``uid-so``, member ``uid-m``, subscriber ``uid-sub``;
    ``uid-out`` exists locally but is not a member. Returns ``(svc, bus
    events, list)``."""
    bus = EventBus()
    events: list = []

    async def _capture(ev):
        events.append(ev)

    bus.subscribe(TaskAssigned, _capture)
    bus.subscribe(TaskCompleted, _capture)
    svc = SpaceTaskService(
        env.space_task_repo,
        bus,
        space_repo=env.space_repo,
        remote_member_repo=env.remote_member_repo,
    )
    await env.db.enqueue(
        "INSERT INTO users(username, user_id, display_name) VALUES(?,?,?)",
        ("sowner", "uid-so", "SO"),
    )
    await env.db.enqueue(
        """INSERT INTO spaces(id, name, owner_instance_id, owner_username,
           identity_public_key, config_sequence, space_type, join_mode, archived)
           VALUES(?,?,?,?,?,0,'private','invite_only',?)""",
        (
            "sp-m",
            "M",
            env.iid,
            "sowner",
            generate_identity_keypair().public_key.hex(),
            1 if archived else 0,
        ),
    )
    for uid, role in (
        ("uid-so", "owner"),
        ("uid-m", "member"),
        ("uid-sub", "subscriber"),
    ):
        await env.db.enqueue(
            "INSERT INTO space_members(space_id, user_id, role) VALUES(?,?,?)",
            ("sp-m", uid, role),
        )
    lst = await svc.create_list(space_id="sp-m", name="L", created_by="uid-so")
    return svc, events, lst


async def test_space_task_rejects_a_non_member_assignee(env):
    svc, events, lst = await _space_with_members(env)
    with pytest.raises(ValueError):
        await svc.create_task(
            space_id="sp-m",
            list_id=lst.id,
            title="SECRET",
            created_by="uid-so",
            assignees=["uid-m", "uid-out"],
        )
    assert await env.space_task_repo.list_by_list(lst.id, space_id="sp-m") == []
    assert events == []


async def test_space_task_accepts_members_and_remote_members(env):
    svc, events, lst = await _space_with_members(env)
    await env.remote_member_repo.add(
        space_id="sp-m",
        instance_id="peer-1",
        user_id="uid-remote",
        user_pk=None,
        display_name="R",
    )
    task = await svc.create_task(
        space_id="sp-m",
        list_id=lst.id,
        title="T",
        created_by="uid-so",
        assignees=["uid-m", "uid-remote"],
    )
    assert task.assignees == ("uid-m", "uid-remote")
    assigned = [e for e in events if isinstance(e, TaskAssigned)]
    assert {e.assigned_to for e in assigned} == {"uid-m", "uid-remote"}
    assert all(e.space_id == "sp-m" for e in assigned)


async def test_space_task_update_validates_only_added_assignees(env):
    svc, events, lst = await _space_with_members(env)
    task = await svc.create_task(
        space_id="sp-m",
        list_id=lst.id,
        title="T",
        created_by="uid-so",
        assignees=["uid-m"],
    )
    # uid-m leaves: a stale assignee must not block unrelated edits.
    await env.db.enqueue(
        "DELETE FROM space_members WHERE space_id='sp-m' AND user_id='uid-m'"
    )
    events.clear()
    kept = await svc.update_task(
        task.id,
        space_id="sp-m",
        actor_user_id="uid-so",
        title="T2",
        assignees=["uid-m"],
    )
    assert kept.title == "T2"
    assert events == []
    with pytest.raises(ValueError):
        await svc.update_task(
            task.id,
            space_id="sp-m",
            actor_user_id="uid-so",
            assignees=["uid-m", "uid-out"],
        )
    got = await env.space_task_repo.get(task.id)
    assert got[1].assignees == ("uid-m",)
    assert events == []


async def test_space_task_completion_carries_its_space(env):
    svc, events, lst = await _space_with_members(env)
    task = await svc.create_task(
        space_id="sp-m", list_id=lst.id, title="T", created_by="uid-so"
    )
    await svc.update_task(
        task.id, space_id="sp-m", actor_user_id="uid-so", status="done"
    )
    done = [e for e in events if isinstance(e, TaskCompleted)]
    assert len(done) == 1 and done[0].space_id == "sp-m"


@pytest.mark.parametrize(
    "bad",
    [
        "uid-m",  # a string must never be split into characters
        ["uid-m", ""],
        ["uid-m", "   "],
        ["uid-m", 7],
        [f"u{i}" for i in range(11)],
        {"uid-m": True},
    ],
)
async def test_assignees_must_be_a_short_list_of_ids(env, bad):
    svc, _, lst = await _space_with_members(env)
    with pytest.raises(ValueError):
        await svc.create_task(
            space_id="sp-m",
            list_id=lst.id,
            title="T",
            created_by="uid-so",
            assignees=bad,
        )
    household = await env.task_svc.create_list(name="H", created_by="u1")
    with pytest.raises(ValueError):
        await env.task_svc.create_task(
            list_id=household.id, title="T", created_by="u1", assignees=bad
        )
    t = await env.task_svc.create_task(list_id=household.id, title="T", created_by="u1")
    with pytest.raises(ValueError):
        await env.task_svc.update_task(t.id, actor_user_id="u1", assignees=bad)


async def test_household_task_accepts_a_list_of_ids(env):
    await _seed_users(env, *(f"u{i}" for i in range(10)))
    household = await env.task_svc.create_list(name="H", created_by="u1")
    t = await env.task_svc.create_task(
        list_id=household.id,
        title="T",
        created_by="u1",
        assignees=[f"u{i}" for i in range(10)],
    )
    assert len(t.assignees) == 10


# ─── Writer gate: subscribers + archived spaces ─────────────────────────


async def test_require_writer_admits_a_member(env):
    svc, _, _ = await _space_with_members(env)
    await svc.require_writer("sp-m", "uid-m")


async def test_require_writer_refuses_a_subscriber(env):
    svc, _, _ = await _space_with_members(env)
    with pytest.raises(SpacePermissionError):
        await svc.require_writer("sp-m", "uid-sub")


async def test_require_writer_refuses_a_non_member(env):
    svc, _, _ = await _space_with_members(env)
    with pytest.raises(SpacePermissionError):
        await svc.require_writer("sp-m", "uid-out")


async def test_require_writer_refuses_an_archived_space(env):
    svc, _, _ = await _space_with_members(env, archived=True)
    with pytest.raises(SpacePermissionError):
        await svc.require_writer("sp-m", "uid-so")


async def test_require_writer_on_a_missing_space_is_not_found(env):
    svc = SpaceTaskService(env.space_task_repo, space_repo=env.space_repo)
    with pytest.raises(KeyError):
        await svc.require_writer("nope", "uid-so")


# ─── Priority, labels, quick-add status, next position (v_40) ───────────


async def test_create_task_with_status_priority_labels(env):
    tl = await env.task_svc.create_list(name="L", created_by="u1")
    t = await env.task_svc.create_task(
        list_id=tl.id,
        title="T",
        created_by="u1",
        status="in_progress",
        priority="urgent",
        labels=[" Car ", "car", "Bills"],
    )
    assert t.status is TaskStatus.IN_PROGRESS
    assert t.priority is TaskPriority.URGENT
    assert t.labels == ("Car", "Bills")
    stored = await env.task_svc.get_task(t.id)
    assert stored.priority is TaskPriority.URGENT
    assert stored.labels == ("Car", "Bills")


@pytest.mark.parametrize(
    "kwargs",
    [
        {"priority": "critical"},
        {"priority": 3},
        {"status": "blocked"},
        {"labels": "not-a-list"},
        {"labels": ["ok", 5]},
        {"labels": [f"l{i}" for i in range(11)]},
        {"labels": ["x" * 33]},
    ],
)
async def test_create_task_rejects_bad_priority_status_labels(env, kwargs):
    tl = await env.task_svc.create_list(name="L", created_by="u1")
    with pytest.raises(ValueError):
        await env.task_svc.create_task(
            list_id=tl.id, title="T", created_by="u1", **kwargs
        )


async def test_new_tasks_append_at_the_bottom(env):
    tl = await env.task_svc.create_list(name="L", created_by="u1")
    a = await env.task_svc.create_task(list_id=tl.id, title="A", created_by="u1")
    b = await env.task_svc.create_task(list_id=tl.id, title="B", created_by="u1")
    c = await env.task_svc.create_task(
        list_id=tl.id, title="C", created_by="u1", status="done"
    )
    assert (a.position, b.position, c.position) == (0, 1, 2)


async def test_update_task_null_clears_and_omitted_keeps(env):
    tl = await env.task_svc.create_list(name="L", created_by="u1")
    t = await env.task_svc.create_task(
        list_id=tl.id,
        title="T",
        created_by="u1",
        description="desc",
        due_date="2026-10-03",
        priority="high",
        labels=["a"],
    )
    # Omitted fields are left alone.
    same = await env.task_svc.update_task(t.id, actor_user_id="u1", title="T2")
    assert same.description == "desc"
    assert same.due_date == date(2026, 10, 3)
    assert same.priority is TaskPriority.HIGH
    assert same.labels == ("a",)
    # Explicit None clears.
    cleared = await env.task_svc.update_task(
        t.id,
        actor_user_id="u1",
        description=None,
        due_date=None,
        priority=None,
        labels=None,
    )
    assert cleared.description is None
    assert cleared.due_date is None
    assert cleared.priority is None
    assert cleared.labels == ()
    stored = await env.task_svc.get_task(t.id)
    assert stored.due_date is None and stored.description is None
    # None on a non-nullable field is still "no change".
    kept = await env.task_svc.update_task(
        t.id, actor_user_id="u1", title=None, status=None
    )
    assert kept.title == "T2" and kept.status is TaskStatus.TODO


async def test_update_task_rejects_bad_position(env):
    tl = await env.task_svc.create_list(name="L", created_by="u1")
    t = await env.task_svc.create_task(list_id=tl.id, title="T", created_by="u1")
    with pytest.raises(ValueError, match="invalid position"):
        await env.task_svc.update_task(t.id, actor_user_id="u1", position="top")


# ─── Household edit rights: creator, assignee or admin ──────────────────


async def test_update_task_allowed_for_an_assignee(env):
    await _seed_users(env, "u1", "u2")
    tl = await env.task_svc.create_list(name="L", created_by="u1")
    t = await env.task_svc.create_task(
        list_id=tl.id, title="T", created_by="u1", assignees=["u2"]
    )
    updated = await env.task_svc.update_task(t.id, actor_user_id="u2", status="done")
    assert updated.status is TaskStatus.DONE
    archived = await env.task_svc.archive_task(t.id, actor_user_id="u2")
    assert archived.archived_at is not None


async def test_update_task_refused_for_a_non_assignee(env):
    await _seed_users(env, "u1", "u2", "u3")
    tl = await env.task_svc.create_list(name="L", created_by="u1")
    t = await env.task_svc.create_task(
        list_id=tl.id, title="T", created_by="u1", assignees=["u2"]
    )
    with pytest.raises(PermissionError):
        await env.task_svc.update_task(t.id, actor_user_id="u3", title="x")


# ─── Household assignees must be active local users ─────────────────────


async def test_create_task_rejects_unknown_assignee(env):
    await _seed_users(env, "u1")
    tl = await env.task_svc.create_list(name="L", created_by="u1")
    with pytest.raises(ValueError, match="active member"):
        await env.task_svc.create_task(
            list_id=tl.id, title="T", created_by="u1", assignees=["ghost"]
        )


async def test_create_task_rejects_inactive_assignee(env):
    await _seed_users(env, "u1")
    await env.user_repo.save(
        User(user_id="gone", username="gone", display_name="G", state="inactive")
    )
    tl = await env.task_svc.create_list(name="L", created_by="u1")
    with pytest.raises(ValueError, match="active member"):
        await env.task_svc.create_task(
            list_id=tl.id, title="T", created_by="u1", assignees=["gone"]
        )


async def test_update_task_checks_only_added_assignees(env):
    """An assignee who has since been deactivated must not block an
    unrelated edit; a newly added unknown id is refused."""
    await _seed_users(env, "u1", "u2")
    tl = await env.task_svc.create_list(name="L", created_by="u1")
    t = await env.task_svc.create_task(
        list_id=tl.id, title="T", created_by="u1", assignees=["u2"]
    )
    await env.user_repo.save(
        User(user_id="u2", username="u2", display_name="u2", state="inactive")
    )
    kept = await env.task_svc.update_task(
        t.id, actor_user_id="u1", assignees=["u2", "u1"]
    )
    assert kept.assignees == ("u2", "u1")
    with pytest.raises(ValueError, match="active member"):
        await env.task_svc.update_task(
            t.id, actor_user_id="u1", assignees=["u2", "ghost"]
        )


# ─── Household reorder: same edit rights per moved task ─────────────────


async def test_reorder_refused_when_the_dragged_task_is_not_editable(env):
    await _seed_users(env, "u1", "u2")
    tl = await env.task_svc.create_list(name="L", created_by="u1")
    a = await env.task_svc.create_task(list_id=tl.id, title="A", created_by="u1")
    b = await env.task_svc.create_task(list_id=tl.id, title="B", created_by="u2")
    # u2 drags u1's card → refused, nothing moves.
    with pytest.raises(PermissionError):
        await env.task_svc.reorder_tasks(
            tl.id, ordered_ids=[b.id, a.id], moved_id=a.id, actor_user_id="u2"
        )
    rows = {t.id: t.position for t in await env.task_svc.list_tasks(tl.id)}
    assert rows == {a.id: 0, b.id: 1}


async def test_reorder_own_card_among_others_needs_no_rights_on_them(env):
    """Dragging your own card shifts the neighbours' positions as a side
    effect; that needs no rights on them."""
    await _seed_users(env, "u1", "u2")
    tl = await env.task_svc.create_list(name="L", created_by="u1")
    a = await env.task_svc.create_task(list_id=tl.id, title="A", created_by="u1")
    b = await env.task_svc.create_task(list_id=tl.id, title="B", created_by="u1")
    mine = await env.task_svc.create_task(list_id=tl.id, title="M", created_by="u2")
    moved = await env.task_svc.reorder_tasks(
        tl.id,
        ordered_ids=[mine.id, a.id, b.id],
        moved_id=mine.id,
        actor_user_id="u2",
    )
    assert {t.id for t in moved} == {mine.id, a.id, b.id}
    rows = {t.id: t.position for t in await env.task_svc.list_tasks(tl.id)}
    assert rows == {mine.id: 0, a.id: 1, b.id: 2}


@pytest.mark.parametrize("moved_id", ["", "not-in-order"])
async def test_reorder_needs_a_moved_id_from_the_order(env, moved_id):
    tl = await env.task_svc.create_list(name="L", created_by="u1")
    a = await env.task_svc.create_task(list_id=tl.id, title="A", created_by="u1")
    with pytest.raises(ValueError, match="moved_id"):
        await env.task_svc.reorder_tasks(
            tl.id, ordered_ids=[a.id], moved_id=moved_id, actor_user_id="u1"
        )


async def test_reorder_moved_id_from_another_list_is_404(env):
    l1 = await env.task_svc.create_list(name="L1", created_by="u1")
    l2 = await env.task_svc.create_list(name="L2", created_by="u1")
    other = await env.task_svc.create_task(list_id=l2.id, title="O", created_by="u1")
    with pytest.raises(KeyError):
        await env.task_svc.reorder_tasks(
            l1.id, ordered_ids=[other.id], moved_id=other.id, actor_user_id="u1"
        )


async def test_delete_task_needs_edit_rights(env):
    await _seed_users(env, "u1", "u2", "u3")
    tl = await env.task_svc.create_list(name="L", created_by="u1")
    t = await env.task_svc.create_task(
        list_id=tl.id, title="T", created_by="u1", assignees=["u2"]
    )
    with pytest.raises(PermissionError):
        await env.task_svc.delete_task(t.id, actor_user_id="u3")
    await env.task_svc.delete_task(t.id, actor_user_id="u2")  # an assignee
    with pytest.raises(KeyError):
        await env.task_svc.get_task(t.id)


async def test_reorder_allowed_for_admin(env):
    await _seed_users(env, "u1", "u2")
    await _seed_users(env, "boss", is_admin=True)
    tl = await env.task_svc.create_list(name="L", created_by="u1")
    a = await env.task_svc.create_task(list_id=tl.id, title="A", created_by="u1")
    b = await env.task_svc.create_task(list_id=tl.id, title="B", created_by="u2")
    moved = await env.task_svc.reorder_tasks(
        tl.id, ordered_ids=[b.id, a.id], moved_id=a.id, actor_user_id="boss"
    )
    assert {t.id for t in moved} == {a.id, b.id}


# ─── Space: create fields, UNSET, reorder ───────────────────────────────


async def test_space_create_task_with_status_priority_labels(env):
    svc, _, lst = await _space_with_members(env)
    a = await svc.create_task(
        space_id="sp-m", list_id=lst.id, title="A", created_by="uid-m"
    )
    b = await svc.create_task(
        space_id="sp-m",
        list_id=lst.id,
        title="B",
        created_by="uid-m",
        status="done",
        priority="low",
        labels=["Trip"],
    )
    assert (a.position, b.position) == (0, 1)
    assert b.status is TaskStatus.DONE
    held = await env.space_task_repo.get(b.id)
    assert held is not None
    assert held[1].priority is TaskPriority.LOW
    assert held[1].labels == ("Trip",)


async def test_space_update_task_null_clears(env):
    svc, _, lst = await _space_with_members(env)
    t = await svc.create_task(
        space_id="sp-m",
        list_id=lst.id,
        title="A",
        created_by="uid-m",
        description="d",
        due_date="2026-10-03",
        priority="high",
    )
    kept = await svc.update_task(
        t.id, space_id="sp-m", actor_user_id="uid-m", title="A2"
    )
    assert kept.due_date == date(2026, 10, 3)
    assert kept.priority is TaskPriority.HIGH
    cleared = await svc.update_task(
        t.id,
        space_id="sp-m",
        actor_user_id="uid-m",
        description=None,
        due_date=None,
        priority=None,
    )
    assert cleared.description is None
    assert cleared.due_date is None
    assert cleared.priority is None


async def test_space_reorder_tasks(env):
    svc, _, lst = await _space_with_members(env)
    bus_events: list = []

    async def _cap(ev):
        bus_events.append(ev)

    svc._bus.subscribe(TaskUpdated, _cap)
    a = await svc.create_task(
        space_id="sp-m", list_id=lst.id, title="A", created_by="uid-m"
    )
    b = await svc.create_task(
        space_id="sp-m", list_id=lst.id, title="B", created_by="uid-so"
    )
    moved = await svc.reorder_tasks(
        "sp-m",
        lst.id,
        ordered_ids=[b.id, a.id, "unknown"],
        moved_id=b.id,
        actor_user_id="uid-so",
    )
    assert {t.id for t in moved} == {a.id, b.id}
    rows = {t.id: t.position for t in await svc.list_tasks("sp-m")}
    assert rows == {b.id: 0, a.id: 1}
    assert all(e.space_id == "sp-m" for e in bus_events)
    assert len(bus_events) == 2


async def test_space_reorder_refuses_another_spaces_list(env):
    svc, _, lst = await _space_with_members(env)
    with pytest.raises(KeyError):
        await svc.reorder_tasks(
            "sp-other", lst.id, ordered_ids=["x"], moved_id="x", actor_user_id="uid-so"
        )


async def test_space_reorder_skips_a_task_of_another_space(env):
    svc, _, lst = await _space_with_members(env)
    await env.db.enqueue(
        """INSERT INTO spaces(id, name, owner_instance_id, owner_username,
           identity_public_key, config_sequence, space_type, join_mode)
           VALUES(?,?,?,?,?,0,'private','invite_only')""",
        ("sp-x", "X", env.iid, "sowner", generate_identity_keypair().public_key.hex()),
    )
    await env.db.enqueue(
        "INSERT INTO space_members(space_id, user_id, role) VALUES(?,?,?)",
        ("sp-x", "uid-m", "member"),
    )
    other_list = await svc.create_list(space_id="sp-x", name="X", created_by="uid-m")
    foreign = await svc.create_task(
        space_id="sp-x", list_id=other_list.id, title="F", created_by="uid-m"
    )
    mine = await svc.create_task(
        space_id="sp-m", list_id=lst.id, title="M", created_by="uid-m"
    )
    moved = await svc.reorder_tasks(
        "sp-m",
        lst.id,
        ordered_ids=[foreign.id, mine.id],
        moved_id=mine.id,
        actor_user_id="uid-so",
    )
    assert [t.id for t in moved] == [mine.id]
    held = await env.space_task_repo.get(foreign.id)
    assert held is not None and held[0] == "sp-x" and held[1].position == 0


async def test_space_reorder_moved_id_of_another_space_is_404(env):
    svc, _, lst = await _space_with_members(env)
    await env.db.enqueue(
        """INSERT INTO spaces(id, name, owner_instance_id, owner_username,
           identity_public_key, config_sequence, space_type, join_mode)
           VALUES(?,?,?,?,?,0,'private','invite_only')""",
        ("sp-y", "Y", env.iid, "sowner", generate_identity_keypair().public_key.hex()),
    )
    other_list = await svc.create_list(space_id="sp-y", name="Y", created_by="uid-m")
    foreign = await svc.create_task(
        space_id="sp-y", list_id=other_list.id, title="F", created_by="uid-m"
    )
    with pytest.raises(KeyError):
        await svc.reorder_tasks(
            "sp-m",
            lst.id,
            ordered_ids=[foreign.id],
            moved_id=foreign.id,
            actor_user_id="uid-so",
        )
    with pytest.raises(ValueError, match="moved_id"):
        await svc.reorder_tasks(
            "sp-m", lst.id, ordered_ids=[], moved_id="", actor_user_id="uid-so"
        )


async def test_space_task_list_ids_are_owner_bound(env):
    svc, _, lst = await _space_with_members(env)
    assert (
        check_owner_bound_id(
            SPACE_TASK_LIST_KIND, lst.id, space_id="sp-m", owner_user_id="uid-so"
        )
        is OwnerBinding.VALID
    )


# ─── Adversarial-review regressions (service) ───────────────────────────


async def test_reorder_cannot_rearrange_others_cards_through_own_moved_id(env):
    """I3: Bob names his own card as ``moved_id`` but also swaps Alice's two
    cards — refused (403) and nothing moves."""
    await _seed_users(env, "alice", "bob")
    tl = await env.task_svc.create_list(name="L", created_by="alice")
    a1 = await env.task_svc.create_task(list_id=tl.id, title="A1", created_by="alice")
    a2 = await env.task_svc.create_task(list_id=tl.id, title="A2", created_by="alice")
    b = await env.task_svc.create_task(list_id=tl.id, title="B", created_by="bob")
    with pytest.raises(PermissionError):
        await env.task_svc.reorder_tasks(
            tl.id,
            ordered_ids=[b.id, a2.id, a1.id],
            moved_id=b.id,
            actor_user_id="bob",
        )
    rows = {t.id: t.position for t in await env.task_svc.list_tasks(tl.id)}
    assert rows == {a1.id: 0, a2.id: 1, b.id: 2}
    # A legit move — only his own card changes relative order.
    await env.task_svc.reorder_tasks(
        tl.id, ordered_ids=[a1.id, b.id, a2.id], moved_id=b.id, actor_user_id="bob"
    )
    rows = {t.id: t.position for t in await env.task_svc.list_tasks(tl.id)}
    assert rows == {a1.id: 0, b.id: 1, a2.id: 2}


async def test_reorder_admin_may_rearrange_everything(env):
    await _seed_users(env, "alice")
    await _seed_users(env, "boss", is_admin=True)
    tl = await env.task_svc.create_list(name="L", created_by="alice")
    a1 = await env.task_svc.create_task(list_id=tl.id, title="A1", created_by="alice")
    a2 = await env.task_svc.create_task(list_id=tl.id, title="A2", created_by="alice")
    await env.task_svc.reorder_tasks(
        tl.id, ordered_ids=[a2.id, a1.id], moved_id=a2.id, actor_user_id="boss"
    )
    rows = {t.id: t.position for t in await env.task_svc.list_tasks(tl.id)}
    assert rows == {a2.id: 0, a1.id: 1}


async def test_reorder_duplicate_ids_are_422(env):
    tl = await env.task_svc.create_list(name="L", created_by="u1")
    a = await env.task_svc.create_task(list_id=tl.id, title="A", created_by="u1")
    with pytest.raises(ValueError, match="duplicate"):
        await env.task_svc.reorder_tasks(
            tl.id, ordered_ids=[a.id, a.id], moved_id=a.id, actor_user_id="u1"
        )


async def test_space_reorder_duplicate_ids_are_422(env):
    svc, _, lst = await _space_with_members(env)
    a = await svc.create_task(
        space_id="sp-m", list_id=lst.id, title="A", created_by="uid-m"
    )
    with pytest.raises(ValueError, match="duplicate"):
        await svc.reorder_tasks(
            "sp-m",
            lst.id,
            ordered_ids=[a.id, a.id],
            moved_id=a.id,
            actor_user_id="uid-m",
        )


@pytest.mark.parametrize("position", [float("inf"), 1e30, 1.5, "x", 2**63, True])
async def test_update_task_bad_position_is_422(env, position):
    """M3: a non-integer or out-of-range position is a 422, never a 500."""
    tl = await env.task_svc.create_list(name="L", created_by="u1")
    t = await env.task_svc.create_task(list_id=tl.id, title="T", created_by="u1")
    with pytest.raises(ValueError, match="invalid position"):
        await env.task_svc.update_task(t.id, actor_user_id="u1", position=position)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"title": "T" * 201},
        {"title": "​‮  "},
        {"title": "ok", "description": "d" * 5001},
    ],
)
async def test_create_task_rejects_overlong_or_invisible_text(env, kwargs):
    """M4: REST text caps + visibly-empty titles are 422."""
    tl = await env.task_svc.create_list(name="L", created_by="u1")
    with pytest.raises(ValueError):
        await env.task_svc.create_task(list_id=tl.id, created_by="u1", **kwargs)


async def test_task_text_is_sanitised(env):
    tl = await env.task_svc.create_list(name="‮Chores‎", created_by="u1")
    assert tl.name == "Chores"
    t = await env.task_svc.create_task(
        list_id=tl.id,
        title="‮Buy⁦ milk",
        description="line1\n‮line2",
        created_by="u1",
    )
    assert t.title == "Buy milk"
    assert t.description == "line1\nline2"
    t2 = await env.task_svc.update_task(
        t.id, actor_user_id="u1", title="⁧New", description="​"
    )
    assert t2.title == "New"
    assert t2.description is None


@pytest.mark.parametrize("name", ["N" * 101, "​⁦"])
async def test_list_names_are_capped_and_visible(env, name):
    with pytest.raises(ValueError):
        await env.task_svc.create_list(name=name, created_by="u1")
    svc, _, lst = await _space_with_members(env)
    with pytest.raises(ValueError):
        await svc.create_list(space_id="sp-m", name=name, created_by="uid-m")
    with pytest.raises(ValueError):
        await svc.rename_list(
            lst.id, space_id="sp-m", name=name, actor_user_id="u-test"
        )


# ─── ADMIN_ONLY tasks (§4.3 feature access levels) ──────────────────────


async def _admin_only_tasks(env):
    """``sp-ao``: owner, admin, moderator, member; a list + a task made
    while the board was OPEN, then ``tasks_access`` flips to ADMIN_ONLY."""
    svc = SpaceTaskService(env.space_task_repo, space_repo=env.space_repo)
    await env.db.enqueue(
        """INSERT INTO spaces(id, name, owner_instance_id, owner_username,
           identity_public_key, config_sequence, space_type, join_mode)
           VALUES(?,?,?,?,?,0,'private','invite_only')""",
        ("sp-ao", "AO", env.iid, "o", generate_identity_keypair().public_key.hex()),
    )
    for uid, role in (
        ("u-owner", "owner"),
        ("u-admin", "admin"),
        ("u-mod", "moderator"),
        ("u-member", "member"),
    ):
        await env.db.enqueue(
            "INSERT INTO space_members(space_id, user_id, role) VALUES(?,?,?)",
            ("sp-ao", uid, role),
        )
    lst = await svc.create_list(space_id="sp-ao", name="L", created_by="u-member")
    task = await svc.create_task(
        space_id="sp-ao", list_id=lst.id, title="T", created_by="u-member"
    )
    await env.db.enqueue("UPDATE spaces SET tasks_access='admin_only' WHERE id='sp-ao'")
    return svc, lst, task


def _task_writes(lst, task, actor):
    """Every task / list write, as ``actor``."""
    return {
        "create_list": lambda s: s.create_list(
            space_id="sp-ao", name="New", created_by=actor
        ),
        "rename_list": lambda s: s.rename_list(
            lst.id, space_id="sp-ao", name="Renamed", actor_user_id=actor
        ),
        "delete_list": lambda s: s.delete_list(
            lst.id, space_id="sp-ao", actor_user_id=actor
        ),
        "create_task": lambda s: s.create_task(
            space_id="sp-ao", list_id=lst.id, title="New", created_by=actor
        ),
        "update_task": lambda s: s.update_task(
            task.id, space_id="sp-ao", actor_user_id=actor, title="Changed"
        ),
        "move_task": lambda s: s.update_task(
            task.id, space_id="sp-ao", actor_user_id=actor, status="done"
        ),
        "reorder": lambda s: s.reorder_tasks(
            "sp-ao",
            lst.id,
            ordered_ids=[task.id],
            moved_id=task.id,
            actor_user_id=actor,
        ),
        "archive": lambda s: s.archive_task(
            task.id, space_id="sp-ao", actor_user_id=actor
        ),
        "unarchive": lambda s: s.unarchive_task(
            task.id, space_id="sp-ao", actor_user_id=actor
        ),
        "delete_task": lambda s: s.delete_task(
            task.id, space_id="sp-ao", actor_user_id=actor
        ),
    }


@pytest.mark.parametrize("actor", ["u-member", "u-mod"])
@pytest.mark.parametrize(
    "op",
    [
        "create_list",
        "rename_list",
        "delete_list",
        "create_task",
        "update_task",
        "move_task",
        "reorder",
        "archive",
        "unarchive",
        "delete_task",
    ],
)
async def test_admin_only_tasks_refuse_members_and_moderators(env, actor, op):
    from socialhome.domain.space import AccessAdminOnlyError

    svc, lst, task = await _admin_only_tasks(env)
    with pytest.raises(AccessAdminOnlyError):
        await _task_writes(lst, task, actor)[op](svc)
    # Nothing changed.
    assert [x.id for x in await svc.list_lists("sp-ao")] == [lst.id]
    held = await env.space_task_repo.get(task.id)
    assert held is not None
    assert held[1].title == "T"
    assert held[1].status == TaskStatus.TODO
    assert held[1].archived_at is None


@pytest.mark.parametrize("actor", ["u-owner", "u-admin"])
async def test_admin_only_tasks_let_admins_work_the_board(env, actor):
    svc, lst, task = await _admin_only_tasks(env)
    writes = _task_writes(lst, task, actor)
    for op in (
        "create_list",
        "rename_list",
        "create_task",
        "update_task",
        "move_task",
        "reorder",
        "archive",
        "unarchive",
        "delete_task",
        "delete_list",
    ):
        await writes[op](svc)
    assert await env.space_task_repo.get(task.id) is None


async def test_space_task_events_name_their_actor(env):
    """The actor rides every space task / list event into federation."""
    from socialhome.domain.events import (
        TaskCreated,
        TaskDeleted,
        TaskListCreated,
        TaskListDeleted,
        TaskListUpdated,
    )

    svc, _lst, _task = await _admin_only_tasks(env)
    await env.db.enqueue("UPDATE spaces SET tasks_access='open' WHERE id='sp-ao'")
    bus = EventBus()
    seen: list = []
    for et in (
        TaskCreated,
        TaskUpdated,
        TaskDeleted,
        TaskListCreated,
        TaskListUpdated,
        TaskListDeleted,
    ):
        bus.subscribe(et, seen.append)
    svc._bus = bus
    lst = await svc.create_list(space_id="sp-ao", name="X", created_by="u-mod")
    await svc.rename_list(lst.id, space_id="sp-ao", name="Y", actor_user_id="u-admin")
    t = await svc.create_task(
        space_id="sp-ao", list_id=lst.id, title="T", created_by="u-member"
    )
    await svc.update_task(t.id, space_id="sp-ao", actor_user_id="u-owner", title="U")
    await svc.reorder_tasks(
        "sp-ao", lst.id, ordered_ids=["pad", t.id], moved_id=t.id, actor_user_id="u-mod"
    )
    await svc.archive_task(t.id, space_id="sp-ao", actor_user_id="u-admin")
    await svc.delete_task(t.id, space_id="sp-ao", actor_user_id="u-member")
    await svc.delete_list(lst.id, space_id="sp-ao", actor_user_id="u-owner")
    assert [(type(e).__name__, e.actor_user_id) for e in seen] == [
        ("TaskListCreated", "u-mod"),
        ("TaskListUpdated", "u-admin"),
        ("TaskCreated", "u-member"),
        ("TaskUpdated", "u-owner"),
        ("TaskUpdated", "u-mod"),
        ("TaskUpdated", "u-admin"),
        ("TaskDeleted", "u-member"),
        ("TaskListDeleted", "u-owner"),
    ]


# ─── MODERATED tasks (§4.3 review queue) ────────────────────────────────


async def _moderated_tasks(env):
    """``sp-mo`` (hosted here): owner, moderator, two members, an assignee;
    a list + a task by the owner, ``tasks_access`` MODERATED."""
    svc = SpaceTaskService(env.space_task_repo, space_repo=env.space_repo)
    await env.db.enqueue(
        """INSERT INTO spaces(id, name, owner_instance_id, owner_username,
           identity_public_key, config_sequence, space_type, join_mode)
           VALUES(?,?,?,?,?,0,'private','invite_only')""",
        ("sp-mo", "MO", env.iid, "o", generate_identity_keypair().public_key.hex()),
    )
    for uid, role in (
        ("u-owner", "owner"),
        ("u-mod", "moderator"),
        ("u-member", "member"),
        ("u-assignee", "member"),
    ):
        await env.db.enqueue(
            "INSERT INTO space_members(space_id, user_id, role) VALUES(?,?,?)",
            ("sp-mo", uid, role),
        )
    lst = await svc.create_list(space_id="sp-mo", name="L", created_by="u-owner")
    task = await svc.create_task(
        space_id="sp-mo",
        list_id=lst.id,
        title="T",
        created_by="u-owner",
        assignees=["u-assignee"],
        description="keep me",
    )
    await env.db.enqueue(
        "UPDATE spaces SET tasks_access='moderated', feature_todo=1 WHERE id='sp-mo'"
    )
    mod = SpaceModerationService(
        env.space_repo, user_repo=env.user_repo, own_instance_id=env.iid
    )
    svc.attach_moderation(mod)
    handler = TaskModerationHandler(svc)
    for action in (ContentAction.CREATE, ContentAction.EDIT, ContentAction.DELETE):
        mod.register("tasks", action, handler)
    return svc, mod, lst, task


async def test_moderated_member_task_create_queues_then_approves(env):
    svc, mod, lst, _task = await _moderated_tasks(env)
    with pytest.raises(ContentQueuedForReview) as exc:
        await svc.create_task(
            space_id="sp-mo", list_id=lst.id, title="New", created_by="u-member"
        )
    item = exc.value.item
    assert [t.title for t in await svc.list_tasks("sp-mo")] == ["T"]
    await mod.approve("sp-mo", item.id, actor_user_id="u-mod")
    created = await svc.get_task_in_space(item.payload["target_id"], "sp-mo")
    assert (created.title, created.created_by) == ("New", "u-member")


async def test_moderated_list_create_rename_delete_queue(env):
    svc, mod, lst, _task = await _moderated_tasks(env)
    with pytest.raises(ContentQueuedForReview) as c:
        await svc.create_list(space_id="sp-mo", name="Mine", created_by="u-member")
    with pytest.raises(ContentQueuedForReview) as r:
        await svc.rename_list(
            lst.id, space_id="sp-mo", name="Renamed", actor_user_id="u-member"
        )
    with pytest.raises(ContentQueuedForReview) as d:
        await svc.delete_list(lst.id, space_id="sp-mo", actor_user_id="u-member")
    assert [x.name for x in await svc.list_lists("sp-mo")] == ["L"]
    await mod.approve("sp-mo", c.value.item.id, actor_user_id="u-mod")
    await mod.approve("sp-mo", r.value.item.id, actor_user_id="u-mod")
    assert sorted(x.name for x in await svc.list_lists("sp-mo")) == ["Mine", "Renamed"]
    await mod.approve("sp-mo", d.value.item.id, actor_user_id="u-mod")
    assert [x.name for x in await svc.list_lists("sp-mo")] == ["Mine"]


async def test_moderated_others_task_edit_queues_field_patch(env):
    svc, mod, _lst, task = await _moderated_tasks(env)
    with pytest.raises(ContentQueuedForReview) as exc:
        await svc.update_task(
            task.id, space_id="sp-mo", actor_user_id="u-member", title="Changed"
        )
    item = exc.value.item
    assert item.payload["patch"] == {"title": "Changed"}
    # Meanwhile the owner edits the description — must survive the approve.
    await svc.update_task(
        task.id, space_id="sp-mo", actor_user_id="u-owner", description="newer"
    )
    await mod.approve("sp-mo", item.id, actor_user_id="u-mod")
    got = await svc.get_task_in_space(task.id, "sp-mo")
    assert (got.title, got.description, got.created_by) == (
        "Changed",
        "newer",
        "u-owner",
    )


async def test_moderated_status_change_of_others_task_queues(env):
    svc, _mod, _lst, task = await _moderated_tasks(env)
    with pytest.raises(ContentQueuedForReview) as exc:
        await svc.update_task(
            task.id, space_id="sp-mo", actor_user_id="u-member", status="done"
        )
    assert exc.value.item.payload["patch"] == {"status": "done"}
    assert (await svc.get_task_in_space(task.id, "sp-mo")).status is TaskStatus.TODO


async def test_moderated_assignee_owns_status_but_not_title(env):
    svc, _mod, _lst, task = await _moderated_tasks(env)
    moved = await svc.update_task(
        task.id,
        space_id="sp-mo",
        actor_user_id="u-assignee",
        status="in_progress",
        position=0,
    )
    assert moved.status is TaskStatus.IN_PROGRESS
    with pytest.raises(ContentQueuedForReview):
        await svc.update_task(
            task.id, space_id="sp-mo", actor_user_id="u-assignee", title="Mine now"
        )


async def test_moderated_same_column_reorder_never_queues(env):
    svc, _mod, lst, task = await _moderated_tasks(env)
    await svc.reorder_tasks(
        "sp-mo",
        lst.id,
        ordered_ids=[task.id],
        moved_id=task.id,
        actor_user_id="u-member",
    )
    await svc.update_task(
        task.id, space_id="sp-mo", actor_user_id="u-member", position=3
    )


async def test_moderated_archive_and_delete_of_others_task_queue(env):
    svc, mod, _lst, task = await _moderated_tasks(env)
    with pytest.raises(ContentQueuedForReview) as a:
        await svc.archive_task(task.id, space_id="sp-mo", actor_user_id="u-member")
    assert a.value.item.payload["op"] == "archive"
    assert (await svc.get_task_in_space(task.id, "sp-mo")).archived_at is None
    await mod.approve("sp-mo", a.value.item.id, actor_user_id="u-mod")
    assert (await svc.get_task_in_space(task.id, "sp-mo")).archived_at is not None
    with pytest.raises(ContentQueuedForReview) as d:
        await svc.delete_task(task.id, space_id="sp-mo", actor_user_id="u-member")
    await mod.approve("sp-mo", d.value.item.id, actor_user_id="u-mod")
    assert [t.id for t in await svc.list_tasks("sp-mo")] == []


async def test_moderated_task_edit_of_deleted_task_410(env):
    svc, mod, _lst, task = await _moderated_tasks(env)
    with pytest.raises(ContentQueuedForReview) as exc:
        await svc.update_task(
            task.id, space_id="sp-mo", actor_user_id="u-member", title="x"
        )
    await svc.delete_task(task.id, space_id="sp-mo", actor_user_id="u-owner")
    with pytest.raises(ModerationTargetGoneError):
        await mod.approve("sp-mo", exc.value.item.id, actor_user_id="u-mod")
    assert (
        await mod.get_item("sp-mo", exc.value.item.id)
    ).status is ModerationStatus.EXPIRED


async def test_moderated_task_create_reject_persists_nothing(env):
    svc, mod, lst, _task = await _moderated_tasks(env)
    with pytest.raises(ContentQueuedForReview) as exc:
        await svc.create_task(
            space_id="sp-mo", list_id=lst.id, title="Nope", created_by="u-member"
        )
    await mod.reject("sp-mo", exc.value.item.id, actor_user_id="u-mod")
    assert [t.title for t in await svc.list_tasks("sp-mo")] == ["T"]


async def test_moderated_moderator_writes_directly(env):
    svc, _mod, lst, task = await _moderated_tasks(env)
    await svc.create_task(
        space_id="sp-mo", list_id=lst.id, title="M", created_by="u-mod"
    )
    await svc.update_task(task.id, space_id="sp-mo", actor_user_id="u-mod", title="M2")
