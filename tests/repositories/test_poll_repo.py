"""Tests for SqlitePollRepo — household reply polls closing at their deadline."""

from __future__ import annotations

import pytest

from socialhome.crypto import derive_instance_id, generate_identity_keypair
from socialhome.db.database import AsyncDatabase
from socialhome.repositories.poll_repo import SqlitePollRepo

_OPTIONS = [
    {"id": "o-y", "text": "Yes", "position": 0},
    {"id": "o-n", "text": "No", "position": 1},
]


@pytest.fixture
async def env(tmp_dir):
    kp = generate_identity_keypair()
    db = AsyncDatabase(tmp_dir / "polls.db", batch_timeout_ms=10)
    await db.startup()
    await db.enqueue(
        "INSERT INTO instance_identity(instance_id, identity_private_key,"
        " identity_public_key, routing_secret) VALUES(?,?,?,?)",
        (
            derive_instance_id(kp.public_key),
            kp.private_key.hex(),
            kp.public_key.hex(),
            "aa" * 32,
        ),
    )
    repo = SqlitePollRepo(db)

    async def poll(post_id: str, closes_at: str | None, *, deleted: int = 0):
        await db.enqueue(
            "INSERT INTO feed_posts(id, author, type, content, deleted)"
            " VALUES(?,?,?,?,?)",
            (post_id, "uid-anna", "poll", "?", deleted),
        )
        await repo.create_poll(
            post_id=post_id,
            question="Pizza?",
            closes_at=closes_at,
            allow_multiple=False,
            options=[dict(o, id=f"{post_id}-{o['id']}") for o in _OPTIONS],
        )

    class E:
        pass

    e = E()
    e.db, e.repo, e.poll = db, repo, poll
    yield e
    await db.shutdown()


async def test_close_if_open_closes_once(env):
    await env.poll("p1", None)
    assert await env.repo.close_if_open("p1") is True
    assert await env.repo.close_if_open("p1") is False
    assert (await env.repo.get_meta("p1"))["closed"] is True
    assert await env.repo.close_if_open("nope") is False


async def test_list_due_to_close_reads_the_deadline(env):
    await env.poll("past-z", "2026-10-28T09:00:00.000Z")
    await env.poll("past-offset", "2026-10-28T11:30:00+02:00")  # 09:30 UTC
    await env.poll("past-naive", "2026-10-28T09:59:00")
    await env.poll("future", "2026-10-28T10:00:01.000Z")
    await env.poll("open-ended", None)
    await env.poll("deleted", "2026-10-01T00:00:00Z", deleted=1)
    await env.poll("closed", "2026-10-01T00:00:00Z")
    await env.repo.close("closed")
    due = await env.repo.list_due_to_close("2026-10-28T10:00:00+00:00")
    assert {d["post_id"] for d in due} == {"past-z", "past-offset", "past-naive"}
    assert all(d["author"] == "uid-anna" and d["space_id"] is None for d in due)
