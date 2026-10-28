"""PollService — closing at the deadline, once (§9)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from socialhome.crypto import derive_instance_id, generate_identity_keypair
from socialhome.db.database import AsyncDatabase
from socialhome.domain.events import PollClosed
from socialhome.infrastructure.event_bus import EventBus
from socialhome.repositories.poll_repo import SqlitePollRepo
from socialhome.repositories.space_poll_repo import SqliteSpacePollRepo
from socialhome.services.poll_service import PollClosedError, PollService

NOW = datetime(2026, 10, 28, 10, 0, tzinfo=UTC)


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
    await db.enqueue(
        "INSERT INTO users(username, user_id, display_name) VALUES(?,?,?)",
        ("anna", "uid-anna", "Anna"),
    )
    await db.enqueue(
        "INSERT INTO spaces(id, name, owner_instance_id, owner_username,"
        " identity_public_key) VALUES(?,?,?,?,?)",
        ("sp-1", "Crew", "inst-x", "anna", "aabb" * 16),
    )
    bus = EventBus()
    closed: list[PollClosed] = []

    async def _keep(event: PollClosed) -> None:
        closed.append(event)

    bus.subscribe(PollClosed, _keep)

    class E:
        pass

    e = E()
    e.db, e.bus, e.closed = db, bus, closed
    e.household = PollService(SqlitePollRepo(db), bus)
    e.space = PollService(SqliteSpacePollRepo(db), bus)

    async def household_poll(post_id: str, closes_at: str | None) -> None:
        await db.enqueue(
            "INSERT INTO feed_posts(id, author, type, content) VALUES(?,?,?,?)",
            (post_id, "uid-anna", "poll", "?"),
        )
        await e.household.create_poll(
            post_id=post_id,
            question="Pizza?",
            options=["Yes", "No"],
            closes_at=closes_at,
        )

    async def space_poll(post_id: str, author: str, closes_at: str | None) -> None:
        await db.enqueue(
            "INSERT INTO space_posts(id, space_id, author, type, content)"
            " VALUES(?,?,?,?,?)",
            (post_id, "sp-1", author, "poll", "?"),
        )
        await e.space.create_poll(
            post_id=post_id,
            question="Pizza?",
            options=["Yes", "No"],
            closes_at=closes_at,
            space_id="sp-1",
        )

    e.household_poll, e.space_poll = household_poll, space_poll
    yield e
    await db.shutdown()


async def test_a_poll_past_its_deadline_closes_once(env):
    await env.household_poll("p1", "2026-10-28T09:59:00.000Z")
    await env.household_poll("p2", "2026-10-28T10:01:00.000Z")
    assert await env.household.close_due_polls(NOW) == 1
    (event,) = env.closed
    assert (event.post_id, event.space_id, event.actor_user_id) == (
        "p1",
        None,
        "uid-anna",
    )
    assert event.at_deadline is True and event.origin_instance_id is None
    assert (await env.household.summary("p1"))["closed"] is True
    # The next sweep, or one after a restart (a fresh service on the same
    # database), finds nothing more to close.
    assert await env.household.close_due_polls(NOW) == 0
    restarted = PollService(SqlitePollRepo(env.db), env.bus)
    assert await restarted.close_due_polls(NOW + timedelta(seconds=30)) == 0
    assert len(env.closed) == 1
    # p2's deadline passes later.
    assert await restarted.close_due_polls(NOW + timedelta(minutes=2)) == 1
    assert [e.post_id for e in env.closed] == ["p1", "p2"]


async def test_the_author_household_closes_a_space_poll_members_do_not(env):
    await env.space_poll("sp-own", "uid-anna", "2026-10-28T09:00:00Z")
    await env.space_poll("sp-peer", "uid-remote", "2026-10-28T09:00:00Z")
    assert await env.space.close_due_polls(NOW) == 1
    (event,) = env.closed
    assert (event.post_id, event.space_id, event.at_deadline) == (
        "sp-own",
        "sp-1",
        True,
    )
    # A member household's copy waits for its author's household's close —
    # but reads closed and refuses votes past the deadline meanwhile.
    summary = await env.space.summary("sp-peer")
    assert summary["closed"] is True
    with pytest.raises(PollClosedError):
        await env.space.cast_vote(
            post_id="sp-peer",
            option_id=summary["options"][0]["id"],
            voter_user_id="uid-anna",
            space_id="sp-1",
        )


async def test_closing_by_hand_after_the_deadline_announces_nothing_new(env):
    await env.household_poll("p1", "2026-10-28T09:00:00Z")
    await env.household.close_due_polls(NOW)
    await env.household.close_poll(post_id="p1", actor_user_id="uid-anna")
    assert len(env.closed) == 1


async def test_closing_by_hand_twice_announces_once(env):
    await env.household_poll("p1", None)
    await env.household.close_poll(post_id="p1", actor_user_id="uid-anna")
    await env.household.close_poll(post_id="p1", actor_user_id="uid-anna")
    (event,) = env.closed
    assert event.at_deadline is False
    # Closed by hand: the sweep has nothing to do.
    assert await env.household.close_due_polls(NOW) == 0


async def test_votes_still_count_before_the_deadline(env):
    future = (datetime.now(UTC) + timedelta(hours=1)).isoformat()
    await env.household_poll("p1", future)
    summary = await env.household.summary("p1")
    assert summary["closed"] is False
    await env.household.cast_vote(
        post_id="p1",
        option_id=summary["options"][0]["id"],
        voter_user_id="uid-anna",
    )
    assert (await env.household.summary("p1"))["total_votes"] == 1
