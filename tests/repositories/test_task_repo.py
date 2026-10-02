"""Tests for SqliteTaskRepo and SqliteSpaceTaskRepo."""

from __future__ import annotations

from datetime import date, datetime, timezone

import pytest

import copy

from socialhome.domain.task import Task, TaskList, TaskPriority, TaskStatus
from socialhome.repositories.task_repo import SqliteSpaceTaskRepo, SqliteTaskRepo


@pytest.fixture
async def env(tmp_dir):
    """Env with task repos over a real SQLite database."""
    from socialhome.crypto import generate_identity_keypair, derive_instance_id
    from socialhome.db.database import AsyncDatabase

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
    await env.space_repo.save_list(tl, space_id="sp-1")
    result = await env.space_repo.get_list("spl-1")
    assert result is not None
    sid, fetched = result
    assert sid == "sp-1"
    assert fetched.name == "Space Tasks"


async def test_space_save_and_get_task(env):
    """SqliteSpaceTaskRepo save / get task roundtrip."""
    tl = _list_("spl-t", "ST")
    await env.space_repo.save_list(tl, space_id="sp-1")
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
    await env.space_repo.save(task, space_id="sp-1")
    result = await env.space_repo.get("sp-t1")
    assert result is not None
    sid, fetched = result
    assert sid == "sp-1"
    assert fetched.title == "Space task"


async def test_space_list_by_list(env):
    """list_by_list on space repo returns tasks in the list."""
    tl = _list_("spl-lbl")
    await env.space_repo.save_list(tl, space_id="sp-1")
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
        await env.space_repo.save(t, space_id="sp-1")
    results = await env.space_repo.list_by_list("spl-lbl", space_id="sp-1")
    assert len(results) == 3
    # The read is space-filtered: the same list id seen from another
    # space yields nothing.
    assert await env.space_repo.list_by_list("spl-lbl", space_id="sp-other") == []
    done_hidden = await env.space_repo.list_by_list(
        "spl-lbl", space_id="sp-1", include_done=False
    )
    assert len(done_hidden) == 3


async def test_space_list_by_space(env):
    """list_by_space returns all tasks belonging to the space."""
    tl1 = _list_("spl-bs1")
    tl2 = _list_("spl-bs2")
    await env.space_repo.save_list(tl1, space_id="sp-1")
    await env.space_repo.save_list(tl2, space_id="sp-1")
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
    await env.space_repo.save(t1, space_id="sp-1")
    await env.space_repo.save(t2, space_id="sp-1")
    results = await env.space_repo.list_by_space("sp-1")
    ids = {t.id for t in results}
    assert {"sp-tbs1", "sp-tbs2"}.issubset(ids)


async def test_space_delete_task(env):
    """delete on space task repo removes the task."""
    tl = _list_("spl-del")
    await env.space_repo.save_list(tl, space_id="sp-1")
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
    await env.space_repo.save(task, space_id="sp-1")
    await env.space_repo.delete("sp-tdel", space_id="sp-1")
    assert await env.space_repo.get("sp-tdel") is None


# ─── §24.11 cross-space scoping ──────────────────────────────


@pytest.fixture
async def two_spaces(env):
    """Space sp-1 and space sp-2, each with a task list and one task."""
    await env.db.enqueue(
        "INSERT INTO spaces(id, name, owner_instance_id, owner_username,"
        " identity_public_key) VALUES(?,?,?,?,?)",
        ("sp-2", "OtherSpace", "inst-x", "alice", "ccdd" * 16),
    )
    for sid, lid, tid in (("sp-1", "l-1", "t-1"), ("sp-2", "l-2", "t-2")):
        assert await env.space_repo.save_list(_list_(lid, f"list-{sid}"), space_id=sid)
        assert await env.space_repo.save(_task(tid, lid, f"task-{sid}"), space_id=sid)
    return env


async def _task_row(env, task_id):
    row = await env.db.fetchone("SELECT * FROM space_tasks WHERE id=?", (task_id,))
    return dict(row) if row is not None else None


async def test_space_task_save_refuses_cross_space_id(two_spaces):
    """Re-saving sp-2's task id under sp-1 leaves sp-2's row untouched."""
    before = await _task_row(two_spaces, "t-2")
    assert (
        await two_spaces.space_repo.save(_task("t-2", "l-1", "stolen"), space_id="sp-1")
        is False
    )
    assert await _task_row(two_spaces, "t-2") == before
    assert [t.id for t in await two_spaces.space_repo.list_by_space("sp-1")] == ["t-1"]


