"""Tests for SqliteTaskRepo and SqliteSpaceTaskRepo."""

from __future__ import annotations

from datetime import date, datetime, timezone

import pytest

from social_home.domain.task import Task, TaskList, TaskStatus
from social_home.repositories.task_repo import SqliteSpaceTaskRepo, SqliteTaskRepo


@pytest.fixture
async def env(tmp_dir):
    """Env with task repos over a real SQLite database."""
    from social_home.crypto import generate_identity_keypair, derive_instance_id
    from social_home.db.database import AsyncDatabase

    kp = generate_identity_keypair()
    iid = derive_instance_id(kp.public_key)
    db = AsyncDatabase(tmp_dir / "test.db", batch_timeout_ms=10)
    await db.startup()
    await db.enqueue(
        "INSERT INTO instance_identity(instance_id, identity_private_key,"
        " identity_public_key, routing_secret) VALUES(?,?,?,?)",
        (iid, kp.private_key.hex(), kp.public_key.hex(), "aa" * 32),
    )
    await db.enqueue(
        "INSERT INTO users(username, user_id, display_name) VALUES(?,?,?)",
        ("alice", "uid-alice", "Alice"),
    )
    await db.enqueue(
        "INSERT INTO spaces(id, name, owner_instance_id, owner_username, identity_public_key)"
        " VALUES(?,?,?,?,?)",
        ("sp-1", "TestSpace", "inst-x", "alice", "aabb" * 16),
    )

    class E:
        pass

    e = E()
    e.db = db
    e.repo = SqliteTaskRepo(db)
    e.space_repo = SqliteSpaceTaskRepo(db)
    yield e
    await db.shutdown()


def _list_(list_id: str = "lst-1", name: str = "Chores") -> TaskList:
    return TaskList(id=list_id, name=name, created_by="uid-alice")


def _task(
    task_id: str,
    list_id: str = "lst-1",
    title: str = "Do something",
    status: TaskStatus = TaskStatus.TODO,
) -> Task:
    now = datetime.now(timezone.utc)
    return Task(
        id=task_id,
        list_id=list_id,
        title=title,
        status=status,
        position=0,
        created_by="uid-alice",
        created_at=now,
        updated_at=now,
    )


# ── Household task lists ───────────────────────────────────────────────────


async def test_save_and_get_list(env):
    """save_list persists a list; get_list retrieves it."""
    tl = _list_("lst-1")
    await env.repo.save_list(tl)
    fetched = await env.repo.get_list("lst-1")
    assert fetched is not None
    assert fetched.name == "Chores"


async def test_get_missing_list_returns_none(env):
    """get_list returns None for an unknown id."""
    assert await env.repo.get_list("no-such") is None


async def test_list_lists(env):
    """list_lists returns all task lists."""
    await env.repo.save_list(_list_("lst-a", "A"))
    await env.repo.save_list(_list_("lst-b", "B"))
    lists = await env.repo.list_lists()
    ids = [lst.id for lst in lists]
    assert "lst-a" in ids
    assert "lst-b" in ids


async def test_delete_list(env):
    """delete_list removes the task list."""
    await env.repo.save_list(_list_("lst-del"))
    await env.repo.delete_list("lst-del")
    assert await env.repo.get_list("lst-del") is None


# ── Household tasks ────────────────────────────────────────────────────────


async def test_save_and_get_task(env):
    """save persists a task; get retrieves it."""
    await env.repo.save_list(_list_("lst-t"))
    task = _task("t-1", "lst-t")
    await env.repo.save(task)
    fetched = await env.repo.get("t-1")
    assert fetched is not None
    assert fetched.title == "Do something"


async def test_get_missing_task_returns_none(env):
    """get returns None for an unknown task id."""
    assert await env.repo.get("nope") is None


async def test_list_by_list(env):
    """list_by_list returns all tasks in the given list."""
    await env.repo.save_list(_list_("lst-lbl"))
    await env.repo.save(_task("t-lbl1", "lst-lbl"))
    await env.repo.save(_task("t-lbl2", "lst-lbl"))
    tasks = await env.repo.list_by_list("lst-lbl")
    assert len(tasks) == 2


async def test_list_by_list_exclude_done(env):
    """list_by_list with include_done=False excludes DONE tasks."""
    await env.repo.save_list(_list_("lst-done"))
    done_task = _task("t-done1", "lst-done", status=TaskStatus.DONE)
    todo_task = _task("t-todo1", "lst-done", status=TaskStatus.TODO)
    await env.repo.save(done_task)
    await env.repo.save(todo_task)
    tasks = await env.repo.list_by_list("lst-done", include_done=False)
    ids = [t.id for t in tasks]
    assert "t-done1" not in ids
    assert "t-todo1" in ids


async def test_list_by_status(env):
    """list_by_status returns tasks matching the given status."""
    await env.repo.save_list(_list_("lst-status"))
    await env.repo.save(_task("t-s1", "lst-status", status=TaskStatus.IN_PROGRESS))
    await env.repo.save(_task("t-s2", "lst-status", status=TaskStatus.TODO))
    in_progress = await env.repo.list_by_status(TaskStatus.IN_PROGRESS)
    assert any(t.id == "t-s1" for t in in_progress)
    assert not any(t.id == "t-s2" for t in in_progress)


