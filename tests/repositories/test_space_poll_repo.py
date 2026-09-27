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


# ─── §24.11 space-scoped writes ────────────────────────────────────────────


@pytest.fixture
async def two_spaces(env):
    """sp-1 owns post-1 (reply poll + schedule poll), sp-2 owns post-2."""
    await env.db.enqueue(
        "INSERT INTO spaces(id, name, owner_instance_id, owner_username, "
        "identity_public_key) VALUES(?,?,?,?,?)",
        ("sp-2", "Other", "inst-x", "alice", "ccdd" * 16),
    )
    await env.db.enqueue(
        "INSERT INTO space_posts(id, space_id, author, type, content)"
        " VALUES(?,?,?,?,?)",
        ("post-2", "sp-2", "uid-bob", "poll", "Tacos?"),
    )
    for post_id, prefix in (("post-1", "a"), ("post-2", "b")):
        await env.repo.create_poll(
            post_id=post_id,
            question="Q",
            closes_at=None,
            allow_multiple=False,
            options=[{"id": f"o-{prefix}", "text": "Yes", "position": 0}],
        )
        await env.repo.create_schedule_poll(
            post_id=post_id,
            title="When?",
            deadline=None,
            slots=[{"id": f"s-{prefix}", "slot_date": "2026-06-01"}],
        )
    await env.repo.insert_vote(option_id="o-b", voter_user_id="u-b")
    await env.repo.upsert_schedule_response(
        slot_id="s-b", user_id="u-b", response="yes"
    )
    return env


async def test_cast_vote_in_space_replaces_the_previous_vote(two_spaces):
    env = two_spaces
    await env.repo.insert_vote(option_id="o-a", voter_user_id="u-x")
    assert await env.repo.cast_vote_in_space(
        space_id="sp-1", post_id="post-1", option_id="o-a", voter_user_id="u-x"
    )
    assert await env.repo.list_user_votes("post-1", "u-x") == ["o-a"]


async def test_cast_vote_in_space_refuses_a_poll_of_another_space(two_spaces):
    env = two_spaces
    assert not await env.repo.cast_vote_in_space(
        space_id="sp-1", post_id="post-2", option_id="o-b", voter_user_id="u-evil"
    )
    # sp-2's tally is untouched and the evil vote never landed.
    assert await env.repo.list_user_votes("post-2", "u-evil") == []
    assert await env.repo.list_user_votes("post-2", "u-b") == ["o-b"]


async def test_cast_vote_in_space_refuses_an_option_of_another_post(two_spaces):
    env = two_spaces
    assert not await env.repo.cast_vote_in_space(
        space_id="sp-1", post_id="post-1", option_id="o-b", voter_user_id="u-x"
    )
    assert await env.repo.list_user_votes("post-2", "u-x") == []


async def test_cast_vote_in_space_refusal_keeps_the_existing_vote(two_spaces):
    """A refused vote must not have cleared the voter's standing vote."""
    env = two_spaces
    assert not await env.repo.cast_vote_in_space(
        space_id="sp-1", post_id="post-2", option_id="o-b", voter_user_id="u-b"
    )
    assert await env.repo.list_user_votes("post-2", "u-b") == ["o-b"]


async def test_close_in_space_is_scoped(two_spaces):
    env = two_spaces
    assert not await env.repo.close_in_space("post-2", space_id="sp-1")
    assert (await env.repo.get_meta("post-2"))["closed"] is False
    assert await env.repo.close_in_space("post-2", space_id="sp-2")
    assert (await env.repo.get_meta("post-2"))["closed"] is True


async def test_create_schedule_poll_in_space_refuses_foreign_post(two_spaces):
    env = two_spaces
    assert not await env.repo.create_schedule_poll_in_space(
        space_id="sp-1",
        post_id="post-2",
        title="pwned",
        deadline=None,
        slots=[{"id": "s-b", "slot_date": "1999-01-01"}],
    )
    assert (await env.repo.get_schedule_meta("post-2"))["title"] == "When?"
    slots = await env.repo.list_schedule_slots("post-2")
    assert [s["slot_date"] for s in slots] == ["2026-06-01"]


async def test_create_schedule_poll_in_space_cannot_rewrite_foreign_slot(
    two_spaces,
):
    """Naming another poll's slot id on your own post leaves that slot alone."""
    env = two_spaces
    assert await env.repo.create_schedule_poll_in_space(
        space_id="sp-1",
        post_id="post-1",
        title="Mine",
        deadline=None,
        slots=[
            {"id": "s-a", "slot_date": "2026-07-01"},
            {"id": "s-b", "slot_date": "1999-01-01"},
        ],
    )
    assert (await env.repo.get_schedule_meta("post-1"))["title"] == "Mine"
    mine = await env.repo.list_schedule_slots("post-1")
    theirs = await env.repo.list_schedule_slots("post-2")
    assert [s["slot_date"] for s in mine] == ["2026-07-01"]
    assert [s["slot_date"] for s in theirs] == ["2026-06-01"]


async def test_schedule_response_in_space_is_scoped(two_spaces):
    env = two_spaces
    assert not await env.repo.upsert_schedule_response_in_space(
        space_id="sp-1", slot_id="s-b", user_id="u-evil", response="no"
    )
    assert not await env.repo.upsert_schedule_response_in_space(
        space_id="sp-1", slot_id="s-b", user_id="u-b", response="no"
    )
    assert not await env.repo.delete_schedule_response_in_space(
        space_id="sp-1", slot_id="s-b", user_id="u-b"
    )
    assert await env.repo.list_schedule_responses("post-2") == [
        {"slot_id": "s-b", "user_id": "u-b", "availability": "yes"}
    ]
    assert await env.repo.upsert_schedule_response_in_space(
        space_id="sp-2", slot_id="s-b", user_id="u-b", response="maybe"
    )
    rows = await env.repo.list_schedule_responses("post-2")
    assert rows[0]["availability"] == "maybe"
    assert await env.repo.delete_schedule_response_in_space(
        space_id="sp-2", slot_id="s-b", user_id="u-b"
    )
    assert await env.repo.list_schedule_responses("post-2") == []


async def test_finalize_schedule_poll_in_space_is_scoped(two_spaces):
    env = two_spaces
    assert not await env.repo.finalize_schedule_poll_in_space(
        space_id="sp-1", post_id="post-2", slot_id="s-b"
    )
    # A slot of another post can't finalise this one either.
    assert not await env.repo.finalize_schedule_poll_in_space(
        space_id="sp-1", post_id="post-1", slot_id="s-b"
    )
    assert (await env.repo.get_schedule_meta("post-2"))["closed"] is False
    assert (await env.repo.get_schedule_meta("post-1"))["closed"] is False
    assert await env.repo.finalize_schedule_poll_in_space(
        space_id="sp-2", post_id="post-2", slot_id="s-b"
    )
    meta = await env.repo.get_schedule_meta("post-2")
    assert meta["closed"] is True
    assert meta["finalized_slot_id"] == "s-b"
