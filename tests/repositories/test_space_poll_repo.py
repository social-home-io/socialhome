"""Tests for SqliteSpacePollRepo — the space-scoped poll repository.

Exercises both reply-poll and schedule-poll paths against the
``space_*`` tables directly, verifying the ``AbstractPollRepo``
protocol shape.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

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


# ── Create-once (concurrent moderation resumes, review N1) ─────────────────


async def test_create_poll_if_absent_creates_once_even_when_racing(env):
    async def attempt(tag: str) -> bool:
        return await env.repo.create_poll_if_absent(
            post_id="post-1",
            question="Q?",
            closes_at=None,
            allow_multiple=False,
            options=[
                {"id": f"{tag}-a", "text": "a", "position": 0},
                {"id": f"{tag}-b", "text": "b", "position": 1},
            ],
        )

    results = await asyncio.gather(attempt("x"), attempt("y"), attempt("z"))
    assert sorted(results) == [False, False, True]
    rows = await env.db.fetchall(
        "SELECT id FROM space_poll_options WHERE post_id='post-1'"
    )
    assert len(rows) == 2


async def test_create_schedule_poll_if_absent_creates_once_even_when_racing(env):
    async def attempt(tag: str) -> bool:
        return await env.repo.create_schedule_poll_if_absent(
            post_id="post-1",
            title="When?",
            deadline=None,
            slots=[
                {"id": f"{tag}-1", "slot_date": "2026-11-01", "position": 0},
                {"id": f"{tag}-2", "slot_date": "2026-11-02", "position": 1},
            ],
        )

    results = await asyncio.gather(attempt("x"), attempt("y"))
    assert sorted(results) == [False, True]
    rows = await env.db.fetchall(
        "SELECT id FROM space_schedule_slots WHERE post_id='post-1'"
    )
    assert len(rows) == 2


# ─── A deleted post holds no poll (migration 0085) ───────────────────


async def test_a_post_soft_delete_drops_its_poll_and_schedule(two_spaces):
    env = two_spaces
    await env.db.enqueue("UPDATE space_posts SET deleted=1 WHERE id='post-2'")
    assert await env.repo.get_meta("post-2") is None
    assert await env.repo.get_schedule_meta("post-2") is None
    assert await env.repo.list_schedule_slots("post-2") == []
    # post-1's stay.
    assert await env.repo.get_schedule_meta("post-1") is not None


async def test_a_deleted_post_takes_no_schedule_from_the_wire(two_spaces):
    env = two_spaces
    await env.db.enqueue("UPDATE space_posts SET deleted=1 WHERE id='post-2'")
    assert not await env.repo.create_schedule_poll_in_space(
        space_id="sp-2",
        post_id="post-2",
        title="Back?",
        deadline=None,
        slots=[{"id": "s-new", "slot_date": "2026-06-02"}],
    )
    assert await env.repo.get_schedule_meta("post-2") is None
    assert await env.repo.list_schedule_slots("post-2") == []
    assert not await env.repo.create_poll_if_absent(
        post_id="post-2",
        question="Q",
        closes_at=None,
        allow_multiple=False,
        options=[{"id": "o-new", "text": "x"}],
    )
    assert await env.repo.get_meta("post-2") is None


# ── closing at the deadline ─────────────────────────────────────────


async def test_close_if_open_closes_once(env):
    await env.repo.create_poll(
        post_id="post-1",
        question="Pizza?",
        closes_at=None,
        allow_multiple=False,
        options=[{"id": "o-1", "text": "Yes", "position": 0}],
    )
    assert await env.repo.close_if_open("post-1") is True
    assert await env.repo.close_if_open("post-1") is False


async def test_only_polls_a_local_user_wrote_are_due_here(env):
    """Every member household holds a copy of a space poll; only the
    author's household closes it (peers take SPACE_POLL_CLOSED only from
    there)."""
    await env.db.enqueue(
        "INSERT INTO users(username, user_id, display_name) VALUES(?,?,?)",
        ("alice", "uid-alice", "Alice"),
    )
    for post_id, author in (("post-remote", "uid-remote"), ("post-later", "uid-alice")):
        await env.db.enqueue(
            "INSERT INTO space_posts(id, space_id, author, type, content)"
            " VALUES(?,?,?,?,?)",
            (post_id, "sp-1", author, "poll", "?"),
        )
    for post_id, closes_at in (
        ("post-1", "2026-10-28T09:00:00.000Z"),
        ("post-remote", "2026-10-28T09:00:00.000Z"),
        ("post-later", "2026-10-28T11:00:00.000Z"),
    ):
        await env.repo.create_poll(
            post_id=post_id,
            question="Pizza?",
            closes_at=closes_at,
            allow_multiple=False,
            options=[{"id": f"{post_id}-o", "text": "Yes", "position": 0}],
        )
    now = "2026-10-28T10:00:00+00:00"
    assert await env.repo.list_due_to_close(now) == [
        {"post_id": "post-1", "author": "uid-alice", "space_id": "sp-1"}
    ]
    await env.repo.close("post-1")
    assert await env.repo.list_due_to_close(now) == []


# ── §25.6 sync (v_92) ────────────────────────────────────────────────────


async def _sync_poll(env, *, space_id="sp-1", closed=False, votes=None, keep=()):
    return await env.repo.apply_synced_poll(
        space_id=space_id,
        post_id="post-1",
        question="Pizza?",
        closes_at=None,
        allow_multiple=False,
        closed=closed,
        options=[{"id": "o-y", "text": "Yes"}, {"id": "o-n", "text": "No"}],
        votes=votes,
        keep_voters=frozenset(keep),
    )


async def _post_seq(env) -> int:
    return int(
        await env.db.fetchval(
            "SELECT sync_seq FROM space_posts WHERE id='post-1'", (), 0
        )
    )


async def test_apply_synced_poll_creates_it_and_takes_the_votes(env):
    assert await _sync_poll(env, votes=[("o-y", "u-b"), ("o-n", "u-c")])
    assert [o["count"] for o in await env.repo.list_options_with_counts("post-1")] == [
        1,
        1,
    ]
    assert await _sync_poll(env, votes=[("o-n", "u-b")])
    assert await env.repo.list_votes("post-1") == [
        {"option_id": "o-n", "voter_user_id": "u-b"}
    ]


async def test_apply_synced_poll_refuses_another_spaces_post(env):
    assert not await _sync_poll(env, space_id="sp-other")
    assert await env.repo.get_meta("post-1") is None


async def test_apply_synced_poll_keeps_our_peoples_votes_and_older_hosts_votes(env):
    await _sync_poll(env, votes=[])
    await env.repo.cast_vote_in_space(
        space_id="sp-1", post_id="post-1", option_id="o-y", voter_user_id="u-ours"
    )
    await env.repo.cast_vote_in_space(
        space_id="sp-1", post_id="post-1", option_id="o-n", voter_user_id="u-c"
    )
    await _sync_poll(env, votes=[], keep={"u-ours"})
    assert await env.repo.list_votes("post-1") == [
        {"option_id": "o-y", "voter_user_id": "u-ours"}
    ]
    # An older host sends no votes: the held ones stay.
    await env.repo.cast_vote_in_space(
        space_id="sp-1", post_id="post-1", option_id="o-n", voter_user_id="u-c"
    )
    await _sync_poll(env, votes=None)
    assert len(await env.repo.list_votes("post-1")) == 2


async def test_apply_synced_poll_never_reopens_and_ignores_foreign_votes(env):
    await _sync_poll(env, closed=True, votes=[("o-elsewhere", "u-b")])
    await _sync_poll(env, closed=False, votes=[("o-elsewhere", "u-b")])
    meta = await env.repo.get_meta("post-1")
    assert meta is not None and meta["closed"] is True
    assert await env.repo.list_votes("post-1") == []


async def test_reapplying_the_same_poll_stamps_nothing(env):
    await _sync_poll(env, votes=[("o-y", "u-b")])
    seq = await _post_seq(env)
    await _sync_poll(env, votes=[("o-y", "u-b")])
    assert await _post_seq(env) == seq


async def _schedule(env):
    await env.db.enqueue(
        "UPDATE space_posts SET type='schedule' WHERE id='post-1'",
    )
    await env.repo.create_schedule_poll_in_space(
        space_id="sp-1",
        post_id="post-1",
        title="When?",
        deadline=None,
        slots=[
            {"id": "s-1", "slot_date": "2026-11-20", "position": 0},
            {"id": "s-2", "slot_date": "2026-11-21", "position": 1},
        ],
    )


async def test_apply_synced_schedule_state_takes_answers_and_the_pick(env):
    await _schedule(env)
    await env.repo.upsert_schedule_response_in_space(
        space_id="sp-1", slot_id="s-1", user_id="u-c", response="maybe"
    )
    await env.repo.upsert_schedule_response_in_space(
        space_id="sp-1", slot_id="s-1", user_id="u-ours", response="no"
    )
    later = datetime.now(UTC).isoformat()
    assert await env.repo.apply_synced_schedule_state(
        space_id="sp-1",
        post_id="post-1",
        finalized_slot_id="s-2",
        responses=[
            {
                "slot_id": "s-2",
                "user_id": "u-c",
                "availability": "yes",
                "responded_at": later,
            },
            {"slot_id": "s-2", "user_id": "u-d", "availability": "bogus"},
            {"slot_id": "s-elsewhere", "user_id": "u-d", "availability": "yes"},
        ],
        keep_users=frozenset({"u-ours"}),
        responses_at=later,
    )
    got = sorted(
        (r["slot_id"], r["user_id"], r["availability"])
        for r in await env.repo.list_schedule_responses("post-1")
    )
    assert got == [("s-1", "u-ours", "no"), ("s-2", "u-c", "yes")]
    meta = await env.repo.get_schedule_meta("post-1")
    assert meta["finalized_slot_id"] == "s-2" and meta["closed"] is True
    # A pick never reverts, and another space's poll is refused.
    await env.repo.apply_synced_schedule_state(
        space_id="sp-1",
        post_id="post-1",
        finalized_slot_id="s-1",
        responses=None,
        keep_users=frozenset(),
    )
    assert (await env.repo.get_schedule_meta("post-1"))["finalized_slot_id"] == "s-2"
    assert not await env.repo.apply_synced_schedule_state(
        space_id="sp-other",
        post_id="post-1",
        finalized_slot_id=None,
        responses=[],
        keep_users=frozenset(),
    )


async def _answer(env, user, availability, at, *, responses_at=None, partial=False):
    return await env.repo.apply_synced_schedule_state(
        space_id="sp-1",
        post_id="post-1",
        finalized_slot_id=None,
        responses=[
            {
                "slot_id": "s-1",
                "user_id": user,
                "availability": availability,
                "responded_at": at,
            }
        ],
        keep_users=frozenset(),
        responses_at=responses_at,
        responses_partial=partial,
    )


async def _answers(env) -> list[tuple[str, str, str]]:
    return sorted(
        (r["slot_id"], r["user_id"], r["availability"])
        for r in await env.repo.list_schedule_responses("post-1")
    )


async def test_apply_synced_schedule_state_is_last_writer_wins(env):
    await _schedule(env)
    t0 = datetime.now(UTC)
    await _answer(env, "u-c", "yes", (t0 - timedelta(minutes=5)).isoformat())
    # An older answer never overwrites a newer one; a newer one does.
    await _answer(env, "u-c", "no", (t0 - timedelta(minutes=9)).isoformat())
    assert await _answers(env) == [("s-1", "u-c", "yes")]
    await _answer(env, "u-c", "maybe", (t0 - timedelta(minutes=1)).isoformat())
    assert await _answers(env) == [("s-1", "u-c", "maybe")]
    # A stamp years ahead is no stamp: it never wins over a held answer.
    await _answer(env, "u-c", "no", "2036-01-01T00:00:00+00:00")
    assert await _answers(env) == [("s-1", "u-c", "maybe")]


async def test_apply_synced_schedule_state_deletes_only_what_the_snapshot_saw(env):
    await _schedule(env)
    t0 = datetime.now(UTC)
    await env.repo.upsert_schedule_response_in_space(
        space_id="sp-1", slot_id="s-2", user_id="u-live", response="yes"
    )
    # The host read its list before u-live's answer reached it: kept.
    await _answer(
        env,
        "u-c",
        "yes",
        (t0 - timedelta(minutes=9)).isoformat(),
        responses_at=(t0 - timedelta(minutes=1)).isoformat(),
    )
    assert ("s-2", "u-live", "yes") in await _answers(env)
    # A cut list never deletes; a whole one read after the answer does.
    later = (datetime.now(UTC) + timedelta(seconds=1)).isoformat()
    await _answer(env, "u-c", "yes", later, responses_at=later, partial=True)
    assert ("s-2", "u-live", "yes") in await _answers(env)
    await _answer(env, "u-c", "yes", later, responses_at=later)
    assert await _answers(env) == [("s-1", "u-c", "yes")]


async def test_list_schedule_answers_newest_first_with_a_limit(env):
    await _schedule(env)
    t0 = datetime.now(UTC)
    for n, user in enumerate(("u-1", "u-2", "u-3")):
        at = (t0 - timedelta(minutes=10 - n)).isoformat()
        await _answer(env, user, "yes", at, partial=True)
    got = await env.repo.list_schedule_answers("post-1", limit=2)
    assert [a["user_id"] for a in got] == ["u-3", "u-2"]
    assert got[0]["responded_at"]


async def test_list_votes_with_a_limit_puts_recent_voters_first(env):
    await _sync_poll(env, votes=[])
    for voter in ("u-a", "u-b"):
        await env.repo.cast_vote_in_space(
            space_id="sp-1", post_id="post-1", option_id="o-y", voter_user_id=voter
        )
    await env.db.enqueue(
        "UPDATE space_poll_votes SET voted_at='2026-01-01 00:00:00'"
        " WHERE voter_user_id='u-a'"
    )
    got = await env.repo.list_votes("post-1", limit=1)
    assert got == [{"option_id": "o-y", "voter_user_id": "u-b"}]


async def test_a_partial_vote_list_converges_only_the_voters_it_names(env):
    await _sync_poll(env, votes=[("o-y", "u-a"), ("o-y", "u-b")])
    await env.repo.apply_synced_poll(
        space_id="sp-1",
        post_id="post-1",
        question="Pizza?",
        closes_at=None,
        allow_multiple=False,
        closed=False,
        options=[{"id": "o-y", "text": "Yes"}, {"id": "o-n", "text": "No"}],
        votes=[("o-n", "u-b")],
        keep_voters=frozenset(),
        votes_partial=True,
    )
    assert await env.repo.list_votes("post-1") == [
        {"option_id": "o-y", "voter_user_id": "u-a"},
        {"option_id": "o-n", "voter_user_id": "u-b"},
    ]


# ─── A peer's poll definition and vote selection ───────────────────────────


_NEW = [{"id": "n-1", "text": "Yes"}, {"id": "n-2", "text": "No"}]


async def _bare_post(env, post_id: str = "post-3", space_id: str = "sp-1") -> None:
    await env.db.enqueue(
        "INSERT INTO space_posts(id, space_id, author, type, content)"
        " VALUES(?,?,?,?,?)",
        (post_id, space_id, "uid-bob", "poll", ""),
    )


async def _define(env, *, space_id="sp-1", post_id="post-3", options=None, multi=True):
    return await env.repo.create_poll_in_space(
        space_id=space_id,
        post_id=post_id,
        question="Which?",
        closes_at="2030-01-01T00:00:00+00:00",
        allow_multiple=multi,
        options=_NEW if options is None else options,
    )


async def test_create_poll_in_space_keeps_the_ids_and_the_order(two_spaces):
    env = two_spaces
    await _bare_post(env)
    assert await _define(env) is True
    meta = await env.repo.get_meta("post-3")
    assert (meta["question"], meta["allow_multiple"], meta["closed"]) == (
        "Which?",
        True,
        False,
    )
    assert meta["closes_at"] == "2030-01-01T00:00:00+00:00"
    assert await env.repo.list_options_with_counts("post-3") == [
        {"id": "n-1", "text": "Yes", "count": 0},
        {"id": "n-2", "text": "No", "count": 0},
    ]


async def test_create_poll_in_space_never_rewrites_a_held_poll(two_spaces):
    env = two_spaces
    assert await _define(env, post_id="post-1") is False
    assert (await env.repo.get_meta("post-1"))["question"] == "Q"
    assert [o["id"] for o in await env.repo.list_options_with_counts("post-1")] == [
        "o-a"
    ]


async def test_create_poll_in_space_refuses_another_spaces_post(two_spaces):
    env = two_spaces
    await _bare_post(env, "post-4", "sp-2")
    assert await _define(env, post_id="post-4") is None
    assert await _define(env, post_id="no-such-post") is None
    assert await env.repo.get_meta("post-4") is None


async def test_create_poll_in_space_refuses_a_deleted_post(two_spaces):
    env = two_spaces
    await _bare_post(env)
    await env.db.enqueue("UPDATE space_posts SET deleted=1 WHERE id='post-3'")
    assert await _define(env) is None
    assert await env.repo.get_meta("post-3") is None


async def test_create_poll_in_space_refuses_another_polls_option_id(two_spaces):
    """All or nothing: an option id another poll owns creates no half poll."""
    env = two_spaces
    await _bare_post(env)
    taken = [{"id": "n-1", "text": "Yes"}, {"id": "o-b", "text": "theirs"}]
    assert await _define(env, options=taken) is None
    assert await env.repo.get_meta("post-3") is None
    assert await env.repo.option_belongs_to_post(option_id="o-b", post_id="post-2")


async def test_set_votes_in_space_replaces_the_whole_selection(two_spaces):
    env = two_spaces
    await _bare_post(env)
    await _define(env)
    vote = {"space_id": "sp-1", "post_id": "post-3", "voter_user_id": "u-x"}
    assert await env.repo.set_votes_in_space(option_ids=["n-1", "n-2", "n-1"], **vote)
    assert sorted(await env.repo.list_user_votes("post-3", "u-x")) == ["n-1", "n-2"]
    assert await env.repo.set_votes_in_space(option_ids=["n-2"], **vote)
    assert await env.repo.list_user_votes("post-3", "u-x") == ["n-2"]
    # An empty selection is the retraction.
    assert await env.repo.set_votes_in_space(option_ids=[], **vote)
    assert await env.repo.list_user_votes("post-3", "u-x") == []


async def test_set_votes_in_space_refusals_keep_the_held_vote(two_spaces):
    env = two_spaces
    await _bare_post(env)
    await _define(env, multi=False)
    vote = {"space_id": "sp-1", "post_id": "post-3", "voter_user_id": "u-x"}
    assert await env.repo.set_votes_in_space(option_ids=["n-1"], **vote)
    # Two options on a single-choice poll, another poll's option, a post
    # with no poll, the poll of another space.
    assert not await env.repo.set_votes_in_space(option_ids=["n-1", "n-2"], **vote)
    assert not await env.repo.set_votes_in_space(option_ids=["n-2", "o-a"], **vote)
    assert not await env.repo.set_votes_in_space(
        space_id="sp-1", post_id="no-poll", option_ids=[], voter_user_id="u-x"
    )
    assert not await env.repo.set_votes_in_space(
        space_id="sp-1", post_id="post-2", option_ids=[], voter_user_id="u-b"
    )
    assert await env.repo.list_user_votes("post-3", "u-x") == ["n-1"]
    assert await env.repo.list_user_votes("post-2", "u-b") == ["o-b"]


@pytest.mark.parametrize(
    "close",
    [
        "UPDATE space_polls SET closed=1 WHERE post_id='post-3'",
        "UPDATE space_polls SET closes_at='2000-01-01T00:00:00' WHERE post_id='post-3'",
    ],
    ids=["closed", "past-its-deadline"],
)
async def test_set_votes_in_space_refuses_a_closed_poll(two_spaces, close):
    """Neither a new vote nor a retraction changes a closed poll's result."""
    env = two_spaces
    await _bare_post(env)
    await _define(env)
    vote = {"space_id": "sp-1", "post_id": "post-3", "voter_user_id": "u-x"}
    assert await env.repo.set_votes_in_space(option_ids=["n-1"], **vote)
    await env.db.enqueue(close)
    assert not await env.repo.set_votes_in_space(option_ids=["n-2"], **vote)
    assert not await env.repo.set_votes_in_space(option_ids=[], **vote)
    assert await env.repo.list_user_votes("post-3", "u-x") == ["n-1"]