async def test_space_task_save_refuses_foreign_parent_list(two_spaces):
    """A task may not be filed under another space's list."""
    assert (
        await two_spaces.space_repo.save(
            _task("t-new", "l-2", "smuggled"), space_id="sp-1"
        )
        is False
    )
    assert await two_spaces.space_repo.get("t-new") is None
    assert [
        t.id for t in await two_spaces.space_repo.list_by_list("l-2", space_id="sp-2")
    ] == ["t-2"]
    assert await two_spaces.space_repo.list_by_list("l-2", space_id="sp-1") == []


async def test_space_task_delete_refuses_foreign_space(two_spaces):
    """A delete routed as sp-1 cannot remove sp-2's task."""
    assert await two_spaces.space_repo.delete("t-2", space_id="sp-1") is False
    assert await two_spaces.space_repo.get("t-2") is not None
    assert await two_spaces.space_repo.delete("t-2", space_id="sp-2") is True
    assert await two_spaces.space_repo.get("t-2") is None


async def test_space_task_list_save_refuses_cross_space_id(two_spaces):
    """Re-saving sp-2's list id under sp-1 leaves sp-2's list untouched."""
    assert (
        await two_spaces.space_repo.save_list(_list_("l-2", "renamed"), space_id="sp-1")
        is False
    )
    result = await two_spaces.space_repo.get_list("l-2")
    assert result is not None
    assert result[0] == "sp-2"
    assert result[1].name == "list-sp-2"


async def test_space_task_list_delete_refuses_foreign_space(two_spaces):
    """A list delete routed as sp-1 cannot remove sp-2's list."""
    assert await two_spaces.space_repo.delete_list("l-2", space_id="sp-1") is False
    assert await two_spaces.space_repo.get_list("l-2") is not None
    assert await two_spaces.space_repo.delete_list("l-2", space_id="sp-2") is True
    assert await two_spaces.space_repo.get_list("l-2") is None


# ── Priority, labels, next_position (0064) ────────────────────────────────


async def test_household_priority_and_labels_round_trip(env):
    await env.repo.save_list(_list_())
    t = copy.replace(
        _task("t-pl"), priority=TaskPriority.URGENT, labels=("Home", "Car")
    )
    await env.repo.save(t)
    got = await env.repo.get("t-pl")
    assert got is not None
    assert got.priority is TaskPriority.URGENT
    assert got.labels == ("Home", "Car")
    # Upsert clears both.
    await env.repo.save(copy.replace(t, priority=None, labels=()))
    got = await env.repo.get("t-pl")
    assert got is not None and got.priority is None and got.labels == ()


async def test_household_unknown_stored_priority_reads_as_none(env):
    await env.repo.save_list(_list_())
    await env.repo.save(_task("t-raw"))
    row = await env.db.fetchone("SELECT priority, labels_json FROM tasks")
    assert row["priority"] is None and row["labels_json"] == "[]"


async def test_household_next_position(env):
    await env.repo.save_list(_list_())
    await env.repo.save_list(_list_("lst-2", "Other"))
    assert await env.repo.next_position("lst-1") == 0
    await env.repo.save(copy.replace(_task("a"), position=4))
    await env.repo.save(copy.replace(_task("b"), position=1))
    await env.repo.save(copy.replace(_task("c", list_id="lst-2"), position=9))
    assert await env.repo.next_position("lst-1") == 5
    assert await env.repo.next_position("lst-2") == 10


async def test_space_priority_labels_and_next_position(env):
    await env.space_repo.save_list(_list_("sl-1"), space_id="sp-1")
    assert await env.space_repo.next_position("sl-1", space_id="sp-1") == 0
    t = copy.replace(
        _task("st-pl", list_id="sl-1"),
        position=2,
        priority=TaskPriority.LOW,
        labels=("School",),
    )
    assert await env.space_repo.save(t, space_id="sp-1")
    held = await env.space_repo.get("st-pl")
    assert held is not None
    assert held[1].priority is TaskPriority.LOW
    assert held[1].labels == ("School",)
    assert await env.space_repo.next_position("sl-1", space_id="sp-1") == 3
    # Another space's view of the same list id sees nothing.
    assert await env.space_repo.next_position("sl-1", space_id="sp-other") == 0