async def test_list_by_assignee(env):
    """list_by_assignee returns tasks assigned to the given user_id."""
    await env.repo.save_list(_list_("lst-assign"))
    now = datetime.now(timezone.utc)
    assigned = Task(
        id="t-assigned",
        list_id="lst-assign",
        title="Assigned task",
        status=TaskStatus.TODO,
        position=0,
        created_by="uid-alice",
        created_at=now,
        updated_at=now,
        assignees=("uid-alice",),
    )
    unassigned = _task("t-unassigned", "lst-assign")
    await env.repo.save(assigned)
    await env.repo.save(unassigned)
    results = await env.repo.list_by_assignee("uid-alice")
    assert any(t.id == "t-assigned" for t in results)
    assert not any(t.id == "t-unassigned" for t in results)


async def test_list_due_on(env):
    """list_due_on returns non-done tasks due on the exact date."""
    await env.repo.save_list(_list_("lst-due"))
    today = date(2025, 7, 4)
    now = datetime.now(timezone.utc)
    due_task = Task(
        id="t-due",
        list_id="lst-due",
        title="Due today",
        status=TaskStatus.TODO,
        position=0,
        created_by="uid-alice",
        created_at=now,
        updated_at=now,
        due_date=today,
    )
    await env.repo.save(due_task)
    results = await env.repo.list_due_on(today)
    assert any(t.id == "t-due" for t in results)


async def test_delete_task(env):
    """delete removes the task from the database."""
    await env.repo.save_list(_list_("lst-del-t"))
    task = _task("t-del", "lst-del-t")
    await env.repo.save(task)
    await env.repo.delete("t-del")
    assert await env.repo.get("t-del") is None


# ── Space tasks ────────────────────────────────────────────────────────────


async def test_space_save_and_get_list(env):
    """SqliteSpaceTaskRepo save_list / get_list roundtrip."""
    tl = _list_("spl-1", "Space Tasks")
    await env.space_repo.save_list("sp-1", tl)
    result = await env.space_repo.get_list("spl-1")
    assert result is not None
    sid, fetched = result
    assert sid == "sp-1"
    assert fetched.name == "Space Tasks"


async def test_space_save_and_get_task(env):
    """SqliteSpaceTaskRepo save / get task roundtrip."""
    tl = _list_("spl-t", "ST")
    await env.space_repo.save_list("sp-1", tl)
    now = datetime.now(timezone.utc)
    task = Task(
        id="sp-t1",
        list_id="spl-t",
        title="Space task",
        status=TaskStatus.TODO,
        position=0,
        created_by="uid-alice",
        created_at=now,
        updated_at=now,
    )
    await env.space_repo.save("sp-1", task)
    result = await env.space_repo.get("sp-t1")
    assert result is not None
    sid, fetched = result
    assert sid == "sp-1"
    assert fetched.title == "Space task"


async def test_space_list_by_list(env):
    """list_by_list on space repo returns tasks in the list."""
    tl = _list_("spl-lbl")
    await env.space_repo.save_list("sp-1", tl)
    now = datetime.now(timezone.utc)
    for i in range(3):
        t = Task(
            id=f"sp-tlbl-{i}",
            list_id="spl-lbl",
            title=f"T{i}",
            status=TaskStatus.TODO,
            position=i,
            created_by="uid-alice",
            created_at=now,
            updated_at=now,
        )
        await env.space_repo.save("sp-1", t)
    results = await env.space_repo.list_by_list("spl-lbl")
    assert len(results) == 3


async def test_space_list_by_space(env):
    """list_by_space returns all tasks belonging to the space."""
    tl1 = _list_("spl-bs1")
    tl2 = _list_("spl-bs2")
    await env.space_repo.save_list("sp-1", tl1)
    await env.space_repo.save_list("sp-1", tl2)
    now = datetime.now(timezone.utc)
    t1 = Task(
        id="sp-tbs1",
        list_id="spl-bs1",
        title="T1",
        status=TaskStatus.TODO,
        position=0,
        created_by="uid-alice",
        created_at=now,
        updated_at=now,
    )
    t2 = Task(
        id="sp-tbs2",
        list_id="spl-bs2",
        title="T2",
        status=TaskStatus.TODO,
        position=0,
        created_by="uid-alice",
        created_at=now,
        updated_at=now,
    )
    await env.space_repo.save("sp-1", t1)
    await env.space_repo.save("sp-1", t2)
    results = await env.space_repo.list_by_space("sp-1")
    ids = {t.id for t in results}
    assert {"sp-tbs1", "sp-tbs2"}.issubset(ids)


async def test_space_delete_task(env):
    """delete on space task repo removes the task."""
    tl = _list_("spl-del")
    await env.space_repo.save_list("sp-1", tl)
    now = datetime.now(timezone.utc)
    task = Task(
        id="sp-tdel",
        list_id="spl-del",
        title="Del",
        status=TaskStatus.TODO,
        position=0,
        created_by="uid-alice",
        created_at=now,
        updated_at=now,
    )
    await env.space_repo.save("sp-1", task)
    await env.space_repo.delete("sp-tdel")
    assert await env.space_repo.get("sp-tdel") is None
