"""Extra coverage for :class:`TaskService` — comments, attachments,
reorder, recurrence. Pairs with :mod:`tests.services.test_task_service`
which covers the happy-path CRUD.
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from social_home.crypto import derive_instance_id, generate_identity_keypair
from social_home.db.database import AsyncDatabase
from social_home.repositories.task_repo import SqliteSpaceTaskRepo, SqliteTaskRepo
from social_home.services.task_service import SpaceTaskService, TaskService


@pytest.fixture
async def env(tmp_dir):
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
    e.space_task_svc = SpaceTaskService(e.space_task_repo)
    yield e
    await db.shutdown()


# ── Comments ────────────────────────────────────────────────────────


async def test_comment_lifecycle(env):
    lst = await env.task_svc.create_list(name="L", created_by="u1")
    task = await env.task_svc.create_task(
        list_id=lst.id,
        title="T",
        created_by="u1",
    )
    # Empty comment is rejected.
    with pytest.raises(ValueError):
        await env.task_svc.add_comment(
            task.id,
            author_user_id="u1",
            content="   ",
        )
    # Add two comments.
    c1 = await env.task_svc.add_comment(
        task.id,
        author_user_id="u1",
        content="first",
    )
    await env.task_svc.add_comment(
        task.id,
        author_user_id="u1",
        content="second",
    )
    rows = await env.task_svc.list_comments(task.id)
    assert len(rows) == 2
    # Unknown task → KeyError (from get_task).
    with pytest.raises(KeyError):
        await env.task_svc.list_comments("nope")
    # Delete one comment.
    await env.task_svc.delete_comment(c1.id, actor_user_id="u1")


# ── Attachments ─────────────────────────────────────────────────────


async def test_attachment_lifecycle(env):
    lst = await env.task_svc.create_list(name="L2", created_by="u1")
    task = await env.task_svc.create_task(
        list_id=lst.id,
        title="T2",
        created_by="u1",
    )
    # size_bytes 0 is rejected.
    with pytest.raises(ValueError):
        await env.task_svc.add_attachment(
            task.id,
            uploaded_by="u1",
            url="http://x",
            filename="f.pdf",
            mime="application/pdf",
            size_bytes=0,
        )
    # Positive size ok.
    att = await env.task_svc.add_attachment(
        task.id,
        uploaded_by="u1",
        url="http://x/f.pdf",
        filename="f.pdf",
        mime="application/pdf",
        size_bytes=1024,
    )
    rows = await env.task_svc.list_attachments(task.id)
    assert len(rows) == 1
    await env.task_svc.delete_attachment(att.id)
    # Attachment to unknown task raises.
    with pytest.raises(KeyError):
        await env.task_svc.add_attachment(
            "missing-task",
            uploaded_by="u1",
            url="http://x",
            filename="f.pdf",
            mime="application/pdf",
            size_bytes=10,
        )


# ── Reorder ─────────────────────────────────────────────────────────


async def test_reorder_moves_positions(env):
    lst = await env.task_svc.create_list(name="Reo", created_by="u1")
    a = await env.task_svc.create_task(list_id=lst.id, title="A", created_by="u1")
    b = await env.task_svc.create_task(list_id=lst.id, title="B", created_by="u1")
    c = await env.task_svc.create_task(list_id=lst.id, title="C", created_by="u1")
    # Reverse order.
    await env.task_svc.reorder_tasks(
        lst.id,
        ordered_ids=[c.id, b.id, a.id],
    )
    rows = sorted(
        await env.task_svc.list_tasks(lst.id),
        key=lambda t: t.position,
    )
    assert [r.id for r in rows] == [c.id, b.id, a.id]


async def test_reorder_unknown_list_raises(env):
    with pytest.raises(KeyError):
        await env.task_svc.reorder_tasks("nope", ordered_ids=[])


async def test_reorder_skips_foreign_task_id(env):
    l1 = await env.task_svc.create_list(name="X", created_by="u1")
    l2 = await env.task_svc.create_list(name="Y", created_by="u1")
    t_in_l1 = await env.task_svc.create_task(
        list_id=l1.id,
        title="a",
        created_by="u1",
    )
    t_in_l2 = await env.task_svc.create_task(
        list_id=l2.id,
        title="b",
        created_by="u1",
    )
    # Passing l2's task into l1's reorder is a silent skip.
    updated = await env.task_svc.reorder_tasks(
        l1.id,
        ordered_ids=[t_in_l2.id, t_in_l1.id],
    )
    # t_in_l2 was skipped; t_in_l1 was unchanged (already at position 0 → 1).
    ids = {u.id for u in updated}
    assert t_in_l2.id not in ids


# ── List CRUD edge cases ────────────────────────────────────────────


async def test_rename_list(env):
    lst = await env.task_svc.create_list(name="Old", created_by="u1")
    renamed = await env.task_svc.rename_list(lst.id, name="New")
    assert renamed.name == "New"


async def test_rename_list_unknown_raises(env):
    with pytest.raises(KeyError):
        await env.task_svc.rename_list("missing", name="X")


async def test_delete_list_cascades(env):
    lst = await env.task_svc.create_list(name="D", created_by="u1")
    await env.task_svc.create_task(
        list_id=lst.id,
        title="t",
        created_by="u1",
    )
    await env.task_svc.delete_list(lst.id)
    with pytest.raises(KeyError):
        await env.task_svc.get_list(lst.id)


# ── Recurrence spawn ───────────────────────────────────────────────


async def test_create_task_with_due_date(env):
    lst = await env.task_svc.create_list(name="DD", created_by="u1")
    due = (date.today() + timedelta(days=1)).isoformat()
    task = await env.task_svc.create_task(
        list_id=lst.id,
        title="Review",
        created_by="u1",
        due_date=due,
    )
    assert task.due_date is not None


async def test_spawn_overdue_no_recurring(env):
    # Empty DB — nothing to spawn.
    spawned = await env.task_svc.spawn_overdue_recurrences()
    assert spawned == []


# ── Space task service basic ───────────────────────────────────────


async def test_space_task_service_list_empty(env):
    # List for a space with no rows — empty list, no error.
    rows = await env.space_task_svc.list_tasks("space-id-x")
    assert rows == []
    rows = await env.space_task_svc.list_lists("space-id-x")
    assert rows == []