async def test_space_list_lists_since(env):
    await env.space_repo.save_list(_list_("sl-old"), space_id="sp-1")
    await env.db.enqueue(
        "UPDATE space_task_lists SET created_at='2026-01-01 00:00:00' WHERE id='sl-old'"
    )
    await env.space_repo.save_list(_list_("sl-new"), space_id="sp-1")
    got = await env.space_repo.list_lists_since("sp-1", "2026-06-01T00:00:00+00:00")
    assert [lst.id for lst in got] == ["sl-new"]
    assert await env.space_repo.list_lists_since("sp-other", "2000-01-01") == []


async def test_space_list_lists_since_includes_the_same_second(env):
    """M6: ``created_at`` has second precision, so a list created in the
    same second as ``since`` must still be replayed (``>=``)."""
    await env.space_repo.save_list(_list_("sl-same"), space_id="sp-1")
    await env.db.enqueue(
        "UPDATE space_task_lists SET created_at='2026-06-01 10:00:00' WHERE id='sl-same'"
    )
    got = await env.space_repo.list_lists_since("sp-1", "2026-06-01T10:00:00.400+00:00")
    assert [lst.id for lst in got] == ["sl-same"]


# ── Open counts (Organize hub chip) ──────────────────────────────────────


async def _archive(env, table: str, task_id: str) -> None:
    await env.db.enqueue(
        f"UPDATE {table} SET archived_at=? WHERE id=?",
        (datetime.now(timezone.utc).isoformat(), task_id),
    )


async def test_open_counts_empty_and_mixed(env):
    """One grouped count per list: not done, not archived; empty lists absent."""
    assert await env.repo.open_counts() == {}
    await env.repo.save_list(_list_("l-empty", "Empty"))
    await env.repo.save_list(_list_("l-mixed", "Mixed"))
    await env.repo.save_list(_list_("l-done", "Done"))
    await env.repo.save(_task("m1", "l-mixed", status=TaskStatus.TODO))
    await env.repo.save(_task("m2", "l-mixed", status=TaskStatus.IN_PROGRESS))
    await env.repo.save(_task("m3", "l-mixed", status=TaskStatus.DONE))
    await env.repo.save(_task("m4", "l-mixed", status=TaskStatus.TODO))
    await _archive(env, "tasks", "m4")
    await env.repo.save(_task("d1", "l-done", status=TaskStatus.DONE))
    assert await env.repo.open_counts() == {"l-mixed": 2}


async def test_space_open_counts_are_scoped_to_the_space(two_spaces):
    """The space count groups by list inside one space only."""
    env = two_spaces
    assert await env.space_repo.open_counts("sp-1") == {"l-1": 1}
    assert await env.space_repo.open_counts("sp-2") == {"l-2": 1}
    await env.space_repo.save(
        _task("t-1b", "l-1", status=TaskStatus.DONE), space_id="sp-1"
    )
    await env.space_repo.save(_task("t-1c", "l-1"), space_id="sp-1")
    await _archive(env, "space_tasks", "t-1c")
    assert await env.space_repo.open_counts("sp-1") == {"l-1": 1}
    assert await env.space_repo.open_counts("sp-none") == {}


# ── Space list tombstones (migration 0069) ───────────────────────────────


async def _deleted_by(env, list_id):
    row = await env.db.fetchone(
        "SELECT deleted_by FROM space_task_lists WHERE id=?", (list_id,)
    )
    return row["deleted_by"]


async def test_space_list_delete_tombstones_and_drops_its_tasks(two_spaces):
    env = two_spaces
    assert (
        await env.space_repo.delete_list("l-1", space_id="sp-1", deleted_by="u-x")
        is True
    )
    assert await env.space_repo.get_list("l-1") is None
    assert await env.space_repo.list_lists("sp-1") == []
    assert await env.space_repo.is_list_deleted("l-1", space_id="sp-1") is True
    assert await _deleted_by(env, "l-1") == "u-x"
    assert await env.space_repo.get("t-1") is None  # the trigger's cascade
    # Scoped: not a tombstone of another space; that space's rows untouched.
    assert await env.space_repo.is_list_deleted("l-1", space_id="sp-2") is False
    assert await env.space_repo.get("t-2") is not None
    assert await env.space_repo.is_list_deleted("l-2", space_id="sp-2") is False
    # A second delete changes nothing (and keeps the first deleter).
    assert (
        await env.space_repo.delete_list("l-1", space_id="sp-1", deleted_by="u-y")
        is False
    )
    assert await _deleted_by(env, "l-1") == "u-x"


