"""Tests for SqliteSpacePollRepo — the space-scoped poll repository.

Exercises both reply-poll and schedule-poll paths against the
``space_*`` tables directly, verifying the ``AbstractPollRepo``
protocol shape.
"""

from __future__ import annotations

import pytest

from socialhome.crypto import derive_instance_id, generate_identity_keypair
from socialhome.db.database import AsyncDatabase
from socialhome.repositories.space_poll_repo import SqliteSpacePollRepo


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
    await db.enqueue(
        "INSERT INTO spaces(id, name, owner_instance_id, owner_username, "
        "identity_public_key) VALUES(?,?,?,?,?)",
        ("sp-1", "Polls", "inst-x", "alice", "aabb" * 16),
    )
    await db.enqueue(
        "INSERT INTO space_posts(id, space_id, author, type, content)"
        " VALUES(?,?,?,?,?)",
        ("post-1", "sp-1", "uid-alice", "poll", "Pizza?"),
    )

    class E:
        pass

    e = E()
    e.db = db
    e.repo = SqliteSpacePollRepo(db)
    yield e
    await db.shutdown()


async def test_create_poll_persists_meta_and_options(env):
    await env.repo.create_poll(
        post_id="post-1",
        question="Pizza?",
        closes_at=None,
        allow_multiple=False,
        options=[
            {"id": "o-y", "text": "Yes", "position": 0},
            {"id": "o-n", "text": "No", "position": 1},
        ],
    )
    meta = await env.repo.get_meta("post-1")
    assert meta == {
        "post_id": "post-1",
        "question": "Pizza?",
        "closes_at": None,
        "closed": False,
        "allow_multiple": False,
    }
    opts = await env.repo.list_options_with_counts("post-1")
    assert [o["text"] for o in opts] == ["Yes", "No"]
    assert all(o["count"] == 0 for o in opts)


async def test_get_meta_missing_returns_none(env):
    assert await env.repo.get_meta("missing") is None


async def test_vote_and_clear_and_list(env):
    await env.repo.create_poll(
        post_id="post-1",
        question="Q",
        closes_at=None,
        allow_multiple=True,
        options=[
            {"id": "o-a", "text": "A", "position": 0},
            {"id": "o-b", "text": "B", "position": 1},
        ],
    )
    await env.repo.insert_vote(option_id="o-a", voter_user_id="u1")
    await env.repo.insert_vote(option_id="o-b", voter_user_id="u1")
    assert set(await env.repo.list_user_votes("post-1", "u1")) == {"o-a", "o-b"}
    counts = {
        o["id"]: o["count"] for o in await env.repo.list_options_with_counts("post-1")
    }
    assert counts == {"o-a": 1, "o-b": 1}
    await env.repo.clear_user_votes(post_id="post-1", voter_user_id="u1")
    assert await env.repo.list_user_votes("post-1", "u1") == []


async def test_close_flips_flag(env):
    await env.repo.create_poll(
        post_id="post-1",
        question="Q",
        closes_at=None,
        allow_multiple=False,
        options=[
            {"id": "o-a", "text": "A", "position": 0},
            {"id": "o-b", "text": "B", "position": 1},
        ],
    )
    await env.repo.close("post-1")
    assert (await env.repo.get_meta("post-1"))["closed"] is True


async def test_option_belongs_to_post(env):
    await env.repo.create_poll(
        post_id="post-1",
        question="Q",
        closes_at=None,
        allow_multiple=False,
        options=[{"id": "o-a", "text": "A", "position": 0}],
    )
    assert await env.repo.option_belongs_to_post(option_id="o-a", post_id="post-1")
    assert not await env.repo.option_belongs_to_post(
        option_id="ghost", post_id="post-1"
    )


async def test_get_post_author_reads_space_posts(env):
    assert await env.repo.get_post_author("post-1") == "uid-alice"
    assert await env.repo.get_post_author("missing") is None


# ── Schedule polls ────────────────────────────────────────────────────


async def test_create_schedule_poll_roundtrip(env):
    await env.repo.create_schedule_poll(
        post_id="post-1",
        title="When?",
        deadline=None,
        slots=[
            {"id": "s-a", "slot_date": "2026-05-01", "position": 0},
            {
                "id": "s-b",
                "slot_date": "2026-05-02",
                "start_time": "18:00",
                "position": 1,
            },
        ],
    )
    meta = await env.repo.get_schedule_meta("post-1")
    assert meta == {
        "post_id": "post-1",
        "title": "When?",
        "deadline": None,
        "finalized_slot_id": None,
        "closed": False,
    }
    slots = await env.repo.list_schedule_slots("post-1")
    assert [s["id"] for s in slots] == ["s-a", "s-b"]
    assert slots[1]["start_time"] == "18:00"


async def test_upsert_schedule_response_updates_existing(env):
    await env.repo.create_schedule_poll(
        post_id="post-1",
        title="When?",
        deadline=None,
        slots=[{"id": "s-a", "slot_date": "2026-05-01"}],
    )
    await env.repo.upsert_schedule_response(slot_id="s-a", user_id="u1", response="yes")
    await env.repo.upsert_schedule_response(slot_id="s-a", user_id="u1", response="no")
    rows = await env.repo.list_schedule_responses("post-1")
    assert len(rows) == 1
    assert rows[0]["availability"] == "no"


async def test_delete_schedule_response(env):
    await env.repo.create_schedule_poll(
        post_id="post-1",
        title="When?",
        deadline=None,
        slots=[{"id": "s-a", "slot_date": "2026-05-01"}],
    )
    await env.repo.upsert_schedule_response(slot_id="s-a", user_id="u1", response="yes")
    await env.repo.delete_schedule_response(slot_id="s-a", user_id="u1")
    assert await env.repo.list_schedule_responses("post-1") == []


async def test_finalize_schedule_poll(env):
    await env.repo.create_schedule_poll(
        post_id="post-1",
        title="When?",
        deadline=None,
        slots=[
            {"id": "s-a", "slot_date": "2026-05-01"},
            {"id": "s-b", "slot_date": "2026-05-02"},
        ],
    )
    slot = await env.repo.finalize_schedule_poll(post_id="post-1", slot_id="s-b")
    assert slot is not None
    assert slot["id"] == "s-b"
    meta = await env.repo.get_schedule_meta("post-1")
    assert meta["finalized_slot_id"] == "s-b"
    assert meta["closed"] is True


async def test_finalize_rejects_foreign_slot(env):
    await env.repo.create_schedule_poll(
        post_id="post-1",
        title="When?",
        deadline=None,
        slots=[{"id": "s-a", "slot_date": "2026-05-01"}],
    )
    result = await env.repo.finalize_schedule_poll(post_id="post-1", slot_id="not-mine")
    assert result is None