async def test_a_delete_naming_nobody_stores_null(two_spaces):
    env = two_spaces
    assert await env.space_repo.delete_list("l-1", space_id="sp-1")
    assert await _deleted_by(env, "l-1") is None
    (tomb,) = await env.space_repo.list_list_tombstones("sp-1")
    assert tomb.deleted_by == ""


async def test_a_tombstoned_list_never_comes_back(two_spaces):
    env = two_spaces
    await env.space_repo.delete_list("l-1", space_id="sp-1")
    assert (
        await env.space_repo.save_list(_list_("l-1", "back"), space_id="sp-1") is False
    )
    assert await env.space_repo.save(_task("t-new", "l-1"), space_id="sp-1") is False
    assert await env.space_repo.get_list("l-1") is None
    assert await env.space_repo.get("t-new") is None


async def test_tombstone_list_stubs_only_an_unseen_id(two_spaces):
    env = two_spaces
    assert await env.space_repo.tombstone_list(
        "l-unseen", space_id="sp-1", created_by="u-c", deleted_by="u-d"
    )
    assert await env.space_repo.is_list_deleted("l-unseen", space_id="sp-1")
    assert await env.space_repo.save_list(_list_("l-unseen"), space_id="sp-1") is False
    (tomb,) = await env.space_repo.list_list_tombstones("sp-1")
    assert (tomb.id, tomb.created_by, tomb.deleted_by) == ("l-unseen", "u-c", "u-d")
    # Insert-only: a held id (live, tombstoned, or another space's) is never
    # touched, and neither is a space we don't hold.
    assert not await env.space_repo.tombstone_list(
        "l-1", space_id="sp-1", created_by="u-c"
    )
    assert await env.space_repo.get_list("l-1") is not None
    assert not await env.space_repo.tombstone_list(
        "l-unseen", space_id="sp-1", created_by="u-c"
    )
    assert not await env.space_repo.tombstone_list(
        "l-2", space_id="sp-1", created_by="u-c"
    )
    assert await env.space_repo.get_list("l-2") is not None
    assert not await env.space_repo.tombstone_list(
        "l-x", space_id="sp-none", created_by="u-c"
    )


async def test_list_list_tombstones_newest_first_and_since(two_spaces):
    env = two_spaces
    await env.space_repo.save_list(_list_("l-1b"), space_id="sp-1")
    await env.space_repo.delete_list("l-1", space_id="sp-1")
    await env.space_repo.delete_list("l-1b", space_id="sp-1")
    await env.db.enqueue(
        "UPDATE space_task_lists SET deleted_at='2026-01-01 00:00:00' WHERE id='l-1'"
    )
    got = await env.space_repo.list_list_tombstones("sp-1")
    assert [t.id for t in got] == ["l-1b", "l-1"]
    assert got[1].deleted_at == "2026-01-01 00:00:00"
    assert got[1].created_by == "uid-alice"
    since = await env.space_repo.list_list_tombstones(
        "sp-1", since="2026-06-01T00:00:00+00:00"
    )
    assert [t.id for t in since] == ["l-1b"]
    assert await env.space_repo.list_list_tombstones("sp-1", limit=1) == got[:1]
    assert await env.space_repo.list_list_tombstones("sp-2") == []


async def test_a_rename_stamps_updated_at_for_the_resume_replay(env):
    await env.space_repo.save_list(_list_("sl-1", "Old"), space_id="sp-1")
    await env.db.enqueue(
        "UPDATE space_task_lists SET created_at='2026-01-01 00:00:00' WHERE id='sl-1'"
    )
    since = "2026-06-01T00:00:00+00:00"
    # Re-saving the same name (every host sync does) is not a change.
    assert await env.space_repo.save_list(_list_("sl-1", "Old"), space_id="sp-1")
    assert await env.space_repo.list_lists_since("sp-1", since) == []
    assert await env.space_repo.save_list(_list_("sl-1", "New"), space_id="sp-1")
    got = await env.space_repo.list_lists_since("sp-1", since)
    assert [(lst.id, lst.name) for lst in got] == [("sl-1", "New")]
    await env.space_repo.delete_list("sl-1", space_id="sp-1")
    assert await env.space_repo.list_lists_since("sp-1", since) == []
